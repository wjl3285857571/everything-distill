"""Level 3 视觉轨道：按需、局部、抽帧。

触发条件（克制）：
    - 用户明确说「看画面 / 看图表 / 这张图 / 12:30 那里」
    - 或蒸馏时发现主播频繁说「大家看屏幕上」（字幕里能检测到）
绝对不下载完整视频。

网络注意（亮子 2026-10-24 补充）：
    YouTube 片段下载必须复用现有代理配置 —— 本机 IP 直连会被 bot 检测拦。
    实现：优先 Invidious 实例的 local=true 代理下载完整音频/视频流的做法
    不适用于视频（太大），所以视频片段仍走 yt-dlp --download-sections，
    但让 yt-dlp 挂上 Hermes 浏览器 profile 的 cookie（有效登录态 + 新鲜指纹），
    也就是和 Level 1/2 同一套网络策略：能用就用，失败给诚实诊断。
"""

import os
import re
import subprocess
import tempfile
from typing import Any, Dict, List, Optional


def _hhmmss(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600:01d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def _ts_to_sec(ts: str) -> float:
    parts = [float(x) for x in ts.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def _browser_cookie_args() -> List[str]:
    """Hermes 托管浏览器 profile 的 cookie（Chrome 登录态副本）。

    Keychain 拒绝时 yt-dlp 解不出 cookie（Extracted 0 cookies），
    那就退回无 cookie 模式 —— 诊断里如实说。
    """
    prof = os.path.expanduser("~/.hermes/browser-profile/chrome")
    if not os.path.isdir(prof):
        return []
    return ["--cookies-from-browser", f"chrome:{prof}"]


def grab_frames(url: str, at: str, window: float = 30.0,
                fps: float = 0.5, scene_detect: bool = False,
                max_frames: int = 12,
                outdir: Optional[str] = None) -> Dict[str, Any]:
    """下载 [at-window/2, at+window/2] 的视频片段并抽帧。

    at:            时间戳 "00:12:30" 或 "750"（秒）
    window:        抓取窗口秒数（默认 30s）
    fps:           固定抽帧率（0.5 = 每 2 秒一帧）
    scene_detect:  True 时用场景变化检测（适合 PPT/图表切换），忽略 fps
    max_frames:    最多保留多少帧（防爆内存）
    返回 {"ok": True, "frames": [路径...], "clip": 路径}
    """
    try:
        center = _ts_to_sec(at)
    except ValueError:
        return {"ok": False, "reason": f"时间戳格式不对：{at}（应为 00:12:30 或秒数）"}
    start = max(0.0, center - window / 2)
    end = center + window / 2
    seg = f"*{_hhmmss(start)}-{_hhmmss(end)}"

    td = outdir or tempfile.mkdtemp(prefix="ed-frames-")
    clip = os.path.join(td, "clip.mp4")

    # ① 下载片段（带浏览器 cookie 走现有网络策略）
    p = subprocess.run(
        ["yt-dlp", *_browser_cookie_args(),
         "-f", "bv*[height<=720]+ba/b[height<=720]/bv*/b",  # 抽帧不需要 4K
         "--download-sections", seg,
         "--force-keyframes-at-cuts",
         "--no-warnings", "-o", clip, url],
        capture_output=True, text=True, timeout=600)
    if p.returncode != 0 or not os.path.exists(clip) \
            or os.path.getsize(clip) < 10000:
        err = (p.stderr or "").strip().splitlines()[-1][:200] if p.stderr \
            else "片段下载失败"
        # YouTube 被 IP 拦是最常见的失败，诊断里给对应下一步
        if "Sign in" in err or "bot" in err:
            err += " —— 本机 IP 被 YouTube 拦，需换节点或等 Invidious 支持视频代理"
        return {"ok": False, "reason": err}

    # ② 抽帧：场景检测 或 固定帧率
    vf = (f"select='gt(scene,0.3)',showinfo" if scene_detect
          else f"fps={fps}")
    pattern = os.path.join(td, "frame_%03d.jpg")
    p2 = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", clip,
         "-vf", vf, "-vsync", "vfr", "-frames:v", str(max_frames), pattern],
        capture_output=True, text=True, timeout=300)
    if p2.returncode != 0:
        return {"ok": False, "reason": f"抽帧失败：{(p2.stderr or '')[:200]}"}

    frames = sorted(
        os.path.join(td, f) for f in os.listdir(td)
        if f.startswith("frame_") and f.endswith(".jpg"))
    if not frames:
        return {"ok": False, "reason": "片段太静态，没有抽到帧（可调低 scene 阈值或换 fps）"}
    return {"ok": True, "frames": frames[:max_frames], "clip": clip,
            "window": {"start": _hhmmss(start), "end": _hhmmss(end),
                       "center": _hhmmss(center)},
            "count": len(frames[:max_frames])}


def detect_visual_cues(text: str) -> List[Dict[str, Any]]:
    """从逐字稿里检测「看屏幕」类提示 —— Level 3 的自动触发线索。

    返回 [{"ts": "00:12:30", "cue": "大家看屏幕上这张表"}]
    """
    pats = [
        r"看(?:一下)?(?:这个|屏幕|画面|图表|这张|这个图)",
        r"屏幕上(?:的)?",
        r"如(?:图|表)所示",
        r"(?:这个|下面这个)(?:表格|图表|截图|画面)",
    ]
    out: List[Dict[str, Any]] = []
    seen = set()
    for ln in text.splitlines():
        m = re.match(r"\[(\d{1,2}:\d{2}:\d{2})\]\s*(.*)", ln.strip())
        if not m:
            continue
        ts, body = m.group(1), m.group(2)
        for pat in pats:
            mm = re.search(pat, body)
            if mm and ts not in seen:
                seen.add(ts)
                out.append({"ts": ts, "cue": body[:80]})
                break
        if len(out) >= 20:
            break
    return out
