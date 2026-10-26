"""
第二段：结合读者处境的匹配与重组。

输入 = 第一段产出 + 读者处境
输出 = Stage2（含依据清单）

关键约束：**每条跟读者有关的结论必须挂依据**。
依据来自记忆文件或会话的**原句**，不能是模型的转述。
"""

import json
import os
import re
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


def _shingles(s: str, n: int = 8) -> set:
    if len(s) <= n:
        return {s} if s else set()
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def quote_in_source(quote: str, source: str, min_coverage: float = 0.8) -> bool:
    """引文是否真的来自来源。

    规则：
      - 归一化后整段出现 → 通过（最理想）
      - 模型常做的两件事要容忍：去掉 markdown 加粗、截断括号里的补充。
        所以退一步看「8 字片段覆盖率」：至少 80% 的片段在来源里出现。
      - 覆盖率不够就当编的。
    """
    from .stage1 import normalize
    q = normalize(quote)
    s = normalize(source)
    if not q or not s:
        return False
    if q in s:
        return True
    if len(q) < 16:
        return False
    grams = _shingles(q)
    if not grams:
        return False
    hit = sum(1 for g in grams if g in s)
    return (hit / len(grams)) >= min_coverage


def _coverage(grams: set, text: str) -> float:
    if not grams:
        return 0.0
    return sum(1 for g in grams if g in text) / len(grams)


def _sentences(text: str) -> List[str]:
    return [p.strip() for p in re.split(r"[。！？\n]+", text) if len(p.strip()) >= 8]


def snap_basis_to_sources(stage2: Stage2, ctx=None, doc=None) -> List[Basis]:
    """把模型写的依据引文替换成来源里的真实句子。

    模型常把记忆压缩成一句话（去掉加粗、括号、改标点），直接当引文
    是不诚实的。这里按 8 字片段覆盖率找最接近的原句，替换掉；
    找不到足够接近的就判定这条依据无效，并连带删掉引用它的关联。
    """
    from .stage1 import normalize

    pools: Dict[str, List[tuple]] = {}
    wholes: Dict[str, str] = {}
    if ctx is not None:
        pools["memory"] = [(name, s)
                           for name, text in ctx.memories.items()
                           for s in _sentences(text)]
        wholes["memory"] = "\n".join(ctx.memories.values())
        pools["mention"] = [("会话", str(s.get("quote", "")))
                            for s in ctx.sessions
                            if str(s.get("quote", "")).strip()]
        wholes["mention"] = "\n".join(str(s.get("quote", ""))
                                      for s in ctx.sessions)
    if doc is not None:
        pools["content"] = [("原文", s) for s in _sentences(doc.text)]
        wholes["content"] = doc.text

    bad: List[Basis] = []
    for b in stage2.basis:
        pool = pools.get(b.kind) or []
        if not pool:
            bad.append(b)
            continue
        grams = _shingles(normalize(b.quote))
        if not grams:
            bad.append(b)
            continue

        # 先看整篇：跨句引用（两句拼在一起）在单句里找不到，
        # 但它确实来自原文，不该误杀。
        if _coverage(grams, normalize(wholes.get(b.kind, ""))) >= 0.8:
            if b.kind == "memory" and ctx is not None:
                # 模型可能把来源标错（比如标成没传进去的 MEMORY.md），
                # 这里按实际命中的文件纠正。
                best_file, best_file_cov = "", 0.0
                for name, text in ctx.memories.items():
                    cov = _coverage(grams, normalize(text))
                    if cov > best_file_cov:
                        best_file, best_file_cov = name, cov
                if best_file_cov >= 0.8:
                    b.source = best_file
            continue

        best_src, best_text, best_cov = "", "", 0.0
        for src, text in pool:
            cov = _coverage(grams, normalize(text))
            if cov > best_cov:
                best_src, best_text, best_cov = src, text, cov
        if best_cov >= 0.5:
            b.quote = best_text
            if b.kind == "memory":
                b.source = best_src
        else:
            bad.append(b)

    if bad:
        bad_ids = {b.id for b in bad}
        stage2.basis = [b for b in stage2.basis if b.id not in bad_ids]
        stage2.relevant = [
            r for r in stage2.relevant
            if not (set(r.get("basis") or []) & bad_ids)
        ]
    return bad


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
