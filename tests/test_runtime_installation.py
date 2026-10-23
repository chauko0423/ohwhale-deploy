import hashlib
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from ohwhale import config
from ohwhale.runtime import install_tts_source


class RuntimeInstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="ohwhale-runtime-")
        self.base=Path(self.temp.name).resolve()
        self.env=patch.dict(os.environ,{"OHWHALE_HOME":str(self.base)},clear=True)
        self.env.start();config.reset()
        (self.base/"config").mkdir()

    def tearDown(self):
        config.reset();self.env.stop();self.temp.cleanup()

    def archive(self, names):
        archive=self.base/"fixture.zip"
        with zipfile.ZipFile(archive,"w") as z:
            for name,data in names.items():z.writestr(name,data)
        (self.base/"config/runtimes.json").write_text(json.dumps({"gpt_sovits":{
            "commit":"fixture", "overrides":[], "archive":{
                "id":"fixture","destination":"fixture.zip","size":archive.stat().st_size,
                "sha256":hashlib.sha256(archive.read_bytes()).hexdigest(),"urls":[]}}}),encoding="utf-8")

    def test_unknown_existing_file_prevents_all_writes(self):
        self.archive({"source/api_v2.py":"new API", "source/user.py":"upstream"})
        runtime=self.base/"runtime/GPT-SoVITS";runtime.mkdir(parents=True)
        (runtime/"user.py").write_text("user change")
        with self.assertRaisesRegex(ValueError,"Existing TTS source differs"):
            install_tts_source()
        self.assertFalse((runtime/"api_v2.py").exists())
        self.assertEqual((runtime/"user.py").read_text(),"user change")

    def test_traversal_is_rejected_before_extraction(self):
        self.archive({"source/api_v2.py":"API", "source/../../outside.py":"unsafe"})
        with self.assertRaisesRegex(ValueError,"Unsafe source archive path"):
            install_tts_source()
        self.assertFalse((self.base/"runtime/outside.py").exists())
        self.assertFalse((self.base/"runtime/GPT-SoVITS/api_v2.py").exists())

    def test_running_installation_refuses_source_updates(self):
        self.archive({"source/api_v2.py":"API"})
        (self.base/"data").mkdir()
        (self.base/"data/processes.json").write_text('[{"pid":12345}]')
        with self.assertRaisesRegex(ValueError,"Stop this installation"):
            install_tts_source()
        self.assertFalse((self.base/"runtime/GPT-SoVITS/api_v2.py").exists())


if __name__=="__main__":unittest.main()
