"""arXiv 论文接入。

只做两件事，都不需要模型、不需要 key、不需要登录：
  1. 抓 arXiv 官方 HTML 版全文（``arxiv.org/html/<id>``），保留章节层级
  2. 查 arXiv 导出 API 补元数据（标题 / 作者 / 年份 / 原始摘要）

拿不到就明确说为什么（没有 HTML 版 / 网络失败 / ID 认不出来），
不静默返回空内容。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from typing import Dict, List, Optional

from ..schema import Source
from .base import Document, build_blocks, effective_words

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# 新式 ID：1706.03762 / 1706.03762v7
_NEW_ID = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")
# 老式 ID：cs/0112017、math.GT/0309136
_OLD_ID = re.compile(r"([a-z][a-z\-]*(?:\.[A-Za-z]{2})?/\d{7})(v\d+)?")


def is_arxiv(raw: str) -> bool:
    """判断输入是不是 arXiv（链接或裸 ID）。"""
    s = (raw or "").strip()
    if not s:
        return False
    if re.search(r"arxiv\.org/(?:abs|pdf|html)/", s, re.I):
        return True
    # 裸 ID：整串就是一个 ID（可带版本号），不能是文件路径
    if re.fullmatch(r"\d{4}\.\d{4,5}(v\d+)?", s):
        return True
    if re.fullmatch(r"[a-z][a-z\-]*(?:\.[A-Za-z]{2})?/\d{7}(v\d+)?", s):
        return True
    return False


def parse_arxiv_id(raw: str) -> Optional[str]:
    """从 ID / abs 链接 / pdf 链接 / html 链接里抠出 arXiv ID。"""
    s = (raw or "").strip()
    if not s:
        return None
    m = _NEW_ID.search(s)
    if m:
        return m.group(1)
    m = _OLD_ID.search(s)
    if m:
        return m.group(1)
    return None


def _get(url: str, timeout: int = 40) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def fetch_html(arxiv_id: str) -> str:
    """抓 arXiv HTML 版全文。"""
    url = f"https://arxiv.org/html/{arxiv_id}"
    try:
        return _get(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ValueError(
                f"这篇论文没有 HTML 版（arXiv {arxiv_id}）。"
                "arXiv 只给部分论文生成 HTML；可以下载 PDF 后用本地 PDF 解析。"
            ) from e
        raise ValueError(f"抓 arXiv HTML 失败（HTTP {e.code}）：{url}") from e
    except Exception as e:
        raise ValueError(f"抓 arXiv HTML 失败：{e}") from e


def fetch_meta(arxiv_id: str) -> Dict[str, str]:
    """查 arXiv 导出 API 拿元数据。失败不致命，返回空字典。"""
    url = f"http://export.arxiv.org/api/query?id_list={arxiv_id}"
    try:
        xml = _get(url, timeout=25)
    except Exception:
        return {}
    ns = {"a": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return {}
    entry = root.find("a:entry", ns)
    if entry is None:
        return {}
    title = (entry.findtext("a:title", "", ns) or "").strip()
    summary = (entry.findtext("a:summary", "", ns) or "").strip()
    published = (entry.findtext("a:published", "", ns) or "").strip()
    authors = [
        (a.findtext("a:name", "", ns) or "").strip()
        for a in entry.findall("a:author", ns)
    ]
    return {
        "title": re.sub(r"\s+", " ", title),
        "authors": ", ".join(a for a in authors if a),
        "published": published[:10],
        "abstract": re.sub(r"\s+", " ", summary),
        "id": arxiv_id,
    }


# ---------------------------------------------------------------- HTML → 文本

_HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4}
# 这些容器里的文字不要（导航、脚本、公式渲染符号）
_SKIP_TAGS = {"script", "style", "nav", "math", "svg"}

# 表格保留上限：论文表格常有几十行，全塞进去会把上下文顶爆；
# 前 30 行足够覆盖主要基线对比和消融。
MAX_TABLE_ROWS = 30
MAX_TABLE_COLS = 12


class _LatexMLParser(HTMLParser):
    """把 LaTeXML 生成的论文页面压成带 markdown 标题的纯文本。

    保留：章节标题（#/##/###）、正文段落、公式的 LaTeX 源（math 的 alttext）、
          图表标题（figcaption）、参考文献条目、表格（压成 Markdown，限行数）。
    丢弃：导航条、公式的排版符号。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: List[str] = []
        self.cur: List[str] = []
        self.skip = 0
        self.heading = 0
        self.in_caption = False
        self.in_bib = False
        self.captions: List[str] = []
        # 表格状态
        self.in_table = False
        self.table_kind = "data"      # data | equation
        self.eq_alt = ""
        self.rows: List[List[str]] = []
        self.row: List[str] = []
        self.cell: List[str] = []
        self.cell_span = 1

    # -- 工具
    def _flush(self, prefix: str = "") -> None:
        text = re.sub(r"\s+", " ", "".join(self.cur)).strip()
        self.cur = []
        if not text:
            return
        if prefix:
            self.out.append(f"{prefix}{text}")
        else:
            self.out.append(text)

    def _attr(self, attrs, name: str) -> str:
        for k, v in attrs:
            if k == name:
                return v or ""
        return ""

    # -- 表格
    def _cell_text(self) -> str:
        s = re.sub(r"\s+", " ", "".join(self.cell)).strip()
        return s.replace("|", "\\|")

    def _flush_table(self) -> None:
        rows = [r for r in self.rows if any(c.strip() for c in r)]
        if not rows:
            self.rows = []
            return
        total = len(rows)
        rows = rows[:MAX_TABLE_ROWS]
        ncol = min(max(len(r) for r in rows), MAX_TABLE_COLS)
        if ncol == 0:
            self.rows = []
            return
        norm = [(r + [""] * ncol)[:ncol] for r in rows]
        lines = ["| " + " | ".join(norm[0]) + " |",
                 "| " + " | ".join(["---"] * ncol) + " |"]
        for r in norm[1:]:
            lines.append("| " + " | ".join(r) + " |")
        if total > MAX_TABLE_ROWS:
            lines.append(f"*（表格共 {total} 行，只保留前 {MAX_TABLE_ROWS} 行）*")
        self.out.append("\n".join(lines))
        self.rows = []

    def _table_start(self, tag: str, attrs) -> None:
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self.cell = []
            raw = self._attr(attrs, "colspan") or "1"
            try:
                self.cell_span = max(1, min(int(raw), MAX_TABLE_COLS))
            except ValueError:
                self.cell_span = 1
        elif tag == "math":
            alt = self._attr(attrs, "alttext")
            if not alt:
                return
            if self.table_kind == "equation":
                # 公式的排队表：不渲染成表格，取第一条 LaTeX 源
                if not self.eq_alt:
                    self.eq_alt = alt
            else:
                self.cell.append(f" ${alt}$ ")

    def _table_end(self, tag: str) -> None:
        if tag in ("td", "th"):
            self.row.append(self._cell_text())
            # colspan 的格子占多列，补空位让后面的列对齐
            for _ in range(self.cell_span - 1):
                self.row.append("")
            self.cell = []
            self.cell_span = 1
        elif tag == "tr":
            if any(c.strip() for c in self.row):
                self.rows.append(list(self.row))
            self.row = []
        elif tag == "table":
            if self.table_kind == "equation":
                if self.eq_alt:
                    self.out.append(f"$$ {self.eq_alt} $$")
            else:
                self._flush_table()
            self.in_table = False
            self.table_kind = "data"
            self.eq_alt = ""

    # -- HTMLParser 回调
    def handle_starttag(self, tag: str, attrs) -> None:
        cls = self._attr(attrs, "class")

        if self.in_table:
            self._table_start(tag, attrs)
            return
        if tag == "table":
            if self.skip:
                self.skip += 1
                return
            self._flush()
            self.in_table = True
            low = (cls or "").lower()
            self.table_kind = ("equation"
                               if ("ltx_equation" in low or "ltx_eqn" in low)
                               else "data")
            self.eq_alt = ""
            self.rows, self.row, self.cell = [], [], []
            return

        # 文档大标题和 "Abstract" 字样不重复输出（元数据里已有）
        if tag in ("h1", "h6") and (
            "ltx_title_document" in cls or "ltx_title_abstract" in cls
        ):
            self.skip += 1
            return
        # 作者块里塞满了脚注和邮箱，从元数据拿就够了
        if tag == "div" and "ltx_authors" in cls:
            self.skip += 1
            return
        # 摘要单独成节，方便蒸馏时定位
        if tag == "div" and "ltx_abstract" in cls:
            self._flush()
            self.out.append("## 摘要")
            return

        if tag in _SKIP_TAGS:
            if tag == "math":
                alt = self._attr(attrs, "alttext")
                if alt and not self.skip:
                    self.cur.append(f" ${alt}$ ")
            self.skip += 1
            return
        if self.skip:
            self.skip += 1
            return

        if tag in _HEADING_TAGS:
            # 只认 LaTeXML 的标题类名，避开页面上的杂项标题。
            # 层级整体上提一级：论文的「章」就是最大的结构单位，
            # 这样切块会按章断开；文档大标题已经存在元数据里，不重复输出。
            if tag == "h1" and "ltx_title_document" in cls:
                self.heading = 0
            elif tag == "h2" and "ltx_title_section" in cls:
                self.heading = 1
            elif tag == "h2" and "ltx_title_bibliography" in cls:
                self.heading = 1
                self.in_bib = True
            elif tag == "h3" and "ltx_title_subsection" in cls:
                self.heading = 2
            elif tag == "h4" and "ltx_title_subsubsection" in cls:
                self.heading = 3
            if self.heading:
                self._flush()
            return

        if tag == "p":
            self._flush()
        elif tag == "figcaption":
            self._flush()
            self.in_caption = True
        elif tag == "li" and "ltx_bibitem" in cls:
            self._flush()
            self.cur.append("- ")

    def handle_endtag(self, tag: str) -> None:
        if self.in_table:
            self._table_end(tag)
            return
        if self.skip:
            self.skip -= 1
            return
        if tag in _HEADING_TAGS and self.heading:
            self._flush("#" * self.heading + " ")
            self.heading = 0
        elif tag == "p":
            self._flush()
        elif tag == "figcaption":
            self._flush("[图表] ")
            self.in_caption = False

    def handle_data(self, data: str) -> None:
        if self.in_table:
            if not self.skip:
                self.cell.append(data)
            return
        if self.skip or not data:
            return
        # 标题里的章节编号 span 会混进来，保留（方便定位），只压空白
        self.cur.append(data)

    def close(self) -> None:
        super().close()
        self._flush()


def latexml_to_text(raw: str) -> str:
    """arXiv HTML → 带 markdown 标题的纯文本。"""
    # 只取 <article class="ltx_document">，把 arXiv 页面自己的页头
    # （公告条、GitHub 反馈表单、导航）挡在外面
    m = re.search(
        r"<article[^>]*class=\"[^\"]*ltx_document[^\"]*\"[^>]*>(.*?)</article>",
        raw,
        re.S,
    )
    if m:
        raw = m.group(1)
    parser = _LatexMLParser()
    parser.feed(raw)
    parser.close()
    text = "\n\n".join(x for x in parser.out if x.strip())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def load_arxiv(raw: str) -> Document:
    """arXiv ID 或链接 → Document。"""
    arxiv_id = parse_arxiv_id(raw)
    if not arxiv_id:
        raise ValueError(f"认不出 arXiv ID：{raw}")

    html = fetch_html(arxiv_id)
    meta = fetch_meta(arxiv_id)
    text = latexml_to_text(html)

    if len(text) < 2000:
        raise ValueError(
            f"arXiv {arxiv_id} 的 HTML 抓下来了但正文太少（{len(text)} 字符），"
            "可能是页面结构变了。请把这篇的 PDF 给我走 PDF 解析。"
        )

    title = meta.get("title") or f"arXiv {arxiv_id}"
    # 元数据里的摘要补在最前面，HTML 里那段有时被拆得很碎
    abstract = meta.get("abstract", "")
    if abstract and abstract[:60] not in text:
        text = f"## 摘要\n\n{abstract}\n\n{text}"

    src = Source(
        kind="arxiv",
        origin=f"https://arxiv.org/abs/{arxiv_id}",
        title=title,
        author=meta.get("authors", ""),
        meta={
            "arxiv_id": arxiv_id,
            "published": meta.get("published", ""),
            "html_url": f"https://arxiv.org/html/{arxiv_id}",
        },
    )

    text = re.sub(r"\n{4,}", "\n\n\n", text).strip()
    blocks = build_blocks(text, target_words=3000)
    stats = effective_words(text)
    stats["块数"] = len(blocks)
    stats["标题数"] = len(re.findall(r"^#{1,6}\s+", text, re.M))
    return Document(text=text, blocks=blocks, source=src, stats=stats)
