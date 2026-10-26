"""渲染「先讲一遍」的蒸馏版。"""

import datetime
from typing import List, Optional

from ..distill.brief import Brief
from ..schema import Source
from .document import clean_title


def render_brief(brief: Brief, source: Source,
                 generated_at: Optional[str] = None) -> str:
    title = clean_title(brief.title or source.title or "") or "蒸馏版"
    now = generated_at or datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    L: List[str] = [f"# {title}", ""]
    L.append(f"> **蒸馏版** ｜ 生成于 {now}")
    meta = []
    if source.author:
        meta.append(f"作者：{source.author}")
    if source.kind:
        meta.append(f"类型：{source.kind}")
    if meta:
        L.append(">")
        for m in meta:
            L.append(f"> {m}")
    L.append("")

    if brief.error:
        L += [f"> ⚠ **概述生成失败**：{brief.error}", ""]

    L += ["## 这内容在讲什么", "", brief.gist or "（没有生成概述）", ""]

    if brief.external_refs:
        names = "、".join(f"《{n}》" for n in brief.external_refs)
        L += [f"> ⚠ 概述里提到原文没有的书名（{names}），可能是模型的补充，"
              "引用前请核实。", ""]

    if len(brief.sections) >= 2:
        L += ["## 脉络", ""]
        for s in brief.sections:
            if s.error or not s.gist:
                continue
            L += [f"### {s.title}", "", s.gist, ""]

    L += [
        "---",
        "",
        "*本文由 everything-distill 生成：先把内容大致讲一遍，不是全文，也不替代原文。"
        "概述由模型基于原文转述；引用前请回原文核对。*",
    ]
    return "\n".join(L)
