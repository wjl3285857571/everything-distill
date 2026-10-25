"""
第一段：内容蒸馏（无处境）。

这一段**不带任何关于用户的信息**。
目的是让内容部分不被用户的处境带偏——否则会只看到跟他有关的部分，
漏掉他可能不知道但有价值的领域。

产出：Stage1 对象 + 每批的回执记录。

校验在这一层就做掉两件事：
  1. 覆盖率 —— 批内每块都要有回执行
  2. 可定位性 —— 每条 support 必须能在对应块的原文里 substring 命中
     （模型编造的、或转述而非原文的，都会被这里抓出来）
"""

import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from ..ingest.base import Document, effective_words
from ..runner import Runner
from ..schema import Insight, Locator, Source, Stage1

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "templates")

BATCH_SIZE = 5


# ---------------------------------------------------------------- 归一化


def normalize(s: str) -> str:
    """归一化：让摘录能容忍标点/空白差异。

    中文引号必须处理——模型引用时常自己加「」《》，原文里没有，
    不处理会导致锚点整体错位一位。
    """
    s = re.sub(r"\s+", "", s)
    s = s.replace("“", '"').replace("”", '"')
    s = s.replace("‘", "'").replace("’", "'")
    s = s.replace("，", ",").replace("。", ".").replace("；", ";")
    s = s.replace("：", ":").replace("、", ",")
    s = s.replace("（", "(").replace("）", ")")
    for ch in "「」『』《》〈〉【】":
        s = s.replace(ch, "")
    s = s.replace("—", "-").replace("–", "-").replace("…", "...")
    return s


def anchor_hit(excerpt: str, block_text: str) -> bool:
    """摘录是否在块原文中。先试前 30 字锚点，再退到任意 12 字窗口。"""
    ne = normalize(excerpt)
    nb = normalize(block_text)
    if not ne or not nb:
        return False
    if ne in nb:
        return True
    anchor = ne[:30] if len(ne) > 30 else ne
    if anchor and anchor in nb:
        return True
    if len(ne) >= 12:
        for i in range(0, max(1, len(ne) - 12)):
            if ne[i:i + 12] in nb:
                return True
    return False


# ---------------------------------------------------------------- 回执解析

RECEIPT_RE = re.compile(r"第\s*(\d+)\s*块[：:]\s*(.+?)\s*$", re.M)


def parse_receipts(text: str) -> Dict[int, str]:
    return {int(m.group(1)): m.group(2).strip() for m in RECEIPT_RE.finditer(text)}


def claimed_count(receipt: str) -> Optional[int]:
    m = re.search(r"(\d+)\s*条", receipt)
    if m:
        return int(m.group(1))
    if re.search(r"无|没有|0\s*条", receipt):
        return 0
    return None


# ---------------------------------------------------------------- 批次


def make_batches(doc: Document, size: int = BATCH_SIZE) -> List[List]:
    return [doc.blocks[i:i + size] for i in range(0, len(doc.blocks), size)]


def render_blocks(blocks: List) -> str:
    out = []
    for b in blocks:
        out.append(f"<<<块 {b.index}>>>")
        if b.heading:
            out.append(f"[当前小节：{b.heading}]")
        out.append(b.text)
        out.append("")
    return "\n".join(out)


def load_template(name: str) -> str:
    p = os.path.join(TEMPLATE_DIR, name)
    with open(p, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------- 主流程


def run_stage1(
    doc: Document,
    runner: Runner,
    batch_size: int = BATCH_SIZE,
    id_start: int = 1,
    progress=None,
    workers: int = 4,
) -> Tuple[Stage1, Dict[str, Any]]:
    """跑完第一段。返回 (Stage1, 报告)。

    批次之间没有依赖，可以并发。每批是一次独立的模型调用，
    串行的话一本书要等 N 倍单批时间；并发后总时长约等于最慢那批。
    """
    system = load_template("stage1_system.md")
    batches = make_batches(doc, batch_size)
    total_batches = len(batches)

    results: List[Optional[Dict[str, Any]]] = [None] * len(batches)
    lock = threading.Lock()
    done = [0]

    def work(idx_batch):
        idx, blocks = idx_batch
        raw = ""
        err = None
        try:
            raw = runner.run(system, render_blocks(blocks))
            data = _parse(raw)
        except Exception as e:
            data = None
            err = str(e)[:200]
        with lock:
            done[0] += 1
            if progress:
                mark = "✘" if err else "✔"
                progress(f"{mark} 第 {done[0]}/{total_batches} 批"
                         f"（块 {blocks[0].index}–{blocks[-1].index}）")
        return {"data": data, "error": err, "raw": raw, "blocks": blocks}

    if workers <= 1:
        raw_results = [work((i, b)) for i, b in enumerate(batches)]
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(batches))) as ex:
            raw_results = list(ex.map(work, list(enumerate(batches))))

    insights: List[Insight] = []
    glossary: List[Dict[str, str]] = []
    counters: List[Insight] = []
    open_questions: List[str] = []
    receipts_log: List[Dict[str, Any]] = []
    next_id = id_start

    # 按批次顺序归并，保证 ID 与输出稳定（并发完成顺序不固定）
    for res in raw_results:
        blocks = res["blocks"]
        if res["error"]:
            receipts_log.append({
                "批次": None, "状态": "失败", "原因": res["error"],
                "块": [b.index for b in blocks],
            })
            continue
        data = res["data"]
        batch_insights: List[Insight] = []
        for raw_i in data.get("insights", []) or []:
            ins = _make_insight(raw_i, next_id, doc)
            next_id += 1
            batch_insights.append(ins)
            insights.append(ins)

        for raw_c in data.get("counter_examples", []) or []:
            c = _make_insight(raw_c, next_id, doc, force_type="counter")
            next_id += 1
            counters.append(c)

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

        receipts = parse_receipts(res["raw"])
        missing = [b.index for b in blocks if b.index not in receipts]
        receipts_log.append({
            "批次": None,
            "状态": "缺回执" if missing else "完整",
            "块": [b.index for b in blocks],
            "缺回执": missing,
            "条目数": len(batch_insights),
        })

    report = {
        "批数": len(batches),
        "回执": receipts_log,
        "缺失回执总数": sum(len(r.get("缺回执", [])) for r in receipts_log),
    }
    stage1 = Stage1(
        source=doc.source,
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
    return stage1, report


def _parse(raw: str) -> Dict:
    from ..runner import parse_json_loose
    return parse_json_loose(raw)


def _make_insight(raw: Dict, new_id: int, doc: Document,
                  force_type: str = "") -> Insight:
    loc_raw = raw.get("locator") or {}
    locator = Locator(
        block=_int_or_none(loc_raw.get("block")),
        chapter=(str(loc_raw["chapter"]).strip() if loc_raw.get("chapter") else None),
        position=(str(loc_raw["position"]).strip() if loc_raw.get("position") else None),
    )
    t = (raw.get("type") or "").strip() or force_type or "other"
    return Insight(
        # ID 由本地计数器强制分配 —— 模型自己编号会跨批撞车
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


# ---------------------------------------------------------------- 校验


def verify_stage1(stage1: Stage1, doc: Document) -> Dict[str, Any]:
    """校验第一段产出：摘录能不能在原文定位。

    这是「模型有没有在编」的硬检查。
    """
    missing_support = []
    misattributed = []
    ok = 0

    items = list(stage1.insights) + list(stage1.counter_examples)
    for ins in items:
        if not ins.support:
            missing_support.append({"id": ins.id, "标题": ins.claim[:40],
                                    "原因": "没有原文摘录"})
            continue
        blk = ins.locator.block
        if not blk or not (1 <= blk <= len(doc.blocks)):
            # 没有块号 → 在全文里找
            if any(anchor_hit(ins.support, b.text) for b in doc.blocks):
                ok += 1
            else:
                missing_support.append({"id": ins.id, "标题": ins.claim[:40],
                                        "原因": "摘录在全文中定位不到"})
            continue
        block_text = doc.block_text(blk)
        if anchor_hit(ins.support, block_text):
            ok += 1
        else:
            # 归错了块？在别的块找
            found = None
            for b in doc.blocks:
                if b.index != blk and anchor_hit(ins.support, b.text):
                    found = b.index
                    break
            if found:
                misattributed.append({
                    "id": ins.id, "标题": ins.claim[:40],
                    "声称块": blk, "实际块": found,
                })
                ins.locator.block = found
            else:
                missing_support.append({"id": ins.id, "标题": ins.claim[:40],
                                        "原因": "摘录定位不到"})

    return {
        "总数": len(items),
        "定位成功": ok,
        "无法定位": len(missing_support),
        "无法定位明细": missing_support[:10],
        "归属错误": len(misattributed),
        "归属错误明细": misattributed[:10],
        "通过": not missing_support and not misattributed,
    }
