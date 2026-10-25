"""
命令行入口。

    ed ingest   <输入> --workdir <目录>        只做转换与切块
    ed stage1  --workdir <目录>               第一段：内容蒸馏（无处境）
    ed stage2  --workdir <目录>               第二段：结合处境匹配
    ed render  --workdir <目录> --out <md>    生成最终文档
    ed verify  --workdir <目录>               校验
    ed run     <输入> --out <md>              全流程

模型不锁死：默认走 `hermes -z`，用宿主当前配置的默认模型。
换模型用 --runner 换实现，见 ed/runner.py。
"""

import argparse
import json
import os
import sys
from typing import List, Optional

from . import workspace
from .context.profile import Context
from .distill.stage1 import run_stage1, verify_stage1
from .distill.stage2 import run_stage2, verify_stage2
from .ingest.base import load
from .render.document import render
from .runner import CommandRunner, FileRunner, HermesRunner, Runner, RunnerError

DEFAULT_MEM_DIR = os.path.expanduser("~/.hermes/memories")

DEPTH_HINT = {
    "quick": "精简。只给最关键的 3-5 条洞察，不要长篇。",
    "standard": "适中。覆盖主要结构与关键洞察，每条说清即可。",
    "deep": "充分展开。有结构的洞察要把步骤写全，重要的案例要讲清过程。",
}


def make_runner(args) -> Runner:
    if args.runner == "hermes":
        return HermesRunner(timeout=args.timeout)
    if args.runner == "command":
        return CommandRunner(template=json.loads(args.command),
                            timeout=args.timeout)
    if args.runner == "file":
        return FileRunner(path=args.command)
    raise ValueError(args.runner)


def load_context(args) -> Context:
    ctx = Context.from_dir(args.mem_dir, include=args.mem_files or None)
    if args.sessions:
        with open(args.sessions, encoding="utf-8") as f:
            data = json.load(f)
        ctx.add_sessions(data if isinstance(data, list) else data.get("sessions", []))
    return ctx


def cmd_ingest(args) -> int:
    raw = args.input
    from .ingest.arxiv import is_arxiv
    if is_arxiv(raw):
        from .ingest.arxiv import load_arxiv
        doc = load_arxiv(raw)
    elif raw.startswith(("http://", "https://")):
        from .ingest.base import load_url
        doc = load_url(raw)
    else:
        doc = load(args.input)
    workspace.save_document(args.workdir, doc)
    print(f"✓ {args.input}")
    print(f"  标题：{doc.source.title or '（无）'}")
    print(f"  字数：{doc.stats.get('有效字数', 0):,} ｜ 块数：{len(doc.blocks)}")
    if doc.source.meta.get("scanned"):
        print("  ⚠ 疑似扫描件")
    return 0


def cmd_stage1(args) -> int:
    doc = workspace.load_document(args.workdir)
    runner = make_runner(args)
    print(f"第一段：{len(doc.blocks)} 块，批大小 {args.batch_size}，并发 {args.workers}")
    stage1, report = run_stage1(
        doc, runner, batch_size=args.batch_size, workers=args.workers,
        progress=lambda m: print("  " + m, flush=True),
    )
    v = verify_stage1(stage1, doc)
    workspace.save_stage1(args.workdir, stage1, {**report, "verify": v})
    print(f"\n洞察 {len(stage1.insights)} ｜ 反例 {len(stage1.counter_examples)} "
          f"｜ 术语 {len(stage1.glossary)}")
    print(f"回执缺失 {report['缺失回执总数']} ｜ "
          f"摘录定位 {v['定位成功']}/{v['总数']} ｜ "
          f"归属错误 {v['归属错误']}")
    if v["无法定位明细"]:
        print("  ✘ 无法定位：")
        for m in v["无法定位明细"][:5]:
            print(f"     {m['id']} {m['标题'][:30]} — {m['原因']}")
    if v["归属错误明细"]:
        print("  ↷ 归属错误（已自动修正）：")
        for m in v["归属错误明细"][:5]:
            print(f"     {m['id']} 块{m['声称块']} → 块{m['实际块']}")
    if v.get("无数值结果"):
        print(f"  ⚠ 标成结果却没数字（{len(v['无数值结果'])} 条，建议改标 claim）：")
        for m in v["无数值结果"][:5]:
            print(f"     {m['id']} {m['标题'][:40]}")
    return 0 if v["通过"] else 2


def cmd_stage2(args) -> int:
    stage1 = workspace.load_stage1(args.workdir)
    ctx = load_context(args)
    runner = make_runner(args)
    print(f"第二段：处境 {ctx.summary()}")
    stage2 = run_stage2(stage1, ctx, runner)
    v = verify_stage2(stage2)
    workspace.save_stage2(args.workdir, stage2)
    print(f"关联 {len(stage2.relevant)} ｜ 依据 {len(stage2.basis)} ｜ "
          f"下一步 {len(stage2.next_actions)}")
    if not v["通过"]:
        print("  ✘ 校验没过：")
        if v["无依据的关联"]:
            print(f"     {v['无依据的关联']} 条关联没有依据")
            for d in v["无依据明细"][:3]:
                print(f"       {d['insight_id']} {d['why']}")
        if v["悬空引用"]:
            print(f"     {v['悬空引用']} 处引用指向不存在的依据")
    else:
        print("  ✓ 每条关联都有依据")
    return 0 if v["通过"] else 2


def cmd_render(args) -> int:
    stage1 = workspace.load_stage1(args.workdir)
    stage2 = None
    p = os.path.join(args.workdir, "stage2.json")
    if os.path.exists(p):
        stage2 = workspace.load_stage2(args.workdir)
    depth_note = DEPTH_HINT.get(args.depth, "")
    md = render(stage1, stage2, title=args.title, source_note=args.source_note,
                depth_note=depth_note)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"✓ {args.out} （{len(md):,} 字符）")
    return 0


def cmd_verify(args) -> int:
    doc = workspace.load_document(args.workdir)
    stage1 = workspace.load_stage1(args.workdir)
    v1 = verify_stage1(stage1, doc)
    print("第一段：", {k: v1[k] for k in ("总数", "定位成功", "无法定位", "归属错误", "通过")})
    ok = v1["通过"]
    p = os.path.join(args.workdir, "stage2.json")
    if os.path.exists(p):
        v2 = verify_stage2(workspace.load_stage2(args.workdir))
        print("第二段：", {k: v2[k] for k in ("关联条数", "无依据的关联", "悬空引用", "通过")})
        ok = ok and v2["通过"]
    return 0 if ok else 2


def cmd_transcript(args) -> int:
    """把视频链接变成逐字稿，并接上普通的 ingest 流程。"""
    from .ingest.video import diagnose_only, to_transcript
    if args.diagnose_only:
        info = diagnose_only(args.url)
        for k, v in info.items():
            print(f"{k}: {v}")
        return 0
    r = to_transcript(args.url, prefer=args.prefer, asr_model=args.asr_model,
                      language=args.language, asr_engine=args.asr_engine)
    print(f"平台：{r.get('platform', '?')}")
    for a in r.get("attempts", []):
        mark = "✔" if a.get("成功") else "✘"
        print(f"  {mark} {a.get('步骤')}：{a.get('细节', '')}")
    if not r.get("ok"):
        print(f"\n失败原因：{r.get('reason_code')} —— {r.get('reason')}")
        print(f"怎么办：{r.get('next_step')}")
        if r.get("detail"):
            print(f"细节：{r['detail'][:300]}")
        return 2
    text = r["text"]
    print(f"✓ 逐字稿 {len(text):,} 字（经由 {r.get('via')}）")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"→ {args.out}")
    return 0


def cmd_plan(args) -> int:
    from .orchestrate import plan
    items = plan(args.workdir, args.batch_size)
    print(f"共 {len(items)} 批\n")
    for it in items:
        print(f"  第 {it['batch']:>2} 批  块 {it['blocks'][0]}–{it['blocks'][-1]}"
              f"  prompt: {it['prompt']}")
        print(f"           返回写到: {it['output']}")
    print(f"\n派完子 Agent 后运行： python3 -m ed collect --workdir {args.workdir}")
    return 0


def cmd_prompt(args) -> int:
    from .orchestrate import batch_dir
    from .distill.stage1 import load_template, make_batches, render_blocks
    from . import workspace
    doc = workspace.load_document(args.workdir)
    batches = make_batches(doc, args.batch_size)
    if not (1 <= args.batch <= len(batches)):
        print(f"没有第 {args.batch} 批（共 {len(batches)} 批）", file=sys.stderr)
        return 1
    system = load_template("stage1_system.md")
    user = render_blocks(batches[args.batch - 1])
    print(f"=== SYSTEM ===\n{system}\n\n=== USER ===\n{user}")
    return 0


def cmd_collect(args) -> int:
    from .orchestrate import collect
    r = collect(args.workdir, args.batch_size, args.max_insights)
    v = r.get("verify", {})
    print(f"批数 {r['批数']} ｜ 回执缺失 {r['缺失回执总数']}")
    print(f"摘录定位 {v.get('定位成功', 0)}/{v.get('总数', 0)} ｜ "
          f"无法定位 {v.get('无法定位', 0)} ｜ 归属错误 {v.get('归属错误', 0)}")
    for m in (v.get("无法定位明细") or [])[:8]:
        print(f"  ✘ {m.get('id')} {str(m.get('标题'))[:36]} — {m.get('原因')}")
    for m in (v.get("归属错误明细") or [])[:8]:
        print(f"  ↷ {m.get('id')} 块{m.get('声称块')} → 块{m.get('实际块')}")
    sel = r.get("筛选", {})
    if sel:
        if sel.get("原始", 0) == 0:
            print("筛选：0 条 —— 没有任何洞察产出")
        else:
            pb = sel.get("每块保留", {})
            print(f"筛选：{sel.get('原始')} → {sel.get('保留')} 条"
                  f"（{sel.get('策略', '')}；每块 {pb.get('最少','?')}–{pb.get('最多','?')} 条，"
                  f"覆盖 {pb.get('覆盖块数','?')} 块）")
            if sel.get("类型分布"):
                print("  类型：" + " ".join(f"{k}{v}" for k, v in sel["类型分布"].items()))
    print(f"→ stage1.json 已写入 {args.workdir}")
    return 0 if v.get("通过") else 2


def cmd_stage2_prompt(args) -> int:
    from .orchestrate import stage2_prompt
    text = stage2_prompt(args.workdir)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"✓ {args.out} （{len(text):,} 字符）")
    else:
        print(text)
    return 0


def cmd_collect2(args) -> int:
    from .orchestrate import collect2
    r = collect2(args.workdir, args.out)
    if "错误" in r:
        print(f"✘ {r['错误']}", file=sys.stderr)
        return 1
    v = r["校验"]
    print(f"关联 {r['关联']} ｜ 依据 {r['依据']}")
    print(f"无依据 {v['无依据的关联']} ｜ 悬空引用 {v['悬空引用']} ｜ "
          f"{'✓ 通过' if v['通过'] else '✘ 未通过'}")
    for d in v.get("无依据明细", [])[:5]:
        print(f"  ✘ {d}")
    return 0 if v["通过"] else 2


def cmd_horizon_prompt(args) -> int:
    from .distill.horizon import horizon_prompt
    text = horizon_prompt(args.workdir)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"✓ {args.out} （{len(text):,} 字符）")
    else:
        print(text)
    return 0


def cmd_collect_horizon(args) -> int:
    from .distill.horizon import collect_horizon
    r = collect_horizon(args.workdir)
    if "错误" in r:
        print(f"✘ {r['错误']}", file=sys.stderr)
        return 1
    c = r["分档"]
    print(f"已标注 {r['已标注']} / {r['总数']} ｜ 缺标签 {r['缺标签']}")
    print(f"今天能用 {c['today']} ｜ 值得想 {c['think']} ｜ 存着 {c['later']}")
    if r["缺标签编号"]:
        print(f"  缺标签的编号：{r['缺标签编号']}")
    return 0


def cmd_metadata(args) -> int:
    from .ingest.metadata import metadata, render_brief
    r = metadata(args.url)
    if not r.get("ok"):
        print(f"✘ {r.get('reason', '失败')}", file=sys.stderr)
        return 1
    brief = render_brief(r)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(brief)
        print(f"✓ {args.out}")
    else:
        print(brief)
    return 0


def cmd_frames(args) -> int:
    from .ingest.frames import grab_frames
    r = grab_frames(args.url, args.at, window=args.window,
                    fps=args.fps, scene_detect=args.scene,
                    max_frames=args.max_frames,
                    outdir=args.outdir)
    if not r.get("ok"):
        print(f"✘ {r.get('reason', '失败')}", file=sys.stderr)
        return 1
    print(f"✓ 窗口 {r['window']['start']}–{r['window']['end']}，抽 {r['count']} 帧：")
    for f in r["frames"]:
        print(f"  {f}")
    return 0


def cmd_check_skill(args) -> int:
    from .verify.skill_meta import check_skill
    r = check_skill(args.path, kind=args.kind)
    flag = "✔" if r["通过"] else "✘"
    print(f"{flag} {r['目录']}（{r['形态']}形态）"
          f" description {r.get('description_字数', '?')} 字")
    if r.get("说明"):
        print(f"  {r['说明']}")
    for p in r["问题"]:
        print(f"  ⚠ {p}")
    return 0 if r["通过"] else 2


def cmd_run(args) -> int:
    rc = cmd_ingest(args)
    if rc:
        return rc
    rc = cmd_stage1(args)
    if rc:
        return rc
    rc = cmd_stage2(args)
    if rc:
        print("⚠ 第二段没过校验，仍继续渲染（问题会记在文档里）")
    return cmd_render(args)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="ed", description="万物皆可蒸")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--runner", default="hermes",
                       choices=["hermes", "command", "file"])
        p.add_argument("--command", default=None,
                       help="command runner 的命令模板（JSON 数组），file runner 的结果文件路径")
        p.add_argument("--timeout", type=int, default=900)
        p.add_argument("--batch-size", type=int, default=5)
        p.add_argument("--workers", type=int, default=4,
                       help="批次并发数，越大越快但占用更多")

    p = sub.add_parser("ingest", help="转换与切块")
    p.add_argument("input")
    p.add_argument("--workdir", required=True)
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("stage1", help="第一段：内容蒸馏")
    p.add_argument("--workdir", required=True)
    common(p)
    p.set_defaults(func=cmd_stage1)

    p = sub.add_parser("stage2", help="第二段：结合处境")
    p.add_argument("--workdir", required=True)
    p.add_argument("--mem-dir", default=DEFAULT_MEM_DIR)
    p.add_argument("--mem-files", nargs="*", default=None,
                   help="纳入哪些记忆文件，默认 USER.md PROJECTS.md")
    p.add_argument("--sessions", default=None,
                   help="按需检索到的会话片段（JSON 文件）")
    common(p)
    p.set_defaults(func=cmd_stage2)

    p = sub.add_parser("render", help="生成最终文档")
    p.add_argument("--workdir", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--title", default="")
    p.add_argument("--source-note", default="")
    p.add_argument("--depth", default="standard", choices=list(DEPTH_HINT))
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("verify", help="校验")
    p.add_argument("--workdir", required=True)
    p.set_defaults(func=cmd_verify)

    # ---- 编排者模式 ----
    p = sub.add_parser("plan", help="列出批次并生成 prompt（编排者模式）")
    p.add_argument("--workdir", required=True)
    p.add_argument("--batch-size", type=int, default=5)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("prompt", help="输出某一批的完整 prompt")
    p.add_argument("--workdir", required=True)
    p.add_argument("--batch", type=int, required=True)
    p.add_argument("--batch-size", type=int, default=5)
    p.set_defaults(func=cmd_prompt)

    p = sub.add_parser("collect", help="收各批返回，合并校验")
    p.add_argument("--workdir", required=True)
    p.add_argument("--batch-size", type=int, default=5)
    p.add_argument("--max-insights", type=int, default=200,
                   help="送入第二段的洞察上限（默认 200）")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("stage2-prompt", help="输出第二段 prompt")
    p.add_argument("--workdir", required=True)
    p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_stage2_prompt)

    p = sub.add_parser("collect2", help="收第二段返回")
    p.add_argument("--workdir", required=True)
    p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_collect2)

    p = sub.add_parser("transcript", help="视频链接 → 逐字稿")
    p.add_argument("url")
    p.add_argument("--out", default=None, help="逐字稿输出路径")
    p.add_argument("--prefer", default="auto", choices=["auto", "subtitle", "audio"])
    p.add_argument("--asr-model", default="whisper-large-v3-turbo",
                   help="ASR 模型（默认 Groq 云端 whisper-large-v3-turbo）")
    p.add_argument("--asr-engine", default="auto", choices=["auto", "cloud", "local"],
                   help="auto=云端优先(需GROQ_API_KEY)；local=本地faster-whisper(需自装)")
    p.add_argument("--language", default=None,
                   help="强制指定语言（zh/en/...）；默认让模型自动检测")
    p.add_argument("--diagnose-only", action="store_true",
                   help="只诊断能不能处理，不实际抓取")
    p.set_defaults(func=cmd_transcript)

    p = sub.add_parser("horizon-prompt", help="给已有第二段结果补「时间距离」分档")
    p.add_argument("--workdir", required=True)
    p.add_argument("--out")
    p.set_defaults(func=cmd_horizon_prompt)

    p = sub.add_parser("collect-horizon", help="收分档结果并合并进第二段")
    p.add_argument("--workdir", required=True)
    p.set_defaults(func=cmd_collect_horizon)

    p = sub.add_parser("metadata", help="提取视频元数据+章节（Level 1）")
    p.add_argument("url")
    p.add_argument("--out")
    p.set_defaults(func=cmd_metadata)

    p = sub.add_parser("frames", help="按需局部抽帧（Level 3）")
    p.add_argument("url")
    p.add_argument("--at", required=True, help="时间戳 00:12:30 或秒数")
    p.add_argument("--window", type=float, default=30.0, help="抓取窗口秒数")
    p.add_argument("--fps", type=float, default=0.5, help="固定抽帧率")
    p.add_argument("--scene", action="store_true", help="用场景变化检测抽帧")
    p.add_argument("--max-frames", type=int, default=12)
    p.add_argument("--outdir")
    p.set_defaults(func=cmd_frames)

    p = sub.add_parser("check-skill", help="检查一个 skill 的元信息合规性")
    p.add_argument("path", help="skill 目录")
    p.add_argument("--kind", default="manual", choices=["manual", "package"],
                   help="manual=编排手册形态；package=AI 可调用的技能包形态")
    p.set_defaults(func=cmd_check_skill)

    p = sub.add_parser("run", help="全流程")
    p.add_argument("input")
    p.add_argument("--workdir", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--title", default="")
    p.add_argument("--source-note", default="")
    p.add_argument("--depth", default="standard", choices=list(DEPTH_HINT))
    p.add_argument("--mem-dir", default=DEFAULT_MEM_DIR)
    p.add_argument("--mem-files", nargs="*", default=None)
    p.add_argument("--sessions", default=None)
    common(p)
    p.set_defaults(func=cmd_run)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except RunnerError as e:
        print(f"✘ 模型调用失败：{e}", file=sys.stderr)
        return 3
    except Exception as e:
        print(f"✘ {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
