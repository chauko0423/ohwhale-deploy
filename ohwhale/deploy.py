"""Installation helpers. All checks report their actual scope and fail closed."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from .config import get, path, root, secret, settings
from .download import fetch, matches, destination


def result(stage, checks):
    return {"ok": all(c["ok"] for c in checks), "stage": stage, "checks": checks}


def catalog():
    return json.loads((root() / "config/downloads.json").read_text("utf-8"))["files"]


def verify(stage):
    checks = []
    def check(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
    if stage == 0:
        check("os", platform.system() == "Windows" and platform.machine().endswith("64"), "Windows x64 required")
        check("python", sys.version_info >= (3, 12), platform.python_version())
        free = shutil.disk_usage(root()).free
        check("disk", free >= 10 * 1024 ** 3, f"{free // 1024 ** 3} GiB free")
    elif stage == 1:
        for line in (root() / "requirements.txt").read_text().splitlines():
            if not line or line.startswith("#"):
                continue
            name, _, expected = line.partition("==")
            try:
                version = importlib.metadata.version(name)
                check(name, not expected or expected == version, version)
            except importlib.metadata.PackageNotFoundError:
                check(name, False, "not installed")
    elif stage == 2:
        for entry in catalog():
            target = destination(entry, root())
            check(entry["id"], matches(target, entry["size"], entry["sha256"]), "size + SHA256 check")
        for key in ("llbot_exe", "tts_python"):
            check(key, Path(path(key)).is_file(), path(key))
        check("tts_source", (Path(path("gpt_sovits")) / "api_v2.py").is_file(), "Install the upstream GPT-SoVITS runtime separately")
    elif stage == 3:
        check("local_config", (root() / "config.json").is_file(), "config.json must be created and edited")
        for key in (get("llm.chat.key_env"), "DASHSCOPE_API_KEY", "ASTRBOT_DASHBOARD_PASSWORD"):
            check(key, bool(secret(key, required=False)), "presence only; no API call or balance check")
        check("master.qq", str(get("master.qq", "")).isdigit(), "Set your own owner QQ")
        check("persona", Path(path("persona")).is_file(), "default persona text")
        if get("features.bilibili"):
            for key in ("master.session", "master.bili_uid", "master.bili_name"):
                check(key, bool(get(key)), "required for Bilibili sharing")
    elif stage == 4:
        for name in ("astrbot_url", "tts_url"):
            parsed = urlsplit(get("services." + name))
            try:
                with socket.create_connection((parsed.hostname, parsed.port or 80), timeout=2):
                    pass
                check(name, True, "TCP reachable only; does not establish successful QQ login")
            except OSError:
                check(name, False, "service unreachable")
        evidence = read_evidence()
        for name in ("qq_logged_in", "qq_text_reply", "persona_imported"):
            check(name, evidence.get(name) is True, "manual confirmation from this installation required")
    elif stage == 5:
        completed = subprocess.run([sys.executable, str(root()/"scripts/audio_doctor.py"), "--json"],
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        try:
            report = json.loads(completed.stdout)
            check("audio_doctor", completed.returncode == 0 and report.get("ok") is True, report)
        except ValueError:
            check("audio_doctor", False, "Doctor failed before producing JSON; check dependencies and audio devices")
    elif stage == 6:
        e = read_evidence()
        check("fresh_machine", e.get("fresh_machine") is True, "fresh Windows installation must be verified")
        check("recorded_at", bool(e.get("recorded_at")), "record the test date")
        features = json.loads((root()/"config/acceptance.example.json").read_text("utf-8"))["functional"]
        for name in features:
            check(name, e.get("functional", {}).get(name) is True, "requires observed functional test")
        limits = {"answer_first_audio_s":2.5,"asr_cost_s":0.3,"tts_first_chunk_s":0.5,
                  "interrupt_false_positive":0.1,"reply_len_avg":35}
        values = e.get("metrics", {})
        for name, upper in limits.items():
            value=values.get(name)
            check(name, type(value) in (int,float) and 0 <= value <= upper, f"observed {value}; limit {upper}")
        fps=values.get("avatar_fps")
        check("avatar_fps", type(fps) in (int,float) and fps >= 15, f"observed {fps}; minimum 15")
        for name in ("interrupted", "teased", "casual_chat"):
            check("experience."+name, e.get("experience", {}).get(name) is True, "human listening review required")
        check("doctor_all_pass", e.get("doctor_all_pass") is True, "full audio check on test machine")
    return result(stage, checks)


def read_evidence():
    p = Path(path("data")) / "acceptance.json"
    return json.loads(p.read_text("utf-8")) if p.exists() else {}


def configure():
    created=[]
    for source, target in (("config/config.example.json","config.json"),("config/.env.example",".env")):
        p=root()/target
        if not p.exists():
            shutil.copyfile(root()/source,p)
            created.append(target)
    for name in ("data","logs","astrbot"):
        Path(path(name)).mkdir(parents=True,exist_ok=True)
    target=Path(path("tts_config"))
    if not target.exists():
        target.parent.mkdir(parents=True,exist_ok=True)
        custom={"device":"cuda","is_half":True,"version":"v2Pro",
                "t2s_weights_path":path("gpt_weights"),"vits_weights_path":path("sovits_weights"),
                "bert_base_path":str(Path(path("pretrained_dir"))/"chinese-roberta-wwm-ext-large"),
                "cnhuhbert_base_path":str(Path(path("pretrained_dir"))/"chinese-hubert-base")}
        target.write_text(json.dumps({"custom":custom},ensure_ascii=False,indent=2),encoding="utf-8")
        created.append("TTS local configuration")
    # Install source plugins only; never copy cookies, chat history, QR codes or state.
    plugin_root=Path(path("astrbot"))/"data/plugins"
    for p in (root()/"plugins").rglob("*"):
        if p.is_file() and p.suffix in (".py",".yaml",".md"):
            out=plugin_root/p.relative_to(root()/"plugins")
            if out.exists() and out.read_bytes()!=p.read_bytes():
                raise ValueError(f"Existing plugin differs; review before updating {out.name}")
            out.parent.mkdir(parents=True,exist_ok=True)
            if not out.exists():shutil.copyfile(p,out)
    return {"ok":True,"created":created,"next":"Edit config.json and .env; configure AstrBot providers, QQ and persona as described in QUICKSTART.md"}


def processes_path():
    return Path(path("data"))/"processes.json"


def stop():
    import psutil
    record=processes_path()
    if not record.exists():return {"ok":True,"stopped":0}
    owned=json.loads(record.read_text("utf-8"))
    stopped=0
    pending=[]
    for item in reversed(owned):
        try:
            p=psutil.Process(item["pid"])
            if abs(p.create_time()-item["created"])>0.01 or p.cmdline()!=item["cmdline"]:
                continue
            children=p.children(recursive=True)
            p.terminate()
            for child in children:
                try:child.terminate()
                except psutil.Error:pass
            alive=psutil.wait_procs([p,*children],timeout=5)[1]
            if alive:pending.append(item)
            else:stopped+=1
        except psutil.NoSuchProcess:pass
        except psutil.Error:pending.append(item)
    record.write_text(json.dumps(pending,indent=2),encoding="utf-8")
    return {"ok":not pending,"stopped":stopped,"remaining":len(pending)}


def start():
    import psutil
    required=verify(3)
    if not required["ok"]:return required
    record=processes_path()
    if record.exists() and json.loads(record.read_text("utf-8")):
        raise ValueError("Owned process records exist; stop this installation first")
    # A pre-existing listener may be the user's production environment.
    for key in ("astrbot_url","tts_url"):
        parsed=urlsplit(get("services."+key))
        if parsed.hostname not in ("127.0.0.1","localhost"):
            raise ValueError("Local launcher requires loopback service addresses")
        with socket.socket() as sock:
            try:sock.bind((parsed.hostname,parsed.port))
            except OSError:raise ValueError(f"{key} port is occupied; no process was stopped")
    commands=[("tts",[path("tts_python"),"api_v2.py","-a","127.0.0.1","-p",str(urlsplit(get("services.tts_url")).port),"-c",path("tts_config")],path("gpt_sovits")),
              ("astrbot",[str(Path(path("python")).with_name("astrbot.exe")),"run"],path("astrbot")),
              ("qq",[path("llbot_exe")],str(Path(path("llbot_exe")).parent))]
    extras=["watchdog"]
    if get("features.phone"):extras.append("call_agent")
    if get("features.avatar"):extras.append("avatar_live")
    if get("features.mic_guard"):extras.append("mic_guard")
    commands.extend((n,[path("python"),"-u",str(root()/"scripts"/(n+".py"))],str(root())) for n in extras)
    for name,argv,cwd in commands:
        if not Path(argv[0]).is_file() or not Path(cwd).is_dir():
            raise ValueError(f"Missing executable or work directory for {name}")
    owned=[]
    record.parent.mkdir(parents=True,exist_ok=True)
    Path(path("logs")).mkdir(parents=True,exist_ok=True)
    try:
        for name,argv,cwd in commands:
            with (Path(path("logs"))/(name+".log")).open("ab") as out:
                p=subprocess.Popen(argv,cwd=cwd,stdout=out,stderr=subprocess.STDOUT,
                    creationflags=0 if name=="qq" else getattr(subprocess,"CREATE_NO_WINDOW",0))
            proc=psutil.Process(p.pid)
            owned.append({"name":name,"pid":p.pid,"created":proc.create_time(),"cmdline":proc.cmdline()})
            record.write_text(json.dumps(owned,indent=2),encoding="utf-8")
    except Exception:
        stop()
        raise
    return {"ok":True,"status":"processes-launched-not-verified","next":"Complete QQ login, then verify stages 4, 5 and 6"}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("action",choices=["verify","configure","download","start","stop"])
    parser.add_argument("--stage",type=int,choices=range(7),default=3)
    args=parser.parse_args()
    try:
        if args.action=="verify":report=verify(args.stage)
        elif args.action=="configure":report=configure()
        elif args.action=="download":
            entries=catalog()
            outcomes=[]
            for entry in entries:
                outcomes.append({"file":entry["id"],"status":fetch(entry,root())})
            report={"ok":True,"downloads":outcomes}
        elif args.action=="start":report=start()
        else:report=stop()
    except Exception as exc:
        report={"ok":False,"status":"BLOCKED","action":args.action,"detail":str(exc)}
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report["ok"] else 1


if __name__=="__main__":
    sys.exit(main())
