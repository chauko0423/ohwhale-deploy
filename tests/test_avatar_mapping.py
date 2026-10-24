"""Verify avatar startup when Global shared memory is unavailable."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock, patch


class AvatarMappingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "avatar_mapping_under_test", Path(__file__).parents[1] / "scripts/avatar_live.py")
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def setUp(self):
        self.m = self.module
        self.kernel = Mock()
        self.kernel.OpenFileMappingW.return_value = 0
        self.advapi = Mock()
        self.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.return_value = 1
        patches = [patch.object(self.m, "k32", self.kernel),
                   patch.object(self.m.ctypes, "WinDLL", return_value=self.advapi),
                   patch.object(self.m.ctypes, "get_last_error", return_value=5)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_existing_global_is_reused_without_creation(self):
        self.kernel.OpenFileMappingW.return_value = 17
        self.assertEqual(self.m.open_section(), (17, False))
        self.kernel.CreateFileMappingW.assert_not_called()
        self.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.assert_not_called()

    def test_existing_local_is_reused_before_trying_global_creation(self):
        self.kernel.OpenFileMappingW.side_effect = [0, 18]
        self.assertEqual(self.m.open_section(), (18, False))
        self.kernel.CreateFileMappingW.assert_not_called()

    def test_global_permission_failure_falls_back_to_local(self):
        self.kernel.CreateFileMappingW.side_effect = [0, 19]
        self.assertEqual(self.m.open_section(), (19, True))
        names = [c.args[-1] for c in self.kernel.CreateFileMappingW.call_args_list]
        self.assertEqual(names, [self.m.SECTION_NAME, self.m.SECTION_NAME_LOCAL])
        self.kernel.LocalFree.assert_called_once()

    def test_both_mappings_fail_explicitly_and_release_descriptor(self):
        self.kernel.CreateFileMappingW.return_value = 0
        with self.assertRaises(OSError):
            self.m.open_section()
        self.kernel.LocalFree.assert_called_once()

    def test_descriptor_failure_does_not_create_unprotected_mapping(self):
        self.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.return_value = 0
        with self.assertRaises(OSError):
            self.m.open_section()
        self.kernel.CreateFileMappingW.assert_not_called()


if __name__ == "__main__":
    unittest.main()
