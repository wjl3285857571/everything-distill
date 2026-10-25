"""转写稿 / 字幕清洗：统一成 `[HH:MM:SS] 文本` 紧凑格式。

为什么需要这一层：
原始字幕（YouTube 自动字幕的 VTT/SRV3、ASR 输出的逐段文本）是给播放器看的，
不是给 LLM 看的——滚动重复行、细碎时间轴、HTML 标签、同帧多行，直接喂进去
token 浪费一倍以上，蒸馏时还容易被重复行带偏。

输出契约（Gemini 三级漏斗规范）：
    [HH:MM:SS] 文本内容
    - 每行一个时间锚点，取该段**起始**时间，不写时间段
    - 自然句为主（有标点按标点切），单行超过 10 秒强制断
    - 无标点的自动字幕：按原始 Cue 时间轴边界合并到 8~10 秒一行，
      **绝不在单个 Cue 词块中间硬截断**（自动字幕的词块是语义最小单位，
      中间截断会产生大量无意义碎片）
"""

import re
from typing import Any, Dict, List, Optional, Tuple

# 句末标点（中英文）。全角问号叹号也在内
_SENT_END = re.compile(r"[。？！.?!]+[”’」』）)\]]*")
# Cue 内部时间轴（VTT/SRT）
_CUE_TIME = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*\d{1,2}:\d{2}:\d{2}[.,]\d{3}")
# 行内 inline 标签 <c>、<00:00:01.000>、<i> 等
_INLINE_TAG = re.compile(r"<[^>]+>")
# 滚动重复行/装饰行
_NOISE = re.compile(
    r"^\s*(\[?(音乐|Music|掌声|Applause|笑声|Laughter)\]?|♪+|\(.*?\))\s*$",
    re.I)

MAX_SPAN = 10.0        # 单行最长秒数（超过强制断）
TARGET_SPAN = 8.0      # 无标点合并时的目标秒数


def _fmt(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def _clean_text(s: str) -> str:
    s = _INLINE_TAG.sub("", s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&gt;", ">")\
         .replace("&lt;", "<").replace("&#39;", "'").replace("&quot;", '"')
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ---------------------------------------------------------------- 输入解析

def parse_cues(raw: str) -> List[Tuple[float, float, str]]:
    """把 VTT/SRT/纯文本行解析成 (start, end, text) 列表。

    - VTT/SRT：按时间轴行切 Cue，Cue 内多行文本合并
    - 已是 `[HH:MM:SS] 文本` 格式：按时间差估 end
    - 纯文本（无时间轴）：整段作为一个 Cue，start=0
    """
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cues: List[Tuple[float, float, str]] = []

    # 尝试 VTT/SRT 模式
    i = 0
    n = len(lines)
    while i < n:
        m = _CUE_TIME.search(lines[i])
        if not m:
            i += 1
            continue
        start = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) \
            + int(m.group(4)) / 1000
        # Cue 正文：时间行之后直到空行或下一个时间行
        i += 1
        buf: List[str] = []
        while i < n and lines[i].strip() and not _CUE_TIME.search(lines[i]):
            t = _clean_text(lines[i])
            if t:
                buf.append(t)
            i += 1
        text = " ".join(buf).strip()
        if text:
            cues.append((start, start, text))
        continue

    if cues:
        # VTT 里 end 缺失（我们只存了 start）；用下一 Cue 的 start 补，
        # 且 YouTube 自动字幕同文本常连续滚 2~3 帧，下面去重时处理
        for j in range(len(cues) - 1):
            cues[j] = (cues[j][0], cues[j + 1][0], cues[j][2])
        if cues:
            last = cues[-1]
            cues[-1] = (last[0], last[0] + 3.0, last[2])
        return _strip_rolling_dupes(cues)

    # 尝试 [HH:MM:SS] 文本 模式（ASR 已有的输出）
    ts = re.compile(r"\[(\d{1,2}):(\d{2}):(\d{2})\]\s*(.*)")
    for ln in lines:
        m = ts.match(ln.strip())
        if m:
            start = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            text = _clean_text(m.group(4))
            if text:
                cues.append((float(start), float(start) + 3.0, text))
    if cues:
        for j in range(len(cues) - 1):
            s0, _, t0 = cues[j]
            cues[j] = (s0, cues[j + 1][0], t0)
        return cues

    # 纯文本：整段一个 Cue
    text = _clean_text(raw)
    if text:
        return [(0.0, 0.0, text)]
    return []


def _strip_rolling_dupes(cues: List[Tuple[float, float, str]]
                         ) -> List[Tuple[float, float, str]]:
    """YouTube 自动字幕的滚动重复：同一句在第 N 帧出现一半、N+1 帧全量。

    处理：当前 Cue 文本是前一 Cue 的延续（前一 Cue 是当前的前缀）时，
    保留当前（更全），start 用前一 Cue 的。
    """
    out: List[Tuple[float, float, str]] = []
    for start, end, text in cues:
        if out:
            p_start, p_end, p_text = out[-1]
            if text == p_text:
                out[-1] = (p_start, max(p_end, end), p_text)
                continue
            # 当前帧是上一帧的扩展（滚动补全）
            if text.startswith(p_text) and len(text) > len(p_text):
                out[-1] = (p_start, end, text)
                continue
            # 上一帧是本帧的前缀片段（顺序滚动时少见，防一手）
            if p_text.startswith(text):
                out[-1] = (p_start, p_end, p_text)
                continue
        out.append((start, end, text))
    return out


# ---------------------------------------------------------------- 合并输出

def merge_to_lines(cues: List[Tuple[float, float, str]],
                   max_span: float = MAX_SPAN,
                   target_span: float = TARGET_SPAN) -> List[Dict[str, Any]]:
    """Cue 流 → `[HH:MM:SS] 文本` 行。

    有标点：按句切，句子累计不超过 max_span；单句超长在标点处断不开时
    （无标点长句）按 target_span 在 Cue 边界断——绝不切进 Cue 内部。
    无标点：按 Cue 累计到 target_span~max_span 之间，在 Cue 边界收行。
    """
    lines: List[Dict[str, Any]] = []
    buf_start: Optional[float] = None
    buf_texts: List[str] = []
    buf_end = 0.0
    has_punct = any(_SENT_END.search(t) for _, _, t in cues)

    def flush():
        nonlocal buf_start, buf_texts, buf_end
        if buf_texts and buf_start is not None:
            text = "".join(buf_texts).strip() if has_punct \
                else " ".join(buf_texts).strip()
            if text:
                lines.append({"start": buf_start, "text": text})
        buf_start, buf_texts, buf_end = None, [], 0.0

    for start, end, text in cues:
        if not text:
            continue
        if _NOISE.match(text):
            continue

        # 有标点时先把 Cue 内部按句拆开（Cue 内切分是安全的——标点是硬边界）
        pieces: List[Tuple[float, float, str]] = []
        if has_punct and _SENT_END.search(text):
            pos = 0.0
            span = max(end - start, 0.4)
            segs = [x for x in _SENT_END.split(text) if x and x.strip()]
            if len(segs) > 1:
                per = span / len(segs)
                for k, seg in enumerate(segs):
                    pieces.append((start + k * per,
                                   start + (k + 1) * per,
                                   seg.strip()))
                    pos += per
            else:
                pieces.append((start, end, text))
        else:
            pieces.append((start, end, text))

        for p_start, p_end, p_text in pieces:
            if buf_start is None:
                buf_start, buf_texts, buf_end = p_start, [p_text], p_end
                continue

            cur_span = p_end - buf_start

            # ① 当前块自己已经把缓冲撑爆 → 先收行再开新行
            if cur_span > max_span:
                flush()
                buf_start, buf_texts, buf_end = p_start, [p_text], p_end
                continue

            buf_texts.append(p_text)
            buf_end = p_end

            # ② 句末标点：自然断点。超过 target_span 就收，不然继续攒
            if has_punct and _SENT_END.search(p_text):
                if (buf_end - buf_start) >= target_span:
                    flush()
                continue

            # ③ 无标点（或未到句末）：达到目标区间就在 Cue 边界收行
            if not has_punct and (buf_end - buf_start) >= target_span:
                flush()

    flush()
    return lines


def format_lines(lines: List[Dict[str, Any]]) -> str:
    return "\n".join(f"[{_fmt(l['start'])}] {l['text']}" for l in lines)


def clean_transcript(raw: str) -> Dict[str, Any]:
    """主入口：任意原始转写/字幕 → 清洗后的 [HH:MM:SS] 文本 + 统计。"""
    cues = parse_cues(raw)
    if not cues:
        return {"ok": False, "reason": "没有可解析的内容", "lines": [], "text": ""}
    lines = merge_to_lines(cues)
    text = format_lines(lines)
    orig_chars = len(re.sub(r"\s", "", raw))
    new_chars = len(re.sub(r"\s", "", text))
    return {"ok": True, "text": text, "lines": lines,
            "stats": {"原始字符": orig_chars, "清洗后字符": new_chars,
                      "压缩比": round(new_chars / max(orig_chars, 1), 3),
                      "行数": len(lines)}}
