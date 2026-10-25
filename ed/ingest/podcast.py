"""播客平台（小宇宙 / 喜马拉雅）→ 音频直链。

小宇宙（2026-10-24 实测通过）
    单集页 HTML 里有 `<script id="__NEXT_DATA__">`，JSON 路径
    props.pageProps.episode.enclosure.url → media.xyzcdn.net 直链，
    无需任何登录态，直接 GET 可下。

喜马拉雅（2026-10-24 实测：免费内容通，付费内容拿不到地址）
    xima.tv 短链 302 → m.ximalaya.com/selfshare/sound/<trackId>?...
    从 Location 里抠出 trackId 后请求移动端接口：
        https://mobile.ximalaya.com/mobile/v1/track/<trackId>
    返回标题/专辑/时长/isPaid 等；**免费**内容带 playUrl 系字段，
    付费内容（isPaid=True）不返回音频地址 —— 这是版权保护不是技术墙，
    应如实告诉用户「这集要 VIP」，而不是反复重试。
"""

import json
import re
from typing import Any, Dict, Optional, Tuple

DESKTOP_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")
MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")


def _http(url: str, headers: Optional[Dict[str, str]] = None,
          timeout: int = 30) -> Tuple[int, str]:
    import urllib.request
    h = {"User-Agent": DESKTOP_UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, str(e)


# ---------------------------------------------------------------- 小宇宙

def xiaoyuzhou_audio_url(url: str) -> Dict[str, Any]:
    """小宇宙单集页 → 音频直链 + 元信息。"""
    st, html = _http(url)
    if st != 200 or not html:
        return {"ok": False, "reason": f"小宇宙页面请求失败（HTTP {st}）"}
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html, re.S)
    if not m:
        return {"ok": False, "reason": "页面没有 __NEXT_DATA__（结构变更）"}
    try:
        d = json.loads(m.group(1))
        ep = d["props"]["pageProps"]["episode"]
    except (KeyError, TypeError, json.JSONDecodeError) as e:
        return {"ok": False, "reason": f"__NEXT_DATA__ 解析失败（{e}）"}
    audio = (ep.get("enclosure") or {}).get("url") or ""
    if not audio:
        return {"ok": False, "reason": "单集没有音频附件"}
    import html as _html
    title = _html.unescape(ep.get("title") or "")
    return {"ok": True, "audio_url": audio, "title": title,
            "duration": ep.get("duration"),
            "source": "xiaoyuzhou"}


# ---------------------------------------------------------------- 喜马拉雅

def ximalaya_track_id(url: str) -> Optional[str]:
    """xima.tv 短链跟随 302，从最终 Location 抠 trackId。"""
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": MOBILE_UA})
    try:
        resp = opener.open(req, timeout=20)
        final = resp.url
    except urllib.error.HTTPError as e:      # 302 抛 HTTPError
        final = e.headers.get("Location", "")
    except Exception:
        final = ""
    # 依次试：最终地址、原始链接、以及 302 链里 URL 编码过的 jt= 参数
    # （xima.tv 第一跳是 /jt/new_share?...jt=<urlencode(/selfshare/sound/<id>?...)>）
    candidates = [final, url]
    import urllib.parse as _up
    m2 = re.search(r"[?&]jt=([^&]+)", final)
    if m2:
        candidates.append(_up.unquote(m2.group(1)))
    m2b = re.search(r"[?&]rowkey=([^&]+)", final)
    if m2b:
        candidates.append(_up.unquote(m2b.group(1)))
    for c in candidates:
        m = (re.search(r"/sound/(\d+)", c)
             or re.search(r"srcId=(\d+)", c)
             or re.search(r"track[_/](\d+)", c))
        if m:
            return m.group(1)
    return None


def ximalaya_audio_url(url: str) -> Dict[str, Any]:
    """喜马拉雅链接 → 音频直链（仅免费内容）。"""
    tid = ximalaya_track_id(url)
    if not tid:
        return {"ok": False, "reason": "无法从链接提取 trackId"}
    st, body = _http(
        f"https://mobile.ximalaya.com/mobile/v1/track/{tid}",
        headers={"User-Agent": MOBILE_UA,
                 "Referer": "https://m.ximalaya.com/"})
    if st != 200:
        return {"ok": False, "reason": f"喜马拉雅接口请求失败（HTTP {st}）"}
    try:
        d = json.loads(body)
    except json.JSONDecodeError:
        return {"ok": False, "reason": "接口返回非 JSON"}

    info = {
        "title": d.get("title", ""),
        "album": d.get("albumTitle", ""),
        "duration": d.get("duration"),
        "is_paid": bool(d.get("isPaid")),
        "track_id": tid,
    }
    if d.get("isPaid") and not d.get("isFree"):
        return {"ok": False,
                "reason": (f"《{info['title']}》是付费内容"
                           f"（{d.get('displayPrice') or 'VIP'}），"
                           "音频地址受版权保护拿不到。需要你手动提供音频文件，"
                           "或换免费内容"),
                "meta": info}

    play = (d.get("playUrl64") or d.get("playPathAacv164")
            or d.get("playUrl32") or d.get("playPathAacv224") or "")
    if not play:
        return {"ok": False,
                "reason": "接口没返回播放地址（可能需要登录或已下架）",
                "meta": info}
    return {"ok": True, "audio_url": play, "source": "ximalaya", **info}


# ---------------------------------------------------------------- 统一入口

def podcast_audio(url: str, outdir: str) -> Dict[str, Any]:
    """判断播客平台 → 拿音频直链 → 下载到 outdir。"""
    import os
    import subprocess

    if "xiaoyuzhoufm.com" in url:
        r = xiaoyuzhou_audio_url(url)
        dl_headers = []
    elif "xima.tv" in url or "ximalaya.com" in url:
        r = ximalaya_audio_url(url)
        dl_headers = ["-H", "User-Agent: " + MOBILE_UA,
                      "-H", "Referer: https://m.ximalaya.com/"]
    else:
        return {"ok": False, "reason": "不是已支持的播客平台"}

    if not r.get("ok"):
        return r

    audio_url = r["audio_url"]
    ext = ".m4a" if ".m4a" in audio_url else ".mp3"
    out_path = os.path.join(outdir, "podcast" + ext)
    p = subprocess.run(
        ["curl", "-sL", "--max-time", "600", "-o", out_path,
         "-H", f"User-Agent: {DESKTOP_UA}", *dl_headers, audio_url],
        capture_output=True, timeout=660)
    if p.returncode != 0 or os.path.getsize(out_path) < 50000:
        return {"ok": False, "reason": "音频下载失败或文件过小",
                "meta": {k: r.get(k) for k in ("title", "album")}}
    return {"ok": True, "path": out_path, "title": r.get("title", ""),
            "meta": {k: r.get(k) for k in ("title", "album", "duration")}}
