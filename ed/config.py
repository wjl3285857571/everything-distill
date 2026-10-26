"""项目级配置：定位项目根目录、读取 .env。

为什么单独抽出来：
    以前 key 的路径写死在 `~/Projects/everything-distill/.env`。
    用户把仓库克隆到别的目录、或者用 pip 安装后，程序就找不到 .env，
    报「没配置 key」——但用户明明配了。路径应该相对**项目本身**，
    而不是相对某台机器的固定目录。

查找顺序（先命中先用）：
    1. 环境变量 `ED_ENV_FILE` 指定的文件（显式覆盖）
    2. 项目根目录的 .env（即 ed/ 的上一级）
    3. 当前工作目录的 .env（从别处运行时也能用）
    4. 旧路径 ~/Projects/everything-distill/.env（向后兼容）
    5. 进程环境变量
"""

import os
from typing import List


def project_root() -> str:
    """项目根目录：ed/config.py -> ed/ -> 根。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def env_file_candidates() -> List[str]:
    """按优先级返回可能存在的 .env 路径（去重保序）。"""
    paths: List[str] = []
    explicit = os.environ.get("ED_ENV_FILE", "").strip()
    if explicit:
        paths.append(os.path.expanduser(explicit))
    paths.append(os.path.join(project_root(), ".env"))
    paths.append(os.path.join(os.getcwd(), ".env"))
    paths.append(os.path.expanduser("~/Projects/everything-distill/.env"))

    out: List[str] = []
    seen = set()
    for p in paths:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _parse_env_file(path: str):
    """.env 里的 KEY=VALUE 逐行产出；忽略注释和空行。"""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                yield k.strip(), v.strip().strip('"').strip("'")
    except OSError:
        return


def get_env(name: str, default: str = "") -> str:
    """按查找顺序取一个配置值；找不到返回 default。"""
    for path in env_file_candidates():
        if not os.path.isfile(path):
            continue
        for k, v in _parse_env_file(path):
            if k == name and v:
                return v
    return os.environ.get(name, default)


def get_all_env(prefix: str) -> List[str]:
    """取所有以 prefix 开头的值（用于 GROQ_API_KEY / GROQ_API_KEY2..9）。

    文件里的顺序优先，环境变量补在后面；重复值只留一次。
    """
    values: List[str] = []
    for path in env_file_candidates():
        if not os.path.isfile(path):
            continue
        for k, v in _parse_env_file(path):
            if k.startswith(prefix) and v and v not in values:
                values.append(v)
    for k, v in os.environ.items():
        if k.startswith(prefix) and v and v not in values:
            values.append(v)
    return values
