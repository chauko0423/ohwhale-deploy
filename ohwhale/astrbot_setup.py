"""Provision a new AstrBot 4.29.1 profile without reading another installation."""
from __future__ import annotations

import asyncio
import copy
from contextlib import closing
import hashlib
import importlib.metadata
import json
import os
import secrets
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from .config import get, path, root, secret


def managed_path(key):
    target = Path(path(key)).resolve()
    runtime = (root() / "runtime").resolve()
    if not target.is_relative_to(runtime) or target == runtime:
        raise ValueError(f"{key} must be inside this installation's runtime directory")
    return target


def make_profile(defaults, hash_password, hash_md5, validate_password):
    owner = str(get("master.qq", ""))
    if not owner.isdigit():
        raise ValueError("Set master.qq before provisioning AstrBot")
    password = secret("ASTRBOT_DASHBOARD_PASSWORD")
    validate_password(password)
    dashboard = urlsplit(get("services.astrbot_url"))
    if dashboard.hostname not in ("127.0.0.1", "localhost") or not dashboard.port:
        raise ValueError("AstrBot dashboard must use a loopback address and explicit port")
    tts = urlsplit(get("services.tts_url"))
    if tts.hostname not in ("127.0.0.1", "localhost") or not tts.port:
        raise ValueError("Local TTS must use a loopback address and explicit port")
    config = copy.deepcopy(defaults)
    config["admins_id"] = [owner]
    config["dashboard"].update({
        "username": get("services.dashboard_username"), "host": "127.0.0.1",
        "port": dashboard.port, "password": hash_md5(password),
        "pbkdf2_password": hash_password(password), "password_storage_upgraded": True,
        "password_change_required": False, "jwt_secret": secrets.token_urlsafe(48),
    })
    config["provider_sources"] = [
        {"id": "ohwhale_chat", "provider": "deepseek", "type": "openai_chat_completion",
         "provider_type": "chat_completion", "enable": True,
         "key": [secret(get("llm.chat.key_env"))], "api_base": get("llm.chat.base_url"),
         "timeout": 120, "proxy": "", "custom_headers": {}},
        {"id": "ohwhale_local_voice", "provider": "gsv_local", "type": "gsv_tts_selfhost",
         "provider_type": "text_to_speech", "enable": True, "key": [],
         "api_base": f"http://127.0.0.1:{tts.port}"},
    ]
    config["provider"] = [
        {"id": "ohwhale_chat", "model": get("llm.chat.model"), "enable": True,
         "provider_type": "chat_completion", "capability": "chat_completion",
         "provider_source_id": "ohwhale_chat"},
        {"id": get("voice.local_provider_id"), "enable": True, "provider": "gsv_local",
         "type": "gsv_tts_selfhost", "provider_type": "text_to_speech", "capability": "tts",
         "provider_source_id": "ohwhale_local_voice", "timeout": 60,
         "api_base": f"http://127.0.0.1:{tts.port}",
         "gsv_default_parms": {
             "gsv_text_lang": get("voice.text_lang"), "gsv_ref_audio_path": path("voice_ref"),
             "gsv_prompt_text": get("voice.prompt_text"), "gsv_prompt_lang": get("voice.prompt_lang"),
             "gsv_text_split_method": "cut0", "gsv_media_type": "wav",
             "gsv_temperature": 0.7, "gsv_top_p": 0.9,
         }},
    ]
    runner = config["agent_runner"]["config"]
    runner["model"]["provider_id"] = "ohwhale_chat"
    runner["persona"]["persona_id"] = "default"
    config["provider_tts_settings"]["enable"] = False
    config["platform"] = [{
        "id": get("services.platform_id", "qq_ohwhale"), "type": "aiocqhttp", "enable": True,
        "ws_reverse_host": "127.0.0.1", "ws_reverse_port": int(get("services.onebot_port")),
        "ws_reverse_token": secret("ONEBOT_ACCESS_TOKEN", required=False),
    }]
    # The owner's friend chat is allowed by AstrBot's existing admin bypass.
    config["platform_settings"]["enable_id_white_list"] = True
    config["platform_settings"]["id_whitelist"] = []
    return config


async def provision():
    if importlib.metadata.version("astrbot") != "4.29.1":
        raise ValueError("This profile adapter is verified against AstrBot 4.29.1 only")
    runtime = managed_path("astrbot")
    config_file = managed_path("astrbot_config")
    database_file = managed_path("astrbot_db")
    for target in (config_file, database_file):
        if not target.is_relative_to(runtime):
            raise ValueError("AstrBot configuration and database must be in its own runtime directory")
    marker = runtime / "ohwhale-profile.json"
    prompt = Path(path("persona")).read_text("utf-8").strip()
    if not prompt:
        raise ValueError("Persona text is empty")
    if marker.exists():
        saved = json.loads(marker.read_text("utf-8"))
        if not config_file.exists() or not database_file.exists():
            raise ValueError("Provisioning record exists but configuration/database is missing")
        if hashlib.sha256(config_file.read_bytes()).hexdigest() != saved["config_sha256"]:
            raise ValueError("AstrBot configuration was edited; preserve it and use the dashboard for further changes")
        import sqlite3
        with closing(sqlite3.connect(database_file.as_uri()+"?mode=ro", uri=True)) as db:
            row = db.execute("SELECT system_prompt FROM personas WHERE persona_id=?", ("default",)).fetchone()
        if row is None or row[0] != prompt:
            raise ValueError("The installed persona differs; review it in the dashboard")
        return {"ok": True, "status": "already-provisioned", "online_tested": False}
    if config_file.exists() or database_file.exists():
        raise ValueError("Existing AstrBot profile detected; no files overwritten. Use a new installation directory")
    runtime.mkdir(parents=True, exist_ok=True)
    config_file.parent.mkdir(parents=True, exist_ok=True)
    database_file.parent.mkdir(parents=True, exist_ok=True)
    lock = runtime / ".ohwhale-setup.lock"
    with lock.open("x", encoding="utf-8"):
        pass
    try:
        # Importing astrbot.core itself creates a default profile. Isolate that
        # side effect in a new temporary directory owned by this operation.
        with tempfile.TemporaryDirectory(prefix=".ohwhale-setup-", dir=runtime) as folder:
            staging = Path(folder).resolve()
            if not staging.is_relative_to(runtime):
                raise ValueError("Unexpected staging directory")
            os.environ["ASTRBOT_ROOT"] = str(staging)
            from astrbot.core.config.default import DEFAULT_CONFIG
            from astrbot.core.db.sqlite import SQLiteDatabase
            from astrbot.core.utils.auth_password import (
                hash_dashboard_password, hash_md5_dashboard_password, validate_dashboard_password,
            )
            config = make_profile(DEFAULT_CONFIG, hash_dashboard_password, hash_md5_dashboard_password, validate_dashboard_password)
            staged_db = staging / "persona.db"
            db = SQLiteDatabase(str(staged_db))
            try:
                await db.initialize()
                await db.insert_persona("default", prompt)
            finally:
                await db.engine.dispose()
            # Exclusive destinations: an unrelated process can never be overwritten.
            with database_file.open("xb") as out:
                out.write(staged_db.read_bytes())
            with config_file.open("x", encoding="utf-8") as out:
                json.dump(config, out, ensure_ascii=False, indent=2)
        marker.write_text(json.dumps({"adapter": 1, "astrbot": "4.29.1",
            "config_sha256": hashlib.sha256(config_file.read_bytes()).hexdigest(),
            "persona_sha256": hashlib.sha256(prompt.encode()).hexdigest()}, indent=2), encoding="utf-8")
    finally:
        lock.unlink()
    return {"ok": True, "status": "profile-created", "online_tested": False,
            "next": "Configure QQ reverse WebSocket, log in, then test a real owner message. Scheduled tasks and cloud voice remain separate setup steps."}


def main():
    try:
        report = asyncio.run(provision())
    except Exception as exc:
        report = {"ok": False, "status": "BLOCKED", "detail": str(exc)}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
