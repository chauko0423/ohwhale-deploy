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
        self.assertEqual(self.m.open_section(), (17, False, self.m.SECTION_NAME))
        self.kernel.CreateFileMappingW.assert_not_called()
        self.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.assert_not_called()

    def test_existing_local_is_reused_before_trying_global_creation(self):
        self.kernel.OpenFileMappingW.side_effect = [0, 18]
        self.assertEqual(self.m.open_section(), (18, False, self.m.SECTION_NAME_LOCAL))
        self.kernel.CreateFileMappingW.assert_not_called()

    def test_global_permission_failure_falls_back_to_local(self):
        self.kernel.CreateFileMappingW.side_effect = [0, 19]
        self.assertEqual(self.m.open_section(), (19, True, self.m.SECTION_NAME_LOCAL))
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

    def make_mapping(self):
        self.kernel.OpenFileMappingW.side_effect = [0, 18]
        self.kernel.MapViewOfFile.return_value = 1800
        mapping = self.m.FrameMapping()
        self.kernel.OpenFileMappingW.side_effect = None
        self.kernel.OpenFileMappingW.return_value = 0
        self.kernel.reset_mock()
        return mapping

    def test_late_camera_service_replaces_local_mapping(self):
        mapping = self.make_mapping()
        mapping.ensure_global(10.0)  # Service has not opened the camera yet.
        self.assertEqual(mapping.view, 1800)
        self.kernel.OpenFileMappingW.return_value = 21
        self.kernel.MapViewOfFile.return_value = 2100
        mapping.ensure_global(10.6)
        self.assertEqual((mapping.handle, mapping.view), (21, 2100))
        self.assertTrue(mapping.global_connected)
        self.kernel.UnmapViewOfFile.assert_called_once_with(1800)
        self.kernel.CloseHandle.assert_called_once_with(18)
        self.kernel.reset_mock()
        mapping.ensure_global(20.0)
        self.kernel.OpenFileMappingW.assert_not_called()

    def test_failed_global_view_keeps_local_output_and_retries(self):
        mapping = self.make_mapping()
        self.kernel.OpenFileMappingW.return_value = 21
        self.kernel.MapViewOfFile.return_value = 0
        mapping.ensure_global(10.0)
        self.assertEqual((mapping.handle, mapping.view), (18, 1800))
        self.assertFalse(mapping.global_connected)
        self.kernel.CloseHandle.assert_called_once_with(21)
        self.kernel.UnmapViewOfFile.assert_not_called()
        self.kernel.MapViewOfFile.return_value = 2200
        mapping.ensure_global(10.6)
        self.assertEqual(mapping.view, 2200)

    def test_unavailable_global_is_polled_at_most_twice_per_second(self):
        mapping = self.make_mapping()
        mapping.ensure_global(10.0)
        mapping.ensure_global(10.2)
        mapping.ensure_global(10.4)
        self.kernel.OpenFileMappingW.assert_called_once()
        mapping.ensure_global(10.5)
        self.assertEqual(self.kernel.OpenFileMappingW.call_count, 2)

    def test_failed_initial_view_releases_handle(self):
        self.kernel.OpenFileMappingW.return_value = 18
        self.kernel.MapViewOfFile.return_value = 0
        with self.assertRaises(OSError):
            self.m.FrameMapping()
        self.kernel.CloseHandle.assert_called_once_with(18)

    def test_close_releases_current_view_and_is_repeatable(self):
        mapping = self.make_mapping()
        mapping.close()
        mapping.close()
        self.kernel.UnmapViewOfFile.assert_called_once_with(1800)
        self.kernel.CloseHandle.assert_called_once_with(18)


if __name__ == "__main__":
    unittest.main()
