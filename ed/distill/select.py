"""
第二段之前的机械筛选。

为什么要筛：
    第一段的子 Agent 天然倾向于「多记」——跑一本书可能出 500+ 条洞察。
    全都塞给第二段的 prompt，一是会爆上下文，二是会让匹配变稀。

**不能靠 type 排序**：
    实测出现过「200 条里 197 条被标成 principle」——模型倾向于把所有东西
    都归成「原则」，类型标注作为筛选依据不可靠。

所以主策略是**按块轮转采样**（round-robin）：
    每个块轮流出一条，直到达到上限。这样即使类型全错，也不会出现
    「某一章被抽干、另一章一条不剩」。

type 和字段质量只在**块内排序**时做小权重，不作为跨块筛选依据。
"""

from typing import Any, Dict, List

from ..schema import Insight, Stage1

# 块内排序用的小权重（不是跨块筛选依据）
TYPE_HINT = {
    "principle": 0,
    "framework": 0,
    "counter": 1,
    "term": 1,
    "case": 2,
    "question": 3,
    "other": 3,
}


def _rank(ins: Insight) -> tuple:
    """块内排序：越小越优先。"""
    has_boundary = 0 if (ins.boundary and ins.boundary != "作者未说明") else 1
    has_detail = 0 if ins.detail else 1
    t = TYPE_HINT.get(ins.type, 3)
    # 摘录太短的信息量低
    short = 1 if len(ins.support or "") < 12 else 0
    return (has_boundary, has_detail, t, short)


def _round_robin(groups: Dict[int, List[Insight]], budget: int,
                 start_per_block: int = 1) -> List[Insight]:
    """按块轮转取，直到用完预算。

    这样每块被抽中的概率相等，不受某块条目多的影响。
    """
    for b in groups:
        groups[b].sort(key=_rank)

    picked: List[Insight] = []
    blocks = sorted(groups)

    # 第一轮：每块先保底
    for b in blocks:
        take = min(start_per_block, len(groups[b]))
        picked.extend(groups[b][:take])
        if len(picked) >= budget:
            return picked[:budget]

    # 后续轮：每块再来一条，直到预算用尽或全部取完
    idx = {b: start_per_block for b in blocks}
    while len(picked) < budget:
        added = 0
        for b in blocks:
            if len(picked) >= budget:
                break
            i = idx[b]
            if i < len(groups[b]):
                picked.append(groups[b][i])
                idx[b] = i + 1
                added += 1
        if not added:
            break
    return picked[:budget]


def select(stage1: Stage1, max_total: int = 200,
           min_per_block: int = 1) -> Dict[str, Any]:
    """从第一段产物里选出进入第二段的洞察。

    返回 {"insights": [...], "dropped": n, "报告": {...}}
    """
    items = list(stage1.insights)
    total_before = len(items)

    if total_before <= max_total:
        # 未超上限也要按块排序，保证两条路径的输出行为一致
        items.sort(key=lambda i: (i.locator.block or 10**6))
        return {
            "insights": items,
            "dropped": 0,
            "报告": {"原始": total_before, "保留": total_before,
                     "说明": "条目数未超上限，全部保留"},
        }

    groups: Dict[int, List[Insight]] = {}
    no_block: List[Insight] = []
    for ins in items:
        b = ins.locator.block
        if b:
            groups.setdefault(b, []).append(ins)
        else:
            no_block.append(ins)

    picked = _round_robin(groups, max_total, min_per_block)

    # 还有预算就补上没块号的
    if len(picked) < max_total and no_block:
        no_block.sort(key=_rank)
        picked.extend(no_block[:max_total - len(picked)])

    picked.sort(key=lambda i: (i.locator.block or 10**6))
    dropped = total_before - len(picked)

    per_block: Dict[int, int] = {}
    for i in picked:
        b = i.locator.block or 0
        per_block[b] = per_block.get(b, 0) + 1

    return {
        "insights": picked,
        "dropped": dropped,
        "报告": {
            "原始": total_before,
            "保留": len(picked),
            "丢弃": dropped,
            "上限": max_total,
            "策略": "按块轮转采样（不依赖 type）",
            "块数": len(groups),
            "每块保留": {
                "最少": min(per_block.values()) if per_block else 0,
                "最多": max(per_block.values()) if per_block else 0,
                "覆盖块数": len(per_block),
            },
            "类型分布": _count_types(picked),
        },
    }


def _count_types(items: List[Insight]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for i in items:
        out[i.type] = out.get(i.type, 0) + 1
    return out


def apply(stage1: Stage1, max_total: int = 200, min_per_block: int = 1) -> Stage1:
    """返回筛选后的 Stage1（不修改原对象）。"""
    r = select(stage1, max_total, min_per_block)
    return Stage1(
        source=stage1.source,
        overview=stage1.overview,
        structure=stage1.structure,
        insights=r["insights"],
        glossary=stage1.glossary,
        counter_examples=stage1.counter_examples,
        open_questions=stage1.open_questions,
        stats={**stage1.stats, "筛选": r["报告"]},
    )
