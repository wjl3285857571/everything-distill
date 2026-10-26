"""
第二段：结合读者处境的匹配与重组。

输入 = 第一段产出 + 读者处境
输出 = Stage2（含依据清单）

关键约束：**每条跟读者有关的结论必须挂依据**。
依据来自记忆文件或会话的**原句**，不能是模型的转述。
"""

import json
import os
from typing import Any, Dict, List, Optional

from ..context.profile import Context
from ..runner import Runner
from ..schema import Basis, Insight, Stage1, Stage2

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "templates")


def load_template(name: str) -> str:
    with open(os.path.join(TEMPLATE_DIR, name), encoding="utf-8") as f:
        return f.read()


def _parse(raw: str) -> Dict:
    from ..runner import parse_json_loose
    return parse_json_loose(raw)


def render_stage1(stage1: Stage1, max_insights: int = 200) -> str:
    """把第一段产出渲染成给第二段看的文本。

    只给**内容**，不给处境——处境是另一块。
    """
    out: Dict[str, Any] = {
        "内容标题": stage1.source.title,
        "内容类型": stage1.source.kind,
        "概览": stage1.overview,
        "整体结构": stage1.structure,
        "洞察": [],
        "失效场景与反例": [],
        "专有名词": stage1.glossary,
        "内容引出的问题": stage1.open_questions,
    }
    for ins in stage1.insights[:max_insights]:
        out["洞察"].append({
            "id": ins.id,
            "说法": ins.claim,
            "类型": ins.type,
            "细节": ins.detail,
            "原文摘录": ins.support,
            "位置": ins.locator.to_dict(),
            "边界": ins.boundary,
        })
    for c in stage1.counter_examples[:80]:
        out["失效场景与反例"].append({
            "id": c.id,
            "说法": c.claim,
            "原文摘录": c.support,
            "位置": c.locator.to_dict(),
        })
    return json.dumps(out, ensure_ascii=False, indent=2)


def run_stage2(stage1: Stage1, ctx: Context, runner: Runner) -> Stage2:
    system = load_template("stage2_system.md")
    user = (
        "## 已蒸好的内容\n\n" + render_stage1(stage1) +
        "\n\n---\n\n## 关于这个读者\n\n" + ctx.render()
    )
    raw = runner.run(system, user)
    data = _parse(raw)

    basis = []
    for b in data.get("basis", []) or []:
        try:
            basis.append(Basis(
                id=int(b.get("id", 0)),
                kind=str(b.get("kind", "content")),
                source=str(b.get("source", "")),
                quote=str(b.get("quote", "")).strip(),
                note=str(b.get("note", "")).strip(),
            ))
        except (TypeError, ValueError):
            continue

    insights = []
    for raw_i in data.get("insights", []) or []:
        insights.append(Insight(
            id=str(raw_i.get("id", "")),
            claim=str(raw_i.get("claim", "")).strip(),
            support=str(raw_i.get("support", "")).strip(),
            boundary=str(raw_i.get("boundary", "")).strip(),
            type=str(raw_i.get("type", "other")).strip() or "other",
            detail=str(raw_i.get("detail", "")).strip(),
            relevance=raw_i.get("relevance"),
        ))

    return Stage2(
        overview=str(data.get("overview", "")).strip(),
        structure=list(data.get("structure", []) or []),
        entry_angles=[str(x).strip() for x in data.get("entry_angles", []) if str(x).strip()],
        reading_order=[str(x).strip() for x in data.get("reading_order", []) if str(x).strip()],
        relevant=list(data.get("relevant", []) or []),
        known_already=[str(x).strip() for x in data.get("known_already", []) if str(x).strip()],
        next_actions=[str(x).strip() for x in data.get("next_actions", []) if str(x).strip()],
        basis=basis,
        insights=insights,
    )


def _basis_haystack(kind: str, ctx, doc) -> str:
    """依据该去哪个来源里核对。"""
    if kind == "memory":
        return "\n".join(ctx.memories.values()) if ctx else ""
    if kind == "mention":
        return "\n".join(str(s.get("quote", ""))
                         for s in ctx.sessions) if ctx else ""
    return doc.text if doc else ""


def quote_in_source(quote: str, source: str) -> bool:
    """引文是否真的出现在来源里（归一化后整段包含）。

    只做整段包含，不做「任意 12 字窗口」——那会让编造的引文靠一个
    常见短语混过去。模型偶尔在句尾补省略号，去掉后再比一次。
    """
    from .stage1 import normalize
    q = normalize(quote)
    s = normalize(source)
    if not q or not s:
        return False
    if q in s:
        return True
    for tail in ("...", "…"):
        if q.endswith(tail):
            q2 = q[:-len(tail)]
            if q2 and q2 in s:
                return True
    return False


def verify_stage2(stage2: Stage2, ctx=None, stage1: Optional[Stage1] = None,
                  doc=None) -> Dict[str, Any]:
    """校验第二段：**每条跟读者有关的结论必须有依据**。

    这是防拍马屁的机制。没有依据的关联，要么补依据，要么删掉。

    传入 ctx / stage1 / doc 时，额外核对三件事：
      - 引文是否真的出现在对应来源里（不是模型自己写的漂亮话）
      - insight_id 是否真的对应第一段的条目
      - 依据编号是否重复
    """
    basis_ids = {b.id for b in stage2.basis}
    dangling: List[Dict[str, Any]] = []
    no_basis: List[Dict[str, Any]] = []

    for r in stage2.relevant:
        ids = r.get("basis") or []
        if not ids:
            no_basis.append({"insight_id": r.get("insight_id", ""),
                             "why": str(r.get("why", ""))[:60]})
            continue
        for i in ids:
            try:
                n = int(i)
            except (TypeError, ValueError):
                dangling.append({"insight_id": r.get("insight_id", ""),
                                 "basis": i, "原因": "编号不是整数"})
                continue
            if n not in basis_ids:
                dangling.append({"insight_id": r.get("insight_id", ""),
                                 "basis": i})

    # 依据本身的类型检查
    bad_kind = [b.id for b in stage2.basis
                if b.kind not in ("memory", "mention", "content")]
    empty_quote = [b.id for b in stage2.basis if not b.quote.strip()]

    # 编号重复会让正文引用指向不确定的那一条
    seen_ids = set()
    duplicate_basis = []
    for b in stage2.basis:
        if b.id in seen_ids:
            duplicate_basis.append(b.id)
        seen_ids.add(b.id)

    # 洞察 ID 必须真的存在
    unknown_insight: List[Dict[str, Any]] = []
    if stage1 is not None:
        known = {i.id for i in stage1.insights}
        known |= {c.id for c in stage1.counter_examples}
        for r in stage2.relevant:
            iid = r.get("insight_id") or r.get("id") or ""
            if iid and iid not in known:
                unknown_insight.append({
                    "insight_id": iid,
                    "why": str(r.get("why", ""))[:40],
                })

    # 引文必须能在来源里找到
    bad_quote: List[Dict[str, Any]] = []
    if ctx is not None or doc is not None:
        for b in stage2.basis:
            hay = _basis_haystack(b.kind, ctx, doc)
            if not hay:
                bad_quote.append({"id": b.id, "kind": b.kind,
                                  "原因": "没有可核对的来源"})
                continue
            if not quote_in_source(b.quote, hay):
                bad_quote.append({"id": b.id, "kind": b.kind,
                                  "原因": "引文在来源里找不到"})

    return {
        "关联条数": len(stage2.relevant),
        "无依据的关联": len(no_basis),
        "无依据明细": no_basis[:8],
        "悬空引用": len(dangling),
        "悬空明细": dangling[:8],
        "依据条数": len(stage2.basis),
        "依据类型异常": bad_kind,
        "依据缺引文": empty_quote,
        "依据重复编号": duplicate_basis,
        "引文无法核对": bad_quote,
        "洞察不存在": unknown_insight,
        "通过": (not no_basis and not dangling and not bad_kind
                 and not empty_quote and not duplicate_basis
                 and not bad_quote and not unknown_insight),
    }
