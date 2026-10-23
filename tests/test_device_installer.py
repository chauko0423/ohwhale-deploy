"""Exercise Windows PowerShell's real native-output and UTF-8 behavior."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('powershell.exe'), 'Windows PowerShell required')
class DeviceInstallerTests(unittest.TestCase):
    def test_native_failure_output_is_retained_in_transcript(self):
        with tempfile.TemporaryDirectory(prefix='ohwhale-installer-') as temp:
            root = Path(temp)
            install = root / 'install'
            install.mkdir()
            (root / 'data').mkdir()
            source = Path(__file__).resolve().parents[1] / 'install/device_test.ps1'
            shutil.copyfile(source, install / 'device_test.ps1')
            (root / 'data/machine-preflight.json').write_text(json.dumps({
                'install_disk_free_gib': 100,
                'os': {'caption': 'Windows 11 专业版', 'architecture': '64 位'},
            }, ensure_ascii=False), encoding='utf-8')
            for name in ('00_machine.ps1', '05_python.ps1'):
                (install / name).write_text('exit 0\n', encoding='ascii')
            # No network or installer is run: a native command emits both
            # streams and fails before any later deployment stage can begin.
            (install / '10_deps.ps1').write_text(
                "& $env:ComSpec /d /c 'echo fixture-stdout & echo fixture-stderr 1>&2 & exit /b 23'\n"
                'exit $LASTEXITCODE\n', encoding='ascii')
            result = subprocess.run([
                'powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-File', str(install / 'device_test.ps1'),
            ], capture_output=True, timeout=30)
            status = json.loads((root / 'data/device-setup.json').read_text('utf-8'))
            log = (root / 'data/device-setup.log').read_text('utf-8-sig')
            self.assertEqual(result.returncode, 1)
            self.assertEqual(status['completed_steps'], ['hardware-report', 'project-python'])
            self.assertEqual(status['current_step'], 'main-dependencies')
            self.assertEqual(status['error'], 'Step failed: main-dependencies (exit 23)')
            self.assertFalse(status['ok'])
            self.assertFalse(status['full_acceptance_passed'])
            self.assertIn('fixture-stdout', log)
            self.assertIn('fixture-stderr', log)


if __name__ == '__main__':
    unittest.main()
