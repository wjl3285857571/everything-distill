#!/usr/bin/env python3
"""校验层与输入链路的回归测试。

这些用例守的是「失败不能冒充通过」和「字幕/文件不能悄悄丢结构」：
每一条都对应过真实缺陷，不是装饰性覆盖。
"""

import json
import os
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ed.distill.stage1 import (  # noqa: E402
    anchor_hit, build_system_prompt, check_batch_receipts, normalize,
    run_stage1, verify_stage1)
from ed.distill.stage2 import quote_in_source, verify_stage2  # noqa: E402
from ed.ingest.base import (Block, Document, extract_main_text,  # noqa: E402
                            load_xhs, read_docx, read_transcript_json)
from ed.ingest.supadata import build_request_url  # noqa: E402
from ed.ingest.video import _fmt, vtt_to_timed_text  # noqa: E402
from ed.orchestrate import collect, plan  # noqa: E402
from ed.runner import Runner  # noqa: E402
from ed.schema import (Basis, Insight, Locator, Source,  # noqa: E402
                       Stage1, Stage2)
from ed.workspace import save_document  # noqa: E402

TEXT = "这是一段长度足够的真实原文，验证正常摘录。"


def make_doc(text: str = TEXT, kind: str = "text") -> Document:
    return Document(text=text,
                    blocks=[Block(index=1, text=text, words=20)],
                    source=Source(kind=kind, origin="fixture", title="fixture"),
                    stats={})


class _Ctx:
    def __init__(self, memories=None, sessions=None):
        self.memories = memories or {}
        self.sessions = sessions or []


class TestReceipts(unittest.TestCase):
    def _ins(self, block=1):
        return Insight(id="I1", claim="c", support=TEXT,
                       locator=Locator(block=block))

    def test_missing_receipt_is_reported(self):
        missing, mismatches = check_batch_receipts(
            make_doc().blocks, '{"insights": []}', [])
        self.assertEqual(missing, [1])
        self.assertEqual(mismatches, [])

    def test_count_mismatch_is_reported(self):
        """回执写 0 条、实际 1 条 —— 这是最容易被放过去的一种。"""
        missing, mismatches = check_batch_receipts(
            make_doc().blocks, "第 1 块：找到 0 条\n", [self._ins()])
        self.assertEqual(missing, [])
        self.assertEqual(len(mismatches), 1)
        self.assertEqual(mismatches[0]["声称"], 0)
        self.assertEqual(mismatches[0]["实际"], 1)

    def test_receipt_counting_with_counters_is_accepted(self):
        """模型可能只数 insights，也可能把反例一起数，两种都该放行。"""
        missing, mismatches = check_batch_receipts(
            make_doc().blocks, "第 1 块：找到 2 条\n",
            [self._ins()], [self._ins()])
        self.assertEqual((missing, mismatches), ([], []))

    def test_accurate_receipt_passes(self):
        missing, mismatches = check_batch_receipts(
            make_doc().blocks, "第 1 块：找到 1 条\n", [self._ins()])
        self.assertEqual((missing, mismatches), ([], []))


class TestStage1Failures(unittest.TestCase):
    def test_model_outage_does_not_pass(self):
        """整批模型调用失败时，0 条洞察不等于「没有问题」。"""
        class Broken(Runner):
            def run(self, system, user):
                raise RuntimeError("model down")

        stage1, report = run_stage1(make_doc(), Broken(), workers=1)
        self.assertFalse(report["通过"])
        self.assertEqual(report["失败批次数"], 1)
        self.assertFalse(verify_stage1(stage1, make_doc(), report)["通过"])

    def test_unfinished_batch_fails_verification(self):
        doc = make_doc()
        ins = Insight(id="I1", claim="ok", support=TEXT,
                      locator=Locator(block=1))
        report = {"回执": [{"状态": "缺回执"}], "缺失回执总数": 1,
                  "数量不符总数": 0, "失败批次数": 0}
        v = verify_stage1(Stage1(source=doc.source, insights=[ins]), doc, report)
        self.assertFalse(v["通过"])
        self.assertEqual(v["批次"]["未完成批次"], 1)

    def test_empty_output_fails(self):
        doc = make_doc()
        v = verify_stage1(Stage1(source=doc.source), doc)
        self.assertFalse(v["通过"])
        self.assertTrue(v["空产出"])


class TestPruneUnlocatable(unittest.TestCase):
    def test_fabricated_entry_is_removed(self):
        """对不上原文的条目要整条删掉，而不是留在结果里。"""
        from ed.distill.stage1 import prune_unlocatable
        from ed.schema import Stage1
        doc = make_doc()
        good = Insight(id="I1", claim="ok", support=TEXT,
                       locator=Locator(block=1))
        bad = Insight(id="I2", claim="bad", support="原文根本没有这句话",
                      locator=Locator(block=1))
        s1 = Stage1(source=doc.source, insights=[good, bad])
        dropped = prune_unlocatable(s1, doc)
        self.assertEqual([d.id for d in dropped], ["I2"])
        self.assertEqual([i.id for i in s1.insights], ["I1"])


class TestQuoteCoverage(unittest.TestCase):
    def test_markdown_and_truncation_tolerated(self):
        """去掉 ** 加粗、截掉括号补充，都算真实引用。"""
        src = ("**求职期的项目策略**：倾向把现成工具拆开重写，作为简历项目"
               "（原话「扒一扒」）。")
        q = "求职期的项目策略：倾向把现成工具拆开重写，作为简历项目。"
        self.assertTrue(quote_in_source(q, src))

    def test_fabricated_quote_rejected(self):
        self.assertFalse(quote_in_source(
            "他养了三只猫并且每天早起跑步", "他喜欢读书和健身。"))

    def test_short_quote_requires_exact_match(self):
        self.assertFalse(quote_in_source("每天早起", "他喜欢读书和健身。"))


class TestBasisSnap(unittest.TestCase):
    def test_paraphrase_snapped_to_real_sentence(self):
        from ed.distill.stage2 import snap_basis_to_sources
        ctx = _Ctx(memories={"USER.md": (
            "他喜欢读书。**求职期的项目策略**：倾向把现成工具拆开重写，"
            "作为简历项目（原话「扒一扒」）。")})
        s2 = Stage2(
            relevant=[{"insight_id": "I1", "why": "x", "basis": [1]}],
            basis=[Basis(id=1, kind="memory", source="USER.md",
                         quote="求职期的项目策略：倾向把现成工具拆开重写，作为简历项目。")],
        )
        bad = snap_basis_to_sources(s2, ctx=ctx)
        self.assertEqual(bad, [])
        # 整篇覆盖率够高时保留原句（截断括号补充是可接受的）
        self.assertIn("求职期的项目策略", s2.basis[0].quote)

    def test_multi_sentence_quote_kept(self):
        """跨句引用不该因为单句比对找不到就被误杀。"""
        from ed.distill.stage2 import snap_basis_to_sources
        ctx = _Ctx(memories={"USER.md": (
            "希望掌控自己的生活。财富自由最吸引他的是免于生计约束。")})
        s2 = Stage2(
            relevant=[{"insight_id": "I1", "why": "x", "basis": [1]}],
            basis=[Basis(id=1, kind="memory", source="USER.md",
                         quote="希望掌控自己的生活。财富自由最吸引他的是免于生计约束。")],
        )
        bad = snap_basis_to_sources(s2, ctx=ctx)
        self.assertEqual(bad, [])
        self.assertIn("财富自由", s2.basis[0].quote)

    def test_unmatchable_basis_removed_with_relevance(self):
        from ed.distill.stage2 import snap_basis_to_sources
        ctx = _Ctx(memories={"USER.md": "他喜欢读书和健身。"})
        s2 = Stage2(
            relevant=[{"insight_id": "I1", "why": "x", "basis": [1]}],
            basis=[Basis(id=1, kind="memory", source="USER.md",
                         quote="他养了三只猫")],
        )
        bad = snap_basis_to_sources(s2, ctx=ctx)
        self.assertEqual(len(bad), 1)
        self.assertEqual(s2.relevant, [])


class TestStage2Provenance(unittest.TestCase):
    def _stage2(self, quote="真实记忆内容", insight_id="I1"):
        return Stage2(
            relevant=[{"insight_id": insight_id, "why": "x", "basis": [1]}],
            basis=[Basis(id=1, kind="memory", source="USER.md", quote=quote)],
        )

    def test_fabricated_memory_quote_fails(self):
        ctx = _Ctx(memories={"USER.md": "这里是真实记忆内容，别的内容没有。"})
        v = verify_stage2(self._stage2(quote="记忆里根本不存在这句话"), ctx=ctx)
        self.assertFalse(v["通过"])
        self.assertEqual(len(v["引文无法核对"]), 1)

    def test_real_memory_quote_passes(self):
        ctx = _Ctx(memories={"USER.md": "这里是真实记忆内容，别的内容没有。"})
        v = verify_stage2(self._stage2(quote="这里是真实记忆内容"), ctx=ctx)
        self.assertTrue(v["通过"], v)

    def test_unknown_insight_fails(self):
        stage1 = Stage1(source=Source(kind="text", origin="f", title="t"),
                        insights=[Insight(id="I1", claim="c", support=TEXT)])
        ctx = _Ctx(memories={"USER.md": "这里是真实记忆内容"})
        v = verify_stage2(self._stage2(quote="这里是真实记忆内容",
                                       insight_id="I999"),
                          ctx=ctx, stage1=stage1)
        self.assertFalse(v["通过"])
        self.assertEqual(len(v["洞察不存在"]), 1)

    def test_duplicate_basis_id_fails(self):
        s2 = Stage2(
            relevant=[{"insight_id": "I1", "why": "x", "basis": [1]}],
            basis=[Basis(id=1, kind="content", source="s", quote=TEXT),
                   Basis(id=1, kind="content", source="s", quote=TEXT)],
        )
        v = verify_stage2(s2)
        self.assertFalse(v["通过"])
        self.assertEqual(v["依据重复编号"], [1])


class TestCollectWorkflow(unittest.TestCase):
    def _write(self, d, payload, receipt=""):
        save_document(d, make_doc())
        os.makedirs(os.path.join(d, "_batches"), exist_ok=True)
        with open(os.path.join(d, "_batches", "batch_001.out"), "w",
                  encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + receipt)

    def test_missing_receipt_makes_collect_fail(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, {"insights": [
                {"claim": "ok", "support": TEXT, "locator": {"block": 1}}]})
            r = collect(d, 5)
            self.assertFalse(r["通过"])
            self.assertEqual(r["缺失回执总数"], 1)

    def test_correct_receipt_passes(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, {"insights": [
                {"claim": "ok", "support": TEXT, "locator": {"block": 1}}]},
                "\n第 1 块：找到 1 条\n")
            r = collect(d, 5)
            self.assertTrue(r["通过"], r)

    def test_manifest_beats_changed_batch_size(self):
        """plan 用 5 块一批、collect 传 3 时，仍按 manifest 收。"""
        text = "\n\n".join(f"# 第{i}章\n\n" + "内容。" * 120 for i in range(1, 4))
        doc = Document(text=text,
                       blocks=[Block(index=1, text=text, words=300)],
                       source=Source(kind="text", origin="f", title="t"),
                       stats={})
        with tempfile.TemporaryDirectory() as d:
            save_document(d, doc)
            plan(d, batch_size=5)
            with open(os.path.join(d, "_batches", "manifest.json"),
                      encoding="utf-8") as f:
                manifest = json.load(f)
            self.assertEqual(manifest["batches"][0]["blocks"], [1])
            r = collect(d, batch_size=999)
            self.assertEqual(r["批数"], 1)


class TestMediaAndFiles(unittest.TestCase):
    def test_vtt_keeps_timestamps(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.vtt")
            with open(p, "w", encoding="utf-8") as f:
                f.write("WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n"
                        "看屏幕上的表格\n\n00:01:05.500 --> 00:01:07.000\n"
                        "第二段\n")
            out = vtt_to_timed_text(p)
            self.assertIn("[00:00:01] 看屏幕上的表格", out)
            self.assertIn("[00:01:05] 第二段", out)

    def test_timestamp_always_three_segments(self):
        self.assertEqual(_fmt(65), "00:01:05")

    def test_json_cues_supported(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"cues": [{"start": 65, "text": "看屏幕"}]}, f)
            self.assertEqual(read_transcript_json(p), "[00:01:05] 看屏幕")

    def test_docx_heading_style_becomes_markdown(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.docx")
            xml = ('<w:document><w:body>'
                   '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
                   '<w:r><w:t>第一章</w:t></w:r></w:p>'
                   '<w:p><w:r><w:t>正文内容</w:t></w:r></w:p>'
                   '</w:body></w:document>')
            with zipfile.ZipFile(p, "w") as z:
                z.writestr("word/document.xml", xml)
            out = read_docx(p)
            self.assertIn("# 第一章", out)
            self.assertIn("正文内容", out)


class TestPaperPromptWiring(unittest.TestCase):
    def test_plan_includes_paper_mode(self):
        """README 推荐的分批路径也必须挂论文提示。"""
        with tempfile.TemporaryDirectory() as d:
            doc = make_doc("# Introduction\n\nBody.", kind="paper")
            save_document(d, doc)
            items = plan(d)
            with open(items[0]["prompt"], encoding="utf-8") as f:
                prompt = f.read()
            self.assertIn("论文模式（本次输入是一篇学术论文）", prompt)

    def test_build_system_prompt_only_for_papers(self):
        self.assertNotIn("论文模式", build_system_prompt("text"))
        self.assertIn("论文模式", build_system_prompt("paper"))
        self.assertIn("论文模式", build_system_prompt("arxiv"))


class TestSupadataEncoding(unittest.TestCase):
    def test_video_url_is_percent_encoded(self):
        u = build_request_url(
            "https://www.youtube.com/watch?v=abc123&list=PL1&index=4")
        self.assertIn("list%3DPL1", u)
        self.assertIn("index%3D4", u)
        self.assertNotIn("&list=", u)


class TestExcerptStrictness(unittest.TestCase):
    def test_fabricated_tail_is_rejected(self):
        """真话后面接一段编造，以前靠 12 字窗口能过，现在必须失败。"""
        real = "这是真实原文里的一句话。"
        quote = real + "后面这段完全是模型编出来的内容，原文里没有。"
        self.assertFalse(anchor_hit(quote, real))

    def test_elided_quote_with_real_fragments_passes(self):
        src = ("第一段真实内容讲了一个问题，中间省略的部分也在原文里，"
               "最后一段真实内容给出了结论。")
        quote = "第一段真实内容讲了一个问题……最后一段真实内容给出了结论。"
        self.assertTrue(anchor_hit(quote, src))

    def test_english_hyphenation_is_joined(self):
        """PDF 断行 trans-\\nformer 归一化后应能和 transformer 对上。"""
        self.assertEqual(normalize("trans-\nformer"), normalize("transformer"))
        self.assertTrue(anchor_hit("transformer architecture",
                                   "the trans-\nformer architecture is used"))

    def test_noncontiguous_common_words_do_not_pass(self):
        """两个常见词分别出现过、但不连在一起，不能算命中。"""
        self.assertFalse(anchor_hit(
            "model architecture",
            "the model is large and the architecture is deep"))


class TestWebExtraction(unittest.TestCase):
    HTML = """<html><body>
    <nav>首页 产品 关于我们 联系方式</nav>
    <article>
      <h1>一篇文章</h1>
      <p>这是正文第一段，讲了一个非常具体的问题，以及作者最后采用的解决办法。
         这段要足够长，才能让正文容器在打分时明显高于导航和页脚。</p>
      <p>这是正文第二段，继续展开上一段的思路，并给出一个可操作的步骤，
         读者照着做就能复现作者的结果。</p>
    </article>
    <footer>版权所有 2026 某某网站 保留所有权利</footer>
    </body></html>"""

    def test_nav_and_footer_removed(self):
        t = extract_main_text(self.HTML)
        self.assertIn("这是正文第一段", t)
        self.assertNotIn("首页", t)
        self.assertNotIn("版权所有", t)

    def test_content_div_is_used_when_no_article(self):
        html = ("<html><body><div class='post-content'>"
                + "<p>正文内容在这里，足够长才能被当成候选容器。</p>" * 8
                + "</div><div class='sidebar'><a href='/x'>推荐链接</a></div>"
                "</body></html>")
        t = extract_main_text(html)
        self.assertIn("正文内容在这里", t)
        self.assertNotIn("推荐链接", t)

    def test_falls_back_to_whole_page(self):
        t = extract_main_text("<html><body><p>短文本</p></body></html>")
        self.assertIn("短文本", t)


class TestXhsText(unittest.TestCase):
    def test_image_note_desc_becomes_document(self):
        info = {"ok": True, "title": "标题",
                "desc": "这是笔记正文，讲了一个具体的方法。",
                "tags": ["效率", "笔记"], "images": ["http://img"],
                "video_url": ""}
        with patch("ed.ingest.cn_platforms.xhs_parse_note", return_value=info):
            doc = load_xhs("https://www.xiaohongshu.com/explore/abc")
        self.assertIn("这是笔记正文", doc.text)
        self.assertIn("#效率", doc.text)
        self.assertEqual(doc.source.kind, "xiaohongshu")

    def test_empty_image_note_raises(self):
        info = {"ok": True, "title": "", "desc": "", "tags": [],
                "images": ["http://img"], "video_url": ""}
        with patch("ed.ingest.cn_platforms.xhs_parse_note", return_value=info):
            with self.assertRaises(ValueError):
                load_xhs("https://www.xiaohongshu.com/explore/abc")

    def test_load_url_routes_xhs_to_text_loader(self):
        from ed.ingest.base import load_url
        with patch("ed.ingest.base.load_xhs", return_value="DOC") as m:
            got = load_url("https://www.xiaohongshu.com/explore/abc")
        self.assertEqual(got, "DOC")
        m.assert_called_once()

    def test_xhslink_cn_is_recognized(self):
        """真实分享短链是 xhslink.cn，以前只认 .com，会掉进通用网页分支。"""
        from ed.ingest.base import load_url
        with patch("ed.ingest.base.load_xhs", return_value="DOC") as m:
            got = load_url("https://xhslink.cn/o/352xomSfhNY")
        self.assertEqual(got, "DOC")
        m.assert_called_once()


class TestPdfLayout(unittest.TestCase):
    @staticmethod
    def _line(text, x0, y0, x1, y1):
        return {"text": text, "bbox": [x0, y0, x1, y1]}

    @staticmethod
    def _page(blocks, w=600, h=800):
        return {"width": w, "height": h, "blocks": blocks}

    def test_two_column_reading_order(self):
        from ed.ingest.base import _order_page_blocks
        page = self._page([
            {"lines": [self._line("Title", 50, 40, 550, 60)],
             "bbox": [50, 40, 550, 60]},
            {"lines": [self._line("L1", 50, 100, 280, 120)],
             "bbox": [50, 100, 280, 120]},
            {"lines": [self._line("L2", 50, 140, 280, 160)],
             "bbox": [50, 140, 280, 160]},
            {"lines": [self._line("R1", 320, 100, 550, 120)],
             "bbox": [320, 100, 550, 120]},
            {"lines": [self._line("R2", 320, 140, 550, 160)],
             "bbox": [320, 140, 550, 160]},
        ])
        order = [b["lines"][0]["text"] for b in _order_page_blocks(page)]
        self.assertEqual(order, ["Title", "L1", "L2", "R1", "R2"])

    def test_repeated_header_and_page_number_removed(self):
        from ed.ingest.base import _pdf_pages_to_text, _strip_page_margins
        pages = []
        for i in range(3):
            pages.append(self._page([
                {"lines": [self._line("IEEE TRANSACTIONS ON SOMETHING",
                                      50, 20, 550, 35)],
                 "bbox": [50, 20, 550, 35]},
                {"lines": [self._line(f"Body paragraph {i}", 50, 300, 550, 320)],
                 "bbox": [50, 300, 550, 320]},
                {"lines": [self._line(str(i + 1), 300, 780, 310, 795)],
                 "bbox": [300, 780, 310, 795]},
            ]))
        pages = _strip_page_margins(pages)
        text = _pdf_pages_to_text(pages)
        self.assertNotIn("IEEE TRANSACTIONS", text)
        self.assertIn("Body paragraph 0", text)

    def test_hyphenated_line_break_is_joined(self):
        from ed.ingest.base import _pdf_pages_to_text
        page = self._page([
            {"lines": [self._line("the trans-", 50, 100, 280, 120),
                       self._line("former architecture", 50, 120, 280, 140)],
             "bbox": [50, 100, 280, 140]},
        ])
        self.assertIn("transformer architecture", _pdf_pages_to_text([page]))


class TestRomanHeadings(unittest.TestCase):
    def test_ieee_roman_sections(self):
        from ed.ingest.base import promote_pdf_headings
        text = ("I. INTRODUCTION\n\nSome text.\n\n"
                "II. RELATED WORK\n\nMore text.\n\n"
                "III. METHOD\n\nMethod text.\n\n"
                "A. Encoder Layer\n\nEncoder text.")
        out, is_paper = promote_pdf_headings(text)
        self.assertTrue(is_paper)
        self.assertIn("# I. INTRODUCTION", out)
        self.assertIn("# III. METHOD", out)
        self.assertIn("## A. Encoder Layer", out)

    def test_table_cell_word_not_promoted(self):
        """表格里的「Model」表头不能被当成章节标题。"""
        from ed.ingest.base import promote_pdf_headings
        body = "正文内容在这里，足够长才能被当成段落。" * 4
        text = (f"Introduction\n\n{body}\n\n"
                f"Model\n\nEN-DE EN-FR 23.75 39.2 2.3 1.4\n\n"
                f"Method\n\n{body}\n\n"
                f"Conclusion\n\n{body}")
        out, is_paper = promote_pdf_headings(text)
        self.assertTrue(is_paper)
        self.assertNotIn("# Model", out)
        self.assertIn("# Method", out)

    def test_author_line_not_promoted(self):
        from ed.ingest.base import promote_pdf_headings
        text = ("I. INTRODUCTION\n\ntext\n\nII. METHOD\n\ntext\n\n"
                "III. RESULTS\n\ntext\n\nA. Smith, B. Jones")
        out, _ = promote_pdf_headings(text)
        self.assertNotIn("# A. Smith", out)


class TestArxivTables(unittest.TestCase):
    def test_th_and_td_become_markdown(self):
        from ed.ingest.arxiv import latexml_to_text
        html = ('<article class="ltx_document"><table>'
                '<tr><th>Model</th><th>BLEU</th></tr>'
                '<tr><td>Ours</td><td>28.4</td></tr>'
                '</table></article>')
        t = latexml_to_text(html)
        self.assertIn("| Model | BLEU |", t)
        self.assertIn("| Ours | 28.4 |", t)

    def test_colspan_keeps_column_alignment(self):
        from ed.ingest.arxiv import latexml_to_text
        html = ('<article class="ltx_document"><table>'
                '<tr><td colspan="2">Wide</td><td>C</td></tr>'
                '<tr><td>A</td><td>B</td><td>C</td></tr>'
                '</table></article>')
        t = latexml_to_text(html)
        self.assertIn("| Wide |  | C |", t)
        self.assertIn("| A | B | C |", t)

    def test_rowspan_repeats_group_label(self):
        """跨行的分组标签要在每一行都出现，否则 Markdown 表读不出归属。"""
        from ed.ingest.arxiv import latexml_to_text
        html = ('<article class="ltx_document"><table>'
                '<tr><td rowspan="2">Group</td><td>A</td></tr>'
                '<tr><td>B</td></tr>'
                '</table></article>')
        t = latexml_to_text(html)
        self.assertIn("| Group | A |", t)
        self.assertIn("| Group | B |", t)

    def test_equation_table_becomes_formula_not_table(self):
        from ed.ingest.arxiv import latexml_to_text
        html = ('<article class="ltx_document">'
                '<table class="ltx_equation ltx_eqn_table"><tr>'
                '<td><math alttext="h_{t}"></math>h_t</td><td>(1)</td>'
                '</tr></table></article>')
        t = latexml_to_text(html)
        self.assertIn("$$ h_{t} $$", t)
        self.assertNotIn("| --- |", t)

    def test_long_table_is_capped(self):
        from ed.ingest.arxiv import latexml_to_text
        rows = "".join(
            f"<tr><td>row{i}</td><td>{i}</td></tr>" for i in range(40))
        t = latexml_to_text(
            f'<article class="ltx_document"><table>{rows}</table></article>')
        self.assertIn("row0", t)
        self.assertIn("row29", t)
        self.assertNotIn("row30", t)
        self.assertIn("只保留前 30 行", t)


class TestSelectPaperTypes(unittest.TestCase):
    @staticmethod
    def _mk(type_, id_):
        return Insight(id=id_, claim=id_, support="s" * 20,
                       locator=Locator(block=1), type=type_)

    def test_paper_prefers_result_over_term(self):
        """论文超过上限时，不能先裁掉方法/结果，留下术语。"""
        from ed.distill.select import select
        s1 = Stage1(source=Source(kind="paper", origin="p", title="p"),
                    insights=[self._mk("term", "T"),
                              self._mk("result", "R")])
        r = select(s1, max_total=1)
        self.assertEqual(r["insights"][0].id, "R")

    def test_book_still_prefers_principle(self):
        from ed.distill.select import select
        s1 = Stage1(source=Source(kind="text", origin="b", title="b"),
                    insights=[self._mk("case", "C"),
                              self._mk("principle", "P")])
        r = select(s1, max_total=1)
        self.assertEqual(r["insights"][0].id, "P")


class TestLaterTier(unittest.TestCase):
    def test_later_has_own_section(self):
        """later 不该被悄悄归进「值得想」。"""
        from ed.render.document import render
        s2 = Stage2(
            relevant=[{"insight_id": "I1", "why": "LATER_SENTINEL",
                       "basis": [1], "horizon": "later"}],
            basis=[Basis(id=1, kind="content", source="s", quote="q")],
        )
        md = render(Stage1(source=Source(kind="text", origin="x", title="x")), s2)
        self.assertIn("存着，以后可能用", md)
        self.assertIn("LATER_SENTINEL", md)


class TestRelevanceCap(unittest.TestCase):
    def test_today_respects_total_cap(self):
        from ed.render.document import _pick_relevant
        items = [{"insight_id": f"I{i}", "horizon": "today",
                  "why": f"item {i}"} for i in range(35)]
        self.assertEqual(len(_pick_relevant(items, cap=30)), 30)


class TestNarrate(unittest.TestCase):
    def _doc(self):
        return Document(
            text="",
            blocks=[Block(index=1, text="甲" * 4000, heading="第一章", words=4000),
                    Block(index=2, text="乙" * 4000, heading="第二章", words=4000)],
            source=Source(kind="text", origin="x", title="x"),
        )

    def test_group_blocks_splits_on_heading(self):
        from ed.distill.narrate import group_blocks
        groups = group_blocks(self._doc(), min_chars=3000, max_chars=100000)
        self.assertEqual([[b.index for b in g] for g in groups], [[1], [2]])

    def test_run_narrate_checks_quotes(self):
        from ed.distill.narrate import run_narrate
        payload = {"title": "第一章", "summary": "讲了甲",
                   "prose": "第一段。\n\n第二段。",
                   "quotes": [{"text": "甲" * 20, "block": 1, "why": "因为"}]}

        class FakeRunner(Runner):
            def run(self, system, user):
                return json.dumps(payload, ensure_ascii=False)

        notes, report = run_narrate(self._doc(), FakeRunner(), workers=1)
        self.assertTrue(report["通过"])
        self.assertEqual(len(notes[0].quotes), 1)
        self.assertIn("第一段", notes[0].prose)

    def test_external_title_flagged(self):
        from ed.distill.narrate import _make_note
        blocks = [Block(index=1, text="阿莉莎走进花园。", heading="第一章", words=9)]
        note = _make_note(
            {"title": "t", "prose": "《窄门》讲的是阿莉莎；《红与黑》是另一本书。",
             "quotes": []},
            blocks, source_title="窄门")
        self.assertIn("红与黑", note.external_refs)
        self.assertNotIn("窄门", note.external_refs)

    def test_external_ref_warning_rendered(self):
        from ed.render.document import render
        md = render(Stage1(source=Source(kind="text", origin="x", title="x")),
                    None, narration=[{
                        "title": "第一章", "summary": "s",
                        "prose": "正文。",
                        "external_refs": ["红与黑"],
                    }])
        self.assertIn("原文里没有的书名", md)
        self.assertIn("红与黑", md)

    def test_render_narration_hides_insight_cards(self):
        from ed.render.document import render
        s1 = Stage1(source=Source(kind="text", origin="x", title="x"),
                    insights=[Insight(id="I1", claim="卡片式洞察",
                                      support="x", locator=Locator(block=1))])
        md = render(s1, None, narration=[{
            "title": "第一章", "summary": "讲了甲",
            "prose": "这是连贯正文。\n\n第二段。",
            "quotes": [{"text": "原文一句", "why": "因为重要"}],
        }])
        self.assertIn("这是连贯正文", md)
        self.assertIn("全书脉络", md)
        self.assertIn("> 原文：原文一句", md)
        self.assertNotIn("关键洞察", md)
        self.assertNotIn("卡片式洞察", md)


class TestRenderMode(unittest.TestCase):
    def test_insights_mode_drops_narration(self):
        from ed.cli import _choose_narration
        self.assertIsNone(_choose_narration("insights", object()))

    def test_narrative_mode_requires_narration(self):
        from ed.cli import _choose_narration
        with self.assertRaises(ValueError):
            _choose_narration("narrative", None)

    def test_auto_keeps_narration(self):
        from ed.cli import _choose_narration
        marker = object()
        self.assertIs(_choose_narration("auto", marker), marker)


class TestFooter(unittest.TestCase):
    def test_no_personal_claim_without_stage2(self):
        """没有个人化内容时，页脚不该说「个人化结论均挂依据」。"""
        from ed.render.document import render
        md = render(Stage1(source=Source(kind="text", origin="x", title="x")), None)
        self.assertNotIn("个人化结论均挂依据", md)

    def test_personal_claim_with_stage2(self):
        from ed.render.document import render
        s2 = Stage2(
            relevant=[{"insight_id": "I1", "why": "x", "basis": [1]}],
            basis=[Basis(id=1, kind="content", source="s", quote="q")],
        )
        md = render(Stage1(source=Source(kind="text", origin="x", title="x")), s2)
        self.assertIn("个人化结论均挂依据", md)


class FakeRunner(Runner):
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []

    def run(self, system: str, user: str) -> str:
        self.prompts.append((system, user))
        if not self.outputs:
            raise RuntimeError("没有更多预设输出")
        return self.outputs.pop(0)


class TestBrief(unittest.TestCase):
    def _doc(self, text=None, blocks=None, title="测试书"):
        text = text or "原文" * 400
        blocks = blocks or [Block(index=1, text=text, heading="第一章")]
        return Document(text=text, blocks=blocks,
                        source=Source(kind="text", origin="x", title=title),
                        stats={})

    def test_single_pass(self):
        from ed.distill.brief import run_brief
        from ed.render.brief import render_brief
        doc = self._doc()
        gist = "这是一段足够长的概述。" * 20
        r = FakeRunner([json.dumps({"gist": gist, "sections": []},
                                   ensure_ascii=False)])
        brief, report = run_brief(doc, r, max_chars=10000)
        self.assertTrue(report["通过"])
        self.assertEqual(report["模式"], "单次")
        self.assertEqual(len(r.prompts), 1)
        md = render_brief(brief, doc.source)
        self.assertIn("这内容在讲什么", md)
        self.assertIn("蒸馏版", md)
        self.assertNotIn("跟你有关", md)

    def test_long_uses_sections_and_synthesis(self):
        from ed.distill.brief import run_brief
        b1 = Block(index=1, text="甲" * 4000, heading="第一章")
        b2 = Block(index=2, text="乙" * 4000, heading="第二章")
        doc = self._doc(text="甲" * 4000 + "乙" * 4000,
                        blocks=[b1, b2], title="长书")
        r = FakeRunner([
            json.dumps({"title": "第一章", "gist": "第一章讲甲。" * 10},
                       ensure_ascii=False),
            json.dumps({"title": "第二章", "gist": "第二章讲乙。" * 10},
                       ensure_ascii=False),
            json.dumps({"gist": "全书从甲走到乙。" * 30}, ensure_ascii=False),
        ])
        brief, report = run_brief(doc, r, max_chars=3000, workers=1)
        self.assertEqual(report["模式"], "分层")
        self.assertEqual(len(brief.sections), 2)
        self.assertTrue(report["通过"])
        self.assertEqual(len(r.prompts), 3)

    def test_empty_gist_fails(self):
        from ed.distill.brief import run_brief
        r = FakeRunner([json.dumps({"gist": ""})])
        brief, report = run_brief(self._doc(), r)
        self.assertFalse(report["通过"])
        self.assertTrue(brief.error)

    def test_short_doc_skips_personal(self):
        from ed.cli import _is_short
        self.assertTrue(_is_short("短" * 100))
        self.assertFalse(_is_short("长" * 3001))

    def test_brief_becomes_narrative_overview(self):
        from ed.render.document import render
        s1 = Stage1(source=Source(kind="text", origin="x", title="书"))
        md = render(s1, None,
                    narration=[{"title": "第一章", "prose": "正文",
                                "summary": "摘要"}],
                    brief={"gist": "这是先讲一遍的概述。"})
        self.assertIn("这是先讲一遍的概述。", md)

    def test_filename_title_falls_back_to_heading(self):
        from ed.distill.brief import run_brief
        doc = self._doc(title="note.txt")
        r = FakeRunner([json.dumps({"gist": "足够长的概述。" * 30},
                                   ensure_ascii=False)])
        brief, _ = run_brief(doc, r)
        self.assertEqual(brief.title, "第一章")

    def test_fast_mode_uses_one_call(self):
        from ed.distill.brief import run_brief
        b1 = Block(index=1, text="甲" * 8000, heading="第一章")
        b2 = Block(index=2, text="乙" * 8000, heading="第二章")
        doc = self._doc(text="甲" * 8000 + "乙" * 8000,
                        blocks=[b1, b2], title="长书")
        r = FakeRunner([json.dumps({"gist": "快速概述。" * 40},
                                   ensure_ascii=False)])
        brief, report = run_brief(doc, r, max_chars=1000, fast=True)
        self.assertEqual(report["模式"], "快速")
        self.assertEqual(len(r.prompts), 1)
        self.assertTrue(report["通过"])


class TestRunners(unittest.TestCase):
    def test_openai_compatible_payload(self):
        import urllib.request
        from ed.runner import OpenAICompatibleRunner

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return (b'{"choices":[{"message":{"content":"hi"}}]}')

        captured = {}

        def fake_urlopen(req, timeout=0):
            captured["url"] = req.full_url
            captured["body"] = req.data
            return Resp()

        with patch.object(urllib.request, "urlopen", fake_urlopen):
            out = OpenAICompatibleRunner(
                base_url="http://localhost:1234/v1",
                api_key="k", model="m").run("sys", "user")
        self.assertEqual(out, "hi")
        self.assertEqual(captured["url"],
                         "http://localhost:1234/v1/chat/completions")
        body = json.loads(captured["body"].decode())
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertEqual(body["messages"][1]["content"], "user")


class TestMediaRouting(unittest.TestCase):
    def test_youtube_is_media(self):
        from ed.cli import _media_platform
        self.assertEqual(_media_platform("https://youtu.be/x"), "YouTube")
        self.assertEqual(_media_platform("https://example.com/a"), "")

    def test_local_media_ext(self):
        from ed.cli import _is_media_input
        self.assertTrue(_is_media_input("/tmp/a.mp3"))
        self.assertFalse(_is_media_input("/tmp/a.txt"))

    def test_load_media_builds_document(self):
        from types import SimpleNamespace
        from ed.cli import _load_media
        with patch("ed.ingest.video.to_transcript", return_value={
                "ok": True, "text": "[00:00:00] 你好\n[00:00:03] 世界",
                "platform": "YouTube", "via": "subtitle", "attempts": []}):
            with patch("ed.ingest.metadata.metadata",
                       return_value={"title": "视频标题"}):
                doc = _load_media("https://youtu.be/x", SimpleNamespace())
        self.assertEqual(doc.source.kind, "video")
        self.assertEqual(doc.source.title, "视频标题")
        self.assertIn("世界", doc.text)


class TestRunnerFactory(unittest.TestCase):
    def test_file_runner_takes_path_kwarg(self):
        from ed.runner import FileRunner, default_runner
        self.assertIsInstance(default_runner("file", path="/tmp/x"),
                              FileRunner)

    def test_command_runner_takes_template_kwarg(self):
        from ed.runner import CommandRunner, default_runner
        self.assertIsInstance(
            default_runner("command", template=["echo", "{prompt}"]),
            CommandRunner)


if __name__ == "__main__":
    unittest.main(verbosity=2)
