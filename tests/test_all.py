#!/usr/bin/env python3
"""
回归测试。运行：python3 tests/test_all.py
"""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ed.context.profile import Context, extract_quotes  # noqa: E402
from ed.distill.stage1 import (anchor_hit, claimed_count,  # noqa: E402
                               normalize, parse_receipts, verify_stage1)
from ed.distill.stage2 import verify_stage2  # noqa: E402
from ed.ingest.base import (Block, Document, build_blocks,  # noqa: E402
                            effective_words, html_to_text, split_paragraphs)
from ed.render.document import render  # noqa: E402
from ed.runner import parse_json_loose  # noqa: E402
from ed.schema import Basis, Insight, Locator, Source, Stage1, Stage2  # noqa: E402


# ---------------------------------------------------------------- 输入层


class TestIngest(unittest.TestCase):
    def test_effective_words(self):
        s = effective_words("中文五个字 hello world 123")
        self.assertEqual(s["汉字"], 5)
        self.assertEqual(s["西文词"], 2)
        self.assertEqual(s["数字串"], 1)
        self.assertEqual(s["有效字数"], 8)

    def test_block_tags_break_lines_inline_does_not(self):
        raw = "<p>第一段</p><p>第二段有<b>加粗</b>字</p>"
        t = html_to_text(raw)
        self.assertIn("第一段", t)
        self.assertIn("第二段有加粗字", t)   # 内联标签不断行
        self.assertNotIn("第一段 第二段", t)  # 块级标签断行

    def test_css_does_not_leak_into_text(self):
        raw = ("<html><head><style>@page {padding: 0pt; margin:0pt}"
               "body { text-align: center; }</style></head>"
               "<body><div><p>正文内容</p></div></body></html>")
        t = html_to_text(raw)
        self.assertIn("正文内容", t)
        self.assertNotIn("@page", t)
        self.assertNotIn("padding", t)

    def test_empty_html_fallback_does_not_leak_css(self):
        """封面页整页是 style+svg，extractor 提取为空后会走兜底路径。"""
        raw = ("<html><head><style type='text/css'>@page {padding: 0pt; margin:0pt}"
               "</style></head><body><div><svg><rect/></svg></div></body></html>")
        t = html_to_text(raw)
        self.assertNotIn("@page", t)
        self.assertNotIn("padding", t)

    def test_split_paragraphs_breaks_embedded_heading(self):
        raw = "正文一段。\n# 标题\n正文二段。"
        paras = split_paragraphs(raw)
        self.assertIn("# 标题", paras)

    def test_build_blocks_does_not_split_on_every_heading(self):
        """标题多的书不能切成一堆碎块。"""
        text = "\n\n".join(
            f"# 第{i}节\n\n" + "内容。" * 200 for i in range(1, 30)
        )
        blocks = build_blocks(text, target_words=8000)
        self.assertLess(len(blocks), 30)

    def test_pdf_paper_headings_promoted(self):
        """论文 PDF 的章节行没有标记，要认出来提升成标题，切块才能按章断开。"""
        from ed.ingest.base import promote_pdf_headings
        text = ("Abstract\n\nSome text.\n\nIntroduction\n\nMore text.\n\n"
                "3 Model Architecture\n\nDetails here.\n\n3.1 Encoder\n\nEncoder.\n\n"
                "Conclusion\n\nDone.")
        out, is_paper = promote_pdf_headings(text)
        self.assertTrue(is_paper)
        self.assertIn("# Abstract", out)
        self.assertIn("# Introduction", out)
        self.assertIn("# 3 Model Architecture", out)
        self.assertIn("## 3.1 Encoder", out)
        self.assertIn("# Conclusion", out)

    def test_pdf_non_paper_untouched(self):
        """不是论文就原样返回，不能把正文行误判成标题。"""
        from ed.ingest.base import promote_pdf_headings
        text = "第一章 开始\n\n这是小说正文。\n\n第二章 继续\n\n又是正文。"
        out, is_paper = promote_pdf_headings(text)
        self.assertFalse(is_paper)
        self.assertEqual(out, text)

    def test_pdf_needs_at_least_three_sections(self):
        """只命中一两个章节词时不动手，避免误伤。"""
        from ed.ingest.base import promote_pdf_headings
        text = "Introduction\n\n正文。\n\n随便一段话。"
        out, is_paper = promote_pdf_headings(text)
        self.assertFalse(is_paper)
        self.assertEqual(out, text)

    def test_build_blocks_no_cross_h1(self):
        text = "# 第一章\n\n" + "甲。" * 500 + "\n\n# 第二章\n\n" + "乙。" * 500
        blocks = build_blocks(text, target_words=200)
        for b in blocks:
            heads = [ln for ln in b.text.split("\n") if ln.startswith("# ")]
            self.assertLessEqual(len(heads), 1, f"块跨了章节：{heads}")


# ---------------------------------------------------------------- 归一化与锚点


class TestProvenance(unittest.TestCase):
    def test_normalize_strips_cjk_brackets(self):
        """模型引用时自己加的「」《》会让锚点错位一位。"""
        a = normalize("「当几个模型联合起来」")
        b = normalize("当几个模型联合起来")
        self.assertEqual(a, b)

    def test_normalize_folds_punctuation(self):
        self.assertEqual(normalize("你好，世界。"), normalize("你好,世界."))

    def test_anchor_hit_exact(self):
        self.assertTrue(anchor_hit("这是一句原文", "前面有 这是一句原文 后面"))

    def test_anchor_hit_with_bracket(self):
        self.assertTrue(anchor_hit("「这是一句原文」", "前面有 这是一句原文 后面"))

    def test_anchor_hit_rejects_fabrication(self):
        self.assertFalse(anchor_hit("完全不存在的一段话", "这是真实的原文内容"))

    def test_parse_receipts(self):
        r = parse_receipts("第 12 块：找到 3 条\n第 13 块：无\n")
        self.assertEqual(r[12], "找到 3 条")
        self.assertEqual(r[13], "无")

    def test_claimed_count(self):
        self.assertEqual(claimed_count("找到 3 条"), 3)
        self.assertEqual(claimed_count("无"), 0)
        self.assertIsNone(claimed_count("不知道"))


# ---------------------------------------------------------------- 第一段校验


def make_doc(text: str) -> Document:
    return Document(text=text, blocks=build_blocks(text),
                    source=Source(kind="text", origin="t", title="t"))


class TestStage1Verify(unittest.TestCase):
    def test_detects_fabricated_excerpt(self):
        doc = make_doc("这是真实的原文内容，讲了一件事。")
        ins = Insight(id="I1", claim="瞎编的", support="原文里根本没有这段话",
                      locator=Locator(block=1))
        v = verify_stage1(Stage1(source=doc.source, insights=[ins]), doc)
        self.assertFalse(v["通过"])
        self.assertEqual(v["无法定位"], 1)

    def test_detects_missing_excerpt(self):
        """空摘录比错摘录更危险 —— 看起来「没什么可查」。"""
        doc = make_doc("这是真实的原文内容。")
        ins = Insight(id="I1", claim="没有摘录", support="",
                      locator=Locator(block=1))
        v = verify_stage1(Stage1(source=doc.source, insights=[ins]), doc)
        self.assertFalse(v["通过"])

    def test_detects_misattribution(self):
        """内容真实但归错块 —— 会污染最终产物的「出处」。"""
        text = "# 甲\n\n这是甲部分的原文内容。\n\n# 乙\n\n这是乙部分的另一段原文。"
        doc = make_doc(text)
        blocks = doc.blocks
        self.assertGreaterEqual(len(blocks), 2)
        ins = Insight(id="I1", claim="x",
                      support="这是乙部分的另一段原文。",
                      locator=Locator(block=blocks[0].index))
        v = verify_stage1(Stage1(source=doc.source, insights=[ins]), doc)
        self.assertFalse(v["通过"])
        self.assertEqual(v["归属错误"], 1)

    def test_honest_passes(self):
        doc = make_doc("这是真实的原文内容，讲了一件事。")
        ins = Insight(id="I1", claim="真话", support="这是真实的原文内容",
                      locator=Locator(block=1))
        v = verify_stage1(Stage1(source=doc.source, insights=[ins]), doc)
        self.assertTrue(v["通过"], v)


# ---------------------------------------------------------------- 第二段校验


class TestStage2Verify(unittest.TestCase):
    def test_relevance_without_basis_fails(self):
        """防拍马屁的核心：没有依据的关联不许存在。"""
        s2 = Stage2(relevant=[{"insight_id": "I1", "why": "跟你有关", "basis": []}],
                    basis=[])
        v = verify_stage2(s2)
        self.assertFalse(v["通过"])
        self.assertEqual(v["无依据的关联"], 1)

    def test_dangling_basis_reference_fails(self):
        s2 = Stage2(relevant=[{"insight_id": "I1", "why": "x", "basis": [7]}],
                    basis=[Basis(id=1, kind="memory", source="USER.md", quote="q")])
        v = verify_stage2(s2)
        self.assertFalse(v["通过"])
        self.assertEqual(v["悬空引用"], 1)

    def test_basis_without_quote_fails(self):
        s2 = Stage2(relevant=[{"insight_id": "I1", "why": "x", "basis": [1]}],
                    basis=[Basis(id=1, kind="memory", source="USER.md", quote="")])
        v = verify_stage2(s2)
        self.assertFalse(v["通过"])

    def test_valid_passes(self):
        s2 = Stage2(
            relevant=[{"insight_id": "I1", "why": "x", "basis": [1]}],
            basis=[Basis(id=1, kind="memory", source="USER.md", quote="原句")],
        )
        v = verify_stage2(s2)
        self.assertTrue(v["通过"], v)


# ---------------------------------------------------------------- 处境


class TestContext(unittest.TestCase):
    def test_default_files_only(self):
        with tempfile.TemporaryDirectory() as d:
            for n in ("USER.md", "PROJECTS.md", "SOUL.md", "MEMORY.md", "TECH.md"):
                with open(os.path.join(d, n), "w") as f:
                    f.write(f"内容-{n}")
            ctx = Context.from_dir(d)
            self.assertIn("USER.md", ctx.memories)
            self.assertIn("PROJECTS.md", ctx.memories)
            self.assertNotIn("SOUL.md", ctx.memories)     # 助理的性格，不是读者
            self.assertNotIn("MEMORY.md", ctx.memories)
            self.assertNotIn("TECH.md", ctx.memories)    # 默认不进

    def test_optional_files_can_be_included(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "TECH.md"), "w") as f:
                f.write("技术内容")
            ctx = Context.from_dir(d, include=["TECH.md"])
            self.assertIn("TECH.md", ctx.memories)

    def test_render_includes_sessions(self):
        ctx = Context(memories={"USER.md": "abc"},
                      sessions=[{"ref": "10月会话", "quote": "他提到过求职"}])
        r = ctx.render()
        self.assertIn("10月会话", r)
        self.assertIn("他提到过求职", r)

    def test_extract_quotes_returns_original_sentences(self):
        text = "他在找工作。他想学英语。上一句。"
        q = extract_quotes(text, ["找工作"], limit=2)
        self.assertTrue(q)
        self.assertIn("找工作", q[0])


# ---------------------------------------------------------------- 渲染


class TestRender(unittest.TestCase):
    def test_sections_in_order(self):
        """由浅入深：概览 → 怎么切进去 → 内容本身 → 跟你有关。"""
        s1 = Stage1(source=Source(kind="text", origin="t", title="测试书"),
                    overview="这是概览。",
                    insights=[Insight(id="I1", claim="一条洞察",
                                      support="原文", locator=Locator(block=1))])
        s2 = Stage2(entry_angles=["从角度一切入"],
                    relevant=[{"insight_id": "I1", "why": "跟你有关", "basis": [1]}],
                    next_actions=["今天做一件事"],
                    basis=[Basis(id=1, kind="memory", source="USER.md", quote="原句")],
                    insights=s1.insights)
        md = render(s1, s2)
        i1 = md.index("## 一、这内容在讲什么")
        i2 = md.index("## 二、怎么切进去")
        i3 = md.index("## 三、内容本身")
        i4 = md.index("## 四、跟你有关")
        i5 = md.index("## 依据")
        self.assertLess(i1, i2)
        self.assertLess(i2, i3)
        self.assertLess(i3, i4)
        self.assertLess(i4, i5)

    def test_stage2_empty_claim_falls_back_to_stage1(self):
        """第二段只回填 relevance、正文留空时，渲染必须用第一段的条目，不能出空条目。"""
        s1 = Stage1(source=Source(kind="text", origin="t", title="测试书"),
                    insights=[Insight(id="I1", claim="一条洞察",
                                      support="原文片段", locator=Locator(block=1))])
        hollow = Insight(id="I1", claim="", type="other",
                         relevance={"level": "high", "note": "跟你有关"})
        s2 = Stage2(insights=[hollow])
        md = render(s1, s2)
        self.assertIn("一条洞察", md)
        self.assertIn("原文片段", md)
        self.assertNotIn("**★ **", md)

    def test_citations_are_anchored(self):
        s1 = Stage1(source=Source(kind="text", origin="t", title="t"))
        s2 = Stage2(relevant=[{"insight_id": "I1", "why": "w", "basis": [1]}],
                    basis=[Basis(id=1, kind="memory", source="USER.md", quote="原句")])
        md = render(s1, s2)
        self.assertIn("[1](#ed-1)", md)
        self.assertIn('<a id="ed-1"></a>', md)
        self.assertIn("### [1]", md)

    def test_provenance_quote_present(self):
        s1 = Stage1(source=Source(kind="text", origin="t", title="t"),
                    insights=[Insight(id="I1", claim="c", support="一段原文摘录",
                                      locator=Locator(block=1))])
        md = render(s1, None)
        self.assertIn("一段原文摘录", md)


# ---------------------------------------------------------------- JSON 解析


class TestSelect(unittest.TestCase):
    """第二段之前的机械筛选。"""

    def _mk(self, n, block=1, types=None):
        from ed.schema import Insight, Locator
        types = types or ["other"] * n
        return [Insight(id=f"I{i}", claim=f"c{i}", support="s",
                        locator=Locator(block=block), type=types[i % len(types)])
                for i in range(n)]

    def test_below_limit_keeps_all(self):
        from ed.schema import Source, Stage1
        from ed.distill.select import select
        s1 = Stage1(source=Source(kind="t", origin="o", title="t"), insights=self._mk(5))
        r = select(s1, max_total=200)
        self.assertEqual(r["保留"] if "保留" in r else len(r["insights"]), 5)
        self.assertEqual(r["dropped"], 0)

    def test_over_limit_respects_max(self):
        from ed.schema import Source, Stage1
        from ed.distill.select import select
        s1 = Stage1(source=Source(kind="t", origin="o", title="t"), insights=self._mk(500))
        r = select(s1, max_total=100)
        self.assertLessEqual(len(r["insights"]), 100)

    def test_every_block_gets_at_least_one(self):
        """保住覆盖 —— 不能为了控总量把某些块全砍掉。"""
        from ed.schema import Source, Stage1
        from ed.distill.select import select
        items = []
        for blk in range(1, 21):
            items.extend(self._mk(10, block=blk))
        s1 = Stage1(source=Source(kind="t", origin="o", title="t"), insights=items)
        r = select(s1, max_total=40, min_per_block=1)
        blocks = {i.locator.block for i in r["insights"]}
        self.assertEqual(blocks, set(range(1, 21)), "有块被完全砍掉了")

    def test_priority_prefers_principle_over_case(self):
        from ed.schema import Insight, Locator, Source, Stage1
        from ed.distill.select import select
        items = [
            Insight(id="A", claim="case", support="s", locator=Locator(block=1), type="case"),
            Insight(id="B", claim="principle", support="s", locator=Locator(block=1), type="principle"),
        ]
        s1 = Stage1(source=Source(kind="t", origin="o", title="t"), insights=items)
        r = select(s1, max_total=1)
        self.assertEqual(r["insights"][0].id, "B")

    def test_output_sorted_by_block(self):
        from ed.schema import Insight, Locator, Source, Stage1
        from ed.distill.select import select
        items = [
            Insight(id="X", claim="c", support="s", locator=Locator(block=50), type="principle"),
            Insight(id="Y", claim="c", support="s", locator=Locator(block=3), type="case"),
        ]
        s1 = Stage1(source=Source(kind="t", origin="o", title="t"), insights=items)
        r = select(s1, max_total=200)
        self.assertEqual([i.locator.block for i in r["insights"]], [3, 50])


class TestPackageLayout(unittest.TestCase):
    """相对导入不能越级。

    这类错误只在 `python3 -m ed` 时暴露（直接 import 反而正常），
    所以必须用测试守住 —— 之前就因为 workspace.py 里两处 `..` 越级
    导致 CLI 完全跑不起来。
    """

    def test_relative_imports_have_exact_depth(self):
        """相对导入的点数不能超过文件层级（越顶会炸），兄弟导入（层级-1 点）合法。

        模块 `ed/ingest/video.py`（层级 2）里：
          from .invidious import x  → ed.ingest.invidious（同包兄弟，1 点，合法）
          from ..schema import x    → ed.schema（上一级，2 点，合法）
          from ...x import x        → 越过 ed 顶层，运行时 ValueError，必须拦下
        欠级不会炸（只是引到别的包），不在静态检查范围。
        """
        import re
        bad = []
        ed_root = os.path.join(ROOT, "ed")
        for root, dirs, files in os.walk(ed_root):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in files:
                if not f.endswith(".py"):
                    continue
                p = os.path.join(root, f)
                # ed/ 下第 1 层用 "."，第 2 层用 ".."，以此类推
                rel = os.path.relpath(p, ed_root)
                depth = rel.count(os.sep) + 1
                try:
                    with open(p, encoding="utf-8") as fh:
                        lines = fh.read().split("\n")
                except OSError:
                    continue
                for i, ln in enumerate(lines, 1):
                    m = re.match(r"\s*from (\.+)([\w\.]*) import", ln)
                    if not m:
                        continue
                    dots = len(m.group(1))
                    if dots > depth:
                        bad.append(f"{rel}:{i} 越级（{dots} 点 vs 层级 {depth}）"
                                   f" {ln.strip()}")
        self.assertEqual(bad, [], "相对导入层级不对：\n" + "\n".join(bad))

    def test_cli_imports_cleanly(self):
        """以包的方式导入（等同 python3 -m ed 的加载路径）。"""
        import importlib
        importlib.import_module("ed.cli")
        importlib.import_module("ed.workspace")
        importlib.import_module("ed.distill.stage1")
        importlib.import_module("ed.distill.stage2")
        importlib.import_module("ed.render.document")
        importlib.import_module("ed.ingest.base")
        importlib.import_module("ed.ingest.video")


class TestRunner(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(parse_json_loose('{"a": 1}')["a"], 1)

    def test_markdown_fenced(self):
        self.assertEqual(parse_json_loose('```json\n{"a": 2}\n```')["a"], 2)

    def test_embedded_in_prose(self):
        self.assertEqual(parse_json_loose('好的，结果如下：{"a": 3} 以上。')["a"], 3)

    def test_raises_on_garbage(self):
        with self.assertRaises(Exception):
            parse_json_loose("完全不是 JSON")


class TestTranscriptCleaner(unittest.TestCase):
    """[HH:MM:SS] 清洗合并器。"""

    def test_punctuated_merges_by_sentence(self):
        from ed.ingest.transcript import parse_cues, merge_to_lines, format_lines
        cues = [(0.0, 2.0, "大家好。"), (2.5, 5.0, "今天讲一个"),
                (5.2, 8.0, "重要的话题。"), (8.5, 11.0, "第二句开头")]
        lines = merge_to_lines(cues)
        text = format_lines(lines)
        self.assertIn("[00:00:00]", text)
        # 句子优先：标点处断行
        self.assertTrue(lines[0]["text"].endswith("。"))

    def test_no_punct_merges_to_target_span(self):
        from ed.ingest.transcript import parse_cues, merge_to_lines
        cues = [(float(i), float(i) + 2.0, f"词块{i}") for i in range(12)]
        lines = merge_to_lines(cues, target_span=8.0, max_span=10.0)
        for l in lines:
            self.assertLessEqual(l["start"] + 10.0, 26.0, "行跨度不能超 max_span")

    def test_never_splits_inside_cue(self):
        """无标点 Cue 不许在内部截断。"""
        from ed.ingest.transcript import merge_to_lines
        cues = [(0.0, 6.0, "这是一个完整语义块不能被截断"),
                (6.0, 12.0, "第二个块也保持完整"),
                (12.0, 18.0, "第三个块同样")]
        lines = merge_to_lines(cues, target_span=4.0, max_span=10.0)
        texts = " ".join(l["text"] for l in lines)
        for full in ("这是一个完整语义块不能被截断", "第二个块也保持完整",
                     "第三个块同样"):
            self.assertIn(full, texts)

    def test_rolling_dupes_removed(self):
        from ed.ingest.transcript import _strip_rolling_dupes
        cues = [(0.0, 2.0, "你好欢迎"), (2.0, 4.0, "你好欢迎来到"),
                (4.0, 6.0, "你好欢迎来到本期节目")]
        out = _strip_rolling_dupes(cues)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][2], "你好欢迎来到本期节目")

    def test_inline_tags_and_noise_stripped(self):
        from ed.ingest.transcript import clean_transcript
        raw = """1
00:00:01,000 --> 00:00:03,000
<c>大家好<00:00:02.000><c>今天</c>

2
00:00:03,500 --> 00:00:05,000
♪
"""
        r = clean_transcript(raw)
        self.assertTrue(r["ok"])
        self.assertNotIn("<c>", r["text"])
        self.assertNotIn("♪", r["text"])

    def test_timestamp_format(self):
        from ed.ingest.transcript import clean_transcript
        r = clean_transcript("1\n00:01:05,000 --> 00:01:08,000\n测试时间格式\n")
        self.assertIn("[00:01:05]", r["text"])

    def test_compression_stats(self):
        from ed.ingest.transcript import clean_transcript
        cues = "\n".join(
            f"{i}\n00:{i//60:02d}:{i%60:02d},000 --> 00:{i//60:02d}:{i%60:02d},500\n重复文本{i}\n"
            for i in range(20))
        r = clean_transcript(cues)
        self.assertIn("压缩比", r["stats"])


class TestArxiv(unittest.TestCase):
    """arXiv 论文接入：ID 识别 + LaTeXML 解析（不联网）。"""

    # 一段最小 LaTeXML 片段，覆盖真实页面里会用到的结构
    HTML = """<html><body>
    <dialog><h5>Report GitHub Issue</h5><p>噪声</p></dialog>
    <div class="ltx_page_content"><article class="ltx_document ltx_authors_1line">
      <h1 class="ltx_title ltx_title_document">A Paper Title</h1>
      <div class="ltx_authors"><span class="ltx_personname">Alice</span>
        <span class="ltx_note">thanks: funded by someone</span></div>
      <div class="ltx_abstract"><h6 class="ltx_title ltx_title_abstract">Abstract</h6>
        <p class="ltx_p">We propose a thing that works.</p></div>
      <section id="S1" class="ltx_section">
        <h2 class="ltx_title ltx_title_section"><span class="ltx_tag">1 </span>Introduction</h2>
        <p class="ltx_p">Motivation with math <math alttext="x_{t}"></math> inside.</p>
      </section>
      <section id="S3" class="ltx_section">
        <h2 class="ltx_title ltx_title_section"><span class="ltx_tag">3 </span>Model</h2>
        <h3 class="ltx_title ltx_title_subsection"><span class="ltx_tag">3.1 </span>Encoder</h3>
        <p class="ltx_p">Encoder body.</p>
        <figure class="ltx_table"><figcaption class="ltx_caption">
          <span class="ltx_tag">Table 1: </span>Results table.</figcaption>
          <table><tr><td>0.99</td><td>0.01</td></tr></table></figure>
      </section>
      <section id="Sb" class="ltx_bibliography">
        <h2 class="ltx_title ltx_title_bibliography">References</h2>
        <ul><li class="ltx_bibitem"><span class="ltx_tag">[1] </span>A. Author. A cited paper.</li></ul>
      </section>
    </article></div></body></html>"""

    def test_is_arxiv(self):
        from ed.ingest.arxiv import is_arxiv
        yes = ["1706.03762", "1706.03762v7",
               "https://arxiv.org/abs/1706.03762",
               "https://arxiv.org/pdf/1706.03762v7.pdf",
               "https://arxiv.org/html/1706.03762v7",
               "cs/0112017"]
        no = ["/tmp/paper.pdf", "https://example.com/a", "", "1706.03762 是论文"]
        for s in yes:
            self.assertTrue(is_arxiv(s), f"应识别为 arXiv: {s}")
        for s in no:
            self.assertFalse(is_arxiv(s), f"不应识别为 arXiv: {s}")

    def test_parse_arxiv_id(self):
        from ed.ingest.arxiv import parse_arxiv_id
        cases = {
            "1706.03762v7": "1706.03762",
            "https://arxiv.org/abs/1706.03762": "1706.03762",
            "https://arxiv.org/pdf/1706.03762v7.pdf": "1706.03762",
            "math.GT/0309136": "math.GT/0309136",
        }
        for raw, want in cases.items():
            self.assertEqual(parse_arxiv_id(raw), want, raw)

    def test_heading_levels_shift_up_one(self):
        from ed.ingest.arxiv import latexml_to_text
        t = latexml_to_text(self.HTML)
        self.assertIn("# 1 Introduction", t)
        self.assertIn("# 3 Model", t)
        self.assertIn("## 3.1 Encoder", t)
        self.assertIn("# References", t)

    def test_abstract_becomes_section(self):
        from ed.ingest.arxiv import latexml_to_text
        t = latexml_to_text(self.HTML)
        self.assertIn("## 摘要", t)
        self.assertIn("We propose a thing that works.", t)
        # "Abstract" 字样不该重复出现
        self.assertNotIn("Abstract\n", t)

    def test_page_noise_is_dropped(self):
        from ed.ingest.arxiv import latexml_to_text
        t = latexml_to_text(self.HTML)
        self.assertNotIn("Report GitHub Issue", t)
        self.assertNotIn("funded by someone", t)      # 作者脚注
        self.assertNotIn("A Paper Title", t)          # 大标题已在元数据

    def test_math_keeps_latex_source(self):
        from ed.ingest.arxiv import latexml_to_text
        t = latexml_to_text(self.HTML)
        self.assertIn("$x_{t}$", t)

    def test_table_cells_kept_as_markdown(self):
        """表格里是实验数字，整表丢掉会让 result 类条目写不出数字。"""
        from ed.ingest.arxiv import latexml_to_text
        t = latexml_to_text(self.HTML)
        self.assertIn("Table 1: Results table.", t)
        self.assertIn("0.99", t)
        self.assertIn("| --- |", t)

    def test_bibliography_items_become_list(self):
        from ed.ingest.arxiv import latexml_to_text
        t = latexml_to_text(self.HTML)
        self.assertIn("- ", t)
        self.assertIn("A cited paper.", t)

    def test_block_heading_is_chapter_not_last_subsection(self):
        """块横跨多个小节时，块头应标章名，而不是最后那个小节。"""
        text = ("# 3 Model\n\n" + "a " * 100 + "\n\n## 3.1 Encoder\n\n" + "b " * 100
                + "\n\n## 3.2 Decoder\n\n" + "c " * 100)
        blocks = build_blocks(text, target_words=5000)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].heading, "3 Model")


if __name__ == "__main__":
    unittest.main(verbosity=2)
