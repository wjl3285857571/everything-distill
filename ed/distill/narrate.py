"""逐章讲述：把内容讲成连贯长文，而不是洞察卡片。

和 stage1 的分工：
    stage1 负责「抽」——把可校验的观点、原文摘录抽出来；
    narrate 负责「讲」——按章节把内容讲成一篇能读的文章。

为什么单独一层：
    「洞察列表」适合做 AI 的知识库，不适合人读。读者要的是这本书
    在讲什么、怎么展开、人物或论证怎么走，而不是 200 张卡片。
"""

import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..ingest.base import Document
from ..runner import Runner, parse_json_loose
from .stage1 import anchor_hit

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "templates")


def load_template(name: str) -> str:
    with open(os.path.join(TEMPLATE_DIR, name), encoding="utf-8") as f:
        return f.read()


@dataclass
class ChapterNote:
    title: str
    summary: str = ""
    prose: str = ""
    quotes: List[Dict[str, Any]] = field(default_factory=list)
    blocks: List[int] = field(default_factory=list)
    dropped_quotes: int = 0
    external_refs: List[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "title": self.title,
            "summary": self.summary,
            "prose": self.prose,
            "quotes": self.quotes,
            "blocks": self.blocks,
            "dropped_quotes": self.dropped_quotes,
        }
        if self.external_refs:
            d["external_refs"] = self.external_refs
        if self.error:
            d["error"] = self.error
        return d


def group_blocks(doc: Document, min_chars: int = 6000,
                 max_chars: int = 18000) -> List[List]:
    """按章节分组，顺便限制单次输入大小。

    同一章的内容尽量放进一组；章太短就合并，太长就切开
    （切开时仍按块边界，不切段落）。
    """
    groups: List[List] = []
    cur: List = []
    cur_chars = 0
    cur_head: Optional[str] = None

    for b in doc.blocks:
        head = b.heading or "正文"
        if (cur and cur_head is not None and head != cur_head
                and cur_chars >= min_chars):
            groups.append(cur)
            cur, cur_chars, cur_head = [], 0, None
        cur.append(b)
        cur_chars += len(b.text)
        if cur_head is None:
            cur_head = head
        if cur_chars >= max_chars:
            groups.append(cur)
            cur, cur_chars, cur_head = [], 0, None

    if cur:
        groups.append(cur)
    return groups


def _render_blocks(blocks: List) -> str:
    out = []
    for b in blocks:
        out.append(f"<<<块 {b.index}>>>")
        if b.heading:
            out.append(f"[当前小节：{b.heading}]")
        out.append(b.text)
        out.append("")
    return "\n".join(out)


def _user_prompt(doc: Document, blocks: List) -> str:
    span = f"块 {blocks[0].index}–{blocks[-1].index}"
    meta = ""
    if doc.source.title:
        meta += f"书名/标题：{doc.source.title}\n"
    if doc.source.author:
        meta += f"作者：{doc.source.author}\n"
    return (f"## 这一部分的范围\n\n{meta}{span}，共 {len(blocks)} 块。\n\n"
            f"## 原文\n\n{_render_blocks(blocks)}")


def _int_or_none(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _make_note(data: Dict[str, Any], blocks: List,
               source_title: str = "") -> ChapterNote:
    group_text = "\n".join(b.text for b in blocks)
    title = str(data.get("title") or blocks[0].heading or "正文").strip()
    if len(title) > 24:
        title = title[:22].rstrip("，、·—- ") + "…"
    prose = str(data.get("prose") or "").strip()

    quotes: List[Dict[str, Any]] = []
    dropped = 0
    for q in data.get("quotes") or []:
        if not isinstance(q, dict):
            continue
        text = str(q.get("text") or "").strip()
        if not text:
            continue
        if not anchor_hit(text, group_text):
            dropped += 1
            continue
        quotes.append({
            "text": text,
            "block": _int_or_none(q.get("block")),
            "why": str(q.get("why") or "").strip(),
        })

    note = ChapterNote(
        title=title,
        summary=str(data.get("summary") or "").strip(),
        prose=prose,
        quotes=quotes,
        blocks=[b.index for b in blocks],
        dropped_quotes=dropped,
    )
    # 正文里提到的书名，原文没出现、也不是本书 → 多半是模型拿外部知识套的
    for name in sorted(set(re.findall(r"《([^》]{1,30})》", prose))):
        if name in group_text or name in source_title:
            continue
        note.external_refs.append(name)
    if not prose:
        note.error = "模型没有返回 prose"
    return note


def run_narrate(doc: Document, runner: Runner, workers: int = 4,
                progress=None,
                max_chars: int = 18000) -> Tuple[List[ChapterNote], Dict[str, Any]]:
    system = load_template("narrate_system.md")
    groups = group_blocks(doc, max_chars=max_chars)
    results: List[Optional[ChapterNote]] = [None] * len(groups)
    lock = threading.Lock()
    done = [0]

    def work(item):
        i, blocks = item
        try:
            raw = runner.run(system, _user_prompt(doc, blocks))
            note = _make_note(parse_json_loose(raw), blocks,
                              source_title=doc.source.title or "")
        except Exception as e:
            note = ChapterNote(
                title=(blocks[0].heading or f"第 {i + 1} 部分"),
                blocks=[b.index for b in blocks],
                error=str(e)[:200],
            )
        with lock:
            done[0] += 1
            if progress:
                mark = "✘" if note.error else "✔"
                progress(f"{mark} 第 {done[0]}/{len(groups)} 部分"
                         f"（块 {blocks[0].index}–{blocks[-1].index}）")
        return note

    if workers <= 1:
        for i, g in enumerate(groups):
            results[i] = work((i, g))
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(groups))) as ex:
            results = list(ex.map(work, list(enumerate(groups))))

    notes = [n for n in results if n is not None]
    failed = [n for n in notes if n.error]
    produced = sum(1 for n in notes if n.prose)
    report = {
        "部分数": len(notes),
        "成功": produced,
        "失败": len(failed),
        "失败明细": [{"title": n.title, "error": n.error} for n in failed][:10],
        "作废引用": sum(n.dropped_quotes for n in notes),
        "外部引用": sum(len(n.external_refs) for n in notes),
        "总字数": sum(len(n.prose) for n in notes),
        "通过": bool(notes) and not failed and produced == len(notes),
    }
    return notes, report
