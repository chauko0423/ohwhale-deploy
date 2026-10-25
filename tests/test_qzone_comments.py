"""QQ 空间评论读取的离线测试（不联网、不依赖 AstrBot）。"""
import asyncio
import importlib.util
import json
import unittest
from pathlib import Path


def load_qzone():
    path = Path(__file__).parents[1] / "plugins" / "astrbot_plugin_qq_power" / "qzone.py"
    spec = importlib.util.spec_from_file_location("qzone_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeClient:
    """只替换网络层，解析逻辑走真实代码。"""

    def __init__(self, module, responses):
        self._module = module
        self._client = module.QzoneClient({}, "12345", 1)
        self._responses = responses

    def comments(self, tid, num=20):
        async def fake_req(method, url, **kw):
            if url in self._responses:
                return True, {"status": 200, "text": json.dumps(self._responses[url], ensure_ascii=False)}, ""
            return False, {}, "no"

        self._client._req = fake_req  # type: ignore[assignment]
        return self._client.comments(tid, num)


class QzoneCommentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_qzone()

    def test_parses_commentlist(self):
        detail = self.m.DETAIL_URL
        fake = FakeClient(self.m, {detail: {"commentlist": [
            {"name": "bolshevik", "uin": 3405733308, "content": "这瓜保熟吗", "create_time": 1792900000},
        ]}})
        r = asyncio.run(fake.comments("TID1"))
        self.assertTrue(r["ok"])
        self.assertEqual(r["count"], 1)
        item = r["items"][0]
        self.assertEqual(item["who"], "bolshevik")
        self.assertEqual(item["text"], "这瓜保熟吗")
        self.assertTrue(item["uin"].startswith("3405"))

    def test_falls_back_to_msglist(self):
        list_url = self.m.MSGLIST_URL
        fake = FakeClient(self.m, {list_url: {"msglist": [
            {"tid": "TID2", "commentlist": [
                {"user": {"nickname": "小号", "uin": 10001}, "content": "哈哈"},
            ]},
        ]}})
        r = asyncio.run(fake.comments("TID2"))
        self.assertTrue(r["ok"])
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["items"][0]["who"], "小号")

    def test_empty_commentlist_is_ok(self):
        fake = FakeClient(self.m, {self.m.DETAIL_URL: {"commentlist": []}})
        r = asyncio.run(fake.comments("TID3"))
        self.assertTrue(r["ok"])
        self.assertEqual(r["count"], 0)

    def test_all_endpoints_fail_returns_error(self):
        fake = FakeClient(self.m, {})
        r = asyncio.run(fake.comments("TID4"))
        self.assertFalse(r["ok"])
        self.assertIn("读评论失败", r["err"])

    def test_missing_tid_is_rejected(self):
        fake = FakeClient(self.m, {})
        r = asyncio.run(fake.comments(""))
        self.assertFalse(r["ok"])


    def test_list_user_parses_msglist(self):
        fake = FakeClient(self.m, {})
        async def fake_req(method, url, **kw):
            return True, {"status": 200, "text": json.dumps({"msglist": [
                {"tid": "TID9", "content": "今天的饭", "created_time": 1792900000, "cmtnum": 2},
            ]}, ensure_ascii=False)}, ""
        fake._client._req = fake_req  # type: ignore[assignment]
        r = asyncio.run(fake._client.list_user("3405733308", 5))
        self.assertTrue(r["ok"])
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["items"][0]["tid"], "TID9")
        self.assertEqual(r["items"][0]["uin"], "3405733308")

    def test_list_user_requires_uin(self):
        fake = FakeClient(self.m, {})
        r = asyncio.run(fake._client.list_user("", 5))
        self.assertFalse(r["ok"])

if __name__ == "__main__":
    unittest.main()
