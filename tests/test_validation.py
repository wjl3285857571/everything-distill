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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ed.distill.stage1 import (  # noqa: E402
    anchor_hit, build_system_prompt, check_batch_receipts, normalize,
    run_stage1, verify_stage1)
from ed.distill.stage2 import verify_stage2  # noqa: E402
from ed.ingest.base import (Block, Document, read_docx,  # noqa: E402
                            read_transcript_json)
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
