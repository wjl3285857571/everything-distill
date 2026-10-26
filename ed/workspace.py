"""
工作区 —— 中间产物的落盘位置。

结构：
    <workdir>/
    ├── source.json          输入元信息
    ├── blocks.json          切好的块（含全文，供校验用）
    ├── stage1.json          第一段产出
    ├── stage1_report.json   第一段校验报告
    ├── stage2.json          第二段产出
    └── 文档.md               最终交付（render 产出）
"""

import json
import os
from typing import Any, Dict, List, Optional

from .ingest.base import Document
from .schema import Basis, Insight, Source, Stage1, Stage2


def ensure(workdir: str) -> str:
    os.makedirs(workdir, exist_ok=True)
    return workdir


def _atomic_write_text(path: str, text: str) -> None:
    """先写临时文件再原子替换：中途被打断不会留下半个 JSON。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _dump(path: str, obj) -> None:
    _atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2))


def save_document(workdir: str, doc: Document) -> None:
    ensure(workdir)
    _dump(os.path.join(workdir, "source.json"), doc.source.to_dict())
    _dump(
        os.path.join(workdir, "blocks.json"),
        {
            "stats": doc.stats,
            "text": doc.text,
            "blocks": [
                {**b.to_dict(), "text": b.text} for b in doc.blocks
            ],
        },
    )


def load_document(workdir: str) -> Document:
    with open(os.path.join(workdir, "source.json"), encoding="utf-8") as f:
        src = Source(**json.load(f))
    with open(os.path.join(workdir, "blocks.json"), encoding="utf-8") as f:
        raw = json.load(f)
    from .ingest.base import Block
    blocks = [Block(**b) for b in raw["blocks"]]
    return Document(text=raw["text"], blocks=blocks, source=src,
                    stats=raw.get("stats", {}))


def save_stage1(workdir: str, stage1: Stage1, report: Dict[str, Any]) -> None:
    ensure(workdir)
    _dump(os.path.join(workdir, "stage1.json"), stage1.to_dict())
    _dump(os.path.join(workdir, "stage1_report.json"), report)


def load_stage1(workdir: str) -> Stage1:
    with open(os.path.join(workdir, "stage1.json"), encoding="utf-8") as f:
        d = json.load(f)
    insights = [_insight_from_dict(x) for x in d.get("insights", [])]
    counters = [_insight_from_dict(x) for x in d.get("counter_examples", [])]
    return Stage1(
        source=Source(**d["source"]),
        overview=d.get("overview", ""),
        structure=d.get("structure", []),
        insights=insights,
        glossary=d.get("glossary", []),
        counter_examples=counters,
        open_questions=d.get("open_questions", []),
        stats=d.get("stats", {}),
    )


def _insight_from_dict(x: Dict) -> Insight:
    from .schema import Locator
    loc = x.get("locator") or {}
    return Insight(
        id=x.get("id", ""),
        claim=x.get("claim", ""),
        support=x.get("support", ""),
        locator=Locator(
            block=loc.get("block"),
            chapter=loc.get("chapter"),
            position=loc.get("position"),
        ),
        boundary=x.get("boundary", ""),
        type=x.get("type", "other"),
        detail=x.get("detail", ""),
        relevance=x.get("relevance"),
    )


def save_stage2(workdir: str, stage2: Stage2) -> None:
    ensure(workdir)
    _dump(os.path.join(workdir, "stage2.json"), stage2.to_dict())


def save_narration(workdir: str, notes, report: Dict[str, Any]) -> None:
    ensure(workdir)
    _dump(os.path.join(workdir, "narrate.json"),
          {"chapters": [n.to_dict() for n in notes], "report": report})


def load_narration(workdir: str) -> Optional[List[Dict[str, Any]]]:
    p = os.path.join(workdir, "narrate.json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        d = json.load(f)
    chapters = d.get("chapters") or []
    return chapters or None


def save_brief(workdir: str, brief, report: Dict[str, Any]) -> None:
    ensure(workdir)
    _dump(os.path.join(workdir, "brief.json"),
          {"brief": brief.to_dict(), "report": report})


def load_brief(workdir: str) -> Optional[Dict[str, Any]]:
    p = os.path.join(workdir, "brief.json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def load_stage2(workdir: str) -> Stage2:
    with open(os.path.join(workdir, "stage2.json"), encoding="utf-8") as f:
        d = json.load(f)
    basis = [Basis(**b) for b in d.get("basis", [])]
    insights = [_insight_from_dict(x) for x in d.get("insights", [])]
    return Stage2(
        overview=d.get("overview", ""),
        structure=d.get("structure", []),
        entry_angles=d.get("entry_angles", []),
        reading_order=d.get("reading_order", []),
        relevant=d.get("relevant", []),
        known_already=d.get("known_already", []),
        next_actions=d.get("next_actions", []),
        basis=basis,
        insights=insights,
    )
