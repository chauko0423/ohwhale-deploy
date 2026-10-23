"""Resume downloads, verify every byte, and publish a file only after validation."""
from __future__ import annotations

import hashlib
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


def tls_context() -> ssl.SSLContext:
    """Use system roots plus Mozilla roots for standalone Python installations."""
    import certifi

    context = ssl.create_default_context()
    context.load_verify_locations(cafile=certifi.where())
    return context


def matches(filename: Path, size: int, digest: str) -> bool:
    if not filename.is_file() or filename.stat().st_size != size:
        return False
    with filename.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest() == digest


def destination(entry: dict, root: Path) -> Path:
    relative = Path(entry["destination"])
    target = (root / relative).resolve()
    if relative.is_absolute() or not target.is_relative_to(root.resolve()):
        raise ValueError("Download destination must stay inside the installation")
    return target


def fetch(entry: dict, root: Path, *, attempts=3, allow_http=False, backoff=1) -> str:
    target = destination(entry, root)
    size, digest = entry["size"], entry["sha256"]
    if not isinstance(size, int) or size <= 0 or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("A positive file size and SHA256 are required")
    if matches(target, size, digest):
        return "already satisfied"
    if not entry.get("urls"):
        raise ValueError(f"No published URL for {entry['id']}; place the verified file at {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    last_error = "no source"
    for url in entry["urls"]:
        scheme = urlsplit(url).scheme
        if scheme != "https" and not (allow_http and scheme == "http"):
            raise ValueError("Download sources must use HTTPS")
        context = tls_context() if scheme == "https" else None
        for attempt in range(attempts):
            try:
                if matches(partial, size, digest):
                    os.replace(partial, target)
                    return "verified partial file"
                offset = partial.stat().st_size if partial.exists() else 0
                if offset >= size:
                    partial.unlink()
                    offset = 0
                request = urllib.request.Request(url, headers={"User-Agent": "OhWhale/0.1"})
                if offset:
                    request.add_header("Range", f"bytes={offset}-")
                with urllib.request.urlopen(request, timeout=20, context=context) as response:
                    if not allow_http and urlsplit(response.url).scheme != "https":
                        raise ValueError("Refusing an insecure download redirect")
                    resume = offset and response.status == 206
                    if response.status == 206:
                        content_range = response.headers.get("Content-Range", "")
                        if not content_range.startswith(f"bytes {offset}-"):
                            raise ValueError("Invalid range response")
                    with partial.open("ab" if resume else "wb") as out:
                        total = offset if resume else 0
                        while chunk := response.read(1024 * 1024):
                            total += len(chunk)
                            if total > size:
                                raise ValueError("Download exceeded the declared size")
                            out.write(chunk)
                if not matches(partial, size, digest):
                    if partial.stat().st_size >= size:
                        partial.unlink()
                    raise ValueError("Size or SHA256 mismatch")
                os.replace(partial, target)
                return "downloaded and verified"
            except (OSError, ValueError, urllib.error.URLError) as exc:
                last_error = type(exc).__name__ + ": " + str(exc)
                if attempt + 1 < attempts:
                    time.sleep(backoff * (2 ** attempt))
    raise RuntimeError(f"BLOCKED: stage2 {entry['id']}: {last_error}")
