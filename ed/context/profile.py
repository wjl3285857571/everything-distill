"""
「处境」——关于这个读者的信息。

**不是所有记忆文件都算处境。** 混进去会让匹配变噪声。

默认纳入：
    USER.md       读者档案：偏好、价值排序、现状
    PROJECTS.md  读者正在做的事

可选纳入：
    HISTORY.md   共同经历（读者和助理之间的历史）
    TECH.md      技术类内容时才相关——助理自己的排障经验

不纳入：
    SOUL.md      助理的性格设定，不是读者的处境
    MEMORY.md    助理自己的技术笔记，除非内容本身是技术类

会话历史**不预先拉取**。默认只用记忆文件；
只有当某方面觉得「光靠记忆不够，得看他当时具体怎么说的」时，
再由编排者按需检索并注入（add_sessions）。
"""

import os
import re
from typing import Any, Dict, List, Optional

# 默认纳入的记忆文件（按优先级）
DEFAULT_FILES = ["USER.md", "PROJECTS.md"]

# 可选纳入（需要显式要求）
OPTIONAL_FILES = ["HISTORY.md", "TECH.md"]

# 明确排除
EXCLUDED_FILES = ["SOUL.md", "MEMORY.md"]


class Context:
    """读者处境。"""

    def __init__(self, memories: Optional[Dict[str, str]] = None,
                 sessions: Optional[List[Dict[str, Any]]] = None,
                 notes: str = ""):
        self.memories = memories or {}
        self.sessions = sessions or []
        self.notes = notes

    # -- 构造 --

    @classmethod
    def from_dir(cls, mem_dir: str, include: Optional[List[str]] = None,
                 exclude: Optional[List[str]] = None) -> "Context":
        """从记忆目录读。include 为 None 时用默认集合。"""
        names = include if include is not None else list(DEFAULT_FILES)
        skip = set(exclude if exclude is not None else EXCLUDED_FILES)
        mems: Dict[str, str] = {}
        for n in names:
            if n in skip:
                continue
            p = os.path.join(mem_dir, n)
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    mems[n] = f.read()
        return cls(memories=mems)

    def add_sessions(self, sessions: List[Dict[str, Any]]) -> "Context":
        """按需注入会话片段。sessions 每项形如
           {"ref": "2026-10-12 会话", "quote": "..."}"""
        self.sessions.extend(sessions)
        return self

    # -- 渲染 --

    def render(self, max_chars_per_file: int = 20000) -> str:
        """渲染成给第二段看的处境文本。

        刻意不加「以下是关于读者的信息」之外的引导语——
        避免模型把处境当任务清单而不是背景。
        """
        parts: List[str] = []
        if self.memories:
            parts.append("## 记忆文件")
            for name, content in self.memories.items():
                c = content.strip()
                if len(c) > max_chars_per_file:
                    c = c[:max_chars_per_file] + "\n…（后续省略）"
                parts.append(f"\n### 来源文件：{name}\n\n{c}")
        if self.sessions:
            parts.append("\n## 会话里提到过的（单次提及，未固化）")
            for i, s in enumerate(self.sessions, 1):
                ref = s.get("ref", f"会话{i}")
                quote = s.get("quote", "").strip()
                parts.append(f"\n- 【{ref}】{quote}")
        if self.notes:
            parts.append(f"\n## 补充说明\n\n{self.notes}")
        if not parts:
            return "（暂无关于读者的信息）"
        return "\n".join(parts)

    def is_empty(self) -> bool:
        return not self.memories and not self.sessions and not self.notes

    def summary(self) -> Dict[str, Any]:
        return {
            "记忆文件": {k: len(v) for k, v in self.memories.items()},
            "会话片段数": len(self.sessions),
            "总字数": sum(len(v) for v in self.memories.values())
                     + sum(len(s.get("quote", "")) for s in self.sessions),
        }


def extract_quotes(text: str, patterns: List[str], limit: int = 5) -> List[str]:
    """从记忆文件里按关键词找原句 —— 给「依据」用。

    依据必须是**原句**，不能是模型的转述，否则引用就不成立了。
    """
    out: List[str] = []
    for pat in patterns:
        for m in re.finditer(pat, text):
            # 往回找句首，往后找句末
            start = m.start()
            while start > 0 and text[start - 1] not in "。！？\n":
                start -= 1
            end = m.end()
            while end < len(text) and text[end] not in "。！？\n":
                end += 1
            s = text[start:end].strip().lstrip("，、；:：")
            # 中文短句在记忆文件里很常见，阈值放到 4
            if 4 <= len(s) <= 200 and s not in out:
                out.append(s)
            if len(out) >= limit:
                return out
    return out
