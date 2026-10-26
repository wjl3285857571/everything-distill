"""视频元数据提取（三级漏斗 Level 1 的一半）。

用 `yt-dlp --dump-json --skip-download` 拿：
    title / uploader / upload_date / duration / description / chapters
章节信息对蒸馏价值极高（AI 可以按章节组织结构），优先提取。

各平台注意事项：
    - YouTube：本机 IP 被拦时 dump-json 也会失败 —— 走 Invidious 的
      /api/v1/videos 拿等价信息（title/author/description/chapters 字段都有）
    - B站：yt-dlp 直连可用
    - 抖音/小红书/播客：解析器各自已有元信息（标题/作者），不强求全字段
"""

import json
import re
import subprocess
from typing import Any, Dict, Optional

from .invidious import _vid as _yt_vid, probe as _inv_probe


def _fmt(sec: Any) -> str:
    try:
        sec = int(sec)
    except (TypeError, ValueError):
        return ""
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def _from_invidious(url: str) -> Dict[str, Any]:
    vid = _yt_vid(url)
    if not vid:
        return {"ok": False, "reason": "不是有效的 YouTube 链接"}
    p = _inv_probe(vid)
    if not p.get("ok"):
        return {"ok": False, "reason": p.get("reason", "Invidious 不可用")}
    d = p["data"]
    chapters = [
        {"title": c.get("title", ""), "start": c.get("start", 0)}
        for c in (d.get("chapters") or [])
    ]
    return {"ok": True, "source": "invidious",
            "title": d.get("title", ""),
            "uploader": d.get("author", ""),
            "duration": d.get("lengthSeconds"),
            "description": (d.get("description") or "")[:2000],
            "chapters": chapters,
            "stats": {"views": d.get("viewCount"),
                      "likes": d.get("likeCount")}}


def _from_ytdlp(url: str) -> Dict[str, Any]:
    try:
        p = subprocess.run(
            ["yt-dlp", "--dump-json", "--no-warnings", "--skip-download", url],
            capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "yt-dlp 超时（120s）"}
    if p.returncode != 0 or not p.stdout.strip():
        return {"ok": False, "reason": (p.stderr or "").strip().splitlines()[-1][:200]
                if p.stderr else "yt-dlp 失败"}
    try:
        d = json.loads(p.stdout)
    except json.JSONDecodeError as e:
        return {"ok": False, "reason": f"dump-json 解析失败（{e}）"}
    chapters = [
        {"title": c.get("title", ""), "start": c.get("start_time", 0)}
        for c in (d.get("chapters") or [])
    ]
    return {"ok": True, "source": "yt-dlp",
            "title": d.get("title", ""),
            "uploader": d.get("uploader") or d.get("channel") or "",
            "upload_date": d.get("upload_date", ""),
            "duration": d.get("duration"),
            "description": (d.get("description") or "")[:2000],
            "chapters": chapters,
            "stats": {"views": d.get("view_count"),
                      "likes": d.get("like_count")}}


def _from_cn(url: str) -> Dict[str, Any]:
    """抖音/小红书/播客：复用各自解析器已有的元信息（标题为主）。"""
    if re.search(r"douyin\.com|iesdouyin\.com", url):
        from .cn_platforms import douyin_video_id, douyin_parse_share_page
        try:
            vid = douyin_video_id(url)
            info = douyin_parse_share_page(vid)
            if info.get("ok"):
                return {"ok": True, "source": "douyin",
                        "title": info.get("title", ""), "chapters": []}
            return {"ok": False, "reason": info.get("reason", "")}
        except ValueError as e:
            return {"ok": False, "reason": str(e)}
    if re.search(r"xiaohongshu\.com|xhslink\.com", url):
        from .cn_platforms import xhs_parse_note
        r = xhs_parse_note(url)
        if r.get("ok"):
            return {"ok": True, "source": "xiaohongshu",
                    "title": r.get("title") or (r.get("desc") or "")[:60],
                    "chapters": []}
        return {"ok": False, "reason": r.get("reason", "")}
    if re.search(r"xiaoyuzhoufm\.com", url):
        from .podcast import xiaoyuzhou_audio_url
        r = xiaoyuzhou_audio_url(url)
        if r.get("ok"):
            return {"ok": True, "source": "xiaoyuzhou",
                    "title": r.get("title", ""),
                    "duration": r.get("duration"), "chapters": []}
        return {"ok": False, "reason": r.get("reason", "")}
    if re.search(r"xima\.tv|ximalaya\.com", url):
        from .podcast import ximalaya_audio_url
        r = ximalaya_audio_url(url)
        # 付费内容也把元信息带出来（付费≠连标题都不给）
        if r.get("meta"):
            m = r["meta"]
            return {"ok": True, "source": "ximalaya",
                    "title": m.get("title", ""), "uploader": m.get("album", ""),
                    "duration": m.get("duration"), "chapters": [],
                    "paywalled": True}
        return {"ok": r.get("ok", False), "reason": r.get("reason", "")}
    return {"ok": False, "reason": "不认识的平台"}


def metadata(url: str) -> Dict[str, Any]:
    """元数据主入口。YouTube 优先 Invidious（IP 稳），其余平台各走各的。"""
    if re.search(r"youtube\.com|youtu\.be", url):
        r = _from_invidious(url)
        if r.get("ok"):
            return r
        # 实例全挂时再试 yt-dlp（万一 IP 解封了）
        r2 = _from_ytdlp(url)
        if r2.get("ok"):
            return r2
        return {"ok": False,
                "reason": f"Invidious 与 yt-dlp 都失败：{r.get('reason', '')}"}
    if re.search(r"bilibili\.com|b23\.tv", url):
        return _from_ytdlp(url)
    return _from_cn(url)


def render_brief(meta: Dict[str, Any]) -> str:
    """把元数据渲染成给 LLM 的上下文块（蒸馏时用于语义纠错）。"""
    if not meta.get("ok"):
        return ""
    parts = []
    if meta.get("title"):
        parts.append(f"标题：{meta['title']}")
    if meta.get("uploader"):
        parts.append(f"频道/作者：{meta['uploader']}")
    if meta.get("duration"):
        parts.append(f"时长：{_fmt(meta['duration'])}")
    if meta.get("upload_date"):
        parts.append(f"发布：{meta['upload_date']}")
    chs = meta.get("chapters") or []
    if chs:
        ch_lines = [f"  {_fmt(c['start'])} {c['title']}" for c in chs]
        parts.append("章节：\n" + "\n".join(ch_lines))
    if meta.get("description"):
        desc = meta["description"].strip()
        if desc:
            parts.append(f"简介：{desc[:500]}")
    return "\n".join(parts)
