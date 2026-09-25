import hashlib
import unittest
from unittest.mock import Mock, patch

from gui.update_manager import APP_VERSION, UpdateError, UpdateManager, _version_key


class MemorySettings:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def value(self, key, default=None, type=None):
        value = self.values.get(key, default)
        return type(value) if type is not None and value is not None else value

    def setValue(self, key, value):
        self.values[key] = value

    def sync(self):
        pass


class UpdateManagerTests(unittest.TestCase):
    def manager(self, **values):
        defaults = {
            "updates/api_url": "https://updates.example.test",
            "updates/device_id": "67ad09cc-fec7-493f-ab73-2fb13a936cff",
            "updates/system_number": "NAKSHA-PC-0017",
            "updates/device_token": "test-device-token",
        }
        defaults.update(values)
        return UpdateManager(MemorySettings(defaults))

    def test_version_comparison_is_numeric(self):
        self.assertGreater(_version_key("2.0.10"), _version_key("2.0.4"))

    def test_device_id_is_created_once(self):
        manager = UpdateManager(MemorySettings())
        first = manager.device_id
        self.assertEqual(first, manager.device_id)
        self.assertEqual(36, len(first))

    def test_rejects_non_https_service(self):
        manager = self.manager(**{"updates/api_url": "http://updates.example.test"})
        with self.assertRaisesRegex(UpdateError, "must use HTTPS"):
            manager.check_for_update()

    @patch("gui.update_manager.requests.post")
    def test_parses_assigned_update(self, post):
        response = Mock(status_code=200)
        response.json.return_value = {
            "update_available": True,
            "version": "2.0.5",
            "download_url": "https://updates.example.test/releases/setup.exe",
            "sha256": hashlib.sha256(b"installer").hexdigest(),
            "size": 9,
            "release_notes": "Test release",
        }
        post.return_value = response
        info = self.manager().check_for_update()
        self.assertEqual("2.0.5", info.version)
        self.assertEqual("Test release", info.release_notes)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(
            "Bearer test-device-token",
            post.call_args.kwargs["headers"]["Authorization"],
        )
        self.assertEqual(APP_VERSION, payload["current_version"])
        self.assertEqual("NAKSHA-PC-0017", payload["system_number"])
        self.assertEqual(64, len(payload["hardware_fingerprint"]))

    @patch("gui.update_manager.requests.post")
    def test_older_assignment_is_ignored(self, post):
        response = Mock(status_code=200)
        response.json.return_value = {
            "version": "2.0.3",
            "download_url": "https://updates.example.test/setup.exe",
            "sha256": "a" * 64,
            "size": 100,
        }
        post.return_value = response
        self.assertIsNone(self.manager().check_for_update())

    @patch("gui.update_manager.requests.post")
    def test_invalid_hash_is_rejected(self, post):
        response = Mock(status_code=200)
        response.json.return_value = {
            "version": "2.0.5",
            "download_url": "https://updates.example.test/setup.exe",
            "sha256": "not-a-hash",
            "size": 100,
        }
        post.return_value = response
        with self.assertRaisesRegex(UpdateError, "invalid SHA-256"):
            self.manager().check_for_update()


if __name__ == "__main__":
    unittest.main()
