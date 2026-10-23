"""Offline source, asset and accidental-secret checks; no service is contacted."""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", "__pycache__", "data", "logs", "cache", "runtime", "models", ".venv", ".venv-video", ".venv-tts"}
TEXT = {".py", ".md", ".json", ".yaml", ".yml", ".ps1", ".bat", ".cmd", ".toml", ".txt", ".example"}


def inspect(root=ROOT):
    problems = []
    files = [p for p in root.rglob("*") if p.is_file() and not (set(p.relative_to(root).parts) & SKIP)]
    private_pattern = re.compile(r"(?:[A-Z]:[/\\]+(?:XiaoManQQ|GPTSoVITS)|Users[/\\]+Administrator|\.work" + r"buddy)", re.I)
    key_pattern = re.compile(r"\bs" + r"k-[A-Za-z0-9_-]{16,}")
    cookie_value = re.compile(r"(?:SESSDATA|bili_jct|p_skey)\s*[=:]\s*[\"']?[A-Za-z0-9%_+/=-]{24,}", re.I)
    for p in files:
        relative = p.relative_to(root).as_posix()
        if p.name == ".env" or p.name in ("config.json", "cmd_config.json", "bili_cookie.json"):
            # These may exist locally but must be ignored by git.
            git = subprocess.run(["git", "check-ignore", "--quiet", "--no-index", str(p)], cwd=root, capture_output=True)
            if git.returncode != 0:
                problems.append({"file": relative, "reason": "private configuration is not ignored"})
            continue
        if p.suffix in TEXT or p.name in ("LICENSE", "NOTICE"):
            try:
                content = p.read_text("utf-8-sig")
            except UnicodeError:
                problems.append({"file":relative,"reason":"invalid UTF-8"})
                continue
            if p.suffix == ".py":
                try:
                    ast.parse(content, filename=relative)
                except SyntaxError as exc:
                    problems.append({"file":relative,"line":exc.lineno,"reason":"Python syntax error"})
            for line, text in enumerate(content.splitlines(), 1):
                if private_pattern.search(text) or key_pattern.search(text) or cookie_value.search(text):
                    problems.append({"file":relative,"line":line,"reason":"private path or credential pattern"})
        if p.stat().st_size > 95 * 1024 * 1024:
            problems.append({"file":relative,"reason":"large file belongs in a Release"})
    index = json.loads((root / "assets/stickers/index.json").read_text("utf-8"))
    seen = set()
    for entries in index["moods"].values():
        for entry in entries:
            base = (root / "assets/stickers").resolve()
            asset = (base / entry["path"]).resolve()
            if not asset.is_relative_to(base) or not asset.is_file():
                problems.append({"file":entry["path"],"reason":"invalid sticker reference"})
            seen.add(entry["path"])
    if len(seen) != index["total"]:
        problems.append({"file":"assets/stickers/index.json","reason":"incorrect sticker count"})
    code_bytes = sum(p.stat().st_size for p in files if "assets" not in p.relative_to(root).parts)
    if code_bytes >= 20 * 1024 * 1024:
        problems.append({"file":".","reason":"source exceeds 20 MiB"})
    return {"ok":not problems,"scope":"offline-source-only","files":len(files),"source_bytes":code_bytes,
            "stickers":len(seen),"moods":len(index["moods"]),"problems":problems}


if __name__ == "__main__":
    result = inspect()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["ok"] else 1)
