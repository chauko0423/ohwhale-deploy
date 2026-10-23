import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


@unittest.skipUnless(importlib.util.find_spec("astrbot"), "Install AstrBot to test real profile provisioning")
class AstrBotProfileTests(unittest.TestCase):
    def test_new_profile_rerun_and_existing_edit(self):
        with tempfile.TemporaryDirectory(prefix="ohwhale-profile-") as folder:
            base=Path(folder)
            (base/"persona").mkdir()
            (base/"persona/default.md").write_text("Fixture persona.",encoding="utf-8")
            (base/"config.json").write_text(json.dumps({"master":{"qq":"100001"}}),encoding="utf-8")
            env=dict(os.environ,OHWHALE_HOME=str(base),PYTHONPATH=str(Path(__file__).resolve().parents[1]),
                     PYTHONUTF8="1",DEEPSEEK_API_KEY="fixture-not-real",ASTRBOT_DASHBOARD_PASSWORD="FixtureOnly-12345")
            def run():
                return subprocess.run([sys.executable,"-m","ohwhale.astrbot_setup"],env=env,cwd=base,
                                      capture_output=True,text=True,encoding="utf-8",timeout=90)
            first=run()
            self.assertEqual(first.returncode,0,first.stdout[-500:]+first.stderr[-500:])
            file=base/"runtime/AstrBot/data/cmd_config.json"
            original=file.read_bytes()
            profile=json.loads(original)
            self.assertEqual(profile["admins_id"],["100001"])
            self.assertEqual(profile["dashboard"]["host"],"127.0.0.1")
            self.assertEqual(profile["agent_runner"]["config"]["model"]["provider_id"],"ohwhale_chat")
            self.assertFalse(profile["provider_tts_settings"]["enable"])
            self.assertNotIn(b"FixtureOnly-12345",original)
            with closing(sqlite3.connect(base/"runtime/AstrBot/data/data_v4.db")) as db:
                self.assertEqual(db.execute("SELECT system_prompt FROM personas").fetchall(),[("Fixture persona.",)])
            self.assertEqual(run().returncode,0)
            self.assertEqual(file.read_bytes(),original)
            file.write_text("{}",encoding="utf-8")
            self.assertEqual(run().returncode,1)
            self.assertEqual(file.read_text(),"{}")


if __name__=="__main__":unittest.main()
