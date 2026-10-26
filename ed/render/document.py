"""
最终文档渲染 —— 产出一份 Markdown。

结构顺序按「由浅入深」：
    一、这内容在讲什么        （概览）
    二、怎么切进去            （初次理解的建议）
    三、内容本身              （结构 / 洞察 / 反例 / 术语）
    四、跟你有关              （关联 / 你已知的 / 下一步）
    依据                      （论文式编号清单）

引用格式：正文用 `[1]`，渲染成可跳转的锚点链接 `[1](#ed-1)`，
文末对应 `<a id="ed-1"></a>### [1] 来源 · 引文`。
Markdown 不支持锚点时自动退化成纯文本编号，格式仍然成立。
"""

import re
import time
from typing import Any, Dict, List, Optional

from ..schema import Basis, Stage1, Stage2

ANCHOR = "ed-"


def _md_escape(s: str) -> str:
    """转义会破坏 markdown 表格/标题的字符。"""
    return s.replace("|", "\\|").replace("\n", " ").strip()


def _cite(ids: Any) -> str:
    """把 basis 列表渲染成 [1][3] 形式的引用（可跳转）。"""
    if not ids:
        return ""
    if isinstance(ids, int):
        ids = [ids]
    out = []
    for i in ids:
        try:
            n = int(i)
        except (TypeError, ValueError):
            continue
        out.append(f"[{n}](#{ANCHOR}{n})")
    return "".join(out)


def _plain_cite(ids: Any) -> str:
    if not ids:
        return ""
    if isinstance(ids, int):
        ids = [ids]
    return "".join(f"[{int(i)}]" for i in ids if str(i).isdigit())


def _type_label(t: str) -> str:
    return {
        "framework": "结构",
        "principle": "原则",
        "case": "事例",
        "counter": "反例",
        "term": "术语",
        "question": "问题",
    }.get(t, "洞察")


def _pick_relevant(rel: List[Dict[str, Any]], cap: int = 30) -> List[Dict[str, Any]]:
    """从模型给的关联里挑出要呈现的条目：总量封顶 + 主题分散。

    模型倾向围着读者最显眼的处境（找工作、做项目）反复说，20 条里有 9 条
    都在讲同一件事，读起来全是回声。这里按「处境关键词」分桶，每桶最多
    2 条，其余让位给不同角度的条目；桶分不出来时（都是独特角度）按原序
    放行。today 优先，但**总上限是硬约束**——否则 35 条 today 会全部挤进来，
    和文档承诺的「最多 30 条」对不上。
    """
    import re as _re

    THEMES = [
        ("求职", r"求职|岗位|投递|拒信|简历|面试|offer|职业|择业|毕业"),
        ("项目", r"蒸馏|项目|管线|工程|代码|工具"),
        ("消费决策", r"买|设备|盒子|路由|购物|下单|成本|钱"),
        ("自我管理", r"拖延|自责|焦虑|主线|方向|执行|习惯|复盘|检查清单"),
        ("健康", r"健康|睡|运动|健身|脊柱|疼"),
        ("关系", r"家人|朋友|伴侣|父母|社交"),
    ]

    def _theme(r: Dict[str, Any]) -> str:
        text = (r.get("why") or "") + (r.get("horizon_reason") or "")
        for name, pat in THEMES:
            if _re.search(pat, text, _re.I):
                return name
        return ""

    today = [r for r in rel if (r.get("horizon") or "").lower() == "today"]
    rest = [r for r in rel if r not in today]

    used_insight_ids: set = set()
    out: List[Dict[str, Any]] = []
    bucket_count: Dict[str, int] = {}
    PER_THEME_CAP = 2

    # today 优先，但仍受总上限约束
    for r in today:
        if len(out) >= cap:
            break
        iid = r.get("insight_id") or r.get("id")
        if iid in used_insight_ids:
            continue
        used_insight_ids.add(iid)
        out.append(r)
        t = _theme(r)
        bucket_count[t] = bucket_count.get(t, 0) + 1

    # 其余的按主题分散收，直到 cap
    for r in rest:
        if len(out) >= cap:
            break
        iid = r.get("insight_id") or r.get("id")
        if iid in used_insight_ids:
            continue
        t = _theme(r)
        if t and bucket_count.get(t, 0) >= PER_THEME_CAP:
            continue
        used_insight_ids.add(iid)
        out.append(r)
        bucket_count[t] = bucket_count.get(t, 0) + 1

    # 还有空位就放宽主题限制补足（说明条目本来就分散）
    if len(out) < cap:
        for r in rest:
            if len(out) >= cap:
                break
            iid = r.get("insight_id") or r.get("id")
            if iid not in used_insight_ids:
                used_insight_ids.add(iid)
                out.append(r)

    return out


def clean_title(t: str) -> str:
    """书名常带副标题和营销语（「价值投资圣经，人生智慧宝典。全新升级版...」）。

     只留主标题：以全角/半角括号或冒号前的部分为界，且不超过 40 字。
     """
    if not t:
        return ""
    s = t.strip()
    # 砍掉括号里的宣传语
    s = re.split(r"[（(]", s)[0].strip()
    # 砍掉「：」后面过长的副标题（保留短的）
    if "：" in s:
        head, tail = s.split("：", 1)
        if len(s) > 40 and len(head) >= 4:
            s = head
    if len(s) > 44:
        s = s[:44].rstrip("：:、—- ")
    return s


def _synth_overview(stage1: Stage1, n: int = 3) -> str:
    """没有概览时，用前几条洞察拼一个 —— 比只说一条好。"""
    ins = stage1.insights[:n]
    if not ins:
        return "（未能生成概览）"
    if len(ins) == 1:
        return f"这份内容主要在讲：{ins[0].claim}"
    parts = [f"{i+1}. {x.claim}" for i, x in enumerate(ins)]
    return ("这份内容的核心主张包括：\n\n" + "\n\n".join(parts))


def render(
    stage1: Stage1,
    stage2: Optional[Stage2],
    title: str = "",
    source_note: str = "",
    depth_note: str = "",
    narration: Optional[List[Dict[str, Any]]] = None,
    brief: Optional[Dict[str, Any]] = None,
) -> str:
    """生成最终 Markdown 文档。"""
    L: List[str] = []
    src = stage1.source
    name = clean_title(title or src.title) or "这份内容"
    now = time.strftime("%Y-%m-%d %H:%M")

    # 正文洞察始终以第一段为准（它才是完整的那份）。第二段负责的是
    # 「哪条对他更要紧」，不是「留哪条」——它回的条数不稳定，直接当正文会随机丢内容。
    # 这里先做一次注入，后面的统计和渲染用的是同一份数据。
    insights = list(stage1.insights)
    if stage2 and stage2.insights:
        rel = {i.id: i.relevance for i in stage2.insights if i.relevance}
        for ins in insights:
            if ins.id in rel:
                ins.relevance = rel[ins.id]
    if stage2 and stage2.relevant:
        # 被写进「跟你有关」的条目就是重点，一律标 ★——
        # 否则正文的 ★ 数量和第四节讲的条数对不上，读者会困惑
        picked = {r.get("insight_id") for r in stage2.relevant}
        for ins in insights:
            if ins.id in picked:
                ins.relevance = {"level": "high",
                                 "note": (ins.relevance or {}).get("note", "")
                                 if isinstance(ins.relevance, dict) else ""}
    _hi = sum(1 for i in insights
              if isinstance(i.relevance, dict) and i.relevance.get("level") == "high")

    # 章节编号：缺失的章节不占号，避免「一 → 三」的跳号
    has_angles = bool(stage2 and stage2.entry_angles)
    sec = 0
    num = {}
    sec += 1
    num["overview"] = sec
    if has_angles:
        sec += 1
        num["angles"] = sec
    sec += 1
    num["content"] = sec
    if stage2:
        sec += 1
        num["me"] = sec
    CN = "一二三四五六七八九十"

    # ---------- 头部 ----------
    L.append(f"# {name}")
    L.append("")
    L.append(f"> **蒸馏理解** ｜ 生成于 {now}")
    if src.author:
        L.append(f"> ")
        L.append(f"> 作者：{src.author}")
    if source_note:
        L.append(f"> ")
        L.append(f"> 来源：{source_note}")
    if depth_note:
        L.append(f"> ")
        L.append(f"> {depth_note}")
    L.append("")

    # ---------- 一、概览 ----------
    L.append(f"## {CN[num['overview']-1]}、这内容在讲什么")
    L.append("")
    # 第二段的概览能看到全部洞察，比第一段单批的更准，优先用它；
    # 但「先讲一遍」的 brief 是最完整的一遍通读，优先级最高。
    overview = ""
    if brief and str(brief.get("gist") or "").strip():
        overview = str(brief["gist"]).strip()
    if not overview and stage2 and getattr(stage2, "overview", ""):
        overview = stage2.overview
    if not overview:
        if narration:
            parts = [f"- **{c.get('title', '')}**：{c.get('summary', '')}"
                     for c in narration if c.get("summary")]
            overview = ("全书大致分这几块：\n\n" + "\n".join(parts)
                        if parts else "（未能生成概览）")
        else:
            overview = stage1.overview or _synth_overview(stage1)
    L.append(overview)
    L.append("")
    if stage1.stats:
        bits = []
        if narration:
            bits.append(f"{len(narration)} 个部分")
            words = sum(len(c.get("prose") or "") for c in narration)
            if words:
                bits.append(f"讲述 {words:,} 字")
        else:
            if stage1.stats.get("有效字数"):
                bits.append(f"{stage1.stats['有效字数']:,} 字")
            # 正文渲染的是第一段的全部洞察，所以条数按第一段算；
            # 第二段的条数只代表「它挑了几条讲」，不是正文有多少条
            total_ins = len(insights)
            if total_ins:
                bits.append(f"{total_ins} 条洞察"
                            + (f"（★ 重点 {_hi} 条）" if _hi else ""))
        if bits:
            L.append(f"*{' ｜ '.join(bits)}*")
            L.append("")

    # ---------- 二、怎么切进去 ----------
    if stage2 and stage2.entry_angles:
        L.append(f"## {CN[num['angles']-1]}、怎么切进去")
        L.append("")
        for a in stage2.entry_angles:
            L.append(f"- {a}")
        L.append("")
        if stage2.reading_order:
            L.append("**建议的顺序**")
            L.append("")
            for r in stage2.reading_order:
                L.append(f"- {r}")
            L.append("")

    # ---------- 三、内容本身 ----------
    L.append(f"## {CN[num['content']-1]}、内容本身")
    L.append("")

    if narration:
        # 讲述模式：先给一张全书地图，再一段一段讲透。
        # 不再渲染「200 条洞察卡片」——那是知识库形态，不适合人读。
        L.append("### 全书脉络")
        L.append("")
        for ch in narration:
            summary = (ch.get("summary") or "").strip()
            if summary:
                L.append(f"- **{ch.get('title', '')}**：{summary}")
        L.append("")
        for ch in narration:
            L.append(f"### {ch.get('title', '')}")
            L.append("")
            for para in (ch.get("prose") or "").split("\n\n"):
                para = para.strip()
                if para:
                    L.append(para)
                    L.append("")
            refs = ch.get("external_refs") or []
            if refs:
                L.append(f"> ⚠ 这一部分提到原文里没有的书名"
                         f"（{'、'.join(refs)}），可能是模型的补充，引用前请核实。")
                L.append("")
            for q in ch.get("quotes") or []:
                text = (q.get("text") or "").strip()
                if not text:
                    continue
                L.append(f"> 原文：{text}")
                why = (q.get("why") or "").strip()
                if why:
                    L.append(">")
                    L.append(f"> — {why}")
                L.append("")

    if not narration and ((stage2 and getattr(stage2, "structure", None))
                          or stage1.structure):
        L.append("### 结构")
        L.append("")
        L.append("| 部分 | 讲什么 | 与前一部分的关系 |")
        L.append("| --- | --- | --- |")
        struct = (stage2.structure if stage2 and getattr(stage2, "structure", None)
                  else stage1.structure)
        for s in struct:
            L.append("| {p} | {w} | {r} |".format(
                p=_md_escape(str(s.get("part", ""))),
                w=_md_escape(str(s.get("what", ""))),
                r=_md_escape(str(s.get("relation", ""))),
            ))
        L.append("")

    # 相关度注入已在函数开头做完，这里只负责排序
    if not narration and any(ins.relevance for ins in insights):
        rank = {"high": 0, "medium": 1, "low": 2}

        def _rank(ins):
            rel = ins.relevance or {}
            level = rel.get("level") if isinstance(rel, dict) else None
            return rank.get(str(level or ""), 3)

        insights.sort(key=_rank)
    if not narration and insights:
        L.append("### 关键洞察")
        L.append("")
        # 条数多时给一行导航：读者得知道重点在哪、要不要全读
        if len(insights) >= 40:
            hi = sum(1 for i in insights
                     if (i.relevance or {}).get("level") == "high")
            if hi:
                L.append(f"*共 {len(insights)} 条，按与你的相关度排序，"
                         f"其中 ★ 重点 {hi} 条。时间紧就只读 ★。*")
                L.append("")
        for ins in insights:
            rel = ins.relevance or {}
            level = rel.get("level")
            tag = {"high": "★", "medium": "☆", "low": "·"}.get(level, "")
            head = f"**{tag} {ins.claim}**" if tag else f"**{ins.claim}**"
            L.append(head)
            L.append("")
            meta = [f"类型：{_type_label(ins.type)}"]
            if ins.locator.chapter:
                meta.append(f"位置：{ins.locator.chapter}")
            elif ins.locator.block:
                meta.append(f"位置：第 {ins.locator.block} 块")
            if ins.id:
                meta.append(f"ID：{ins.id}")
            L.append("　".join(meta))
            L.append("")
            if ins.detail:
                L.append(ins.detail)
                L.append("")
            if ins.support:
                L.append(f"> 原文：{ins.support}")
                L.append("")

    if not narration and stage1.counter_examples:
        L.append("### 失效场景与反例")
        L.append("")
        for c in stage1.counter_examples:
            L.append(f"- **{c.claim}**")
            if c.boundary and c.boundary != "作者未说明":
                L.append(f"  - 边界：{c.boundary}")
            if c.support:
                L.append(f"  - 原文：{c.support}")
        L.append("")

    if not narration and stage1.glossary:
        L.append("### 专有名词")
        L.append("")
        L.append("| 词 | 作者的定义 |")
        L.append("| --- | --- |")
        for g in stage1.glossary:
            L.append(f"| **{_md_escape(g.get('term',''))}** | {_md_escape(g.get('definition',''))} |")
        L.append("")

    if not narration and stage1.open_questions:
        L.append("### 这内容引出的问题")
        L.append("")
        for q in stage1.open_questions:
            L.append(f"- {q}")
        L.append("")

    # ---------- 四、跟你有关 ----------
    if stage2:
        L.append(f"## {CN[num['me']-1]}、跟你有关")
        L.append("")
        if stage2.relevant:
            # 三档：今天能用 / 值得想 / 存着。上限 30 条——
            # 这节是「内容对你的投影」，不是正文主体；条目一多必然互相重复，
            # 读者的注意力就该花在第三节的蒸馏本身上。
            picked = _pick_relevant(stage2.relevant, cap=30)
            tiers = [
                ("today", "眼下能用得上",
                 "正在处理的事，读完能立刻改一个做法。"),
                ("think", "值得想",
                 "下次复盘、下次遇到类似情况时会想起来的角度和判断标准。"),
                ("later", "存着，以后可能用",
                 "现在用不上，但某天真要做那件事时是有价值的地图。"),
            ]
            buckets: Dict[str, List[Dict[str, Any]]] = {k: [] for k, _, _ in tiers}
            for r in stage2.relevant:
                h = (r.get("horizon") or "").strip().lower()
                buckets[h if h in buckets else "think"].append(r)
            buckets = {k: [r for r in picked if r in buckets[k]] for k in buckets}

            for key, title, blurb in tiers:
                items = buckets[key]
                if not items:
                    continue
                L.append(f"### {title}（{len(items)} 条）")
                L.append("")
                L.append(f"*{blurb}*")
                L.append("")
                for r in items:
                    ids = r.get("basis") or []
                    cite = _cite(ids)
                    L.append(f"- **{r.get('why','')}** {cite}")
                L.append("")
        if stage2.known_already:
            L.append("### 你可能已经知道的（可以跳过）")
            L.append("")
            for k in stage2.known_already:
                L.append(f"- {k}")
            L.append("")
        if stage2.next_actions:
            L.append("### 下一步")
            L.append("")
            for a in stage2.next_actions:
                L.append(f"- {a}")
            L.append("")
        if not stage2.relevant and not stage2.next_actions:
            L.append("这份内容里没有特别跟你相关的部分。")
            L.append("")

    # ---------- 依据 ----------
    if stage2 and stage2.basis:
        # 只渲染正文真正引用过的依据 —— 模型常给一批依据但只用其中几条，
        # 剩下的留在文末会让读者以为漏了内容
        cited = set()
        for r in stage2.relevant:
            for i in (r.get("basis") or []):
                try:
                    cited.add(int(i))
                except (TypeError, ValueError):
                    pass
        used = [b for b in sorted(stage2.basis, key=lambda x: x.id)
                if b.id in cited] if cited else sorted(stage2.basis, key=lambda x: x.id)

        if used:
            L.append("## 依据")
            L.append("")
            L.append("每条跟读者有关的结论都挂在这里。`[记忆]` 是长期记忆里确认过的，"
                     "`[提及]` 是会话里单次提到过的，"
                     "`[内容]` 指被蒸的内容本身。")
            L.append("")
            kind_label = {"memory": "记忆", "mention": "提及", "content": "内容"}
            for b in used:
                L.append(f'<a id="{ANCHOR}{b.id}"></a>')
                L.append("")
                L.append(f"### [{b.id}] {kind_label.get(b.kind, b.kind)} · {_md_escape(b.source)}")
                L.append("")
                if b.quote:
                    L.append(f"> {b.quote}")
                    L.append("")
                if b.note:
                    L.append(f"*{b.note}*")
                    L.append("")

    L.append("---")
    L.append("")
    has_personal = bool(stage2 and (stage2.relevant or stage2.basis))
    tail = "内容摘录均经程序回原文逐字核对"
    if has_personal:
        tail += "；个人化结论均挂依据"
    L.append(f"*本文由 everything-distill 生成。{tail}。"
             "蒸馏结果会因所用模型而异。*")
    L.append("")
    return "\n".join(L)


def _fallback_overview(stage1: Stage1) -> str:
    """模型没给概览时，用前几条洞察凑一个。"""
    if not stage1.insights:
        return "（未能生成概览）"
    head = stage1.insights[0].claim
    return f"这份内容主要在讲：{head}"
