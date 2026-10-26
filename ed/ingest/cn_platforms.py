"""抖音 / 小红书 视频直链解析与音频下载（无登录态优先）。

方案参照社区开源实现（chubbyguan/chubbyskills，MIT）的思路自行实现：

抖音
    1. 短链 v.douyin.com → 302 → www.iesdouyin.com/share/video/<id>
    2. 分享页 HTML 里 `window._ROUTER_DATA = {...}` →
       loaderData["video_(id)/page"].videoInfoRes.item_list[0]
       .video.play_addr.url_list[0]（playwm → play 去水印）
    3. 风控时页面只有「蜘蛛壳」（isSpider=true，无 videoInfoRes），
       回退 yt-dlp（本机已验证：yt-dlp 对抖音无登录态可下）

小红书
    1. 笔记页 HTML 里 `window.__INITIAL_STATE__`（括号配平提取，正则会失配）
    2. note.video.media.stream.{h264,h265,...}[].masterUrl
    3. 下载必须带 Referer: https://www.xiaohongshu.com/，可选 cookie 提成功率

两个平台都拿的是**视频**，音频用 ffmpeg 从视频里抽（-vn -acodec libmp3lame）。
"""

import json
import os
import re
import subprocess
import tempfile
from typing import Any, Dict, Optional, Tuple

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) EdgiOS/121.0.2277.107 "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)
DESKTOP_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _http(url: str, headers: Optional[Dict[str, str]] = None,
          timeout: int = 30, head: bool = False) -> Tuple[int, str]:
    """返回 (status, body 或重定向头)。不用 requests，标准库即可。"""
    import urllib.error
    import urllib.request

    h = {"User-Agent": MOBILE_UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h,
                                 method="HEAD" if head else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:500]
    except Exception as e:
        return 0, str(e)


# ---------------------------------------------------------------- 抖音

_TTWID_CACHE = {"token": "", "at": 0.0}


def _douyin_ttwid() -> str:
    """从字节官方接口领游客 ttwid（无需登录，有效期一年）。

    2026 年起无 cookie 的分享页只返回蜘蛛壳（isSpider=true、videoInfoRes 为空
    或缺失），必须带 ttwid 才能拿到真实数据。yt-dlp 的「Fresh cookies needed」
    也是同一件事。
    """
    import time as _time
    if _TTWID_CACHE["token"] and _time.time() - _TTWID_CACHE["at"] < 86400:
        return _TTWID_CACHE["token"]
    p = subprocess.run(
        ["curl", "-s", "--max-time", "15", "-X", "POST",
         "https://ttwid.bytedance.com/ttwid/union/register/",
         "-H", "Content-Type: application/json",
         "-D", "-",
         "-d", json.dumps({
             "region": "cn", "aid": 1768, "needFid": False,
             "service": "www.ixigua.com",
             "migrate_info": {"ticket": "", "source": "node"},
             "cbUrlProtocol": "https", "union": True})],
        capture_output=True, text=True, timeout=30)
    m = re.search(r"set-cookie: ttwid=([^;\r\n]+)", p.stdout, re.I)
    if m:
        _TTWID_CACHE.update(token=m.group(1), at=_time.time())
        return m.group(1)
    return ""


def douyin_video_id(url: str) -> str:
    """短链跟随 302 后取 /video/<id>。"""
    if "/video/" in url:
        m = re.search(r"/video/(\d+)", url)
        if m:
            return m.group(1)
    if "v.douyin.com" in url or "iesdouyin" in url:
        # 302 跟随
        st, _ = _http(url, timeout=15)
        # urlopen 会自动跟随；最终 URL 需要另取，简化：GET 一次拿跳转链
        import urllib.request
        req = urllib.request.Request(url, headers={
            "User-Agent": MOBILE_UA}, method="HEAD")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                url = r.url
        except Exception:
            pass
        m = re.search(r"/video/(\d+)", url)
        if m:
            return m.group(1)
    raise ValueError(f"无法从链接提取抖音视频 ID：{url}")


def douyin_parse_share_page(video_id: str) -> Dict[str, Any]:
    """无登录态抓分享页（带游客 ttwid），从 _ROUTER_DATA 抠标题和去水印直链。"""
    share_url = f"https://www.iesdouyin.com/share/video/{video_id}"
    ttwid = _douyin_ttwid()
    headers = {}
    if ttwid:
        headers["Cookie"] = f"ttwid={ttwid}"
    st, html = _http(share_url, headers=headers, timeout=30)
    if st != 200 or not html:
        return {"ok": False,
                "reason": f"分享页请求失败（HTTP {st}）",
                "share_url": share_url}
    m = re.search(r"window\._ROUTER_DATA\s*=\s*(.*?)</script>", html, re.S)
    if not m:
        return {"ok": False,
                "reason": "分享页没有 _ROUTER_DATA（风控蜘蛛壳或结构变更）",
                "share_url": share_url}
    try:
        data = json.loads(m.group(1).strip())
        item = (data["loaderData"]["video_(id)/page"]["videoInfoRes"]
                ["item_list"][0])
        title = item.get("desc") or ""
        urls = item["video"]["play_addr"]["url_list"]
        # playwm 是带水印，play 是无水印
        video_url = urls[0].replace("playwm", "play")
        return {"ok": True, "title": title, "video_url": video_url}
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        # item_list 为空 = 视频不存在或被删；结构缺失 = 风控
        return {"ok": False,
                "reason": "分享页数据为空（视频可能已删除，或风控未放行）",
                "share_url": share_url}


def douyin_download_audio(url: str, outdir: str) -> Dict[str, Any]:
    """抖音链接 → 音频 mp3。先试无 cookie 分享页，风控则回退 yt-dlp。"""
    try:
        vid = douyin_video_id(url)
    except ValueError as e:
        return {"ok": False, "reason": str(e)}

    info = douyin_parse_share_page(vid)
    attempts = []
    if info.get("ok"):
        video_path = os.path.join(outdir, f"dy_{vid}.mp4")
        # 只用 curl 下一次。以前这里先用 _http 把视频当文本读一遍、
        # 再 curl 一遍，同一条视频请求两次，白白浪费一次带宽和内存。
        p = subprocess.run(
            ["curl", "-s", "-L", "-o", video_path,
             "-H", f"User-Agent: {MOBILE_UA}",
             "-H", "Referer: https://www.douyin.com/",
             info["video_url"]],
            capture_output=True, timeout=600)
        if (p.returncode == 0 and os.path.exists(video_path)
                and os.path.getsize(video_path) > 50000):
            attempts.append({"步骤": "分享页直链", "成功": True})
            audio = _extract_audio(video_path, os.path.join(outdir, f"dy_{vid}.mp3"))
            if audio:
                return {"ok": True, "path": audio,
                        "title": info.get("title", ""), "attempts": attempts}
        attempts.append({"步骤": "分享页直链", "成功": False,
                         "细节": "直链下载失败或文件过小"})
    else:
        attempts.append({"步骤": "分享页解析", "成功": False,
                         "细节": info.get("reason", "")})

    # 回退 yt-dlp（本机已验证无登录态可下抖音）
    from .video import download_audio as ytdlp_audio
    r = ytdlp_audio(f"https://www.douyin.com/video/{vid}", outdir)
    attempts.append({"步骤": "yt-dlp 回退", "成功": r.get("ok", False),
                     "细节": r.get("reason", "") or "下载成功"})
    if r.get("ok"):
        return {"ok": True, "path": r["path"],
                "title": "", "attempts": attempts}
    return {"ok": False, "reason": "分享页与 yt-dlp 都失败",
            "attempts": attempts}


# ---------------------------------------------------------------- 小红书

def _balanced_json(s: str, start: int) -> Optional[str]:
    """从 s[start]（应为 '{'）起做括号配平，正确跳过字符串内花括号。"""
    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        ch = s[i]
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return None


def xhs_parse_note(url: str) -> Dict[str, Any]:
    """抓小红书笔记页，从 __INITIAL_STATE__ 抠笔记数据（含视频直链）。

    三个实战要点（2026-10-24 用真实短链验证）：
    1. 请求必须带 Referer: https://www.xiaohongshu.com/ + 移动 UA，
       否则拿到的是 24KB 骨架页（state 里只有全局配置，没有笔记）
    2. xhslink.cn 短链 302 到 discovery/item/...，直接跟随，URL 里的
       share_token 等参数不能丢 —— 自己拼 URL 会弄丢 token 拿到空页
    3. state 的 JSON 里混着 JS 字面量 undefined；且笔记对象不一定在
       note.noteDetailMap，递归找「含 interactInfo + desc/title」的对象最稳
    """
    st, html = _http(url, headers={
        "User-Agent": MOBILE_UA,
        "Referer": "https://www.xiaohongshu.com/",
        "Accept": "text/html,application/xhtml+xml",
    }, timeout=30)
    if st != 200 or not html:
        return {"ok": False, "reason": f"笔记页请求失败（HTTP {st}）"}

    idx = html.find("window.__INITIAL_STATE__")
    if idx < 0:
        return {"ok": False,
                "reason": "页面没有 __INITIAL_STATE__（登录墙或风控）"}
    brace = html.find("{", idx)
    raw = _balanced_json(html, brace) if brace >= 0 else None
    if not raw:
        return {"ok": False, "reason": "__INITIAL_STATE__ 括号配平失败"}
    try:
        state = json.loads(raw)
    except json.JSONDecodeError:
        # 小红书页面里混着 JS 字面量 undefined（值位置），转 null 再解析；
        # 正则限定「:, [ 后面的 undefined」，避免误伤字符串正文里的该词
        import html as _html
        fixed = re.sub(r"([:,\[]\s*)undefined\b", r"\1null", raw)
        fixed = _html.unescape(fixed)
        try:
            state = json.loads(fixed)
        except json.JSONDecodeError as e:
            return {"ok": False,
                    "reason": f"__INITIAL_STATE__ 解析失败（{e}，已试 undefined→null）"}

    # note 数据在 detail 路径下；结构多变（note.noteDetailMap / 其他），
    # 先走已知路径，再递归找「含 interactInfo + (desc|title)」的笔记本体
    note = None
    try:
        for v in (state.get("note", {}).get("noteDetailMap") or {}).values():
            if isinstance(v, dict) and v.get("note"):
                note = v["note"]
                break
    except Exception:
        note = None

    if note is None:
        def _find_note(o: Any) -> None:
            nonlocal note
            if note is not None or not isinstance(o, dict):
                return
            if "interactInfo" in o and ("desc" in o or "title" in o):
                note = o
                return
            for v in o.values():
                _find_note(v)

        _find_note(state)
    if not note:
        return {"ok": False, "reason": "__INITIAL_STATE__ 里找不到笔记数据"}

    title = note.get("title") or note.get("desc") or ""
    video_url = _xhs_video_url(note.get("video") or {})
    return {"ok": True, "title": title, "video_url": video_url,
            "is_video": bool(video_url)}


def _xhs_video_url(video: Dict[str, Any]) -> str:
    """note.video.media.stream.{h264,h265,...}[].masterUrl，防御性遍历。"""
    stream = (video.get("media") or {}).get("stream") or {}
    for codec in ("h264", "h265", "h266", "av1"):
        for item in (stream.get(codec) or []):
            if isinstance(item, dict) and item.get("masterUrl"):
                return item["masterUrl"]
    found = [None]

    def walk(o: Any) -> None:
        if found[0] is not None:
            return
        if isinstance(o, dict):
            if isinstance(o.get("masterUrl"), str):
                found[0] = o["masterUrl"]
                return
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(video)
    return found[0] or ""


def xhs_download_audio(url: str, outdir: str,
                       cookie: Optional[str] = None) -> Dict[str, Any]:
    """小红书笔记（视频笔记）→ 音频 mp3。"""
    info = xhs_parse_note(url)
    if not info.get("ok"):
        return info
    if not info.get("video_url"):
        return {"ok": False,
                "reason": "这是图文笔记（无视频），不需要转录；"
                          "文字内容可直接交给蒸馏",
                "is_image_note": True}
    video_path = os.path.join(outdir, "xhs_video.mp4")
    headers = ["-H", f"User-Agent: {DESKTOP_UA}",
               "-H", "Referer: https://www.xiaohongshu.com/"]
    if cookie:
        headers += ["-H", f"Cookie: {cookie}"]
    p = subprocess.run(
        ["curl", "-s", "-L", "-o", video_path, *headers, info["video_url"]],
        capture_output=True, timeout=600)
    if p.returncode != 0 or os.path.getsize(video_path) < 50000:
        return {"ok": False,
                "reason": "视频直链下载失败（可能需要登录 cookie）"}
    audio = _extract_audio(video_path, os.path.join(outdir, "xhs_audio.mp3"))
    if not audio:
        return {"ok": False, "reason": "ffmpeg 抽音频失败"}
    return {"ok": True, "path": audio, "title": info.get("title", "")}


# ---------------------------------------------------------------- 公共

def _extract_audio(video_path: str, audio_path: str) -> Optional[str]:
    """ffmpeg 从视频抽音频（mp3 128k）。"""
    try:
        p = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", video_path,
             "-vn", "-acodec", "libmp3lame", "-ab", "128k", audio_path],
            capture_output=True, timeout=300)
        if p.returncode == 0 and os.path.exists(audio_path):
            return audio_path
    except Exception:
        pass
    return None
