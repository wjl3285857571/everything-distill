"""YouTube 走 Invidious 公共实例的兜底链路。

为什么需要这一层：
YouTube 自 2024 年底起对数据中心 / VPN 出口 IP 全面收紧（yt-dlp#10128），
未登录请求无论用什么 client / PO Token 都可能被「Sign in to confirm
you're not a bot」拦下。社区对策是走 Invidious / Piped 代理——**用实例
服务器的 IP** 向 YouTube 拉数据，本地只跟实例通信，与本地出口 IP 无关。

链路（对应 to_transcript 的两步）：
    1. /api/v1/videos 拿 captionTracks → 实例代理下载字幕   ← 优先，快且准
    2. adaptiveFormats 拿音频直链 → latest_version?local=true 代理流 → 本地 ASR

local=true 是关键：googlevideo 直链里烧录了实例服务器的 IP（ip= 参数），
本机直接拉会 403；让实例代理流回本地才能下到。

实例列表动态拉 https://api.invidious.io/instances.json，缓存 24h。
"""

import html
import json
import os
import re
import tempfile
import time
from typing import Any, Dict, List, Optional

_INSTANCES_CACHE = {"at": 0.0, "list": []}
_CACHE_TTL = 86400.0
_FALLBACK_INSTANCES = [
    "yt.chocolatemoo53.com",
    "invidious.nerdvpn.de",
    "inv.nadeko.net",
]

_TIMEOUT = 25


def _http(url: str, timeout: int = _TIMEOUT) -> Optional[bytes]:
    try:
        from urllib.request import Request, urlopen

        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None


def _instances() -> List[str]:
    """活的 https 实例，动态列表 + 静态兜底，缓存 24h。"""
    now = time.time()
    if _INSTANCES_CACHE["list"] and now - _INSTANCES_CACHE["at"] < _CACHE_TTL:
        return _INSTANCES_CACHE["list"]
    raw = _http("https://api.invidious.io/instances.json?sort_by=health", 15)
    live: List[str] = []
    if raw:
        try:
            data = json.loads(raw)
            live = [name for name, info in data
                    if isinstance(info, dict)
                    and info.get("type") == "https"
                    and info.get("api") is not False]
        except Exception:
            live = []
    out = live + [i for i in _FALLBACK_INSTANCES if i not in live]
    _INSTANCES_CACHE.update(at=now, list=out)
    return out


def _get_json(inst: str, vid: str) -> Optional[Dict[str, Any]]:
    raw = _http(f"https://{inst}/api/v1/videos/{vid}", _TIMEOUT)
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except Exception:
        return None
    # 「Endpoint disabled」等错误也是合法 JSON，但没有 title 字段
    return d if isinstance(d, dict) and d.get("title") else None


def _vid(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|embed/|shorts/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else ""


def probe(vid: str) -> Dict[str, Any]:
    """找一个能用的实例并返回视频元数据。失败给明确原因。"""
    if not vid:
        return {"ok": False, "reason": "不是有效的 YouTube 视频链接"}
    last_err = "所有实例都没有响应"
    for inst in _instances():
        d = _get_json(inst, vid)
        if d:
            return {"ok": True, "instance": inst, "data": d}
        last_err = f"{inst} 无响应"
    return {"ok": False, "reason": f"Invidious 实例全部不可用：{last_err}"}


def fetch_subtitle_invidious(url: str) -> Dict[str, Any]:
    """通过 Invidious 拿现成字幕（含自动字幕）。"""
    vid = _vid(url)
    p = probe(vid)
    if not p.get("ok"):
        return {"ok": False, "reason": p.get("reason", "")}
    inst, d = p["instance"], p["data"]
    caps: List[Dict[str, Any]] = d.get("captions") or []
    if not caps:
        return {"ok": False,
                "reason": f"实例 {inst} 可用，但该视频没有字幕轨（含自动字幕）",
                "instance": inst, "title": d.get("title", "")}

    def _rank(c: Dict[str, Any]) -> int:
        lang = (c.get("language_code") or "").lower()
        label = (c.get("label") or "").lower()
        s = 0
        if lang.startswith("zh"):
            s -= 4
        elif lang.startswith("en"):
            s -= 2
        if "auto" in label or "（自动" in label:
            s += 1          # 人工字幕优先于自动
        return s

    caps.sort(key=_rank)
    best = caps[0]
    u = best.get("url") or ""
    if not u:
        return {"ok": False, "reason": "字幕轨没有可用的 url", "instance": inst}
    if u.startswith("/"):
        u = f"https://{inst}{u}"
    raw = _http(u, 60)
    if not raw:
        return {"ok": False, "reason": "字幕下载失败", "instance": inst}
    body = raw.decode("utf-8", "ignore")
    # 实例转发字幕到 Google 时可能被反爬盯上，返回 Google「Sorry...」拦截页
    if "We're sorry" in body or body.lstrip().startswith("<!DOCTYPE html><html><head><meta http-equiv=\"content-type\""):
        return {"ok": False,
                "reason": f"实例 {inst} 的字幕转发被 Google 反爬拦截（该实例字幕功能暂时不可用）",
                "instance": inst}
    text = _vtt_or_xml_to_timed_text(body)
    if not text.strip():
        return {"ok": False, "reason": "字幕是空的", "instance": inst}
    return {"ok": True, "text": text, "instance": inst,
            "lang": best.get("language_code", ""),
            "title": d.get("title", ""),
            "label": best.get("label", ""),
            "timed": True}


def _stream_to_file(url: str, path: str, timeout: int = 600,
                    min_bytes: int = 10000) -> int:
    """把 URL 流式写到文件，返回字节数；太小则删掉文件并返回 0。

    为什么不 urlopen().read()：Invidious 代理的是整段音频，长视频几十上百 MB，
    一次性读进内存会顶爆。分块写盘，内存占用恒定。
    """
    from urllib.request import Request, urlopen
    written = 0
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=timeout) as r, open(path, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                written += len(chunk)
        if written < min_bytes:
            os.unlink(path)
            return 0
        return written
    except Exception:
        try:
            if os.path.exists(path):
                os.unlink(path)
        except OSError:
            pass
        return 0


def download_audio_invidious(url: str, outdir: str) -> Dict[str, Any]:
    """通过 Invidious 的 local=true 代理下载音频。

    逐个实例尝试：某个实例元数据能拿到、但音频流 403/超时，换下一个，
    而不是在同一个实例上把几个 itag 试完就放弃。
    """
    vid = _vid(url)
    if not vid:
        return {"ok": False, "reason": "不是有效的 YouTube 视频链接"}
    out_path = os.path.join(outdir, f"yt_{vid}.webm")
    last_err = "所有实例都没有可用音频"
    tried: List[str] = []
    for inst in _instances()[:5]:
        d = _get_json(inst, vid)
        if not d:
            continue
        tried.append(inst)
        aud = [f for f in (d.get("adaptiveFormats") or [])
               if "audio" in (f.get("type") or "")]
        if not aud:
            last_err = f"实例 {inst} 可用但没有音频流"
            continue
        aud.sort(key=lambda f: int(f.get("bitrate") or 0), reverse=True)
        for f in aud[:4]:
            n = _stream_to_file(
                f"https://{inst}/latest_version?id={vid}"
                f"&itag={f.get('itag')}&local=true", out_path)
            if n:
                return {"ok": True, "path": out_path, "instance": inst,
                        "title": d.get("title", ""), "bytes": n}
        last_err = f"实例 {inst} 音频代理下载失败"
    return {"ok": False, "reason": last_err, "tried": tried}


def _vtt_or_xml_to_text(s: str) -> str:
    """字幕容器（vtt / ttml / json3）转纯文本，去时间轴与重复行。"""
    # ttml / xml
    if "<transcript" in s or "<text" in s:
        return _xml_captions_to_text(s)
    # json3
    if s.strip().startswith("{"):
        try:
            d = json.loads(s)
            ev = d.get("events") or []
            out = []
            for e in ev:
                for seg in e.get("segs") or []:
                    t = seg.get("utf8", "")
                    if t and t != "\n":
                        out.append(t)
            return "".join(out).replace("\n", " ").strip()
        except Exception:
            pass
    # vtt：去 WEBVTT 头、时间行、cue 编号
    lines = []
    seen = set()
    for ln in s.splitlines():
        t = ln.strip()
        if (not t or t.startswith(("WEBVTT", "Kind:", "Language:", "NOTE"))
                or "-->" in t or re.fullmatch(r"\d+", t)):
            continue
        t = re.sub(r"<[^>]+>", "", t)          # <c> 等 inline 标签
        if t and t not in seen:
            seen.add(t)
            lines.append(t)
    return " ".join(lines)


def _xml_captions_to_text(s: str) -> str:
    import html
    texts = re.findall(r"<text[^>]*>(.*?)</text>", s, re.S)
    out = []
    for t in texts:
        t = html.unescape(t)
        t = re.sub(r"<[^>]+>", "", t).strip()
        if t:
            out.append(t)
    return " ".join(out)


def _ts_hhmmss(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _normalize_ts(raw: str) -> str:
    """TTML 的时间写法很杂：00:00:01.000 / 01:02.5 / 12.3s。统一成 HH:MM:SS。"""
    raw = raw.strip()
    m = re.match(r"^(\d{1,2}):(\d{2}):(\d{2})(?:[.,]\d+)?$", raw)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}:{m.group(3)}"
    m = re.match(r"^(\d{1,2}):(\d{2})(?:[.,]\d+)?$", raw)
    if m:
        return f"00:{int(m.group(1)):02d}:{m.group(2)}"
    m = re.match(r"^([\d.]+)s?$", raw)
    if m:
        return _ts_hhmmss(float(m.group(1)))
    return "00:00:00"


def _vtt_or_xml_to_timed_text(s: str) -> str:
    """字幕容器（vtt / ttml / json3）→ `[HH:MM:SS] 文本`，保留时间轴。

    字幕路径以前把时间轴丢掉，导致「每条观点能跳回原视频」在
    YouTube 字幕链路上是空的。这里三种容器都尽量保住起始时间。
    """
    # json3
    if s.strip().startswith("{"):
        try:
            d = json.loads(s)
            out = []
            for e in d.get("events") or []:
                t = "".join(seg.get("utf8", "") for seg in (e.get("segs") or []))
                t = t.replace("\n", " ").strip()
                if t:
                    out.append(
                        f"[{_ts_hhmmss(float(e.get('tStartMs') or 0) / 1000)}] {t}")
            if out:
                return "\n".join(out)
        except Exception:
            pass

    # ttml / xml
    if "<transcript" in s or "<text" in s:
        out = []
        for m in re.finditer(
                r"<(?:text|p)\b([^>]*)>(.*?)</(?:text|p)>", s, re.S):
            attrs, body = m.group(1), m.group(2)
            tm = re.search(r'\b(?:start|begin)\s*=\s*"([^"]+)"', attrs)
            if not tm:
                continue
            body = html.unescape(re.sub(r"<[^>]+>", "", body)).strip()
            if body:
                out.append(f"[{_normalize_ts(tm.group(1))}] {body}")
        if out:
            return "\n".join(out)
        return _xml_captions_to_text(s)

    # vtt
    out = []
    ts = ""
    for ln in s.splitlines():
        line = ln.strip()
        m = re.match(r"(\d{1,2}):(\d{2}):(\d{2})[.,]\d{3}\s*-->", line)
        if m:
            ts = f"{int(m.group(1)):02d}:{m.group(2)}:{m.group(3)}"
            continue
        if (not line or "-->" in line
                or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE"))
                or re.fullmatch(r"\d+", line)):
            continue
        clean = html.unescape(re.sub(r"<[^>]+>", "", line)).strip()
        if clean and ts:
            out.append(f"[{ts}] {clean}")
    return "\n".join(out)
