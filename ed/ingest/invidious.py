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
    text = _vtt_or_xml_to_text(body)
    if not text.strip():
        return {"ok": False, "reason": "字幕是空的", "instance": inst}
    return {"ok": True, "text": text, "instance": inst,
            "lang": best.get("language_code", ""),
            "title": d.get("title", ""),
            "label": best.get("label", "")}


def download_audio_invidious(url: str, outdir: str) -> Dict[str, Any]:
    """通过 Invidious 的 local=true 代理下载音频。"""
    vid = _vid(url)
    p = probe(vid)
    if not p.get("ok"):
        return {"ok": False, "reason": p.get("reason", "")}
    inst, d = p["instance"], p["data"]
    aud = [f for f in (d.get("adaptiveFormats") or [])
           if "audio" in (f.get("type") or "")]
    if not aud:
        return {"ok": False, "reason": f"实例 {inst} 可用但没有音频流",
                "instance": inst}

    def _br(f: Dict[str, Any]) -> int:
        return int(f.get("bitrate") or 0)

    aud.sort(key=_br, reverse=True)
    itag = aud[0].get("itag")
    out_path = os.path.join(outdir, f"yt_{vid}.webm")
    raw = _http(f"https://{inst}/latest_version?id={vid}&itag={itag}&local=true",
                600)
    if not raw or len(raw) < 10000:
        # 逐条降级重试
        for f in aud[1:4]:
            raw = _http(f"https://{inst}/latest_version?id={vid}"
                        f"&itag={f.get('itag')}&local=true", 600)
            if raw and len(raw) > 10000:
                break
        else:
            return {"ok": False, "reason": "音频代理下载失败（<10KB）",
                    "instance": inst}
    with open(out_path, "wb") as f:
        f.write(raw)
    return {"ok": True, "path": out_path, "instance": inst,
            "title": d.get("title", ""),
            "bytes": len(raw)}


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
