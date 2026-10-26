"""
视频 / 音频 → 逐字稿。

设计要点：**跑不通时必须说清为什么、怎么办**，不能只说「失败了」。

失败诊断用 reason_code 分类，每一类都对应一个明确的下一步：

    needs_login      平台要登录态 → 下一步：导出浏览器 cookie，或手动粘贴逐字稿
    platform_changed 平台改了接口 → 下一步：升级 yt-dlp，或手动粘贴
    geo_blocked      地域限制     → 下一步：换网络，或手动粘贴
    no_subs_no_audio 拿不到音轨   → 下一步：手动粘贴逐字稿
    asr_failed      ASR 跑挂了     → 下一步：看 ASR 日志，或换更小的模型
    network          网络问题     → 下一步：检查网络
    missing_tool     缺依赖       → 下一步：给出安装命令

链路顺序（先穷尽无登录态方案）：
    1. yt-dlp 探测并抓现成字幕   ← 最快最稳
    2. yt-dlp 下载音频 → 本地 ASR
    3. 手动粘贴逐字稿            ← 兜底

三级漏斗（2026-10-24 与 Gemini 工作流对齐后的全景图）：
    Level 1  元数据+现有字幕轨   < 3s     fetch_subtitle（各平台解析器 + 清洗合并）
    Level 2  纯音频 + 本地 ASR   10~30s   download_audio → transcribe
             （faster-whisper；large-v3-turbo 走 CTranslate2 版，专有名词明显更准）
    Level 3  视觉轨道（按需抽帧） 用户点名才触发  → 尚未实现，见 docs/设计决策.md
抖音/小红书/播客各有专属解析器（cn_platforms.py / podcast.py / invidious.py），
汇合点都在 to_transcript()：平台差异被隔离在「链接→逐字稿」这一层。
"""

import html
import json
import os
import re
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Tuple

# 平台 → 大致可行性提示（用于给更准的下一步）
PLATFORM_HINTS = [
    (r"youtube\.com|youtu\.be", "YouTube"),
    (r"bilibili\.com|b23\.tv", "B站"),
    (r"douyin\.com|iesdouyin\.com|v\.douyin\.com", "抖音"),
    (r"xiaohongshu\.com|xhslink\.(?:com|cn)", "小红书"),
    (r"tiktok\.com", "TikTok"),
    (r"weibo\.com|weibo\.tv", "微博"),
    (r"zhihu\.com", "知乎"),
    (r"vimeo\.com", "Vimeo"),
    (r"xiaoyuzhoufm\.com", "小宇宙播客"),
    (r"xima\.tv|ximalaya\.com", "喜马拉雅"),
]

from .transcript import clean_transcript  # noqa: E402
from ..config import get_all_env  # noqa: E402

REASONS = {
    "ip_blocked": (
        "当前出口 IP 被平台限制了（不是缺登录态，是 IP 级别被拦）",
        "换一个出口网络再试（代理、换网络环境、或其他 IP）；"
        "加 cookie 也可能无效。这条与『需要登录』不同，先换 IP 再试别的",
    ),
    "needs_login": (
        "平台需要登录态才能拿到直链",
        "在浏览器登录该平台后导出 cookie，我可以带 cookie 重试；或者你直接把逐字稿粘给我",
    ),
    "platform_changed": (
        "平台接口变了，解析器跟不上",
        "先升级 yt-dlp（brew upgrade yt-dlp 或 pip -U yt-dlp）；仍然不行就手动粘贴逐字稿",
    ),
    "geo_blocked": (
        "内容有地域限制，当前网络拿不到",
        "换个网络环境重试；或者你手动粘贴逐字稿",
    ),
    "no_subs_no_audio": (
        "既没有字幕，也拿不到音轨",
        "这个链接可能需要登录、已删除或仅限 App 内播放。手动粘贴逐字稿是最快的办法",
    ),
    "asr_failed": (
        "音频拿到了，但本地转写失败",
        "看 ASR 的错误日志，或换更小的模型（--asr-model tiny）再试",
    ),
    "network": (
        "网络请求失败",
        "检查网络连接和代理设置后重试",
    ),
    "missing_tool": (
        "缺少依赖",
        "按提示安装后再试",
    ),
    "missing_key": (
        "缺少云端服务的 API key（免费注册即可，无需部署本地模型）",
        "按提示注册并把 key 写进项目 .env，30 秒搞定",
    ),
    "unsupported": (
        "这个链接不支持自动抓取",
        "手动把逐字稿粘给我，这是最可靠的方式",
    ),
    "paywalled": (
        "这是付费内容，平台不提供音频地址（版权保护，不是技术问题）",
        "在 App 里听完/下载后把音频文件发给我，或换免费内容",
    ),
}


def diagnose(reason_code: str, detail: str = "", url: str = "") -> Dict[str, Any]:
    """生成结构化的失败诊断。"""
    reason, next_step = REASONS.get(
        reason_code, ("未知原因", "手动粘贴逐字稿")
    )
    return {
        "status": "failed",
        "reason_code": reason_code,
        "reason": reason,
        "detail": detail[:500],
        "next_step": next_step,
        "url": url,
        "platform": platform_of(url),
    }


def platform_of(url: str) -> str:
    for pat, name in PLATFORM_HINTS:
        if re.search(pat, url, re.I):
            return name
    return "未知平台"


def _tool(name: str) -> Optional[str]:
    return shutil.which(name)


def ytdlp_version() -> str:
    if not _tool("yt-dlp"):
        return ""
    try:
        p = subprocess.run(["yt-dlp", "--version"], capture_output=True,
                           text=True, timeout=30)
        return p.stdout.strip()
    except Exception:
        return ""


# ---------------------------------------------------------------- 字幕抓取


def list_subtitles(url: str, timeout: int = 120) -> Dict[str, Any]:
    """探测这个链接有没有现成字幕。"""
    if not _tool("yt-dlp"):
        return {"ok": False, **diagnose("missing_tool", "需要 yt-dlp：brew install yt-dlp", url)}
    cmd = ["yt-dlp", "--list-subs", "--no-warnings", "--skip-download", url]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, **diagnose("network", "探测字幕超时", url)}
    out = (p.stdout or "") + (p.stderr or "")
    if p.returncode != 0:
        code = _classify_error(out)
        return {"ok": False, **diagnose(code, out.strip()[-400:], url)}
    langs = re.findall(r"^([a-zA-Z\-]+)\s+\w+", p.stdout, re.M)
    subs = re.findall(r"Available subtitles(?: in \w+)?:\s*\n((?:\s+.+\n)+)", p.stdout)
    manual = []
    auto = []
    for block in subs:
        for line in block.strip().split("\n"):
            line = line.strip()
            m = re.match(r"^([a-zA-Z\-]+)\s+(.*)$", line)
            if not m:
                continue
            lang, kind = m.group(1), m.group(2)
            (auto if "auto" in kind.lower() else manual).append(lang)
    return {
        "ok": True,
        "manual": sorted(set(manual)),
        "auto": sorted(set(auto)),
        "raw_langs": sorted(set(langs)),
    }


def fetch_subtitle(url: str, lang: Optional[str] = None,
                   timeout: int = 180) -> Dict[str, Any]:
    """抓字幕并转成纯文本。"""
    if not _tool("yt-dlp"):
        return {"ok": False, **diagnose("missing_tool", "需要 yt-dlp", url)}
    langs = [lang] if lang else ["zh-Hans", "zh-CN", "zh", "zh-Hant",
                                 "zh-TW", "en", "en-US"]
    for lg in langs:
        for flag in ("--write-subs", "--write-auto-subs"):
            with tempfile.TemporaryDirectory() as td:
                cmd = ["yt-dlp", flag, "--sub-langs", lg,
                       "--sub-format", "vtt", "--skip-download",
                       "--no-warnings", "-o", os.path.join(td, "s.%(ext)s"), url]
                try:
                    p = subprocess.run(cmd, capture_output=True, text=True,
                                       timeout=timeout)
                except subprocess.TimeoutExpired:
                    continue
                files = [f for f in os.listdir(td) if f.endswith(".vtt")]
                if files:
                    # 用带时间轴的版本：字幕本来就有时间信息，丢掉等于
                    # 把「每条观点可回溯到视频时刻」这条承诺作废。
                    text = vtt_to_timed_text(os.path.join(td, files[0]))
                    if text.strip():
                        return {"ok": True, "text": text, "lang": lg,
                                "source": "subtitle", "timed": True}
    probe = list_subtitles(url)
    if not probe.get("ok"):
        return probe
    return {"ok": False, **diagnose("no_subs_no_audio", "没有可用字幕", url)}


def vtt_to_text(path: str) -> str:
    """WebVTT → 纯文本。去掉时间轴、去重复行、保留说话顺序。"""
    out: List[str] = []
    seen_recent: List[str] = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line == "WEBVTT" or "-->" in line:
                continue
            if line.isdigit():
                continue
            clean = re.sub(r"<[^>]+>", "", line)
            clean = clean.replace("&nbsp;", " ").replace("&", "&")
            clean = clean.strip()
            if not clean:
                continue
            # 逐字稿常有滚动重复行
            if seen_recent and clean == seen_recent[-1]:
                continue
            seen_recent.append(clean)
            if len(seen_recent) > 8:
                seen_recent.pop(0)
            out.append(clean)
    return "\n".join(out)


_CUE_START = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->")


def vtt_to_timed_text(path: str) -> str:
    """WebVTT → `[HH:MM:SS] 文本`，保留每条 cue 的起始时间。

    和 `vtt_to_text` 的区别只在「要不要时间轴」。给蒸馏用的逐字稿必须
    带时间：否则「每条观点能跳回原视频」这个能力在字幕路径上就是空的。
    """
    out: List[str] = []
    seen_recent: List[str] = []
    pending: List[str] = []
    state = {"ts": ""}

    def flush() -> None:
        text = " ".join(pending).strip()
        pending.clear()
        if not state["ts"] or not text:
            return
        text = re.sub(r"<[^>]+>", "", text)
        text = html.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return
        if seen_recent and text == seen_recent[-1]:
            return
        seen_recent.append(text)
        if len(seen_recent) > 8:
            seen_recent.pop(0)
        out.append(f"[{state['ts']}] {text}")

    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            m = _CUE_START.match(line)
            if m:
                flush()
                h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
                state["ts"] = f"{h:02d}:{mi:02d}:{s:02d}"
                continue
            if (not line or line == "WEBVTT" or line.isdigit()
                    or line.startswith(("NOTE", "Kind:", "Language:"))):
                continue
            if state["ts"]:
                pending.append(line)
    flush()
    return "\n".join(out)


# ---------------------------------------------------------------- 音频下载 + ASR


def download_audio(url: str, outdir: str, timeout: int = 900) -> Dict[str, Any]:
    """下载音轨。"""
    if not _tool("yt-dlp"):
        return {"ok": False, **diagnose("missing_tool", "需要 yt-dlp", url)}
    if not _tool("ffmpeg"):
        return {"ok": False, **diagnose(
            "missing_tool", "需要 ffmpeg：brew install ffmpeg", url)}
    cmd = ["yt-dlp", "-f", "bestaudio/best", "-x", "--audio-format", "mp3",
           "--no-warnings", "-o", os.path.join(outdir, "a.%(ext)s"), url]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, **diagnose("network", "下载超时", url)}
    if p.returncode != 0:
        out = (p.stdout or "") + (p.stderr or "")
        return {"ok": False, **diagnose(_classify_error(out), out.strip()[-400:], url)}
    files = [f for f in os.listdir(outdir) if f.endswith((".mp3", ".m4a", ".opus", ".webm"))]
    if not files:
        return {"ok": False, **diagnose("no_subs_no_audio", "下载完成但没找到音频文件", url)}
    return {"ok": True, "path": os.path.join(outdir, files[0])}


def _classify_error(out: str) -> str:
    """从 yt-dlp 的报错里判断原因。

    顺序很重要：**先判 IP 级别被拦**，再判登录态。
    「Sign in to confirm you're not a bot」这句话既像登录问题也像 IP 问题，
    但它的真实含义是 IP 被限流——加 cookie 不一定管用，换 IP 才是正解。
    """
    low = out.lower()
    if ("not a bot" in low or "requestblocked" in low
            or "too many requests" in low or "rate-limit" in low
            or "blocked requests from your ip" in low):
        return "ip_blocked"
    if ("login" in low or "cookies" in low or "sign in" in low
            or "authentication" in low or "private video" in low
            or "members-only" in low or "age-restricted" in low):
        return "needs_login"
    if "unsupported url" in low or "no video formats" in low:
        return "platform_changed"
    if "not available in your country" in low or "geo" in low:
        return "geo_blocked"
    if "timed out" in low or "connection" in low or "temporary failure" in low:
        return "network"
    if "http error 404" in low or "video unavailable" in low:
        return "platform_changed"
    return "platform_changed"


def _transcribe_groq(audio_path: str, model: str,
                     language: Optional[str]) -> Dict[str, Any]:
    """Groq 云端 whisper-large-v3-turbo：217x 实时速度，专有名词准。

    免费档 2000 请求/天。音频 >25MB（免费档单文件上限）时先用 ffmpeg
    压成 64k 单声道 opus——27 分钟音频约 12MB，足够。
    key 在项目 .env 的 GROQ_API_KEY。
    """
    key = _groq_key()
    if not key:
        return {"ok": False, **diagnose(
            "missing_tool", "未配置 GROQ_API_KEY（.env）", "")}

    import subprocess as _sp

    path = audio_path
    tmp_clean = None
    try:
        size_mb = os.path.getsize(audio_path) / 1048576
        if size_mb > 24:
            # 压到 64k mono opus（语音识别质量无损，体积 /10）
            tmp_clean = audio_path + ".groq.opus"
            r = _sp.run(["ffmpeg", "-y", "-loglevel", "error", "-i", audio_path,
                         "-ac", "1", "-b:a", "64k", tmp_clean],
                        capture_output=True, timeout=300)
            if r.returncode == 0 and os.path.getsize(tmp_clean) < 25 * 1048576:
                path = tmp_clean

        mime = "audio/opus" if path.endswith(".opus") else "audio/mpeg"
        d = None
        last_err = ""
        for key in _groq_keys():
            r = _sp.run(
                ["curl", "-s", "--max-time", "600", "-X", "POST",
                 "https://api.groq.com/openai/v1/audio/transcriptions",
                 "-H", f"Authorization: Bearer {key}",
                 "-F", f"file=@{path}",
                 "-F", f"model={model}",
                 "-F", "response_format=verbose_json"]
                + (["-F", f"language={language}"] if language else []),
                capture_output=True, text=True, timeout=660)
            if r.returncode != 0 or not r.stdout.strip():
                last_err = "Groq 无响应"
                continue
            d = json.loads(r.stdout)
            if d.get("error"):
                msg = str(d["error"])
                if "rate" in msg.lower() or "429" in msg or "quota" in msg.lower():
                    last_err = f"key 限流，换下一把: {msg[:80]}"
                    d = None
                    continue
                last_err = f"Groq: {msg[:150]}"
                d = None
                continue
            break
        if d is None:
            return {"ok": False, **diagnose("asr_failed", last_err or "Groq 全部 key 失败", "")}
        segs = [(float(s.get("start", 0)), (s.get("text") or "").strip())
                for s in d.get("segments", []) if (s.get("text") or "").strip()]
        return {"ok": True, "segments": segs,
                "raw_text": "\n".join(f"[{_fmt(a)}] {t}" for a, t in segs),
                "lang": d.get("language", language or ""),
                "asr": "groq-whisper", "model": model}
    except Exception as e:
        return {"ok": False, **diagnose("asr_failed", f"Groq: {e}", "")}
    finally:
        if tmp_clean and os.path.exists(tmp_clean):
            try:
                os.unlink(tmp_clean)
            except OSError:
                pass


def _groq_keys() -> List[str]:
    """全部 Groq key（GROQ_API_KEY, GROQ_API_KEY2..9 + 环境变量），429 时轮换。

    路径交给 ed.config：项目根目录 → 当前目录 → 旧路径 → 环境变量。
    以前写死 ~/Projects/everything-distill/.env，换个目录装就找不到 key。
    """
    return get_all_env("GROQ_API_KEY")


def _groq_key() -> Optional[str]:
    ks = _groq_keys()
    return ks[0] if ks else None


def transcribe(audio_path: str, model: str = "whisper-large-v3-turbo",
               language: Optional[str] = None,
               engine: str = "auto") -> Dict[str, Any]:
    """ASR。设计原则：**装上就能用，不部署本地模型**。

    auto（默认）：Groq 云端（免费 2000 次/天，217x 实时）。
      无 key 时报「30 秒注册」的明确指引，**绝不静默下载本地模型**。
    local：显式 opt-in 的离线兜底（faster-whisper，需自装）。

    「听」交给云端 ASR，「懂」交给会话里的模型（蒸馏子 Agent）——
    本地只负责下载（yt-dlp/ffmpeg）和校验，这是 skill 可移植的关键。
    """
    if engine in ("auto", "cloud"):
        if not _groq_key():
            return {"ok": False, **diagnose(
                "missing_key",
                "云端转写需要 GROQ_API_KEY（免费注册 30 秒：console.groq.com/keys，"
                "写入项目 .env）", "")}
        cloud_model = model if "turbo" in model or "large" in model \
            else "whisper-large-v3-turbo"
        return _transcribe_groq(audio_path, cloud_model, language)

    # engine == "local"：显式 opt-in
    if _tool("whisper-ctranslate2"):
        return _transcribe_faster_whisper(audio_path, model, language)
    try:
        import faster_whisper  # noqa: F401
        return _transcribe_faster_whisper(audio_path, model, language)
    except ImportError:
        return {"ok": False, **diagnose(
            "missing_tool",
            "本地模式需要 faster-whisper（pip install faster-whisper）", "")}


def _load_audio_16k(path: str):
    """用 ffmpeg 解码成 16kHz 单声道 float32。

    为什么不走 faster_whisper 自带的 decode_audio：
    它内部用 PyAV 的 av.open(..., metadata_errors="ignore")，
    而 PyAV 19 已经移除这个参数 —— 版本一升级就炸（TypeError: unexpected keyword argument）。
    ffmpeg 本来就是硬依赖，走它更稳，也不受 PyAV 版本影响。
    """
    import subprocess
    import tempfile
    import wave

    import numpy as np

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        wav_path = tf.name
    try:
        p = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", path,
             "-ac", "1", "-ar", "16000", "-f", "wav", wav_path],
            capture_output=True, text=True, timeout=600,
        )
        if p.returncode != 0:
            raise RuntimeError(f"ffmpeg 解码失败：{(p.stderr or '')[:300]}")
        with wave.open(wav_path, "rb") as wf:
            frames = wf.readframes(wf.getnframes())
        audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        return audio
    finally:
        try:
            os.unlink(wav_path)
        except OSError:
            pass


def _transcribe_faster_whisper(audio_path: str, model: str,
                               language: Optional[str]) -> Dict[str, Any]:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return {"ok": False, **diagnose(
            "missing_tool", "pip install faster-whisper", "")}
    try:
        wm = WhisperModel(model, device="auto", compute_type="auto")
        audio = _load_audio_16k(audio_path)

        # 先开 VAD（去掉静音，识别更快更准）。
        # 但 Silero VAD 在某些音频上会**把所有语音都滤掉**（实测一段 212 秒
        # 的正常音乐，VAD 之后 0 段，关掉之后 66 段）——所以必须回退。
        segments, info = wm.transcribe(
            audio, language=language, vad_filter=True,
            initial_prompt="以下是普通话的句子。",
        )
        segments = list(segments)
        vad_used = True
        if not segments:
            segments, info = wm.transcribe(
                audio, language=language, vad_filter=False,
                initial_prompt="以下是普通话的句子。",
            )
            segments = list(segments)
            vad_used = False

        lines = []
        for s in segments:
            t = s.text.strip()
            if t:
                lines.append(f"[{_fmt(s.start)}] {t}")
        return {"ok": True, "text": "\n".join(lines),
                "lang": getattr(info, "language", language or ""),
                "asr": "faster-whisper", "model": model, "vad": vad_used}
    except Exception as e:
        return {"ok": False, **diagnose("asr_failed", str(e), "")}


def _transcribe_funasr(audio_path: str) -> Dict[str, Any]:
    try:
        from funasr import AutoModel
    except ImportError:
        return {"ok": False, **diagnose(
            "missing_tool", "pip install funasr modelscope", "")}
    try:
        m = AutoModel(model="paraformer-zh", vad_model="fsmn-vad",
                      punc_model="ct-punc")
        res = m.generate(input=audio_path)
        text = "".join(r.get("text", "") for r in res)
        return {"ok": True, "text": text, "asr": "funasr"}
    except Exception as e:
        return {"ok": False, **diagnose("asr_failed", str(e), "")}


def _fmt(sec: float) -> str:
    """统一成 HH:MM:SS —— 下游清洗器、视觉线索检测都按三段式解析。"""
    sec = float(sec)
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ---------------------------------------------------------------- 主入口


def _is_douyin(url: str) -> bool:
    return bool(re.search(r"douyin\.com|iesdouyin\.com", url))


def _is_xhs(url: str) -> bool:
    return bool(re.search(r"xiaohongshu\.com|xhslink\.(?:com|cn)", url))


def _is_podcast(url: str) -> bool:
    return bool(re.search(r"xiaoyuzhoufm\.com|xima\.tv|ximalaya\.com", url))


def to_transcript(url: str, prefer: str = "auto",
                 asr_model: str = "whisper-large-v3-turbo",
                 language: Optional[str] = None,
                 timeout: int = 900,
                 asr_engine: str = "auto") -> Dict[str, Any]:
    """把视频链接变成逐字稿。prefer: subtitle | audio | auto

    默认 auto：先试字幕，拿不到再走音频 + ASR。和命令行的默认行为一致——
    以前这里默认 subtitle，导致「命令行能降级、Python 调用不降级」。
    """
    attempts: List[Dict[str, Any]] = []
    is_yt = bool(re.search(r"youtube\.com|youtu\.be", url))

    # 本地音视频文件：跳过平台解析，直接进 ASR（Groq 云端优先）
    if os.path.isfile(url) and re.search(
            r"\.(mp3|mp4|m4a|wav|webm|opus|flac|ogg|mov|mkv)$", url, re.I):
        attempts.append({"步骤": "本地文件", "成功": True,
                         "细节": os.path.basename(url)})
        r = transcribe(url, model=asr_model, language=language,
                       engine=asr_engine)
        attempts.append({"步骤": "转写", "成功": r.get("ok", False),
                         "细节": (r.get("reason") or r.get("asr", ""))
                         + (f"/{r.get('model','')}" if r.get("ok") else "")})
        if r.get("ok"):
            segs = r.get("segments")
            if segs:
                from .transcript import merge_to_lines, format_lines
                lines = merge_to_lines([(float(a), float(a) + 3.0, t)
                                        for a, t in segs])
                text = format_lines(lines)
            else:
                c = clean_transcript(r.get("raw_text") or r.get("text", ""))
                text = c.get("text", "")
            return {"ok": True, "text": text, "via": "asr",
                    "asr_model": r.get("model", ""), "attempts": attempts,
                    "platform": "本地文件"}
        return {"ok": False, **{k: v for k, v in r.items() if k != "attempts"},
                "attempts": attempts, "platform": "本地文件"}

    # 播客平台（小宇宙/喜马拉雅）：没有字幕概念，直接走音频
    if _is_podcast(url):
        from .podcast import podcast_audio
        with tempfile.TemporaryDirectory() as td:
            d = podcast_audio(url, td)
            attempts.append({"步骤": "播客音频", "成功": d.get("ok", False),
                             "细节": d.get("reason") or d.get("title", "")})
            if d.get("ok"):
                r = transcribe(d["path"], model=asr_model, language=language,
                               engine=asr_engine)
                attempts.append({"步骤": "本地转写", "成功": r.get("ok", False),
                                 "细节": (r.get("reason") or r.get("asr", ""))
                                 + (f"/{r.get('model','')}" if r.get("ok") else "")})
                text = r.get("text", "")
                if r.get("ok"):
                    from .transcript import parse_cues, merge_to_lines, format_lines
                    segs = r.get("segments")
                    if segs:
                        lines = merge_to_lines([(float(a), float(a) + 3.0, t)
                                                for a, t in segs])
                        text = format_lines(lines)
                    else:
                        c = clean_transcript(r.get("raw_text") or text)
                        text = c.get("text", text)
                out = {"ok": r.get("ok", False), "text": text,
                       "via": "asr", "asr_model": r.get("model", ""),
                       "attempts": attempts,
                       "platform": platform_of(url),
                       "title": d.get("title", "")}
                return out
            # 付费墙单独归类 —— 这不是技术问题，重试无意义
            if "付费" in (d.get("reason") or ""):
                return {"ok": False,
                        **diagnose("paywalled", d.get("reason", ""), url),
                        "attempts": attempts}
        # 播客失败就返回（不落回 yt-dlp——那只会再吃一次平台拦截）
        return {"ok": False, **diagnose("unsupported", "播客音频获取失败", url),
                "attempts": attempts}

    if prefer in ("subtitle", "auto") and is_yt:
        # ① Supadata API（最优先：秒级、免下载、带时间戳；免费档 100 次/月）
        from .supadata import fetch_transcript_supadata
        rs = fetch_transcript_supadata(url)
        attempts.append({"步骤": "Supadata", "成功": rs.get("ok", False),
                         "细节": rs.get("reason") or rs.get("via", "")})
        if rs.get("ok"):
            return {"ok": True, "text": rs["text"], "via": "supadata",
                    "attempts": attempts, "platform": platform_of(url)}
        # 额度外/静默失败都继续走下级，只有配了 key 才显示这一步

    if prefer in ("subtitle", "auto"):
        r = fetch_subtitle(url, timeout=min(timeout, 300))
        attempts.append({"步骤": "抓字幕", "成功": r.get("ok", False),
                         "细节": r.get("reason") or r.get("lang", "")})
        if r.get("ok"):
            return {"ok": True, "text": r["text"], "via": "subtitle",
                    "attempts": attempts, "platform": platform_of(url)}

        # YouTube 专属兜底：出口 IP 被 bot 检测拦时，yt-dlp 全家都会挂。
        # 走 Invidious 公共实例（用实例服务器的 IP 拉数据），先试字幕。
        # （Level 2 的 Groq 音频转写在下方 audio 分支，IP 通时 1+分钟出稿）
        if is_yt:
            from .invidious import fetch_subtitle_invidious
            r2 = fetch_subtitle_invidious(url)
            attempts.append({"步骤": "Invidious 字幕", "成功": r2.get("ok", False),
                             "细节": r2.get("reason") or r2.get("instance", "")})
            if r2.get("ok"):
                return {"ok": True, "text": r2["text"], "via": "subtitle-invidious",
                        "attempts": attempts, "platform": platform_of(url)}

    if prefer in ("audio", "auto"):
        with tempfile.TemporaryDirectory() as td:
            d = download_audio(url, td, timeout=timeout)
            attempts.append({"步骤": "下载音频", "成功": d.get("ok", False),
                             "细节": d.get("reason") or os.path.basename(d.get("path", ""))})
            got_audio = d.get("ok")

            # 抖音：无 cookie 分享页直链优先（社区验证方案），yt-dlp 兜底
            if not got_audio and _is_douyin(url):
                from .cn_platforms import douyin_download_audio
                d3 = douyin_download_audio(url, td)
                attempts.append({"步骤": "抖音直链", "成功": d3.get("ok", False),
                                 "细节": d3.get("reason") or d3.get("title", "")})
                if d3.get("ok"):
                    d, got_audio = d3, True

            # 小红书：页面 __INITIAL_STATE__ 抠视频直链
            if not got_audio and _is_xhs(url):
                from .cn_platforms import xhs_download_audio
                d4 = xhs_download_audio(url, td)
                attempts.append({"步骤": "小红书直链", "成功": d4.get("ok", False),
                                 "细节": d4.get("reason") or d4.get("title", "")})
                if d4.get("ok"):
                    d, got_audio = d4, True

            if not got_audio and is_yt:
                # Invidious 音频兜底：实例代理流（local=true），绕开本机 IP 被拦
                from .invidious import download_audio_invidious
                d2 = download_audio_invidious(url, td)
                attempts.append({"步骤": "Invidious 音频", "成功": d2.get("ok", False),
                                 "细节": d2.get("reason") or d2.get("instance", "")})
                if d2.get("ok"):
                    d, got_audio = d2, True
            if got_audio:
                r = transcribe(d["path"], model=asr_model, language=language,
                               engine=asr_engine)
                attempts.append({"步骤": "本地转写", "成功": r.get("ok", False),
                                 "细节": (r.get("reason") or r.get("asr", ""))
                                 + (f"/{r.get('model','')}" if r.get("ok") else "")})
                if r.get("ok"):
                    # ASR 原始分段 → 清洗合并成 [HH:MM:SS] 紧凑格式
                    from .transcript import parse_cues, merge_to_lines, format_lines
                    segs = r.get("segments")
                    if segs:
                        lines = merge_to_lines([(float(a), float(a) + 3.0, t)
                                                for a, t in segs])
                        text = format_lines(lines)
                    else:
                        c = clean_transcript(r.get("raw_text") or r.get("text", ""))
                        text = c.get("text", r.get("text", ""))
                    return {"ok": True, "text": text, "via": "asr",
                            "asr_model": r.get("model", ""),
                            "attempts": attempts, "platform": platform_of(url)}
                last = r
            else:
                last = d
        # 失败路径也要带上 platform —— 诊断信息里平台比什么都重要
        out = {"ok": False, **last, "attempts": attempts}
        # 平台必须按**原始链接**判断，不能用本地路径
        out["platform"] = platform_of(url)
        return out

    return {"ok": False, **diagnose("unsupported", "未找到可用路径", url),
            "attempts": attempts}


def diagnose_only(url: str) -> Dict[str, Any]:
    """只诊断不抓取 —— 用于「这个链接能不能处理」的快速判断。"""
    info = {
        "platform": platform_of(url),
        "yt_dlp": ytdlp_version() or "未安装",
        "ffmpeg": "有" if _tool("ffmpeg") else "未安装",
        "asr": ("faster-whisper 可用" if _tool("whisper-ctranslate2")
                else ("funasr 可用" if _tool("funasr") else "未安装")),
    }
    probe = list_subtitles(url)
    info["有字幕"] = probe.get("ok") and bool(
        probe.get("manual") or probe.get("auto"))
    if not probe.get("ok"):
        info["探测失败"] = probe.get("reason", "")
        info["建议"] = probe.get("next_step", "")
    return info
