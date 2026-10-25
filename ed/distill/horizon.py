"""给已有的第二段结果补 horizon 标签（今天能用 / 值得想 / 存着）。

为什么不重跑整个第二段：
第二段的输出是 149KB 的完整 JSON（含 122 条重写后的洞察），重跑要七八分钟、
模型服务还可能中途断。而补标签只需要看「每条为什么跟他有关」这一句，
输入几千字、输出几十行，一分钟内能完成。

用法：
    python3 -m ed horizon-prompt --workdir runs/穷查理宝典 --out runs/穷查理宝典/_horizon_prompt.txt
    # 派子 Agent 读它，结果写到 runs/穷查理宝典/_horizon.out
    python3 -m ed collect-horizon --workdir runs/穷查理宝典
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from ..context.profile import Context


SYSTEM = """你在给一份「内容蒸馏」文档做最后一道分档。

下面是这份内容里「跟读者直接相关的」每一条的一句话说明。请给每条打一个 `horizon`
标签，判断的是**这条离他现在的行动有多近**，不是有多重要：

- `today` —— 今天或这周就能用上。他眼下正在处理的事（求职、项目、健康、具体决策），
  读完能立刻改一个做法。**宁少勿多**。
- `think` —— 值得想，但不是马上做。给他一个看问题的角度、一个判断标准，
  下次复盘或遇到类似情况时会想起来。
- `later` —— 存着，以后可能有用。知识性、扩展性的内容，现在用不上，
  但某天真要做那件事时是有价值的地图。

判断标准是「离行动多近」。一条最重要的长期原则，如果这周改变不了他任何一个动作，
那是 `think` 而不是 `today`。

## 读者当前处境

{profile}

## 输出格式

只输出 JSON，不要解释，不要 markdown 包裹：

{{
  "tags": [
    {{"id": 1, "horizon": "today", "why_tier": "一句话说明为什么归这一档"}},
    {{"id": 2, "horizon": "think", "why_tier": "..."}}
  ]
}}

`id` 就是下面清单里的编号，一条不能漏，一条不能多。
"""


def horizon_prompt(workdir: str) -> str:
    out = os.path.join(workdir, "_stage2.out")
    if not os.path.exists(out):
        raise FileNotFoundError(f"找不到第二段结果：{out}")
    with open(out, encoding="utf-8") as f:
        data = json.load(f)
    rel = data.get("relevant") or []
    if not rel:
        raise ValueError("第二段结果里没有 relevant 条目")

    lines = []
    for i, r in enumerate(rel, 1):
        why = (r.get("why") or "").strip()
        level = (r.get("relevance") or {}).get("level") or r.get("level") or ""
        prefix = f"[{level}] " if level else ""
        lines.append(f"{i}. {prefix}{why}")

    ctx = Context.from_dir(os.path.expanduser("~/.hermes/memories"))
    text = SYSTEM.replace("{profile}", ctx.render())
    text += "\n## 待分档的条目\n\n" + "\n".join(lines) + "\n"
    return text


def collect_horizon(workdir: str) -> Dict[str, Any]:
    src = os.path.join(workdir, "_horizon.out")
    if not os.path.exists(src):
        return {"错误": f"找不到 {src}"}
    raw = open(src, encoding="utf-8").read().strip()
    # 容忍 markdown 代码块包裹
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        raw = raw.rsplit("```", 1)[0]
    data = json.loads(raw)
    tags = data.get("tags") or []

    stage2_out = os.path.join(workdir, "_stage2.out")
    with open(stage2_out, encoding="utf-8") as f:
        s2 = json.load(f)
    rel: List[Dict[str, Any]] = s2.get("relevant") or []

    by_id = {}
    for t in tags:
        try:
            by_id[int(t.get("id"))] = t
        except (TypeError, ValueError):
            continue

    applied = 0
    missing = []
    for i, r in enumerate(rel, 1):
        t = by_id.get(i)
        if not t:
            missing.append(i)
            r["horizon"] = "think"      # 缺标签不丢内容，归中间档
            continue
        h = (t.get("horizon") or "").strip().lower()
        r["horizon"] = h if h in ("today", "think", "later") else "think"
        if t.get("why_tier"):
            r["horizon_reason"] = t["why_tier"]
        applied += 1

    with open(stage2_out, "w", encoding="utf-8") as f:
        json.dump(s2, f, ensure_ascii=False, indent=2)

    count = {"today": 0, "think": 0, "later": 0}
    for r in rel:
        count[r.get("horizon", "think")] += 1
    return {
        "已标注": applied,
        "缺标签": len(missing),
        "缺标签编号": missing[:20],
        "分档": count,
        "总数": len(rel),
    }
