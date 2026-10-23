"""Install pinned upstream sources and reviewed runtime corrections."""
from __future__ import annotations

import hashlib
import argparse
import json
import stat
import zipfile
from pathlib import Path, PurePosixPath

from .config import path, root
from .download import destination, fetch


def digest(data):
    return hashlib.sha256(data).hexdigest()


def install_tts_source():
    item=json.loads((root()/"config/runtimes.json").read_text("utf-8"))["gpt_sovits"]
    target=Path(path("gpt_sovits")).resolve()
    allowed=(root()/"runtime").resolve()
    if not target.is_relative_to(allowed) or target == allowed:
        raise ValueError("TTS source must stay inside this installation's runtime directory")
    fetch(item["archive"],root())
    archive=destination(item["archive"],root())
    files={}
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            parts=PurePosixPath(info.filename).parts
            if len(parts)<2 or info.is_dir():continue
            if stat.S_ISLNK(info.external_attr>>16):
                raise ValueError("Archive symbolic links are not accepted")
            relative=Path(*parts[1:])
            out=(target/relative).resolve()
            if not out.is_relative_to(target) or ".." in parts or "\\" in info.filename:
                raise ValueError("Unsafe source archive path")
            files[relative.as_posix()]=z.read(info)
    if "api_v2.py" not in files:
        raise ValueError("Pinned archive has no API entry point")
    originals={name:digest(data) for name,data in files.items()}
    for patch in item["overrides"]:
        name=patch["path"]
        if originals.get(name)!=patch["original_sha256"]:
            raise ValueError(f"Unexpected upstream content for {name}")
        source=(root()/"third-party/gpt-sovits-overrides"/name).resolve()
        if not source.is_relative_to((root()/"third-party/gpt-sovits-overrides").resolve()):
            raise ValueError("Unsafe override path")
        data=source.read_bytes()
        if digest(data)!=patch["patched_sha256"]:
            raise ValueError(f"Override checksum failed for {name}")
        files[name]=data
    writes=[]
    for name,data in files.items():
        out=target/name
        if out.exists():
            actual=digest(out.read_bytes())
            if actual==digest(data):continue
            if actual!=originals[name]:
                raise ValueError(f"Existing TTS source differs; no files changed: {name}")
        writes.append((out,data))
    record=Path(path("data"))/"processes.json"
    if writes and record.exists() and json.loads(record.read_text("utf-8")):
        raise ValueError("Stop this installation before changing runtime sources")
    for out,data in writes:
        out.parent.mkdir(parents=True,exist_ok=True)
        out.write_bytes(data)
    return {"ok":True,"status":"source-installed","commit":item["commit"],
            "files_written":len(writes),"runtime_started":False}


def install_ll_client():
    entry=json.loads((root()/"config/client-downloads.json").read_text("utf-8"))["files"][0]
    target=Path(path("llbot_exe")).resolve().parent
    allowed=(root()/"runtime").resolve()
    if not target.is_relative_to(allowed) or target==allowed:
        raise ValueError("Client must stay inside this installation's runtime directory")
    fetch(entry,root())
    files=[]
    with zipfile.ZipFile(destination(entry,root())) as z:
        if "LuckyLillia.exe" not in z.namelist():raise ValueError("Client executable missing from archive")
        for info in z.infolist():
            if info.is_dir():continue
            out=(target/info.filename).resolve()
            if not out.is_relative_to(target) or "\\" in info.filename or stat.S_ISLNK(info.external_attr>>16):
                raise ValueError("Unsafe client archive path")
            data=z.read(info)
            if out.exists():
                if digest(out.read_bytes())!=digest(data):
                    raise ValueError(f"Existing client file differs; no files changed: {info.filename}")
                continue
            files.append((out,data))
    record=Path(path("data"))/"processes.json"
    if files and record.exists() and json.loads(record.read_text("utf-8")):
        raise ValueError("Stop this installation before changing the client")
    for out,data in files:
        out.parent.mkdir(parents=True,exist_ok=True)
        with out.open("xb") as f:f.write(data)
    return {"ok":True,"status":"client-unpacked","version":entry["tag"],"files_written":len(files),
            "logged_in":False,"next":"Install official QQ, choose headed mode in LuckyLillia, configure reverse WebSocket, and scan your own login QR code."}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("component",choices=["tts-source","ll-client"],nargs="?",default="tts-source")
    args=parser.parse_args()
    try:report=install_tts_source() if args.component=="tts-source" else install_ll_client()
    except Exception as exc:report={"ok":False,"status":"BLOCKED","detail":str(exc)}
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report["ok"] else 1


if __name__=="__main__":raise SystemExit(main())
