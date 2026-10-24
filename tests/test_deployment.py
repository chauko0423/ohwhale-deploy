import hashlib
import http.server
import importlib.util
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from ohwhale import config
from ohwhale.download import destination, fetch, matches


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ohwhale 配置 ")
        self.root = Path(self.temp.name).resolve()
        self.env = patch.dict(os.environ, {"OHWHALE_HOME": str(self.root)}, clear=True)
        self.env.start()
        config.reset()

    def tearDown(self):
        config.reset()
        self.env.stop()
        self.temp.cleanup()

    def test_missing_secret_has_no_default(self):
        with self.assertRaisesRegex(ValueError, "ASTRBOT_DASHBOARD_PASSWORD"):
            config.secret("ASTRBOT_DASHBOARD_PASSWORD")

    def test_relocation_and_nested_override(self):
        (self.root/"config.json").write_text(json.dumps({"master":{"qq":"100001"},"paths":{"logs":"新日志"}}),encoding="utf-8")
        self.assertEqual(config.get("master.qq"),"100001")
        self.assertEqual(config.get("master.bili_uid"),0)
        self.assertEqual(Path(config.path("logs")),self.root/"新日志")
        self.assertTrue(Path(config.path("voice_ref")).is_relative_to(self.root))

    def test_dotenv_is_data_not_shell(self):
        (self.root/".env").write_text('TEST_KEY="$(do-nothing)`literal`"\n',encoding="utf-8")
        self.assertEqual(config.secret("TEST_KEY"),'$(do-nothing)`literal`')
        with patch.dict(os.environ,{"TEST_KEY":"environment"}):
            self.assertEqual(config.secret("TEST_KEY"),"environment")

    def test_default_has_no_owner(self):
        self.assertEqual(config.get("master.qq"),"")
        self.assertEqual(config.get("master.session"),"")
        self.assertEqual(config.get("master.bili_uid"),0)

    def test_missing_acceptance_never_passes(self):
        from ohwhale.deploy import verify
        # The schema belongs to the package; only local evidence is isolated.
        example=Path(config.__file__).resolve().parents[1]/"config/acceptance.example.json"
        (self.root/"config").mkdir()
        (self.root/"config/acceptance.example.json").write_bytes(example.read_bytes())
        report=verify(6)
        self.assertFalse(report["ok"])
        self.assertTrue(all(not c["ok"] for c in report["checks"]))

    def test_occupied_port_does_not_launch_or_kill(self):
        import socket
        from ohwhale.deploy import start
        with socket.socket() as bound:
            bound.bind(("127.0.0.1",0));bound.listen()
            (self.root/"config.json").write_text(json.dumps({"services":{"astrbot_url":f"http://127.0.0.1:{bound.getsockname()[1]}"}}),encoding="utf-8")
            with patch("ohwhale.deploy.verify",return_value={"ok":True}),patch("ohwhale.deploy.subprocess.Popen") as launch,patch("ohwhale.deploy.stop") as stop:
                with self.assertRaisesRegex(ValueError,"occupied"):
                    start()
                launch.assert_not_called();stop.assert_not_called()

    def test_reused_pid_is_not_terminated(self):
        from unittest.mock import Mock
        from ohwhale.deploy import stop
        data=self.root/"data";data.mkdir()
        (data/"processes.json").write_text(json.dumps([{"pid":12345,"created":1,"cmdline":["test"]}]),encoding="utf-8")
        other=Mock();other.create_time.return_value=2
        with patch("psutil.Process",return_value=other):
            self.assertTrue(stop()["ok"])
        other.terminate.assert_not_called()

    def test_phone_start_selects_answer_mode_without_running_clients(self):
        from unittest.mock import Mock
        from ohwhale.deploy import start
        for enabled, core_only in ((True, False), (False, False), (True, True)):
            with self.subTest(phone=enabled, core_only=core_only):
                (self.root/"config.json").write_text(json.dumps({"features":{"phone":enabled}}),encoding="utf-8")
                config.reset()
                record=self.root/"data/processes.json"
                if record.exists():record.unlink()
                process=Mock()
                process.create_time.return_value=1.0
                process.cmdline.return_value=["fixture"]
                with patch("ohwhale.deploy.verify",return_value={"ok":True}), \
                     patch("ohwhale.deploy.socket.socket"), \
                     patch("ohwhale.deploy.Path.is_file",return_value=True), \
                     patch("ohwhale.deploy.Path.is_dir",return_value=True), \
                     patch("ohwhale.deploy.subprocess.Popen",return_value=Mock(pid=12345)) as launch, \
                     patch("psutil.Process",return_value=process):
                    self.assertTrue(start(core_only=core_only)["ok"])
                calls=[call.args[0] for call in launch.call_args_list
                       if any(str(arg).endswith("call_agent.py") for arg in call.args[0])]
                self.assertEqual(len(calls),int(enabled and not core_only))
                if enabled and not core_only:self.assertEqual(calls[0][-1],"answer")
                if core_only:
                    launched=[call.args[0] for call in launch.call_args_list]
                    self.assertEqual(len(launched),2)
                    self.assertEqual(launched[0][1],"api_v2.py")
                    self.assertEqual(launched[1][-1],"run")


class Handler(http.server.BaseHTTPRequestHandler):
    payload=b"verified model content"*128
    received=[]
    def do_GET(self):
        type(self).received.append((self.path,self.headers.get("Range")))
        if self.path=="/bad":
            body=b"x"*len(self.payload)
            self.send_response(200)
        else:
            offset=int(self.headers.get("Range","bytes=0-").split("=")[1].split("-")[0])
            body=self.payload[offset:]
            self.send_response(206 if offset else 200)
            if offset:self.send_header("Content-Range",f"bytes {offset}-{len(self.payload)-1}/{len(self.payload)}")
        self.send_header("Content-Length",str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self,*args):pass


class DownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=http.server.ThreadingHTTPServer(("127.0.0.1",0),Handler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
        cls.url=f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="ohwhale-download-")
        self.root=Path(self.temp.name)
        self.entry={"id":"fixture","destination":"models/test.bin","size":len(Handler.payload),
                    "sha256":hashlib.sha256(Handler.payload).hexdigest(),"urls":[self.url+"/good"]}
        Handler.received.clear()

    def tearDown(self):self.temp.cleanup()

    def test_hash_checked_and_idempotent(self):
        fetch(self.entry,self.root,allow_http=True,backoff=0)
        first=len(Handler.received)
        self.assertEqual(fetch(self.entry,self.root,allow_http=True),"already satisfied")
        self.assertEqual(len(Handler.received),first)

    def test_resume_partial(self):
        target=destination(self.entry,self.root);target.parent.mkdir()
        target.with_name(target.name+".part").write_bytes(Handler.payload[:37])
        fetch(self.entry,self.root,allow_http=True,backoff=0)
        self.assertEqual(Handler.received[0][1],"bytes=37-")
        self.assertEqual(target.read_bytes(),Handler.payload)

    def test_corrupt_source_falls_back(self):
        self.entry["urls"]=[self.url+"/bad",self.url+"/good"]
        fetch(self.entry,self.root,attempts=1,allow_http=True,backoff=0)
        self.assertEqual([x[0] for x in Handler.received],["/bad","/good"])

    def test_corruption_never_becomes_final(self):
        self.entry["urls"]=[self.url+"/bad"]
        with self.assertRaises(RuntimeError):fetch(self.entry,self.root,attempts=1,allow_http=True,backoff=0)
        self.assertFalse(destination(self.entry,self.root).exists())

    def test_path_traversal_rejected(self):
        self.entry["destination"]="../outside.bin"
        with self.assertRaises(ValueError):fetch(self.entry,self.root,allow_http=True)

    def test_cleartext_sources_rejected(self):
        with self.assertRaises(ValueError):fetch(self.entry,self.root)


if __name__=="__main__":unittest.main()
