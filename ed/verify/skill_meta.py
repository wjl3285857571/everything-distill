#!/usr/bin/env python3
"""
meta_check.py — 第 6 步的元信息闸门。

SKILL.md 第 6 步要求逐个打开每个 SKILL.md 检查四件事：
  1. 第一行是不是三个减号，且前面没有任何字符
  2. 有没有 name，且跟所在文件夹名完全一致
  3. name 是不是小写英文/拼音加连字符（中文、空格、下划线、大写都不合规）
  4. 有没有 description，长度是不是落在 60 到 120 字之间

「这道闸门不能跳过。下面的盲测本来就是拿 description 来测的——
没有 description，测试无从测起，而你很可能会在没察觉的情况下
测了个寂寞，最后交付一批永远不会被触发的文件。」

这个脚本把四项检查机械化，并额外检查六段结构是否齐全。

用法:
    python3 meta_check.py <技能包目录>
    python3 meta_check.py runs/蒸馏_穷查理宝典/技能包 --json
"""

import argparse
import json
import os
import re
import sys

NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

REQUIRED_SECTIONS = [
    "什么时候用它",
    "核心判断",
    "怎么做",
    "判断标准",
    "什么时候不该用它",
    "出处",
]


def parse_front_matter(text: str):
    """返回 (front_matter_dict, body, 问题列表)"""
    problems = []
    if not text.startswith("---"):
        problems.append("第一行不是 ---（前面可能有空行/BOM/标题）")
        return {}, text, problems
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", text, re.S)
    if not m:
        problems.append("front matter 没有正确闭合（缺少结尾的 ---）")
        return {}, text, problems
    fm_raw = m.group(1)
    body = text[m.end():]
    fm = {}
    for line in fm_raw.split("\n"):
        if not line.strip():
            continue
        if ":" not in line:
            problems.append(f"front matter 有无法解析的行：{line[:40]}")
            continue
        k, v = line.split(":", 1)
        fm[k.strip()] = v.strip()
    return fm, body, problems


def char_len(s: str) -> int:
    """description 的「字数」按中文字符计（中文 1 字，英文单词按 1 计）。"""
    s = s.strip()
    cjk = len(re.findall(r"[\u4e00-\u9fff]", s))
    latin_words = len(re.findall(r"[A-Za-z]+", s))
    digits = len(re.findall(r"\d+", s))
    # 标点不计
    return cjk + latin_words + digits


def check_skill(skill_dir: str, kind: str = "package") -> dict:
    """检查一个技能。

    kind:
      "package" —— AI 可调用的技能包，必须是六段结构
                 （什么时候用它 / 核心判断 / 怎么做 / 判断标准 /
                   什么时候不该用它 / 出处）
      "manual"  —— 给人看的编排手册，只要 front matter 合规
                 （六段结构对它不适用，强制反而是误报）
    """
    name_dir = os.path.basename(skill_dir.rstrip("/"))
    path = os.path.join(skill_dir, "SKILL.md")
    result = {
        "目录": name_dir,
        "路径": path,
        "形态": kind,
        "通过": False,
        "问题": [],
    }
    if not os.path.exists(path):
        result["问题"].append("缺 SKILL.md")
        return result

    with open(path, encoding="utf-8") as f:
        text = f.read()

    # 检查 1 + 解析 front matter
    fm, body, problems = parse_front_matter(text)
    result["问题"] += problems

    # 检查 2：name 存在且等于文件夹名
    name = fm.get("name")
    result["name"] = name
    if not name:
        result["问题"].append("缺 name 字段")
    elif name != name_dir:
        result["问题"].append(f"name「{name}」≠ 文件夹名「{name_dir}」")

    # 检查 3：name 格式
    if name and not NAME_RE.match(name):
        result["问题"].append(
            f"name「{name}」不合规：只能小写英文/数字，用连字符连接"
        )
    if name and len(name) > 64:
        result["问题"].append(f"name 长度 {len(name)} > 64")

    # 检查 4：description 存在 + 长度 60–120
    desc = fm.get("description", "")
    dlen = char_len(desc)
    result["description_字数"] = dlen
    result["description"] = desc
    if not desc:
        result["问题"].append("缺 description 字段")
    elif not (60 <= dlen <= 120):
        result["问题"].append(f"description 字数 {dlen} 不在 60–120 区间")

    # description 必须写明「什么情况不要用」
    if desc and not re.search(r"(不要用|不该用|别用|不适用于|不要触发|不适用)", desc):
        result["问题"].append("description 没写明「什么情况下不要用它」")

    # 额外：六段结构（只对技能包形态要求）
    missing = [s for s in REQUIRED_SECTIONS if s not in body]
    result["缺失章节"] = missing
    if missing and kind == "package":
        result["问题"].append(f"缺章节：{'、'.join(missing)}")
    elif missing:
        result["说明"] = "手册形态，不要求六段结构"

    # 「什么时候不该用它」不能写「无」（技能包形态才检查）
    m = re.search(r"##\s*什么时候不该用它\s*\n+(.*?)(?=\n##\s|\Z)", body, re.S)
    if m and kind == "package":
        content = m.group(1).strip()
        if content in ("无", "无。", "暂无", "N/A", ""):
            result["问题"].append("「什么时候不该用它」写成了「无」")

    # 额外：不该出现的字段
    for bad in ("allowed-tools", "version:", "author:"):
        if re.search(rf"^{re.escape(bad)}", text, re.M):
            result["问题"].append(f"出现了不该写的字段：{bad}")

    result["通过"] = not result["问题"]
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("packagedir")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    root = os.path.abspath(args.packagedir)
    if not os.path.isdir(root):
        print(f"✘ 目录不存在：{root}", file=sys.stderr)
        sys.exit(1)

    dirs = sorted(
        os.path.join(root, d)
        for d in os.listdir(root)
        if os.path.isdir(os.path.join(root, d)) and not d.startswith((".", "_"))
    )
    if not dirs:
        print("✘ 没有找到技能目录", file=sys.stderr)
        sys.exit(1)

    results = [check_skill(d) for d in dirs]
    passed = sum(1 for r in results if r["通过"])

    if args.json:
        print(json.dumps({"技能数": len(results), "通过": passed, "明细": results},
                         ensure_ascii=False, indent=2))
        return

    print("=" * 64)
    print(f"元信息闸门：{passed}/{len(results)} 通过")
    print("=" * 64)
    for r in results:
        flag = "✔" if r["通过"] else "✘"
        print(f"\n{flag} {r['目录']}  (description {r.get('description_字数', '?')} 字)")
        if r.get("description"):
            print(f"   {r['description'][:78]}")
        for p in r["问题"]:
            print(f"   ⚠ {p}")
    if passed < len(results):
        print(f"\n合计 {len(results) - passed} 个技能需修，改完才能做盲测。")
    else:
        print("\n全部通过，可以进入盲测。")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
