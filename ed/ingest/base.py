"""
输入适配器 —— 把任意来源变成统一的中文纯文本 + 块序列。

统一输出：
    Document(text, blocks, source)
  blocks 是带标题层级的段落切分，供两段式蒸馏按块处理与定位。

支持：
    .epub  .pdf  .docx  .html/.htm  .txt/.md  .srt/.vtt  .json(逐字稿)
    URL（网页 / YouTube 等，走 video.py / web.py）
"""

import html
import json
import os
import re
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple

from ..schema import Source

# ---------------------------------------------------------------- 数据结构


@dataclass
class Block:
    """一个文本块，带标题上下文。"""
    index: int                     # 1-based
    text: str
    heading: str = ""              # 所属标题
    level: int = 0                 # 标题层级，0 表示无
    words: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "heading": self.heading,
            "level": self.level,
            "words": self.words,
        }


@dataclass
class Document:
    """统一的输入文档。"""
    text: str
    blocks: List[Block]
    source: Source
    stats: Dict[str, Any] = field(default_factory=dict)

    def block_text(self, index: int) -> str:
        for b in self.blocks:
            if b.index == index:
                return b.text
        return ""


# ---------------------------------------------------------------- 字数统计


def effective_words(text: str) -> Dict[str, int]:
    """中文按字、西文按词、数字按串 —— 所有阈值都基于这个数。"""
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z]+", text))
    digits = len(re.findall(r"\d+", text))
    return {
        "有效字数": cjk + latin + digits,
        "汉字": cjk,
        "西文词": latin,
        "数字串": digits,
        "总字符": len(text),
    }


# ---------------------------------------------------------------- HTML


_SKIP_TAGS = {"script", "style", "head", "svg", "nav"}
_BLOCK_TAGS = {
    "p", "div", "section", "article", "blockquote", "li", "ul", "ol",
    "tr", "td", "th", "table", "figure", "figcaption", "pre",
    "h1", "h2", "h3", "h4", "h5", "h6", "br", "hr", "dd", "dt",
}


class _Extractor(HTMLParser):
    """块级标签断行，内联标签不断行 —— 段落边界是后续切块的前提。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines: List[str] = []
        self._cur: List[str] = []
        self._skip = 0
        self._heading: Optional[str] = None
        self._pending_level: Optional[int] = None
        self._pending_title: Optional[str] = None
        self.headings: List[Tuple[int, str]] = []

    def _flush(self):
        s = "".join(self._cur)
        s = re.sub(r"\s+", " ", s).strip()
        if s:
            if self._pending_level:
                self.lines.append("#" * self._pending_level + " " + s)
                self.headings.append((self._pending_level, s))
            else:
                self.lines.append(s)
        self._cur = []
        self._pending_level = None
        self._pending_title = None

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self._flush()
            if re.match(r"^h[1-6]$", tag):
                self._heading = tag
                self._pending_level = int(tag[1])
                # calibre 有时把标题放在 title 属性里，标签本身是空的
                self._pending_title = dict(attrs).get("title")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self._flush()
            if self._heading == tag:
                self._heading = None

    def handle_data(self, data):
        if self._skip:
            return
        if data.strip() and not _is_css_noise(data):
            self._cur.append(data)

    def text(self) -> str:
        self._flush()
        return "\n\n".join(self.lines).strip()


def _is_css_noise(line: str) -> bool:
    """calibre 有时把 <style> 内容包在 <p> 里，标签规则拦不住，只能按内容识别。"""
    s = line.strip()
    if not s:
        return False
    if s.startswith("@page") or s.startswith("@media") or s.startswith("@import"):
        return True
    # "prop: value; prop: value" 形式的选择器块
    if ":" in s and ";" in s and "{" in s and not re.search(r"[。！？]", s):
        return True
    if s.startswith(("body {", "html {", "div {", "p {", "span {")):
        return True
    return False


def clean_html(raw: str) -> str:
    """兜底解析：去掉标签但保留块级换行。

    注意不能简单地 re.sub 掉所有标签——那会把 CSS 文本当正文放进来
    （calibre 的封面页整页是 <style> + <svg>，extractor 提取为空后
    就会走到这里）。
    """
    raw = re.sub(r"<(script|style|head|svg|nav)\b[^>]*>.*?</\1>", " ", raw,
                 flags=re.I | re.S)
    raw = re.sub(r"<[^>]+>", "\n", raw)
    raw = html.unescape(raw)
    raw = raw.replace("　", " ").replace("\xa0", " ")
    raw = re.sub(r"[ \t]+", " ", raw)
    lines = [ln.strip() for ln in raw.split("\n")]
    lines = [ln for ln in lines if ln and not _is_css_noise(ln)]
    return "\n".join(lines).strip()


def html_to_text(raw: str) -> str:
    p = _Extractor()
    try:
        p.feed(raw)
        out = p.text()
        if out.strip():
            return out
    except Exception:
        pass
    return clean_html(raw)


# ---------------------------------------------------------------- 各格式


def read_text_file(path: str) -> str:
    for enc in ("utf-8", "utf-8-sig", "gb18030", "big5", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except (UnicodeDecodeError, UnicodeError):
            continue
    with open(path, "rb") as f:
        return f.read().decode("utf-8", errors="replace")


def read_epub(path: str) -> Tuple[str, str, str]:
    """返回 (正文, 书名, 作者)。按 OPF spine 顺序，跳过 nav/toc/cover。"""
    title = author = ""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        opf_name = None
        if "META-INF/container.xml" in names:
            container = z.read("META-INF/container.xml").decode("utf-8", "replace")
            m = re.search(r'full-path\s*=\s*"([^"]+)"', container)
            if m:
                opf_name = m.group(1)

        hrefs: List[str] = []
        if opf_name and opf_name in names:
            opf = z.read(opf_name).decode("utf-8", "replace")
            m = re.search(r"<dc:title[^>]*>(.*?)</dc:title>", opf, re.S)
            if m:
                title = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
            creators = re.findall(r"<dc:creator[^>]*>(.*?)</dc:creator>", opf, re.S)
            author = "、".join(
                html.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in creators
            )
            base = os.path.dirname(opf_name)
            manifest: Dict[str, str] = {}
            for m in re.finditer(r"<item\b[^>]*?>", opf):
                tag = m.group(0)
                i = re.search(r'\bid\s*=\s*"([^"]+)"', tag)
                h = re.search(r'\bhref\s*=\s*"([^"]+)"', tag)
                if i and h:
                    manifest[i.group(1)] = h.group(1)
            for m in re.finditer(r'<itemref\b[^>]*\bidref\s*=\s*"([^"]+)"', opf):
                href = manifest.get(m.group(1))
                if href:
                    hrefs.append(os.path.normpath(os.path.join(base, href)))
        if not hrefs:
            hrefs = sorted(
                n for n in names
                if n.lower().endswith((".xhtml", ".html", ".htm"))
                and not re.search(r"(nav|toc|cover)", n, re.I)
            )

        chunks = []
        for href in hrefs:
            if href not in names:
                continue
            # 封面页/扉页只有 svg 和 CSS，没有正文
            if re.search(r"(nav|toc|cover|titlepage)", href, re.I):
                continue
            raw = z.read(href).decode("utf-8", "replace")
            t = html_to_text(raw)
            if t:
                chunks.append(t)
        return "\n\n".join(chunks), title, author


_DOCX_HEADING = re.compile(r'w:pStyle\s+w:val="([^"]+)"')


def _docx_heading_level(style: str) -> int:
    """把 Word 的段落样式映射成 markdown 标题层级。

    覆盖英文（Heading1 / heading 1）、中文（标题1 / 标题 1）和纯数字
    （1..6，部分模板直接拿数字当样式名）三种写法。
    """
    s = (style or "").strip()
    m = re.search(r"(?:heading|标题)\s*([1-6])", s, re.I)
    if m:
        return int(m.group(1))
    if re.fullmatch(r"[1-6]", s):
        return int(s)
    return 0


def read_docx(path: str) -> str:
    """DOCX → 带 markdown 标题的文本。

    以前只是把 XML 标签去掉，标题样式全丢；长 Word 文档会变成一整篇
    没有章节结构的文字，切块和定位都跟着变差。
    """
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")

    out: List[str] = []
    for para in re.split(r"</w:p>", xml):
        if not para.strip():
            continue
        sm = _DOCX_HEADING.search(para)
        level = _docx_heading_level(sm.group(1)) if sm else 0
        body = re.sub(r"<w:br[^>]*/>", "\n", para)
        body = re.sub(r"<w:tab[^>]*/>", "\t", body)
        body = re.sub(r"<[^>]+>", "", body)
        body = html.unescape(body).strip()
        if not body:
            continue
        if level:
            out.append("#" * level + " " + re.sub(r"\s+", " ", body))
        else:
            out.append(body)
    return re.sub(r"\n{3,}", "\n\n", "\n\n".join(out)).strip()


def read_pdf(path: str) -> Tuple[str, bool]:
    """返回 (文本, 是否疑似扫描件)。用 mutool 抽。"""
    import shutil
    import subprocess
    mutool = shutil.which("mutool")
    if not mutool:
        raise RuntimeError("需要 mutool：brew install mupdf-tools")
    out = subprocess.run(
        [mutool, "draw", "-F", "txt", "-o", "-", path],
        capture_output=True, text=True, timeout=900,
    )
    txt = out.stdout or ""
    pages = 0
    try:
        info = subprocess.run(
            [mutool, "info", path], capture_output=True, text=True, timeout=120
        ).stdout
        m = re.search(r"Pages:\s*(\d+)", info)
        if m:
            pages = int(m.group(1))
    except Exception:
        pass
    scrubbed = re.sub(r"\s", "", txt)
    if pages >= 5:
        scanned = (len(scrubbed) / pages) < 50
    else:
        scanned = len(scrubbed) < 20
    txt = "\n".join(ln.rstrip() for ln in txt.split("\n"))
    return txt, scanned


# PDF 抽出来的文本没有标题标记。论文的章节行是独立一行的
# 「Introduction」「3 Model Architecture」这类，认出来才能按章切块。
_PAPER_SECTIONS = {
    "abstract", "introduction", "related work", "background", "preliminaries",
    "method", "methods", "methodology", "approach", "model", "model architecture",
    "architecture", "experiments", "experimental setup", "experimental results",
    "evaluation", "results", "analysis", "ablation", "ablation study",
    "discussion", "limitations", "conclusion", "conclusions", "future work",
    "references", "appendix", "acknowledgments", "acknowledgements",
}
_PAPER_NUMBERED = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,2})\s+([A-Z][^\n]{1,60})$")


def promote_pdf_headings(text: str) -> Tuple[str, bool]:
    """把 PDF 里的论文章节行提升成 markdown 标题。

    返回 (文本, 是否认成论文)。认不出来就原样返回——宁可切得粗，
    也不能把正文行误判成标题、把内容切碎。
    """
    lines = text.split("\n")
    cand: Dict[int, int] = {}
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s or len(s) > 60:
            continue
        level = None
        if s.lower().rstrip(":") in _PAPER_SECTIONS:
            level = 1
        else:
            m = _PAPER_NUMBERED.match(s)
            if m and not s.endswith((".", ",", ";", ":")):
                level = m.group(1).count(".") + 1
        if level:
            cand[i] = level
    if len(cand) < 3:
        return text, False
    out = []
    for i, ln in enumerate(lines):
        lvl = cand.get(i)
        if lvl:
            out.append("#" * min(lvl, 4) + " " + ln.strip().rstrip(":"))
        else:
            out.append(ln)
    return "\n".join(out), True


def read_subtitle(path: str) -> str:
    """读 srt/vtt 字幕，转成带时间戳的纯文本。"""
    raw = read_text_file(path)
    out = []
    for line in raw.split("\n"):
        line = line.strip()
        if not line or line.isdigit():
            continue
        if "-->" in line:
            stamp = line.split("-->")[0].strip()
            out.append(f"[{stamp}]")
            continue
        if line.upper().startswith("WEBVTT"):
            continue
        out.append(line)
    return "\n".join(out)


def read_transcript_json(path: str) -> str:
    """读逐字稿 JSON。兼容常见两种结构：
       {"segments": [{"text":..., "start":...}]} 或 [{"text":...}]
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    segs = (data.get("segments") or data.get("cues")
            or data.get("transcript") or data.get("items") or []
            if isinstance(data, dict) else data)
    if not isinstance(segs, list):
        segs = []
    out = []
    for s in segs:
        if isinstance(s, dict):
            t = s.get("text") or s.get("content") or ""
            start = s.get("start")
            if start is not None:
                out.append(f"[{_fmt_ts(start)}] {t}")
            else:
                out.append(t)
        elif isinstance(s, str):
            out.append(s)
    return "\n".join(out)


def _fmt_ts(sec: float) -> str:
    """统一 HH:MM:SS —— 下游清洗、抽帧、视觉线索都按三段式解析。"""
    sec = float(sec)
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ---------------------------------------------------------------- 切块

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.M)


def split_paragraphs(text: str) -> List[str]:
    """切段落，并把嵌在段落里的标题行拆出来独立成段。

    真实书籍里标题常常紧贴正文（只有单换行），按空行切段会把标题
    一起吞进第一段，章节边界就丢了。
    """
    out: List[str] = []
    for p in re.split(r"\n\s*\n", text):
        if not p.strip():
            continue
        buf: List[str] = []
        for ln in p.split("\n"):
            if HEADING_RE.match(ln.strip()):
                if buf:
                    out.append("\n".join(buf).strip())
                    buf = []
                out.append(ln.strip())
            else:
                buf.append(ln)
        if buf:
            s = "\n".join(buf).strip()
            if s:
                out.append(s)
    return out


def build_blocks(text: str, target_words: int = 8000) -> List[Block]:
    """按结构边界切块。

    规则：
      - 块大小 = 有效字数累积到 target_words 附近就切
      - **标题不切块**，只更新块的 heading/level 上下文
        （否则标题一多就切出一堆碎块——有的书有 200+ 个小标题）
      - **不跨一级标题**：一级标题是最大的结构单位
      - 不在段落中间切断
    """
    paras = split_paragraphs(text)
    blocks: List[Block] = []
    cur: List[str] = []
    cur_words = 0
    heading = ""
    level = 0
    top = 0          # 当前所属的一级标题（用于不跨章）

    def flush():
        nonlocal cur, cur_words
        if not cur:
            return
        body = "\n\n".join(cur)
        # 块头用所属一级标题（章）而不是块内最后一个小节：
        # 一个块常横跨多个小节，标成最后那个小节会让读者误以为整块只讲它。
        label = top or heading
        blocks.append(Block(
            index=len(blocks) + 1,
            text=body,
            heading=label,
            level=1 if top else level,
            words=effective_words(body)["有效字数"],
        ))
        cur = []
        cur_words = 0

    for p in paras:
        m = HEADING_RE.match(p.strip())
        if m:
            lvl = len(m.group(1))
            title = m.group(2).strip()
            if lvl == 1 and top != title:
                # 一级标题变了 → 收尾当前块
                flush()
                top = title
            heading = title
            level = lvl
            cur.append(p)
            cur_words += effective_words(p)["有效字数"]
            continue
        pw = effective_words(p)["有效字数"]
        if cur_words and cur_words + pw > target_words:
            flush()
        cur.append(p)
        cur_words += pw
    flush()
    return blocks


# ---------------------------------------------------------------- 入口

DESKTOP_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TEXT_EXT = {".txt", ".md", ".markdown", ".text"}
HTML_EXT = {".html", ".htm"}
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".webm", ".m4a", ".mp3", ".wav"}


def _weibo_visitor_cookie() -> str:
    """微博游客 cookie（官方 visitor 接口，免登录）。

    微博单条帖子页对无 cookie 请求弹「Sina Visitor System」拦截；
    两步换游客 cookie：genvisitor 拿 tid → visitor?a=incarnate 换 SUB/SUBP。
    注意 incarnate 是 GET + 查询参数（POST 会 404），2026 年验证。
    """
    import subprocess

    def parse_cb(t: str) -> str:
        m = re.search(r"\((\{.*\})\)", t)
        return m.group(1) if m else t

    r = subprocess.run(
        ["curl", "-s", "--max-time", "15", "-X", "POST",
         "https://passport.weibo.com/visitor/genvisitor", "-A", DESKTOP_UA,
         "-d", 'cb=gen_callback&fp={"os":"2","browser":"Chrome126",'
               '"fonts":"undefined","screenInfo":"1920*1080*24","plugins":""}'],
        capture_output=True, text=True, timeout=30)
    tid_m = re.search(r'"tid":"([^"]+)"', r.stdout)
    if not tid_m:
        return ""
    r2 = subprocess.run(
        ["curl", "-s", "--max-time", "15",
         f"https://passport.weibo.com/visitor/visitor?a=incarnate&t={tid_m.group(1)}"
         "&w=2&c=095&gc=&cb=cross_domain&from=weibo&_rand=0.456",
         "-A", DESKTOP_UA],
        capture_output=True, text=True, timeout=30)
    sub = re.search(r'"sub":"([^"]+)"', r2.stdout)
    subp = re.search(r'"subp":"([^"]+)"', r2.stdout)
    if not (sub and subp):
        return ""
    return f"SUB={sub.group(1)}; SUBP={subp.group(1)}"


def _weibo_long_text(pid: str, headers) -> str:
    """长微博全文。列表接口只返回预览，extend 接口才有完整正文。"""
    import html as _html
    import subprocess
    try:
        r = subprocess.run(
            ["curl", "-s", "--max-time", "20",
             f"https://m.weibo.cn/statuses/extend?id={pid}", *headers],
            capture_output=True, text=True, timeout=40)
        d = json.loads(r.stdout)
        raw = (d.get("data") or {}).get("longTextContent") or ""
        if not raw:
            return ""
        raw = _html.unescape(raw)
        raw = re.sub(r"<br\s*/?>", "\n", raw)
        return re.sub(r"<[^>]+>", "", raw).strip()
    except (json.JSONDecodeError, OSError, subprocess.TimeoutExpired):
        return ""


def load_weibo(url: str) -> Document:
    """微博单条帖 → Document。游客 cookie + m.weibo.cn JSON 接口，免登录。"""
    import html as _html
    import subprocess
    import urllib.parse as _up

    m = (re.search(r"/status/(\w+)", url)
         or re.search(r"/(\d+)/([\w]+)", url))
    if not m:
        raise ValueError(f"无法从微博链接提取帖子 ID：{url}")
    # 数字 id 或字母 bid 都可以走 show 接口
    pid = m.group(m.lastindex)

    cookie = _weibo_visitor_cookie()
    headers = ["-A", DESKTOP_UA, "-H", "Referer: https://m.weibo.cn/"]
    if cookie:
        headers += ["-H", f"Cookie: {cookie}"]
    r = subprocess.run(
        ["curl", "-s", "--max-time", "20",
         f"https://m.weibo.cn/statuses/show?id={pid}", *headers],
        capture_output=True, text=True, timeout=40)
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise ValueError("微博接口没返回 JSON（游客 cookie 失效或帖子已删）")
    if d.get("ok") == 0:
        raise ValueError(f"微博接口报错：{d.get('msg', '未知')}")
    mb = d.get("data") or {}

    author = (mb.get("user") or {}).get("screen_name", "")
    text_raw = _html.unescape(mb.get("text") or "")
    text = re.sub(r"<br\s*/?>", "\n", text_raw)
    text = re.sub(r"<[^>]+>", "", text)

    # 长微博：列表接口只给预览，必须再请求 extend 接口拿全文，
    # 否则蒸馏的是被截断的前半段，用户还以为拿到的是完整内容。
    if mb.get("isLongText"):
        long_text = _weibo_long_text(pid, headers)
        if long_text:
            text = long_text

    parts = [text.strip()]
    pics = [p.get("large", {}).get("geo", {}).get("original_path")
            or p.get("url") for p in (mb.get("pic_urls") or [])
            if isinstance(p, dict)]
    # 长文展开
    is_long = mb.get("isLongText") or mb.get("pic_num", 0) > 9
    if pics:
        parts.append("[图片 x{}: {}]".format(
            len(pics), " ".join(p for p in pics[:3] if p)))
    if mb.get("page_info", {}).get("type") == "video":
        parts.append("[含视频]")
    full = "\n\n".join(x for x in parts if x)
    title = (text.strip().split("\n")[0] or "微博")[:60]
    src = Source(kind="weibo", origin=url, title=title, author=author,
                 meta={"created_at": mb.get("created_at", ""),
                       "reposts": mb.get("reposts_count"),
                       "comments": mb.get("comments_count"),
                       "attitudes": mb.get("attitudes_count"),
                       "long_text": bool(is_long)})
    full = re.sub(r"\n{4,}", "\n\n\n", full).strip()
    if not full:
        raise ValueError("微博抓到但内容为空（可能已删）")
    blocks = build_blocks(full)
    stats = effective_words(full)
    stats["块数"] = len(blocks)
    return Document(text=full, blocks=blocks, source=src, stats=stats)


def load_url(url: str) -> Document:
    """网页/社交帖 → Document。X 帖走镜像 API（免登录），普通网页抠正文。"""
    import urllib.request

    # arXiv 链接走专门的解析器：保留章节层级和公式，比通用网页抠正文干净得多
    from .arxiv import is_arxiv, load_arxiv
    if is_arxiv(url):
        return load_arxiv(url)

    def _get(u: str) -> str:
        req = urllib.request.Request(
            u, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X "
                        "10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", "replace")

    if re.search(r"weibo\.com|weibo\.cn|weibo\.com\.cn", url):
        return load_weibo(url)

    m = re.search(r"(?:x\.com|twitter\.com)/(\w+)/status/(\d+)", url)
    if m:
        # X/Twitter：fxtwitter 公共镜像 API，免登录拿正文+图片+元数据
        try:
            d = json.loads(_get(f"https://api.fxtwitter.com/{m.group(1)}/status/{m.group(2)}"))
            t = d.get("tweet") or {}
        except Exception:
            d = json.loads(_get(f"https://cdn.syndication.twimg.com/tweet-result?id={m.group(2)}&token=x"))
            t = {"text": d.get("text", ""),
                 "author": {"name": d.get("user", {}).get("name", "")},
                 "created_at": d.get("created_at", "")}
        author = (t.get("author") or {}).get("name", "")
        parts = [t.get("text", "").strip()]
        # 帖子里的图片也列出来（URL 级别，视觉信息按需再说）
        for mi in (t.get("media") or {}).get("all") or []:
            if mi.get("type") == "photo" and mi.get("url"):
                parts.append(f"[图片: {mi['url']}]")
        text = "\n\n".join(x for x in parts if x)
        title = (t.get("text", "").strip().split("\n")[0] or "X 帖子")[:60]
        src = Source(kind="x_post", origin=url, title=title, author=author,
                     meta={"created_at": t.get("created_at", "")})
    else:
        text = html_to_text(_get(url))
        tm = re.search(r"<title[^>]*>(.*?)</title>", _get(url), re.S | re.I)
        title = (tm.group(1).strip() if tm else url)[:80]
        src = Source(kind="web", origin=url, title=title)

    text = re.sub(r"\n{4,}", "\n\n\n", text).strip()
    if not text:
        raise ValueError(f"这个链接抓不到正文：{url}")
    blocks = build_blocks(text)
    stats = effective_words(text)
    stats["块数"] = len(blocks)
    return Document(text=text, blocks=blocks, source=src, stats=stats)


def load(path: str) -> Document:
    """把任意输入变成 Document。"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".epub":
        text, title, author = read_epub(path)
        src = Source(kind="epub", origin=path, title=title, author=author)
    elif ext == ".pdf":
        text, scanned = read_pdf(path)
        src = Source(kind="pdf", origin=path, title=os.path.basename(path),
                     meta={"scanned": scanned})
        if scanned:
            raise ValueError(
                "这个 PDF 抽不出文字层（扫描件）。请先做 OCR 再给我。"
            )
        # 论文 PDF 的章节行没有标记，认出来才能按章切块
        text, is_paper = promote_pdf_headings(text)
        if is_paper:
            src.meta["paper"] = True
            src.kind = "paper"
    elif ext == ".docx":
        text = read_docx(path)
        src = Source(kind="docx", origin=path, title=os.path.basename(path))
    elif ext in HTML_EXT:
        text = html_to_text(read_text_file(path))
        src = Source(kind="html", origin=path, title=os.path.basename(path))
    elif ext in (".srt", ".vtt"):
        text = read_subtitle(path)
        src = Source(kind="subtitle", origin=path, title=os.path.basename(path))
    elif ext == ".json":
        text = read_transcript_json(path)
        src = Source(kind="transcript", origin=path, title=os.path.basename(path))
    elif ext in TEXT_EXT:
        text = read_text_file(path)
        src = Source(kind="text", origin=path, title=os.path.basename(path))
    elif ext in VIDEO_EXT:
        raise ValueError(
            "媒体文件需要先转成逐字稿。用 ed.ingest.video.transcribe()。"
        )
    else:
        raise ValueError(f"暂不支持的格式：{ext}")

    text = re.sub(r"\n{4,}", "\n\n\n", text).strip()
    # 论文一般 5000-8000 字，按书的 8000 阈值切就整篇一块了
    paper = bool(src.meta.get("paper")) or src.kind == "arxiv"
    blocks = build_blocks(text, target_words=3000 if paper else 8000)
    stats = effective_words(text)
    stats["块数"] = len(blocks)
    stats["标题数"] = len(HEADING_RE.findall(text))
    return Document(text=text, blocks=blocks, source=src, stats=stats)
