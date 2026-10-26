"""先把内容大致讲一遍。

这是默认产物：不管输入是书、论文、网页还是视频，先让模型把「这是什么、
讲了什么、怎么展开的」用连贯中文讲一遍，而不是直接抽成洞察卡片。

和 narrate 的分工：
    brief   —— 先讲一遍，600–1200 字，给人一个整体印象；
    narrate —— 逐章讲透，每部分 1200–2500 字，给想细读的人。

长文走两层：先按章节各写一段概述，再把概述合成全局概述。这样不会因为
一次塞不下而只讲开头。
"""

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..ingest.base import Block, Document
from ..runner import Runner, parse_json_loose
from .narrate import load_template

MIN_GIST_CHARS = 120
SINGLE_PASS_LIMIT = 14_000


@dataclass
class BriefSection:
    title: str
    gist: str = ""
    blocks: List[int] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = {"title": self.title, "gist": self.gist, "blocks": self.blocks}
        if self.error:
            d["error"] = self.error
        return d


@dataclass
class Brief:
    title: str
    gist: str = ""
    sections: List[BriefSection] = field(default_factory=list)
    external_refs: List[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "title": self.title,
            "gist": self.gist,
            "sections": [s.to_dict() for s in self.sections],
        }
        if self.external_refs:
            d["external_refs"] = self.external_refs
        if self.error:
            d["error"] = self.error
        return d


def _blocks_text(blocks: List) -> str:
    out: List[str] = []
    for b in blocks:
        if b.heading:
            out.append(f"[小节：{b.heading}]")
        out.append(b.text)
        out.append("")
    return "\n".join(out)


def _meta(doc: Document) -> str:
    lines = []
    if doc.source.title:
        lines.append(f"标题：{doc.source.title}")
    if doc.source.author:
        lines.append(f"作者：{doc.source.author}")
    if doc.source.kind:
        lines.append(f"类型：{doc.source.kind}")
    return "\n".join(lines)


def _user_prompt(doc: Document, blocks: List, part: bool = False) -> str:
    scope = "这一部分" if part else "全部内容"
    return (f"## 范围\n\n{_meta(doc)}\n\n{scope}，共 {len(blocks)} 段，"
            f"{sum(len(b.text) for b in blocks):,} 字。\n\n"
            f"## 原文\n\n{_blocks_text(blocks)}")


def _synthesis_prompt(doc: Document, sections: List[BriefSection]) -> str:
    parts = [f"## 各部分的概述\n\n{_meta(doc)}\n"]
    for i, s in enumerate(sections, 1):
        parts.append(f"### {i}. {s.title}\n\n{s.gist}\n")
    return "\n".join(parts)


def _digest(doc: Document, per_block: int = 1400,
            max_total: int = 28_000) -> str:
    """按块采样：标题 + 开头/结尾各一半。用于「快速」一次调用出全局概述。"""
    parts: List[str] = []
    used = 0
    for b in doc.blocks:
        text = (b.text or "").strip()
        if len(text) > per_block:
            half = per_block // 2
            text = text[:half] + "\n……\n" + text[-half:]
        chunk = ((f"## {b.heading}\n\n" if b.heading else "") + text).strip()
        if used + len(chunk) > max_total:
            remain = max_total - used
            if remain > 300:
                parts.append(chunk[:remain])
            break
        parts.append(chunk)
        used += len(chunk)
    return "\n\n".join(parts)


def _digest_prompt(doc: Document, digest: str) -> str:
    return ("## 说明\n\n下面是按章节采样的节选，不是全文。只根据这些内容说大致讲什么，"
            "不要补细节，不要猜测没给的部分。\n\n"
            f"{_meta(doc)}\n\n## 采样文本\n\n{digest}")


def _clean(text: Any) -> str:
    s = str(text or "").strip()
    s = re.sub(r"<<<块\s*\d+>>>", "", s)
    s = re.sub(r"(?m)^#{1,6}\s*", "", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _refs(gist: str, source_text: str, title: str) -> List[str]:
    found = []
    for name in sorted(set(re.findall(r"《([^》]{1,30})》", gist))):
        if name in source_text or name in (title or ""):
            continue
        found.append(name)
    return found


def _section(data: Dict[str, Any], blocks: List) -> BriefSection:
    title = str(data.get("title") or blocks[0].heading or "正文").strip()
    if len(title) > 24:
        title = title[:22].rstrip("，、·—- ") + "…"
    gist = _clean(data.get("gist"))
    s = BriefSection(title=title, gist=gist,
                     blocks=[b.index for b in blocks])
    if not gist:
        s.error = "模型没有返回 gist"
    return s


def _split_text(text: str, max_chars: int) -> List[str]:
    """把超长单块按行切开；单行本身超长就硬切。

    逐字稿 ingest 后常常是「一整块 2–4 万字」，group_blocks 只按块边界
    分组，会让整块挤进一次 section 调用，概述的信息在第一步就被压没。
    """
    if len(text) <= max_chars:
        return [text]
    chunks: List[str] = []
    buf: List[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        if buf and size + len(line) > max_chars:
            chunks.append("".join(buf))
            buf, size = [], 0
        if len(line) > max_chars:
            if buf:
                chunks.append("".join(buf))
                buf, size = [], 0
            for i in range(0, len(line), max_chars):
                chunks.append(line[i:i + max_chars])
            continue
        buf.append(line)
        size += len(line)
    if buf:
        chunks.append("".join(buf))
    return chunks


def _brief_groups(doc: Document, max_chars: int,
                  min_chars: int = 3000) -> List[List[Block]]:
    """按章节分组，并且先切开超长单块。"""
    groups: List[List[Block]] = []
    cur: List[Block] = []
    cur_chars = 0
    cur_head: Optional[str] = None

    for b in doc.blocks:
        for chunk in _split_text(b.text or "", max_chars):
            head = b.heading or "正文"
            if cur and cur_chars + len(chunk) > max_chars:
                groups.append(cur)
                cur, cur_chars, cur_head = [], 0, None
            if (cur and cur_head is not None and head != cur_head
                    and cur_chars >= min_chars):
                groups.append(cur)
                cur, cur_chars, cur_head = [], 0, None
            cur.append(Block(index=b.index, text=chunk,
                             heading=b.heading, level=b.level))
            cur_chars += len(chunk)
            if cur_head is None:
                cur_head = head
            if cur_chars >= max_chars:
                groups.append(cur)
                cur, cur_chars, cur_head = [], 0, None

    if cur:
        groups.append(cur)
    return groups


def run_brief(doc: Document, runner: Runner, max_chars: int = SINGLE_PASS_LIMIT,
              workers: int = 2, progress=None,
              fast: bool = False) -> Tuple[Brief, Dict[str, Any]]:
    title = doc.source.title or (doc.blocks[0].heading if doc.blocks else "") or "这份内容"
    if (title.lower().endswith((".txt", ".md", ".pdf", ".epub", ".docx",
                                ".html", ".htm"))
            and doc.blocks and doc.blocks[0].heading):
        title = doc.blocks[0].heading
    total = len(doc.text)
    sections: List[BriefSection] = []
    failed = 0
    mode = "单次" if total <= max_chars else "分层"

    if fast and total > 2000:
        mode = "快速"
        raw = runner.run(load_template("brief_system.md"),
                         _digest_prompt(doc, _digest(doc)))
        gist = _clean(parse_json_loose(raw).get("gist"))
    elif total <= max_chars:
        mode = "单次"
        raw = runner.run(load_template("brief_system.md"),
                         _user_prompt(doc, doc.blocks))
        data = parse_json_loose(raw)
        gist = _clean(data.get("gist"))
        for item in data.get("sections") or []:
            if isinstance(item, dict) and item.get("gist"):
                sections.append(BriefSection(
                    title=str(item.get("title") or "正文").strip(),
                    gist=_clean(item.get("gist"))))
    else:
        groups = _brief_groups(doc, max_chars=max_chars)
        results: List[Optional[BriefSection]] = [None] * len(groups)
        lock = threading.Lock()
        done = [0]

        def work(item):
            i, blocks = item
            try:
                raw = runner.run(load_template("brief_section.md"),
                                 _user_prompt(doc, blocks, part=True))
                s = _section(parse_json_loose(raw), blocks)
            except Exception as e:
                s = BriefSection(
                    title=(blocks[0].heading or f"第 {i + 1} 部分"),
                    blocks=[b.index for b in blocks],
                    error=str(e)[:200])
            with lock:
                done[0] += 1
                if progress:
                    mark = "✘" if s.error else "✔"
                    progress(f"{mark} 第 {done[0]}/{len(groups)} 部分"
                             f"（块 {blocks[0].index}–{blocks[-1].index}）")
            return s

        if workers <= 1:
            for i, g in enumerate(groups):
                results[i] = work((i, g))
        else:
            with ThreadPoolExecutor(max_workers=min(workers, len(groups))) as ex:
                results = list(ex.map(work, list(enumerate(groups))))

        sections = [s for s in results if s is not None]
        failed = sum(1 for s in sections if s.error)
        good = [s for s in sections if not s.error]
        if good:
            raw = runner.run(load_template("brief_synthesis.md"),
                             _synthesis_prompt(doc, good))
            gist = _clean(parse_json_loose(raw).get("gist"))
        else:
            gist = ""

    brief = Brief(title=title, gist=gist, sections=sections)
    if not gist:
        brief.error = "模型没有返回概述"
    elif len(gist) < MIN_GIST_CHARS:
        brief.error = f"概述过短（{len(gist)} 字）"
    brief.external_refs = _refs(
        gist + "\n" + "\n".join(s.gist for s in sections), doc.text, title)

    report = {
        "模式": mode,
        "部分数": len(sections),
        "成功": sum(1 for s in sections if s.gist and not s.error),
        "失败": failed,
        "总字数": len(gist) + sum(len(s.gist) for s in sections),
        "外部引用": len(brief.external_refs),
        "通过": not brief.error and failed == 0,
    }
    return brief, report
