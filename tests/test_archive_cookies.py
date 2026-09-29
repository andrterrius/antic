import json
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import cookies_io
import playwright_runner as runner
import profiles_bundle as bundle
from profiles_store import BrowserProfile


class ArchiveCookiesTests(unittest.TestCase):
    def test_windows_encrypted_values_are_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(cookies_io, "profile_user_data_dir", return_value=root):
                cookies_io.write_profile_cookies("test", [{"host": ".example.test", "name": "auth", "value": ""}])
                db = cookies_io._cookies_db_path("test")
                with sqlite3.connect(db) as con:
                    con.execute("UPDATE cookies SET encrypted_value = ?", (b"encrypted-test-value",))
                con.close()
                with patch.object(cookies_io.sys, "platform", "win32"), patch.object(
                    cookies_io, "_decrypted_values_map", return_value={(".example.test", "/", "auth"): "test-secret"}
                ) as decrypt:
                    result = cookies_io.read_profile_cookies("test")
                decrypt.assert_called_once()
                self.assertEqual(result[0]["value"], "test-secret")

    def test_sessions_apply_at_launch_and_failed_apply_can_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(runner, "profile_user_data_dir", return_value=root):
                runner.inject_cookies_into_profile(BrowserProfile("test", "test"), [
                    {"host": ".example.test", "name": "session", "value": "test-value"}
                ])
            pending = root / "imported-cookies.json"
            self.assertNotIn("expires", json.loads(pending.read_text())[0])
            context = Mock()
            context.add_cookies.side_effect = RuntimeError("rejected")
            with self.assertRaises(RuntimeError):
                runner._apply_imported_cookies(context, root, lambda _: None)
            self.assertTrue(pending.exists())
            context.add_cookies.side_effect = None
            runner._apply_imported_cookies(context, root, lambda _: None)
            self.assertFalse(pending.exists())

    def test_full_export_includes_portable_cookies_and_remapped_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = BrowserProfile("original", "test")
            cookie = {"host": ".example.test", "name": "session", "value": "test-value"}
            source = root / "source"
            source.mkdir()
            (source / "Preferences").write_text("{}")
            with patch.object(bundle, "profile_user_data_dir", return_value=source), patch.object(
                bundle, "read_profile_cookies", return_value=[cookie]
            ):
                archive = bundle.export_profiles_zip(root, [profile])
            with zipfile.ZipFile(archive) as zf:
                self.assertEqual(json.loads(zf.read("cookies/original.json")), [cookie])
                self.assertEqual(bundle._detect_bundle_format(zf), bundle.BUNDLE_FORMAT)
            imported = []
            with patch.object(bundle, "backup_profiles_db"), patch.object(
                bundle, "load_profiles", side_effect=lambda: [profile] + imported
            ), patch.object(bundle, "upsert_profiles", side_effect=imported.extend), patch.object(
                bundle, "profile_user_data_dir", side_effect=lambda pid: root / pid
            ), patch.object(runner, "profile_user_data_dir", side_effect=lambda pid: root / pid):
                _, added, remapped = bundle.import_profiles_zip(archive)
            self.assertEqual((added, remapped), (1, 1))
            final = root / imported[0].profile_id
            self.assertTrue((final / "Preferences").exists())
            self.assertEqual(json.loads((final / "imported-cookies.json").read_text())[0]["value"], "test-value")


if __name__ == "__main__":
    unittest.main()
