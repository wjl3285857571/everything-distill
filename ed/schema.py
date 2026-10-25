"""
数据契约 —— 两段式蒸馏之间的接口。

设计原则：
1. 不预设内容结构。「主张/框架/案例/反例/术语」是「书」的分类，
   视频逐字稿、会议记录、公众号文章都没有这个结构。
   所以统一抽象成「洞察」(Insight)，类型是**标注**而不是**抽屉**——
   模型自己判断这条洞察属于什么性质，可留空。
2. 两条硬约束保留，因为它们让产出可验证：
   - support：必须有原文摘录，且要能在原文里定位
   - boundary：这条的适用边界（不知道就说不知道，不许编）
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 允许的洞察类型标注（用于报告和聚类，不是必填的抽屉）
INSIGHT_TYPES = [
    "framework",   # 有固定步骤或维度的思考结构
    "principle",   # 能指导取舍的规则
    "case",        # 具体发生过的事
    "counter",     # 做错了的做法 / 失效场景
    "term",        # 作者赋予特殊含义的词
    "question",    # 内容引出的好问题
    "other",
]


@dataclass
class Locator:
    """原文定位信息。"""
    block: Optional[int] = None       # 文本块号（1-based）
    chapter: Optional[str] = None     # 章节标题
    position: Optional[str] = None    # 自由文本位置描述

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in {
            "block": self.block,
            "chapter": self.chapter,
            "position": self.position,
        }.items() if v is not None}


@dataclass
class Insight:
    """一条洞察。"""
    id: str
    claim: str                          # 说法本身
    support: str = ""                   # 原文摘录（逐字，必须能在原文定位）
    locator: Locator = field(default_factory=Locator)
    boundary: str = ""                  # 适用边界 / 什么时候不成立
    type: str = "other"                 # 见 INSIGHT_TYPES
    detail: str = ""                    # 展开说明（结构、要素等）
    relevance: Optional[Dict[str, Any]] = None   # 第二段填：与用户的关系

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "id": self.id,
            "claim": self.claim,
            "type": self.type,
        }
        if self.support:
            d["support"] = self.support
        if self.boundary:
            d["boundary"] = self.boundary
        if self.detail:
            d["detail"] = self.detail
        if self.locator and self.locator.to_dict():
            d["locator"] = self.locator.to_dict()
        if self.relevance:
            d["relevance"] = self.relevance
        return d


@dataclass
class Source:
    """输入源信息。"""
    kind: str                           # epub/pdf/docx/text/html/video/transcript
    origin: str                         # 路径或 URL
    title: str = ""                     # 内容标题
    author: str = ""
    duration: Optional[str] = None      # 视频/音频时长
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "origin": self.origin,
            "title": self.title,
            "author": self.author,
            "duration": self.duration,
            "meta": self.meta,
        }


@dataclass
class Stage1:
    """第一段产物：无处境的客观内容蒸馏。"""
    source: Source
    overview: str = ""                              # 30 秒版：这内容在讲什么
    structure: List[Dict[str, str]] = field(default_factory=list)   # 结构：部分/讲什么/关系
    insights: List[Insight] = field(default_factory=list)
    glossary: List[Dict[str, str]] = field(default_factory=list)   # 专有名词
    counter_examples: List[Insight] = field(default_factory=list)  # 失效场景/反例
    open_questions: List[str] = field(default_factory=list)        # 内容引出的问题
    stats: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source.to_dict(),
            "overview": self.overview,
            "structure": self.structure,
            "insights": [i.to_dict() for i in self.insights],
            "glossary": self.glossary,
            "counter_examples": [c.to_dict() for c in self.counter_examples],
            "open_questions": self.open_questions,
            "stats": self.stats,
        }


@dataclass
class Basis:
    """依据条目 —— 个人化结论必须挂依据，否则不许写。

    kind:
      memory  —— 来自长期记忆文件（已验证）
      mention —— 来自会话里单次提到过（未固化）
      content —— 来自被蒸馏的内容本身
    """
    id: int
    kind: str
    source: str          # 文件路径或会话引用
    quote: str           # 依据的原文片段
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "source": self.source,
            "quote": self.quote,
            "note": self.note,
        }


@dataclass
class Stage2:
    """第二段产物：结合用户处境后的重组与关联。"""
    overview: str = ""                                        # 概览：这内容在讲什么
    structure: List[Dict[str, str]] = field(default_factory=list)  # 整体结构
    entry_angles: List[str] = field(default_factory=list)     # 初次理解的切入建议
    reading_order: List[str] = field(default_factory=list)   # 建议的阅读/关注顺序
    relevant: List[Dict[str, Any]] = field(default_factory=list)  # 跟用户有关的部分
    known_already: List[str] = field(default_factory=list)   # 用户可能已经知道的（可跳过）
    next_actions: List[str] = field(default_factory=list)    # 下一步做什么
    basis: List[Basis] = field(default_factory=list)         # 依据清单
    insights: List[Insight] = field(default_factory=list)    # 重排后的洞察（带 relevance）

    def to_dict(self) -> Dict[str, Any]:
        return {
            "overview": self.overview,
            "structure": self.structure,
            "entry_angles": self.entry_angles,
            "reading_order": self.reading_order,
            "relevant": self.relevant,
            "known_already": self.known_already,
            "next_actions": self.next_actions,
            "basis": [b.to_dict() for b in self.basis],
            "insights": [i.to_dict() for i in self.insights],
        }
