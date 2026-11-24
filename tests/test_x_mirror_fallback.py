"""X 帖抓取的三级镜像链：fxtwitter → vxtwitter → syndication。

vxtwitter 返回扁平结构（text / user_name / date / media_extended），
和 fxtwitter 的 tweet 嵌套结构不同，这里锁住归一化与降级顺序。
样本数据按 2026-11-24 实测的真实响应结构裁剪。
"""
import json
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

POST_URL = "https://x.com/ai_jacksaku/status/2124807056117178682"

FXTWITTER_JSON = {
    "tweet": {
        "text": "每天躺赚个几百块钱不比打工香吗？？",
        "author": {"name": "阿川 | AI thinking"},
        "created_at": "Mon Nov 23 10:07:33 +0000 2026",
        "media": {"all": [
            {"type": "photo", "url": "https://pbs.twimg.com/media/A.jpg?name=orig"},
            {"type": "photo", "url": "https://pbs.twimg.com/media/B.jpg?name=orig"},
        ]},
    }
}

VXTWITTER_JSON = {
    "text": "每天躺赚个几百块钱不比打工香吗？？",
    "user_name": "阿川 | AI thinking",
    "user_screen_name": "AI_jacksaku",
    "date": "Mon Nov 23 10:07:33 +0000 2026",
    "date_epoch": 1795428453,
    "mediaURLs": ["https://pbs.twimg.com/media/HXzUwJSasAA72Pl.jpg"],
    "media_extended": [
        {"type": "image", "url": "https://pbs.twimg.com/media/HXzUwJSasAA72Pl.jpg"},
        {"type": "image", "url": "https://pbs.twimg.com/media/HXzUwI8aQAAkKn1.jpg"},
        {"type": "image", "url": "https://pbs.twimg.com/media/HXzUwI_bEAAeUD2.jpg"},
        {"type": "video", "url": "https://video.twimg.com/ext_tw_video/1/pu/vid/1.mp4"},
    ],
}

SYNDICATION_JSON = {
    "text": "每天躺赚个几百块钱不比打工香吗？？",
    "user": {"name": "阿川 | AI thinking"},
    "created_at": "Mon Nov 23 10:07:33 +0000 2026",
}


class _Resp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _fake_urlopen_factory(fail=()):
    calls = []

    def fake_urlopen(req, timeout=0):
        url = req.full_url
        calls.append(url)
        if "api.fxtwitter.com" in url:
            if "fx" in fail:
                raise urllib.error.URLError("fx down")
            return _Resp(FXTWITTER_JSON)
        if "api.vxtwitter.com" in url:
            if "vx" in fail:
                raise urllib.error.URLError("vx down")
            return _Resp(VXTWITTER_JSON)
        if "cdn.syndication.twimg.com" in url:
            return _Resp(SYNDICATION_JSON)
        raise AssertionError(f"unexpected url: {url}")

    return fake_urlopen, calls


class TestXMirrorFallback(unittest.TestCase):
    def _load(self, fail=()):
        from ed.ingest.base import load_url
        fake, calls = _fake_urlopen_factory(fail)
        with patch.object(urllib.request, "urlopen", fake):
            doc = load_url(POST_URL)
        return doc, calls

    def test_fxtwitter_first(self):
        doc, calls = self._load()
        self.assertEqual(doc.source.author, "阿川 | AI thinking")
        self.assertEqual(doc.source.meta["created_at"],
                         "Mon Nov 23 10:07:33 +0000 2026")
        self.assertIn("每天躺赚个几百块钱不比打工香吗？？", doc.text)
        self.assertEqual(doc.text.count("[图片:"), 2)
        # 第一站成功就不该再碰后面两站
        self.assertEqual(len(calls), 1)
        self.assertIn("api.fxtwitter.com", calls[0])

    def test_vxtwitter_fallback_normalizes_flat_structure(self):
        doc, calls = self._load(fail=("fx",))
        self.assertEqual(doc.source.author, "阿川 | AI thinking")
        self.assertEqual(doc.source.meta["created_at"],
                         "Mon Nov 23 10:07:33 +0000 2026")
        self.assertIn("每天躺赚个几百块钱不比打工香吗？？", doc.text)
        # media_extended 里 3 张 image 进正文，video 不进
        self.assertEqual(doc.text.count("[图片:"), 3)
        self.assertIn("https://pbs.twimg.com/media/HXzUwJSasAA72Pl.jpg", doc.text)
        self.assertNotIn("video.twimg.com", doc.text)
        self.assertEqual(len(calls), 2)
        self.assertIn("api.vxtwitter.com", calls[1])

    def test_syndication_is_last_resort(self):
        doc, calls = self._load(fail=("fx", "vx"))
        self.assertEqual(doc.source.author, "阿川 | AI thinking")
        self.assertIn("每天躺赚个几百块钱不比打工香吗？？", doc.text)
        # syndication 兜底没有图片信息
        self.assertEqual(doc.text.count("[图片:"), 0)
        self.assertEqual(len(calls), 3)
        self.assertIn("cdn.syndication.twimg.com", calls[2])


if __name__ == "__main__":
    unittest.main()
