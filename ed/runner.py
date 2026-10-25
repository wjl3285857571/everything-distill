"""
模型调用抽象。

设计要点：**不锁死模型**。
蒸馏结果会因所用模型而异，所以模型由调用方（宿主）决定，
本项目只负责「怎么问」和「怎么接住」，不负责「用哪个模型」。

实现方式：
    HermesRunner   —— 调 `hermes -z`（用 Hermes 的默认模型）
    CommandRunner  —— 调任意外部命令（claude -p / codex exec / …）
    FileRunner     —— 从文件读结果（用于人工编排或跨进程交接）
"""

import json
import os
import shutil
import subprocess
from typing import Dict, List, Optional


class RunnerError(RuntimeError):
    pass


class Runner:
    """模型调用接口。"""

    def run(self, system: str, user: str) -> str:
        raise NotImplementedError

    def run_json(self, system: str, user: str) -> Dict:
        """调用并解析 JSON。容错：模型常在 JSON 外面套 ```json 或加解释文字。"""
        raw = self.run(system, user)
        return parse_json_loose(raw)


def parse_json_loose(raw: str) -> Dict:
    """从模型输出里抠出 JSON。"""
    text = raw.strip()
    if not text:
        raise RunnerError("模型返回空")
    # 去掉 ```json 包裹
    if text.startswith("```"):
        parts = text.split("```")
        for p in parts:
            p = p.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            if p.startswith("{") or p.startswith("["):
                text = p
                break
    # 直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 找最外层的 {...}
    start = text.find("{")
    while start >= 0:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    raise RunnerError("模型输出里找不到合法 JSON：\n" + raw[:600])


class HermesRunner(Runner):
    """调 `hermes -z`。用 Hermes 当前配置的默认模型 —— 不在本项目里写死。"""

    def __init__(self, binary: str = "hermes", extra_args: Optional[List[str]] = None,
                 cwd: Optional[str] = None, timeout: int = 900):
        self.binary = shutil.which(binary) or binary
        self.extra_args = extra_args or []
        self.cwd = cwd
        self.timeout = timeout

    def run(self, system: str, user: str) -> str:
        prompt = user
        if system:
            prompt = system.strip() + "\n\n---\n\n" + user
        cmd = [self.binary, "-z", prompt] + self.extra_args
        try:
            p = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=self.timeout, cwd=self.cwd,
            )
        except subprocess.TimeoutExpired:
            raise RunnerError(f"模型调用超时（{self.timeout}s）")
        if p.returncode != 0:
            raise RunnerError(
                f"模型调用失败（exit {p.returncode}）：{p.stderr[:400]}"
            )
        return p.stdout


class CommandRunner(Runner):
    """调任意外部命令。

    模板里 {prompt} 会被替换成完整 prompt；{system} 与 {user} 分别可用。
    命令的 stdout 视为模型输出。
    """

    def __init__(self, template: List[str], timeout: int = 900, cwd: Optional[str] = None):
        self.template = template
        self.timeout = timeout
        self.cwd = cwd

    def run(self, system: str, user: str) -> str:
        full = (system.strip() + "\n\n---\n\n" + user) if system else user
        cmd = [t.replace("{prompt}", full)
               .replace("{system}", system or "")
               .replace("{user}", user)
               for t in self.template]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=self.timeout, cwd=self.cwd)
        except subprocess.TimeoutExpired:
            raise RunnerError(f"命令超时（{self.timeout}s）")
        if p.returncode != 0:
            raise RunnerError(f"命令失败（exit {p.returncode}）：{p.stderr[:400]}")
        return p.stdout


class FileRunner(Runner):
    """从文件读结果。用于跨进程/人工编排：编排者自己调模型，把结果落盘。"""

    def __init__(self, path: str):
        self.path = path

    def run(self, system: str, user: str) -> str:
        if not os.path.exists(self.path):
            raise RunnerError(f"找不到结果文件：{self.path}")
        with open(self.path, encoding="utf-8") as f:
            return f.read()


def default_runner(kind: str = "hermes", **kwargs) -> Runner:
    """按名字取一个 runner。"""
    if kind == "hermes":
        return HermesRunner(**kwargs)
    if kind == "file":
        return FileRunner(**kwargs["path"])
    if kind == "command":
        return CommandRunner(**kwargs["template"], **{
            k: v for k, v in kwargs.items()
            if k in ("timeout", "cwd")
        })
    raise ValueError(f"未知的 runner：{kind}")
