"""把第一段产物打包成 AI 可调用的 SKILL.md。

和另外两种产物形态的分工：
    narrate   → 给人读的讲述版（连续长文）
    render    → 卡片版（结构表 + 洞察列表）
    package   → 给 AI 用的技能包（六段结构 + front matter）

六段结构由 `ed.verify.skill_meta` 校验，不是随便写写。
"""

import re
from typing import Dict, List, Optional

from ..schema import Insight, Stage1
from ..verify.skill_meta import char_len
from .document import clean_title

MAX_PRINCIPLES = 12
MAX_HOWTO = 10
MAX_COUNTERS = 8


def slugify(text: str, fallback: str = "distilled-skill") -> str:
    """name 只能小写英文/数字加连字符；中文标题会退化成 fallback。"""
    s = (text or "").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    if not s:
        return fallback
    return s[:48].strip("-") or fallback


def _topic(title: str, limit: int = 16) -> str:
    t = clean_title(title) or "这份内容"
    return t[:limit].rstrip("，、·—- ")


def make_description(title: str, terms: Optional[List[str]] = None) -> str:
    """description 要 60–120 字，且必须写明什么时候不要用。"""
    topic = _topic(title)
    base = (f"当需要{topic}里的判断与方法时使用，提供可执行的判断标准和步骤")
    if terms:
        base += f"，涵盖{'、'.join(terms[:3])}"
    base += "。不要用于与这个主题无关的场景，也不要把书中主张当成事实结论。"
    if char_len(base) < 60:
        base = base.replace("。不要", "，适合在决策或复盘前翻查。不要")
    if char_len(base) > 120:
        base = (f"当需要{topic}里的判断与方法时使用，提供可执行的判断标准和步骤。"
                "不要用于与这个主题无关的场景，也不要把书中主张当成事实结论。")
    return base


def _pick(items: List[Insight], types: tuple, limit: int) -> List[Insight]:
    out = [i for i in items if i.type in types and i.claim]
    return out[:limit]


def build_skill(stage1: Stage1, name: Optional[str] = None) -> tuple:
    """返回 (slug, SKILL.md 文本)。"""
    title = clean_title(stage1.source.title) or "这份内容"
    slug = name or slugify(stage1.source.title or title)
    terms = [g.get("term", "") for g in stage1.glossary if g.get("term")]
    description = make_description(stage1.source.title or title, terms)

    principles = _pick(stage1.insights, ("principle", "framework"), MAX_PRINCIPLES)
    howto = [i for i in stage1.insights if i.detail][:MAX_HOWTO]
    counters = stage1.counter_examples[:MAX_COUNTERS]

    L: List[str] = ["---", f"name: {slug}", f"description: {description}", "---", ""]
    L += [f"# {title}", ""]

    L += ["## 什么时候用它", ""]
    if stage1.overview:
        L += [stage1.overview.strip(), ""]
    L += [
        f"- 当你要处理「{_topic(title)}」相关的问题，需要一套判断标准而不是背景介绍时",
        "- 当你需要在几个做法之间取舍，想先看原文给的条件和边界时",
        "",
    ]

    L += ["## 核心判断", ""]
    if principles:
        for ins in principles:
            L.append(f"- **{ins.claim}**")
            if ins.boundary and ins.boundary != "作者未说明":
                L.append(f"  - 边界：{ins.boundary}")
    else:
        L.append("- 原文没有抽出明确的原则型判断；先看「怎么做」里的具体做法。")
    L.append("")

    L += ["## 怎么做", ""]
    if howto:
        for ins in howto:
            L.append(f"- **{ins.claim}**")
            L.append(f"  - {ins.detail}")
    else:
        L.append("- 原文没有给出成步骤的方法；按上面的判断标准逐条对照。")
    L.append("")

    L += ["## 判断标准", ""]
    any_boundary = False
    for ins in principles:
        if ins.boundary and ins.boundary != "作者未说明":
            L.append(f"- {ins.boundary}")
            any_boundary = True
    if not any_boundary:
        L.append("- 原文没有明确写出适用条件；默认只在原文描述的场景里使用。")
    for q in stage1.open_questions[:5]:
        L.append(f"- 自问：{q}")
    L.append("")

    L += ["## 什么时候不该用它", ""]
    if counters:
        for c in counters:
            L.append(f"- {c.claim}")
    else:
        L.append("- 原文没有明确给出失效场景；遇到与原文条件不同的情况，不要直接套用。")
    L.append("- 不要把这里的判断当成事实结论；它只是原文主张的压缩。")
    L.append("")

    L += ["## 出处", ""]
    L.append(f"- 来源：{stage1.source.title or '未命名'}")
    if stage1.source.author:
        L.append(f"- 作者：{stage1.source.author}")
    for ins in principles[:3]:
        if ins.support:
            L.append(f"- 原文：{ins.support}")
    L.append("")

    return slug, "\n".join(L)
