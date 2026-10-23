"""Read project-relative configuration without touching another installation."""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path


def root() -> Path:
    return Path(os.environ.get("OHWHALE_HOME") or Path(__file__).resolve().parents[1]).resolve()


def _merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


@lru_cache(maxsize=1)
def settings() -> dict:
    template = Path(__file__).resolve().parents[1] / "config/config.example.json"
    data = json.loads(template.read_text(encoding="utf-8-sig"))
    local = Path(os.environ.get("OHWHALE_CONFIG") or root() / "config.json")
    if not local.is_absolute():
        local = root() / local
    if local.exists():
        override = json.loads(local.read_text(encoding="utf-8-sig"))
        if not isinstance(override, dict):
            raise ValueError("config.json must contain a JSON object")
        data = _merge(data, override)
    return data


def get(key: str, default=None):
    value = settings()
    for part in key.split("."):
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return value


def path(key: str) -> str:
    value = get("paths." + key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"请先填写 config.json: paths.{key}")
    p = Path(os.path.expandvars(value)).expanduser()
    return str(p.resolve() if p.is_absolute() else (root() / p).resolve())


def project_path(relative: str) -> str:
    return str((root() / relative).resolve())


def state_path(relative: str) -> str:
    p = Path(path("data")) / relative
    p.parent.mkdir(parents=True, exist_ok=True)
    return str(p)


def log_path(name: str) -> str:
    p = Path(path("logs")) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    return str(p)


@lru_cache(maxsize=1)
def _dotenv() -> dict[str, str]:
    p = root() / ".env"
    if not p.exists():
        return {}
    values = {}
    for number, raw in enumerate(p.read_text("utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f".env 第 {number} 行格式错误（需要 NAME=value）")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def secret(name: str, required: bool = True) -> str:
    value = os.environ.get(name, _dotenv().get(name, "")).strip()
    if required and (not value or value.startswith("YOUR_")):
        raise ValueError(f"请先在 .env 或环境变量中填写 {name}（不会读取其他安装的密钥）")
    return value


def llm(kind: str = "chat") -> tuple[str, str, str]:
    prefix = "llm." + kind
    return (secret(get(prefix + ".key_env")),
            get(prefix + ".base_url").rstrip("/"),
            get(prefix + ".model"))


def require(key: str):
    value = get(key)
    if value is None or value == "" or value == 0:
        raise ValueError(f"请先填写 config.json: {key}")
    return value


def reset() -> None:
    settings.cache_clear()
    _dotenv.cache_clear()
