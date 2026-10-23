import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ohwhale.config import reset, _dotenv

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('account_form', ROOT/'account-setup/account_form.py')
form = importlib.util.module_from_spec(spec)
spec.loader.exec_module(form)


class AccountFormTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'OHWHALE_HOME': str(self.root)})
        self.env.start()
        reset()
        (self.root/'.env').write_text('# 保留注释\nONEBOT_ACCESS_TOKEN=keep-token\nDEEPSEEK_API_KEY=\nDASHSCOPE_API_KEY=\nASTRBOT_DASHBOARD_PASSWORD=\n', encoding='utf-8')
        (self.root/'config.json').write_text(json.dumps({'master': {'qq': '', 'bili_uid': 123}, 'features': {'avatar': True}}), encoding='utf-8')

    def tearDown(self):
        self.env.stop()
        reset()
        self.temp.cleanup()

    def request(self):
        return {**form.read_form(), 'owner_qq': '123456789', 'DEEPSEEK_API_KEY': 'dummy-key',
                'DASHSCOPE_API_KEY': 'dummy-bailian', 'ASTRBOT_DASHBOARD_PASSWORD': 'LocalTest123"'}

    def files(self):
        return {p.name: p.read_bytes() for p in (self.root/'.env', self.root/'config.json')}

    def test_save_preserves_comments_optional_token_and_other_config(self):
        self.assertTrue(form.save_form(self.request())['ok'])
        self.assertEqual(_dotenv()['ASTRBOT_DASHBOARD_PASSWORD'], 'LocalTest123"')
        self.assertEqual(_dotenv()['ONEBOT_ACCESS_TOKEN'], 'keep-token')
        self.assertIn('# 保留注释', (self.root/'.env').read_text('utf-8'))
        config = json.loads((self.root/'config.json').read_text())
        self.assertTrue(config['features']['avatar'])
        self.assertEqual(config['master']['bili_uid'], 123)
        self.assertEqual(config['master']['qq'], '123456789')

    def test_invalid_fields_do_not_write(self):
        before = self.files()
        for key, value in [('owner_qq', 'robot'), ('DEEPSEEK_API_KEY', ''),
                           ('DASHSCOPE_API_KEY', 'key\nline'), ('ASTRBOT_DASHBOARD_PASSWORD', 'short')]:
            with self.subTest(key=key):
                request = self.request()
                request[key] = value
                with self.assertRaises(ValueError):
                    form.save_form(request)
                self.assertEqual(self.files(), before)

    def test_stale_form_does_not_overwrite_another_editor(self):
        request = self.request()
        with (self.root/'.env').open('a') as stream:
            stream.write('NEW_SETTING=keep\n')
        before = self.files()
        with self.assertRaisesRegex(ValueError, '其他窗口'):
            form.save_form(request)
        self.assertEqual(self.files(), before)

    def test_second_file_failure_restores_first_and_removes_temporaries(self):
        before = self.files()
        replace = os.replace
        def fail_second(source, target):
            if Path(target).name == 'config.json':
                raise OSError('simulated write failure')
            return replace(source, target)
        with patch.object(form.os, 'replace', side_effect=fail_second):
            with self.assertRaises(OSError):
                form.save_form(self.request())
        self.assertEqual(self.files(), before)
        self.assertFalse(list(self.root.glob('.account-*.tmp')))

    def test_existing_profile_rejects_changed_values_but_allows_recheck(self):
        form.save_form(self.request())
        marker = self.root/'runtime/AstrBot/ohwhale-profile.json'
        marker.parent.mkdir(parents=True)
        marker.write_text('{}')
        self.assertFalse(form.save_form(form.read_form())['changed'])
        before = self.files()
        request = form.read_form()
        request['DEEPSEEK_API_KEY'] = 'new-key'
        with self.assertRaisesRegex(ValueError, '已经建立'):
            form.save_form(request)
        self.assertEqual(self.files(), before)


if __name__ == '__main__':
    unittest.main()
