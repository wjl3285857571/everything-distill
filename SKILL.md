---
name: everything-distill
description: 把书/论文/网页/视频蒸馏成蒸馏版或洞察版。Use when 蒸馏、讲透、提取洞察。
---

# everything-distill · 万物皆可蒸

把任意输入（书 / 论文 / 网页 / 视频 / 播客 / 帖文 / 本地音视频）蒸馏成一份
**给人读的蒸馏版**，或者一份**给 AI 用的洞察版**。

这是一个独立 CLI，不绑定 Hermes：任何能跑 shell 的 agent 都能用；
不能跑 shell 的 agent 走 `plan → prompt → collect` 编排模式。

## 什么时候用

- 用户说「把这本书蒸一下」「讲给我听」「我想读懂」「这视频讲了什么」
- 用户说「蒸给 AI」「拿给 AI 看」「提取关键洞察」
- 需要把长文/视频压成一份能读的文档，而不是一堆摘要卡片

## 依赖

- Python 3.9+（只用标准库）
- 可选：`ffmpeg` / `yt-dlp`（视频）、`mutool`（PDF，`brew install mupdf-tools`）
- 不需要本地模型，不要求任何平台登录

## 安装

```bash
git clone https://github.com/wjl3285857571/everything-distill.git
cd everything-distill
python3 -m ed --help
```

## 模型通道（任选一个）

`--runner auto` 是默认值：按 `Hermes → OpenAI 兼容 → claude/codex/llm` 的顺序探测。

| 通道 | 用法 | 适合 |
|---|---|---|
| 自动 | `--runner auto` | 不确定环境时 |
| Hermes | `--runner hermes` | 本机有 `hermes -z` |
| OpenAI 兼容 | `--runner openai --base-url https://api.deepseek.com/v1 --model deepseek-chat` | OpenAI / DeepSeek / Ollama / vLLM / 自建网关 |
| 任意 CLI | `--runner command --command '["claude","-p","{prompt}"]'` | Claude Code / Codex / 其他 agent CLI |
| 文件 | `--runner file --command result.json` | 跨进程编排，结果由外部写入 |

OpenAI 兼容通道从 `OPENAI_API_KEY` 读 key（可用 `--api-key-env` 改），
base URL 和模型也可用 `OPENAI_BASE_URL` / `OPENAI_MODEL` 指定。

## 默认产物：蒸馏版

```bash
# 长书：--fast 按章采样，一次调用出概述（快，细节少）
python3 -m ed run 书.epub --workdir runs/书名 --out 书名-蒸馏版.md --fast

# 想讲透：不加 --fast，先概述，再逐章讲
python3 -m ed run 书.epub --workdir runs/书名 --out 书名-蒸馏版.md
```

流程：`ingest → brief（先讲一遍）→ narrate（长内容逐章）→ render`。
文档第一节永远是「这内容在讲什么」，后面才是分章正文。
少于 3000 字的内容只出概述，不逐章、不做个人关联。

## 给 AI 的产物：洞察版

```bash
python3 -m ed run 书.epub --workdir runs/书名 --out 书名-蒸馏版.md --deep
# 额外得到 书名-洞察版.md：结构表 + 关键洞察（说法/边界/原文/位置/ID）
```

也可以分步：

```bash
python3 -m ed stage1 --workdir runs/书名
python3 -m ed stage2 --workdir runs/书名      # 短内容会自动跳过
python3 -m ed render --workdir runs/书名 --mode insights --out 书名-洞察版.md
```

## 交付格式（重要）

每次蒸馏完，先给用户 3–5 句「这内容讲的是什么」，再贴出完整文件（或附件）。
不要把「文件已生成，路径是…」当成回答——用户要的是先听懂，再看细节。

文件本身也以「这内容在讲什么」开头，和聊天里的概述对得上。

## 无 shell 的 agent / 并行编排

```bash
python3 -m ed ingest 输入 --workdir runs/x
python3 -m ed plan --workdir runs/x          # 生成 _batches/batch_*.prompt
# 由 agent 逐个执行 prompt，结果写回 _batches/batch_*.out
python3 -m ed collect --workdir runs/x       # 合并 + 硬校验
python3 -m ed stage2-prompt --workdir runs/x --out _stage2_prompt.txt
# 执行后写 _stage2.out
python3 -m ed collect2 --workdir runs/x
python3 -m ed render --workdir runs/x --mode narrative --out 蒸馏版.md
```

## 不要做

- 不要把扫描件当文本硬蒸（会直接报错，让用户先 OCR）
- 不要撞付费墙（喜马拉雅付费内容会报 `paywalled`）
- 不要为了「有个人关联」硬凑（找不到有依据的关联就留空）
- 不要用「讲述版」作为文件名后缀：默认叫 `-蒸馏版`，给 AI 的叫 `-洞察版`
