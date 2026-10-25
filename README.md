# everything-distill · 万物皆可蒸

[中文](#中文) | [English](#english)

---

<a id="中文"></a>

## 这是什么

**everything-distill** 是一个把「任何内容」蒸馏成「一份你能看懂、且与你相关的理解文档」的确定性管线。

不管输入是一本书、一篇论文、一个视频链接（B 站 / YouTube / 抖音 / 小红书）、一期播客、一条推文、一个网页，还是一份本地音视频文件——它都会产出**同一结构**的 Markdown 文档：

```
一、这内容在讲什么        ← 30 秒概览
二、怎么切进去            ← 初次理解的建议（1-3 条）
三、内容本身              ← 结构 / 关键洞察 / 失效场景 / 专有名词（主体）
四、跟你有关              ← 「眼下能用得上」+「值得想」两档，封顶 30 条
依据                      ← 论文式编号引用，正文 [1] 可跳转
```

它不做摘要机器。它的目标是：**读完这一份文档，你不用再去读原著，且每一条结论都能回溯到原文。**

## 核心设计原则

### 1. 装上就能用

不部署任何本地模型，不要求任何平台登录态。本地代码只负责：下载（yt-dlp / ffmpeg）、清洗、校验、渲染。「听」（语音转写）交给云端 ASR，「懂」（理解与蒸馏）交给会话里的模型。

### 2. 两段式蒸馏，处境不污染内容

- **第一段（无处境）**：纯粹按内容本身的重要性提炼，不掺任何用户信息——避免「因为他正在求职就只看到求职相关内容」的取样偏差
- **第二段（有处境）**：拿着第一段结果 + 读者画像做匹配重组——因为读了内容再套处境，不会硬凑关联

### 3. 硬校验，不信模型的自述

模型说的每一句话都被程序验证：

| 校验 | 机制 |
|---|---|
| 摘录真实性 | 每条洞察必须附原文逐字摘录，程序回原文做归一化比对，对不上整条作废 |
| 归属正确性 | 摘录定位不到声明的块时，按摘录在原文中的真实位置重算 |
| 回执一致性 | 每批的「回执条数」必须与实际解析条目数相等 |
| 关联挂依据 | 「跟你有关」的每一条都必须挂依据编号，无依据的关联 = 失败（这是防拍马屁的机制） |
| 时间戳可回溯 | 视频转录稿统一为 `[HH:MM:SS]` 格式，每条观点可跳回原视频对应时刻 |

### 4. 免费层优先，全程可降级

所有云端依赖都有免费档，且每条链路都有多层降级。以 YouTube 为例：

```
① Supadata 字幕（秒级，免费 100 积分/月）
② yt-dlp 直连下载音频 → Groq 转写（免费 2000 请求/天）
③ Invidious 公共实例代理（慢，但 IP 被拦时永不失效）
④ 明确报错 + 给出下一步（换 IP / 手动粘贴），绝不假装成功
```

## 支持的输入

| 类型 | 平台/格式 | 链路要点 |
|---|---|---|
| 视频 | B 站 | yt-dlp 直连，字幕优先 |
| 视频 | YouTube | Supadata → yt-dlp → Invidious 三层降级 |
| 视频 | 抖音 | 官方游客 ttwid 接口 + 分享页直链解析（免登录） |
| 视频/图文 | 小红书 | 页面 `__INITIAL_STATE__` 解析，视频/图文自动分流 |
| 播客 | 小宇宙 / 喜马拉雅 | 公开接口直取音频（付费内容诚实报付费墙） |
| 社交帖 | X/Twitter | fxtwitter 公共镜像 API（免登录） |
| 社交帖 | 微博 | 官方游客 cookie 自动领取 + 移动端 JSON 接口 |
| 论文 | arXiv（链接或 ID） | 官方 HTML 全文 + 导出 API 元数据，保留章节层级与公式 |
| 网页 | 任意博客/文章 | HTML 抓取 + 正文清洗 |
| 本地文件 | epub / pdf / docx / txt / md / srt / vtt / mp3 / mp4 / wav / webm | 直接解析 |
| 本地文件 | 音视频（任意上面格式） | 直接进云端 ASR，秒级出时间戳逐字稿 |

## 快速开始

### 依赖

```bash
# 系统级（仅需两个命令行工具，装完不再管）
brew install yt-dlp ffmpeg        # macOS
# apt install yt-dlp ffmpeg       # Debian/Ubuntu

# Python 3.10+
python3 -m venv .venv && source .venv/bin/activate
pip install websocket-client      # 仅测试需要
```

### API keys（两个免费账号，共 2 分钟）

```bash
cp .env.example .env
# 编辑 .env 填入：
# SUPADATA_API_KEY  → https://supadata.ai （免费 100 积分/月，无需信用卡）
# GROQ_API_KEY      → https://console.groq.com/keys （免费 2000 请求/天）
```

> 没有 key 也能跑文本蒸馏（书/文档/网页），key 只影响视频音频链路。
> key 缺失时会得到明确的 `missing_key` 诊断和注册指引，绝不会静默失败。

### 蒸馏一条视频链接

```bash
# 一步：链接 → 带时间戳的逐字稿（自动识别平台、自动降级）
python3 -m ed transcript "https://www.bilibili.com/video/BVxxxx" --out transcript.txt

# 或者看这个链接能不能处理、会走哪条路（只诊断不下载）
python3 -m ed transcript "https://youtu.be/xxxx" --diagnose-only

# 二步：逐字稿 → 工作区
python3 -m ed ingest transcript.txt --workdir runs/my-video

# 三步：生成批次 prompt，交给你的 Agent / 会话模型执行
python3 -m ed plan --workdir runs/my-video
#   → 每个 prompt 文件是一个独立任务（见「编排模式」）

# 四步：收结果、校验、个性化、渲染
python3 -m ed collect --workdir runs/my-video
python3 -m ed stage2-prompt --workdir runs/my-video --out _stage2_prompt.txt
#   → 第二段 prompt 交给模型，产出写 _stage2.out
python3 -m ed collect2 --workdir runs/my-video
python3 -m ed render --workdir runs/my-video --out 蒸馏理解.md
```

### 蒸馏一本书

```bash
python3 -m ed ingest 书.epub --workdir runs/书名
python3 -m ed plan --workdir runs/书名
# ... 同上
```

### 只拿视频元数据 / 按时间点抽帧

```bash
python3 -m ed metadata "https://youtu.be/xxxx"        # 标题/作者/章节
python3 -m ed frames "https://youtu.be/xxxx" --at 00:12:30 --scene   # 局部抽帧
```

## 编排模式（谁来调模型）

`ed` 本身**不调用任何 LLM**——它负责所有确定性环节（解析、切块、prompt 生成、校验、渲染），把「调模型」留给宿主：

- **Hermes Agent**：用 `delegate_task` 并行派子 Agent 执行每批 prompt（本项目即此用法）
- **Claude Code / Codex / 其他 Agent**：把 prompt 文件直接丢给它们，产出写回 `.out` 文件
- **手动**：人肉把 prompt 贴给任意聊天模型，把回答存成 `.out`

这个设计让管线与模型解耦：换模型、换供应商、换 Agent 框架，`ed` 一行不用改。

## 项目结构

```
ed/
  __main__.py          CLI 入口
  cli.py               命令定义（transcript/ingest/plan/collect/render/...）
  schema.py            数据模型（Insight / Stage1 / Stage2 / Basis）
  workspace.py         工作区读写
  runner.py            模型调用抽象（宿主注入）
  orchestrate.py       编排（批次生成 / 结果校验合并）
  ingest/
    base.py            epub/pdf/docx/html/txt 解析 + 网页/X/微博适配器
    video.py           视频链接 → 逐字稿（平台识别 + 多层降级链）
    cn_platforms.py    抖音 / 小红书 专属解析器
    podcast.py         小宇宙 / 喜马拉雅
    invidious.py       YouTube 的 Invidious 代理兜底层
    supadata.py        Supadata 字幕 API 客户端
    transcript.py      逐字稿清洗合并（[HH:MM:SS] 紧凑格式）
    metadata.py        视频元数据 + 章节提取
    frames.py          Level 3 按需抽帧（场景检测）
  distill/
    stage1.py          第一段：内容蒸馏（批处理 / 回执 / 校验）
    stage2.py          第二段：结合读者处境
    horizon.py         「时间距离」分档（增量补标）
    select.py          机械筛选（轮转采样）
  render/
    document.py        最终文档渲染（分档 / 依据 / 去重）
  verify/
    skill_meta.py      产物元信息校验
templates/             第一段/第二段的 prompt 模板
tests/                 48 项回归测试
```

## 实测性能（Apple M5）

| 场景 | 耗时 |
|---|---|
| 27 分钟无字幕 YouTube 视频（链接→逐字稿） | ≈ 1.5 分钟 |
| 同视频有字幕时 | ≈ 11 秒 |
| 抖音/小红书链接解析 | 1.4 秒 |
| 27 分钟音频纯转写（Groq 云端） | ≈ 20 秒 |
| 《穷查理宝典》全书（32.5 万字） | 第一段 ≈ 35 个并行任务，硬校验 100% 通过 |
| arXiv 论文（5,606 字 → 10 块） | 抓取解析 ≈ 3 秒，第一段 2 批共 ≈ 15 分钟，硬校验 94/94 通过 |

## 设计决策（为什么这么做）

**为什么不本地部署 Whisper？**
第一目标是「装上就能用」：要求用户先装 PyTorch + 下载 1.5GB 模型，就把 90% 的潜在使用者挡在门外。云端 ASR（Groq）免费额度内速度更快（217x 实时）、专有名词更准，本地引擎仅作为 `--asr-engine local` 显式兜底保留。

**为什么两段式而不是一段？**
一段式蒸馏时，用户的处境（如「正在找工作」）会让模型只关注相关内容，遗漏其他。分离后第一段保证覆盖度，第二段保证相关性，两头都要。

**为什么「跟你有关」要封顶 30 条？**
实测模型会给读者生成 68 条关联，其中 9 条在说同一件事——重复的关联是噪音不是价值。封顶 + 按主题分散（每处境最多 2 条）逼出的是多样性而非数量。

**为什么失败要说清原因？**
「失败了」是废物诊断。每条失败都带 `reason_code`（`ip_blocked` / `needs_login` / `paywalled` / `platform_changed` / `missing_key`...）和对应下一步。用户不该在黑盒前猜。

## 论文模式

arXiv 链接或 ID 直接丢进来：

```bash
python3 -m ed ingest 1706.03762 --workdir runs/attention
python3 -m ed ingest https://arxiv.org/abs/1706.03762 --workdir runs/attention
```

### 取的是哪一份

arXiv 官方会给论文生成 HTML 版（`arxiv.org/html/<id>`），带章节标签。我们抓这一份，而不是下载 PDF 再抽文字：

- **章节层级是真的**：`<section>` / `<h2>` 就是论文的第 1、2、3 章，切块按章断开
- **公式是 LaTeX 源**：`<math alttext="h_{t}">` 里存着原始公式，保留成 `$h_{t}$`
- **图表标题保留**：图注里的结论读得到；表格里的数字矩阵丢掉（压成纯文本没有意义）
- **元数据走官方导出 API**：标题、作者、年份、原始摘要

整条路零 key、零登录、零模型。arXiv 没生成 HTML 的旧论文会明确告诉你「这篇没有 HTML 版，请给我 PDF」，不静默失败。

### 论文专属的追问

论文有自己的骨架，通用追问集不够。论文进来时会额外挂一段追问：

| 记什么 | 为什么 |
|---|---|
| 研究问题 | 不是「背景介绍」，是作者认为现有做法哪里不行 |
| 方法机制 | 输入 → 操作 → 输出，以及关键设计取舍 |
| 结果（带数字） | 结果类条目的 `claim` 必须出现具体数字 |
| 消融与对照 | 收益来自哪个组件，不是笼统的「我们更好」 |
| 局限 | 作者承认的边界；作者没承认但明显没验证的，标注成推断 |

`type` 字段在论文里换成论文语义：`claim` / `method` / `result` / `ablation` / `limitation` / `term` / `question`。

### 边界必须分三态

论文最容易被写错的地方就是边界。硬性要求区分：

1. 作者**明确写了**适用条件 → 照抄那个条件
2. 作者**只在某设置下验证** → 写「仅在 X 下验证，未讨论 Y」
3. 作者**完全没提** → 写「作者未说明」

不许因为「看起来应该只对英文有效」就替论文写边界——那是读者的推断，不是论文的结论。

### 摘要不能当结果

论文摘要会把结果说得很漂亮。摘要里的说法在正文找不到对应实验支撑时，这条只能记成 `claim`，不能记成 `result`。程序会拿 `support` 回原文逐字比对，数字对不上整条作废。

## 已知限制

- 抖音/小红书解析依赖页面结构，平台改版需跟进（历史上都跟上了）
- 微博批量抓取会触发风控，单条链接使用稳定
- 喜马拉雅付费内容不提供音频（版权墙，诚实报错而非假装失败）
- 第一段能保证「每块都被读过」，不保证「每个重要洞察都被记下」
- 蒸馏质量取决于所用模型——本项目只保证**结构可信**（摘录可定位、关联有依据），不保证观点正确

## 鸣谢

方法论与部分平台适配思路参考了开源社区（[chubbyguan/chubbyskills](https://github.com/chubbyguan/chubbyskills)、[imlewc/video-to-subtitle-summary-skill](https://github.com/imlewc/video-to-subtitle-summary-skill)），代码为独立实现。分级蒸馏管道的设计受 Gemini 原生音视频工作流的启发，并针对「装上就能用」原则做了重构。

## License

[MIT](LICENSE)

---

<a id="english"></a>

# everything-distill (English)

**everything-distill** is a deterministic pipeline that distills *any content* — books, papers, video links (Bilibili / YouTube / Douyin / Xiaohongshu), podcasts, tweets, web pages, or local media files — into **one readable, personally relevant Markdown document** with a fixed structure:

```
1. What is this about          ← 30-second overview
2. How to approach it          ← 1–3 entry suggestions
3. The content itself          ← structure / key insights / failure modes / glossary
4. What it means for you       ← "usable today" + "worth thinking" tiers, capped at 30
References                     ← numbered citations, [1] jumps back
```

It is not a summarizer. Its goal: **after reading this one document, you don't need to read the original — and every claim can be traced back to it.**

## Design Principles

### 1. Works out of the box
No local model deployment. No platform logins. Local code only downloads (yt-dlp/ffmpeg), cleans, validates, and renders. "Listening" (speech-to-text) goes to cloud ASR; "understanding" goes to your session's LLM.

### 2. Two-stage distillation keeps context from polluting content
Stage 1 distills purely by the content's own importance, blind to the reader. Stage 2 then matches those distilled insights against the reader's profile. This prevents both sampling bias (only seeing job-hunting content because the reader is job-hunting) and forced relevance.

### 3. Hard validation — never trust the model's self-report
Every claim the model makes is programmatically verified:

| Check | Mechanism |
|---|---|
| Excerpt authenticity | Each insight must carry a verbatim quote from the source; normalized substring matching; failure rejects the whole insight |
| Attribution correctness | If an excerpt isn't found in its claimed block, the true location is recomputed from the source text |
| Receipt consistency | Each batch's declared count must equal the parsed count |
| Grounded relevance | Every "relevant to you" item must cite a basis ID; ungrounded relevance = failure (an anti-sycophancy mechanism) |
| Timestamp grounding | Video transcripts are normalized to `[HH:MM:SS]`; every insight links back to the exact moment |

### 4. Free-tier first, always degradable
Every cloud dependency has a free tier, and every link has fallback layers. For YouTube:

```
① Supadata captions (seconds, 100 free credits/mo)
② yt-dlp direct download → Groq ASR (2000 free requests/day)
③ Invidious public instance proxy (slow, but works even when your IP is blocked)
④ Explicit error + next step (change IP / paste manually) — never fake success
```

## Supported Inputs

| Type | Platform | Notes |
|---|---|---|
| Video | Bilibili | yt-dlp direct, subtitles first |
| Video | YouTube | Supadata → yt-dlp → Invidious, three-layer fallback |
| Video | Douyin (TikTok CN) | Official guest ttwid + share-page direct link, no login |
| Video/Post | Xiaohongshu | `__INITIAL_STATE__` parsing, video vs image-note auto-routing |
| Podcast | Xiaoyuzhou / Ximalaya | Public API, honest paywall errors |
| Social post | X/Twitter | fxtwitter public mirror API, no login |
| Social post | Weibo | Auto-fetched guest cookie + mobile JSON API |
| Paper | arXiv (link or ID) | Official HTML full text + export-API metadata, keeps section structure and formulas |
| Web page | Any blog/article | HTML fetch + content extraction |
| Local files | epub / pdf / docx / txt / md / srt / vtt / mp3 / mp4 / wav / webm | Direct parsing |
| Local media | Any of the above | Straight into cloud ASR, seconds to timestamped transcript |

## Quick Start

### Dependencies

```bash
brew install yt-dlp ffmpeg        # macOS
python3 -m venv .venv && source .venv/bin/activate
pip install websocket-client      # tests only
```

### API keys (two free accounts, ~2 minutes)

```bash
cp .env.example .env
# SUPADATA_API_KEY  → https://supadata.ai (100 free credits/mo, no card)
# GROQ_API_KEY      → https://console.groq.com/keys (2000 free req/day)
```

> Text distillation (books/documents/pages) works without any keys.
> Missing keys produce explicit `missing_key` diagnostics with sign-up links — never silent failures.

### Distill a video link

```bash
python3 -m ed transcript "https://www.bilibili.com/video/BVxxxx" --out transcript.txt
python3 -m ed ingest transcript.txt --workdir runs/my-video
python3 -m ed plan --workdir runs/my-video
#   → dispatch each prompt file to your agent / LLM
python3 -m ed collect --workdir runs/my-video
python3 -m ed stage2-prompt --workdir runs/my-video --out _stage2_prompt.txt
#   → stage-2 prompt to the model, output to _stage2.out
python3 -m ed collect2 --workdir runs/my-video
python3 -m ed render --workdir runs/my-video --out distilled.md
```

### Distill a book

```bash
python3 -m ed ingest book.epub --workdir runs/book
python3 -m ed plan --workdir runs/book
# ... same as above
```

## Orchestration

`ed` itself **calls no LLM** — it handles every deterministic step (parsing, chunking, prompt generation, validation, rendering) and leaves model invocation to the host:

- **Hermes Agent**: `delegate_task` fans out batches in parallel (this project's primary usage)
- **Claude Code / Codex / any agent**: feed prompt files, write outputs back as `.out`
- **Manual**: paste prompts into any chat model, save responses as `.out`

This keeps the pipeline model-agnostic: swap models, providers, or agent frameworks without touching `ed`.

## Benchmark (Apple M5)

| Scenario | Time |
|---|---|
| 27-min YouTube video without captions (link → transcript) | ≈ 1.5 min |
| Same video with captions | ≈ 11 s |
| Douyin/XHS link resolution | 1.4 s |
| 27-min audio transcription (Groq cloud) | ≈ 20 s |
| Full book (325k chars) | Stage 1 ≈ 35 parallel tasks, 100% hard-validation pass |
| arXiv paper (5,606 words → 10 chunks) | Fetch + parse ≈ 3 s; stage 1, 2 batches ≈ 15 min; hard validation 94/94 |

## Paper Mode

Feed it an arXiv link or a bare ID:

```bash
python3 -m ed ingest 1706.03762 --workdir runs/attention
python3 -m ed ingest https://arxiv.org/abs/1706.03762 --workdir runs/attention
```

### Which copy it reads

arXiv generates an HTML edition of each paper (`arxiv.org/html/<id>`) with real section tags. We read that instead of downloading the PDF and scraping text:

- **Section hierarchy is real** — `<section>` / `<h2>` *are* chapters 1, 2, 3, so chunking breaks on chapter boundaries
- **Formulas stay as LaTeX** — `<math alttext="h_{t}">` carries the original source, kept as `$h_{t}$`
- **Figure and table captions are kept** — the conclusion in a caption is readable; the numeric matrix inside a table is dropped (flattening it to text means nothing)
- **Metadata comes from the official export API** — title, authors, year, original abstract

The whole path needs no key, no login, no model. Older papers without an HTML edition get an explicit "this paper has no HTML version, give me the PDF" instead of a silent failure.

### Paper-specific interrogation

A paper has its own skeleton, and the generic checklist is not enough. Papers get an extra prompt section:

| Record | Why |
|---|---|
| Research question | Not "background" — where the authors think existing practice fails |
| Method mechanism | Input → operation → output, plus the key design trade-off |
| Results with numbers | A `result` claim must carry a concrete number |
| Ablations and controls | Which component the gain comes from, not a vague "ours is better" |
| Limitations | Boundaries the authors admit; unverified ones get marked as inference |

`type` switches to paper semantics: `claim` / `method` / `result` / `ablation` / `limitation` / `term` / `question`.

### Boundaries must be three-state

This is where paper notes go wrong most often. Hard requirement:

1. The authors **state** a condition → copy that condition
2. The authors **only validate under one setting** → "validated only under X, Y not discussed"
3. The authors **say nothing** → "not stated by the authors"

Never write a boundary because it "obviously only works for English" — that is the reader's inference, not the paper's conclusion.

### An abstract is not a result

Abstracts make everything sound great. If an abstract claim has no matching experiment in the body, it can only be recorded as a `claim`, never a `result`. Every `support` excerpt is compared character-by-character against the source; a mismatched number voids the entry.

## Known Limitations

- Douyin/XHS parsers depend on page structure; platform redesigns require follow-up
- Weibo bulk scraping triggers anti-bot measures; single links are stable
- Ximalaya paid content is inaccessible (copyright wall) — honest error, not fake failure
- Stage 1 guarantees "every chunk was read", not "every important insight was captured"
- Distillation quality depends on the model used — this project guarantees **structural trust** (traceable excerpts, grounded relevance), not factual correctness

## Acknowledgments

Methodology and platform-adaptation ideas reference open-source work ([chubbyguan/chubbyskills](https://github.com/chubbyguan/chubbyskills), [imlewc/video-to-subtitle-summary-skill](https://github.com/imlewc/video-to-subtitle-summary-skill)); the implementation is independent. The tiered distillation design was inspired by Gemini's native audio/video workflow, restructured around a "works out of the box" principle.

## License

[MIT](LICENSE)
