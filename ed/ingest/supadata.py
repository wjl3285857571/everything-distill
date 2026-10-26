"""Supadata 转录 API（三级漏斗 YouTube 路径的第一优先）。

免费档 100 积分/月（1 积分 = 1 个视频的原生字幕；AI 兜底 2 积分/分钟）。
median 延迟 ~49ms（官方口径），实测老梁 27 分钟视频 13s 返回 3020 段。

为什么放在第一优先：
    - 比 yt-dlp 直连省掉下载（字幕直接 JSON 回来），比 Invidious 快两个量级
    - 失败/无额度时自动落到 yt-dlp，不挡路

key 管理：
    放项目 .env 的 SUPADATA_API_KEY=，或环境变量。
    `ed.config` 会按「项目根目录 → 当前目录 → 旧路径 → 环境变量」找，
    不再写死某台机器的绝对路径；额度用完 API 返回 402/429，调用方落回下级即可。

请求构造：
    视频 URL 必须整体做百分号编码后放进 `url` 参数。
    直接拼 `?url={url}` 时，链接里的 `&list=...&index=...` 会被服务器
    当成 Supadata 自己的参数，导致视频地址被截断。
"""

import json
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from ..config import get_env

API_URL = "https://api.supadata.ai/v1/transcript"


def _api_key() -> Optional[str]:
    """从 .env / 环境变量读 key（不写入代码库）。"""
    return get_env("SUPADATA_API_KEY") or None


def _fmt(ms: float) -> str:
    s = int(ms / 1000)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _merge_segments(segs: List[Dict[str, Any]],
                    target_span: float = 8.0) -> List[Tuple[float, str]]:
    """Supadata 的细粒度片段（1~2 词/段）→ (start_sec, text) 列表。

    按 target_span 秒聚合（在片段边界合并，不切开），给下游清洗器。
    """
    out: List[Tuple[float, str]] = []
    buf: List[str] = []
    buf_start: Optional[float] = None
    last_end = 0.0
    for seg in segs:
        start_ms = float(seg.get("offset") or 0)
        dur_ms = float(seg.get("duration") or 0)
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        if buf_start is None:
            buf_start = start_ms
        buf.append(text)
        last_end = max(last_end, start_ms + dur_ms)
        if (last_end - buf_start) / 1000 >= target_span:
            out.append((buf_start / 1000, " ".join(buf)))
            buf, buf_start = [], None
    if buf and buf_start is not None:
        out.append((buf_start / 1000, " ".join(buf)))
    return out


def build_request_url(url: str) -> str:
    """把视频 URL 正确编码进 Supadata 查询参数。

    单独抽成函数是为了能测：带 `&list=` / `&index=` 的链接必须完整保留。
    """
    return f"{API_URL}?{urllib.parse.urlencode({'url': url})}"


def fetch_transcript_supadata(url: str) -> Dict[str, Any]:
    """Supadata 转录。返回 {"ok", "text", "segments", "via"} 或失败原因。

    失败的 reason_code 约定：
        no_api_key   未配置 key（正常，静默落回下级）
        quota        额度用尽/需付费（落回下级，不重试）
        http_<code>  其他 HTTP 错误
    """
    key = _api_key()
    if not key:
        return {"ok": False, "reason_code": "no_api_key",
                "reason": "未配置 SUPADATA_API_KEY"}

    import subprocess
    p = subprocess.run(
        ["curl", "-s", "--max-time", "120",
         build_request_url(url),
         "-H", f"x-api-key: {key}"],
        capture_output=True, text=True, timeout=150)
    raw = p.stdout.strip()
    if not raw:
        return {"ok": False, "reason_code": "network",
                "reason": "Supadata 无响应"}
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "reason_code": "bad_json",
                "reason": f"响应不是 JSON：{raw[:120]}"}

    # 错误形态：{"message": "...", "code": ...} 或 {"error": ...}
    if d.get("error") or (d.get("message") and not d.get("content")):
        msg = str(d.get("message") or d.get("error"))[:150]
        low = msg.lower()
        code = "quota" if ("quota" in low or "credit" in low
                           or "limit" in low or "402" in msg
                           or "429" in msg) else "api_error"
        return {"ok": False, "reason_code": code, "reason": msg}

    content = d.get("content") or []
    if not content:
        return {"ok": False, "reason_code": "no_content",
                "reason": "API 返回空字幕"}

    merged = _merge_segments(content)
    # 交给统一清洗器（时间戳格式、去噪、句聚合）
    from .transcript import merge_to_lines, format_lines
    lines = merge_to_lines([(s, s + 8.0, t) for s, t in merged])
    text = format_lines(lines)
    return {"ok": True, "text": text, "segments": merged,
            "lang": d.get("lang", ""),
            "via": "supadata",
            "chunk_count": len(content)}
