"""
编排者模式 —— 让「编排者」（Hermes skill / 任何能派子 Agent 的宿主）
来驱动模型调用，而 `ed` 只负责确定性的部分：发 prompt、收结果、校验、渲染。

为什么需要这个模式：
    `hermes -z` 每次调用都完整启动一个 agent 进程。跑一本书要调十几次，
    进程启动开销比模型调用还大。宿主里的 delegate_task 复用同一个进程、
    原生并行，快一个量级。

工作流：
    ed plan                 --workdir W          → 列出批次
    ed prompt               --workdir W --batch 3 → 该批的完整 prompt
    （编排者派子 Agent，把返回写成 W/_batches/batch_03.out）
    ed collect              --workdir W          → 合并 + 校验 + 落盘 stage1.json

    ed stage2-prompt        --workdir W          → 第二段的完整 prompt
    （编排者派子 Agent，返回写成 W/_stage2.out）
    ed collect2             --workdir W          → 落盘 stage2.json

    ed render               --workdir W --out D.md
"""

import glob
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from .distill.stage1 import (BATCH_SIZE, build_system_prompt,
                             check_batch_receipts, load_template,
                             make_batches, prune_unlocatable,
                             render_blocks, _parse)
from .ingest.base import Document
from .schema import Basis, Insight, Locator, Stage1, Stage2

BATCH_DIR = "_batches"


def batch_dir(workdir: str) -> str:
    d = os.path.join(workdir, BATCH_DIR)
    os.makedirs(d, exist_ok=True)
    return d


def plan(workdir: str, batch_size: int = BATCH_SIZE) -> List[Dict[str, Any]]:
    """列出批次，并为每批生成 prompt 文件。"""
    doc = _load_doc(workdir)
    batches = make_batches(doc, batch_size)
    system = build_system_prompt(doc.source.kind)
    d = batch_dir(workdir)
    out = []
    for i, blocks in enumerate(batches, 1):
        bid = f"batch_{i:03d}"
        user = render_blocks(blocks)
        with open(os.path.join(d, f"{bid}.txt"), "w", encoding="utf-8") as f:
            f.write(f"=== SYSTEM ===\n{system}\n\n=== USER ===\n{user}")
        with open(os.path.join(d, f"{bid}.meta.json"), "w", encoding="utf-8") as f:
            json.dump({
                "batch": i,
                "blocks": [b.index for b in blocks],
                "prompt": os.path.join(d, f"{bid}.txt"),
                "output": os.path.join(d, f"{bid}.out"),
            }, f, ensure_ascii=False, indent=2)
        out.append({
            "batch": i,
            "blocks": [b.index for b in blocks],
            "prompt": os.path.join(d, f"{bid}.txt"),
            "output": os.path.join(d, f"{bid}.out"),
        })

    # 记下这次 plan 的批次划分。collect 以它为准，避免用户改了
    # --batch-size 之后，plan 和 collect 对不上、把结果收错批。
    manifest = {
        "batch_size": batch_size,
        "block_count": len(doc.blocks),
        "batches": [{"batch": i, "blocks": [b.index for b in blocks]}
                    for i, blocks in enumerate(batches, 1)],
    }
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return out


def _expected_batches(doc: Document, batch_size: int, d: str) -> List[List]:
    """优先用 plan 写的 manifest；没有就按 batch_size 现算。"""
    mp = os.path.join(d, "manifest.json")
    if os.path.exists(mp):
        try:
            with open(mp, encoding="utf-8") as f:
                m = json.load(f)
            by_index = {b.index: b for b in doc.blocks}
            out = []
            for item in m.get("batches", []):
                blocks = [by_index[i] for i in item.get("blocks", [])
                          if i in by_index]
                if blocks:
                    out.append(blocks)
            if out:
                return out
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return make_batches(doc, batch_size)


def _extra_outputs(d: str, expected: int) -> List[str]:
    """批次文件比预期多：多半是上一次跑更大规模留下的，提醒一下。"""
    files = sorted(glob.glob(os.path.join(d, "batch_*.out")))
    if len(files) <= expected:
        return []
    return [os.path.basename(f) for f in files[expected:]]


def _load_doc(workdir: str) -> Document:
    from . import workspace
    return workspace.load_document(workdir)


def collect(workdir: str, batch_size: int = BATCH_SIZE,
            max_insights: int = 200) -> Dict[str, Any]:
    """收集各批的模型返回，合并、筛选、校验、落盘 stage1.json。"""
    from . import workspace
    doc = _load_doc(workdir)
    d = batch_dir(workdir)
    batches = _expected_batches(doc, batch_size, d)

    insights: List[Insight] = []
    counters: List[Insight] = []
    glossary: List[Dict[str, str]] = []
    open_questions: List[str] = []
    receipts_log: List[Dict[str, Any]] = []
    next_id = 1

    for i, blocks in enumerate(batches, 1):
        bid = f"batch_{i:03d}"
        outp = os.path.join(d, f"{bid}.out")
        if not os.path.exists(outp):
            receipts_log.append({
                "批次": i, "状态": "缺输出", "块": [b.index for b in blocks],
                "缺回执": [b.index for b in blocks],
            })
            continue
        with open(outp, encoding="utf-8") as f:
            raw = f.read()
        try:
            data = _parse(raw)
        except Exception as e:
            receipts_log.append({
                "批次": i, "状态": "解析失败", "原因": str(e)[:200],
                "块": [b.index for b in blocks],
            })
            continue

        batch_insights: List[Insight] = []
        batch_counters: List[Insight] = []
        for raw_i in data.get("insights", []) or []:
            ins = _insight(raw_i, next_id, force_type="")
            next_id += 1
            batch_insights.append(ins)
            insights.append(ins)
        for raw_c in data.get("counter_examples", []) or []:
            c = _insight(raw_c, next_id, force_type="counter")
            next_id += 1
            counters.append(c)
            batch_counters.append(c)
        for g in data.get("glossary", []) or []:
            if g.get("term") and g.get("definition"):
                glossary.append({
                    "term": str(g["term"]).strip(),
                    "definition": str(g["definition"]).strip(),
                    "support": str(g.get("support", "")).strip(),
                })
        for q in data.get("open_questions", []) or []:
            if isinstance(q, str) and q.strip():
                open_questions.append(q.strip())

        missing, mismatches = check_batch_receipts(
            blocks, raw, batch_insights, batch_counters)
        if missing:
            status = "缺回执"
        elif mismatches:
            status = "数量不符"
        else:
            status = "完整"
        receipts_log.append({
            "批次": i,
            "状态": status,
            "块": [b.index for b in blocks],
            "缺回执": missing,
            "数量不符": mismatches,
            "条目数": len(batch_insights) + len(batch_counters),
        })

    stage1 = Stage1(
        source=doc.source,
        overview="",
        insights=insights,
        glossary=glossary,
        counter_examples=counters,
        open_questions=open_questions,
        stats={
            "有效字数": doc.stats.get("有效字数"),
            "块数": len(doc.blocks),
            "洞察数": len(insights),
            "反例数": len(counters),
            "术语数": len(glossary),
        },
    )

    # 概览和结构由模型给 —— 从任一批的返回里取（模型通常在第一批给概览）
    overview, structure = _extract_overview(d)
    stage1.overview = overview
    stage1.structure = structure

    total_before = len(stage1.insights) + len(stage1.counter_examples)
    dropped = prune_unlocatable(stage1, doc)

    # 筛选：一本书可能出 500+ 条，全塞给第二段会爆上下文
    from .distill.select import select
    sel = select(stage1, max_total=max_insights)
    stage1.insights = sel["insights"]

    from .distill.stage1 import verify_stage1
    unfinished = [r for r in receipts_log if r.get("状态") != "完整"]
    partial = {
        "回执": receipts_log,
        "缺失回执总数": sum(len(r.get("缺回执", [])) for r in receipts_log),
        "数量不符总数": sum(len(r.get("数量不符", [])) for r in receipts_log),
        "失败批次数": sum(1 for r in receipts_log if r.get("状态") == "失败"),
    }
    v = verify_stage1(stage1, doc, partial)
    produced = len(stage1.insights) + len(stage1.counter_examples)
    drop_ratio = (len(dropped) / total_before) if total_before else 0.0
    report = {
        "批数": len(batches),
        "回执": receipts_log,
        **{k: val for k, val in partial.items() if k != "回执"},
        "未完成批次数": len(unfinished),
        "产出洞察数": produced,
        "作废条数": len(dropped),
        "作废明细": [{"id": d.id, "标题": d.claim[:40],
                      "原因": "摘录在原文定位不到"} for d in dropped[:10]],
        "作废比例": round(drop_ratio, 3),
        "多余输出": _extra_outputs(d, len(batches)),
        "筛选": sel["报告"],
        "verify": v,
        "通过": (v["通过"] and not unfinished
                 and (produced > 0 or not doc.blocks)
                 and drop_ratio <= 0.3),
    }
    workspace.save_stage1(workdir, stage1, report)
    return report


def _extract_overview(d: str) -> Tuple[str, List[Dict[str, str]]]:
    """从批次返回里找概览和结构（如果有的话）。"""
    overview = ""
    structure: List[Dict[str, str]] = []
    for f in sorted(glob.glob(os.path.join(d, "batch_*.out"))):
        try:
            with open(f, encoding="utf-8") as fh:
                data = _parse(fh.read())
        except Exception:
            continue
        if not overview and data.get("overview"):
            overview = str(data["overview"]).strip()
        if not structure and data.get("structure"):
            structure = list(data["structure"])
    return overview, structure


def _insight(raw: Dict, new_id: int, force_type: str = "") -> Insight:
    loc_raw = raw.get("locator") or {}
    locator = Locator(
        block=_int_or_none(loc_raw.get("block")),
        chapter=(str(loc_raw["chapter"]).strip() if loc_raw.get("chapter") else None),
        position=(str(loc_raw["position"]).strip() if loc_raw.get("position") else None),
    )
    t = (raw.get("type") or "").strip() or force_type or "other"
    return Insight(
        id=f"I{new_id}",
        claim=str(raw.get("claim", "")).strip(),
        support=str(raw.get("support", "")).strip(),
        locator=locator,
        boundary=str(raw.get("boundary", "")).strip(),
        type=t,
        detail=str(raw.get("detail", "")).strip(),
    )


def _int_or_none(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 第二段


def stage2_prompt(workdir: str) -> str:
    """生成第二段的完整 prompt。"""
    from . import workspace
    from .distill.stage2 import render_stage1
    stage1 = workspace.load_stage1(workdir)
    system = load_template("stage2_system.md")
    ctx = _default_context()
    user = ("## 已蒸好的内容\n\n" + render_stage1(stage1) +
            "\n\n---\n\n## 关于这个读者\n\n" + ctx.render())
    return f"=== SYSTEM ===\n{system}\n\n=== USER ===\n{user}"


def _default_context() -> Any:
    from .context.profile import Context
    return Context.from_dir(os.path.expanduser("~/.hermes/memories"))


def _locator_from(raw: Any) -> Locator:
    """第二段的「位置」可能是 {block, chapter} 也可能是缺省。"""
    if not isinstance(raw, dict):
        return Locator()
    try:
        blk = int(raw.get("block")) if raw.get("block") is not None else None
    except (TypeError, ValueError):
        blk = None
    return Locator(block=blk, chapter=str(raw.get("chapter") or ""))


def collect2(workdir: str, out_path: Optional[str] = None) -> Dict[str, Any]:
    """收第二段的结果，落盘 stage2.json。"""
    from . import workspace
    from .distill.stage2 import verify_stage2
    p = out_path or os.path.join(workdir, "_stage2.out")
    if not os.path.exists(p):
        return {"错误": f"找不到 {p}"}
    with open(p, encoding="utf-8") as f:
        data = _parse(f.read())

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
            # 第二段模型偶尔把字段名写成中文（说法/原文摘录/边界/细节/位置），
            # 两种都认 —— 否则整批洞察解析成空串，渲染出一片空标题
            claim=str(raw_i.get("claim") or raw_i.get("说法") or "").strip(),
            support=str(raw_i.get("support") or raw_i.get("原文摘录") or "").strip(),
            boundary=str(raw_i.get("boundary") or raw_i.get("边界") or "").strip(),
            type=str(raw_i.get("type") or raw_i.get("类型") or "other").strip() or "other",
            detail=str(raw_i.get("detail") or raw_i.get("细节") or "").strip(),
            relevance=raw_i.get("relevance"),
            locator=_locator_from(raw_i.get("locator") or raw_i.get("位置")),
        ))

    stage2 = Stage2(
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
    workspace.save_stage2(workdir, stage2)
    # 引文要回来源核对：记忆文件 / 会话片段 / 原文。
    # 只查「编号存在 + 引文非空」是拦不住模型自己编一句漂亮话的。
    try:
        stage1 = workspace.load_stage1(workdir)
    except Exception:
        stage1 = None
    try:
        doc = workspace.load_document(workdir)
    except Exception:
        doc = None
    from .distill.stage2 import snap_basis_to_sources
    ctx = _default_context()
    snapped = snap_basis_to_sources(stage2, ctx=ctx, doc=doc)
    v = verify_stage2(stage2, ctx=ctx, stage1=stage1, doc=doc)
    return {"关联": len(stage2.relevant), "依据": len(stage2.basis),
            "作废依据": len(snapped), "校验": v}
