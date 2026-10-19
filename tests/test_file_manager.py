import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from file_manager import ExternalChangeError, FileManager, FileManagerError


class FileManagerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.fm = FileManager(self.root / "config")
        self.docs = self.root / "docs"
        self.docs.mkdir()


class TestWriting(FileManagerTestCase):
    def test_write_and_read_roundtrip(self):
        path = self.docs / "a.md"
        mtime = self.fm.write_file(path, "# Héllo\n")
        loaded = self.fm.read_document(path)
        self.assertEqual((loaded.text, loaded.newline, loaded.mtime_ns), ("# Héllo\n", "\n", mtime))

    def test_creates_missing_folders(self):
        path = self.docs / "x" / "y" / "a.md"
        self.fm.write_file(path, "hi")
        self.assertEqual(path.read_text(), "hi")

    def test_replaces_existing_file_without_leftovers(self):
        path = self.docs / "a.md"
        self.fm.write_file(path, "one")
        self.fm.write_file(path, "two")
        self.assertEqual(path.read_text(), "two")
        self.assertEqual([p.name for p in self.docs.iterdir()], ["a.md"])

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_permissions_preserved_and_default(self):
        path = self.docs / "a.md"
        self.fm.write_file(path, "one")
        os.chmod(path, 0o640)
        self.fm.write_file(path, "two")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

        umask = os.umask(0); os.umask(umask)
        fresh = self.docs / "b.md"
        self.fm.write_file(fresh, "x")
        self.assertEqual(stat.S_IMODE(fresh.stat().st_mode), 0o666 & ~umask)

    def test_failed_write_keeps_original_and_cleans_up(self):
        path = self.docs / "a.md"
        self.fm.write_file(path, "original")
        with mock.patch("file_manager.os.replace", side_effect=OSError("disk gone")):
            with self.assertRaises(OSError):
                self.fm.write_file(path, "new")
        self.assertEqual(path.read_text(), "original")
        self.assertEqual([p.name for p in self.docs.iterdir()], ["a.md"])

    def test_external_change_detected(self):
        path = self.docs / "a.md"
        mtime = self.fm.write_file(path, "mine")
        os.utime(path, ns=(mtime + 5_000_000_000, mtime + 5_000_000_000))
        with self.assertRaises(ExternalChangeError):
            self.fm.write_file(path, "other", expected_mtime_ns=mtime)
        self.assertEqual(path.read_text(), "mine")
        self.fm.write_file(path, "forced")  # no expectation: allowed
        self.assertEqual(path.read_text(), "forced")

    def test_deleted_file_is_recreated_without_conflict(self):
        path = self.docs / "a.md"
        mtime = self.fm.write_file(path, "x")
        path.unlink()
        self.fm.write_file(path, "y", expected_mtime_ns=mtime)
        self.assertEqual(path.read_text(), "y")

    def test_create_file_refuses_existing(self):
        path = self.fm.create_file(self.docs / "n.md", "t")
        with self.assertRaises(FileExistsError):
            self.fm.create_file(path)


class TestReading(FileManagerTestCase):
    def test_crlf_roundtrip_is_preserved(self):
        path = self.docs / "w.md"
        path.write_bytes(b"a\r\nb\r\n")
        loaded = self.fm.read_document(path)
        self.assertEqual((loaded.text, loaded.newline), ("a\nb\n", "\r\n"))
        self.fm.write_file(path, "a\nb\nc\n", newline=loaded.newline)
        self.assertEqual(path.read_bytes(), b"a\r\nb\r\nc\r\n")

    def test_bom_is_stripped(self):
        path = self.docs / "b.md"
        path.write_bytes(b"\xef\xbb\xbfhi")
        self.assertEqual(self.fm.read_file(path), "hi")

    def test_invalid_utf8_refused(self):
        path = self.docs / "bad.md"
        path.write_bytes(b"\xff\xfe\x00bad")
        with self.assertRaises(FileManagerError):
            self.fm.read_document(path)
        self.assertEqual(path.read_bytes(), b"\xff\xfe\x00bad")

    def test_missing_and_directory(self):
        with self.assertRaises(FileNotFoundError):
            self.fm.read_file(self.docs / "nope.md")
        with self.assertRaises(IsADirectoryError):
            self.fm.read_file(self.docs)


class TestPaths(FileManagerTestCase):
    def test_normalize(self):
        n = FileManager.normalize_markdown_path
        self.assertEqual(n("notes", self.docs), self.docs / "notes.md")
        self.assertEqual(n('"notes.MD"', self.docs), self.docs / "notes.MD")
        with self.assertRaises(ValueError):
            n("notes.txt", self.docs)
        with self.assertRaises(ValueError):
            n("   ")
        with self.assertRaises(ValueError):
            n(str(self.docs))

    def test_rename(self):
        old = self.docs / "a.md"
        self.fm.write_file(old, "x")
        self.fm.add_recent_file(old)
        new = self.fm.rename_file(old, self.docs / "sub" / "b.md")
        self.assertTrue(new.exists() and not old.exists())
        self.assertEqual(self.fm.get_recent_files(), [str(new)])
        self.fm.write_file(old, "again")
        with self.assertRaises(FileExistsError):
            self.fm.rename_file(old, new)
        with self.assertRaises(FileNotFoundError):
            self.fm.rename_file(self.docs / "ghost.md", self.docs / "z.md")

    def test_confirm_overwrite(self):
        path = self.docs / "a.md"
        self.assertTrue(self.fm.confirm_overwrite(path, lambda q: False))
        self.fm.write_file(path, "x")
        self.assertFalse(self.fm.confirm_overwrite(path, lambda q: False))
        self.assertTrue(self.fm.confirm_overwrite(path, lambda q: True))


class TestConfigAndRecent(FileManagerTestCase):
    def test_recent_files_limit_order_and_missing(self):
        paths = []
        for i in range(12):
            p = self.docs / f"{i}.md"
            self.fm.write_file(p, "x")
            self.fm.add_recent_file(p)
            paths.append(p)
        recent = self.fm.get_recent_files()
        self.assertEqual(len(recent), 10)
        self.assertEqual(recent[0], str(paths[-1]))
        paths[-1].unlink()
        self.assertNotIn(str(paths[-1]), self.fm.get_recent_files())

    def test_config_persists(self):
        p = self.docs / "a.md"
        self.fm.write_file(p, "x")
        self.fm.add_recent_file(p)
        self.assertEqual(FileManager(self.root / "config").get_recent_files(), [str(p)])

    def test_corrupt_and_hostile_config_is_sanitised(self):
        cfg = self.root / "config" / "config.json"
        cfg.write_text("{not json")
        self.assertEqual(FileManager(self.root / "config").config["autosave_debounce_seconds"], 0.75)
        cfg.write_text(json.dumps({"recent_files": "oops", "autosave_debounce_seconds": 9999,
                                   "last_cursor": -4, "default_folder": 7}))
        fm = FileManager(self.root / "config")
        self.assertEqual(fm.config["recent_files"], [])
        self.assertEqual(fm.config["autosave_debounce_seconds"], 0.75)
        self.assertEqual(fm.config["last_cursor"], 0)
        self.assertEqual(fm.config["default_folder"], "")

    def test_session(self):
        p = self.docs / "a.md"
        self.fm.write_file(p, "x")
        self.fm.remember_session(p, 5)
        self.assertEqual(FileManager(self.root / "config").last_session(), (p, 5))
        self.fm.remember_session(None)
        self.assertEqual(self.fm.last_session(), (None, 0))

    def test_unwritable_config_does_not_crash(self):
        with mock.patch("file_manager.os.replace", side_effect=OSError("ro")):
            self.assertFalse(self.fm.save_config())


class TestRecovery(FileManagerTestCase):
    def test_snapshot_lifecycle(self):
        self.assertIsNone(self.fm.load_recovery_snapshot())
        self.fm.save_recovery_snapshot("draft é", "/x/a.md", cursor=3, base_mtime_ns=7, newline="\r\n")
        snap = self.fm.load_recovery_snapshot()
        self.assertEqual((snap["text"], snap["original_path"], snap["cursor"], snap["base_mtime_ns"], snap["newline"]),
                         ("draft é", "/x/a.md", 3, 7, "\r\n"))
        self.fm.archive_recovery_snapshot()
        self.assertIsNone(self.fm.load_recovery_snapshot())
        self.assertTrue(self.fm.previous_recovery_path.exists())
        self.fm.save_recovery_snapshot("again")
        self.fm.clear_recovery_snapshot()
        self.fm.clear_recovery_snapshot()  # idempotent
        self.assertIsNone(self.fm.load_recovery_snapshot())

    def test_corrupt_snapshot_ignored(self):
        self.fm.recovery_path.write_text("{oops")
        self.assertIsNone(self.fm.load_recovery_snapshot())
        self.fm.recovery_path.write_text(json.dumps({"text": 5}))
        self.assertIsNone(self.fm.load_recovery_snapshot())


class TestSearch(FileManagerTestCase):
    def make_tree(self):
        (self.docs / "sub").mkdir()
        (self.docs / ".git").mkdir()
        (self.docs / "node_modules").mkdir()
        self.fm.write_file(self.docs / "a.md", "Apple pie\nbanana\n")
        self.fm.write_file(self.docs / "sub" / "b.MD", "no fruit\nAPPLE tart\n")
        self.fm.write_file(self.docs / ".git" / "c.md", "apple hidden")
        self.fm.write_file(self.docs / "node_modules" / "d.md", "apple dep")
        (self.docs / "notes.txt").write_text("apple text")
        (self.docs / "bin.md").write_bytes(b"\xff\xfe apple")

    def test_find_files_skips_noise(self):
        self.make_tree()
        names = [p.name for p in self.fm.find_markdown_files(self.docs)]
        self.assertEqual(names, ["a.md", "bin.md", "b.MD"])
        self.assertEqual(self.fm.find_markdown_files(self.docs / "missing"), [])

    def test_search_folder(self):
        self.make_tree()
        hits = self.fm.search_folder(self.docs, "apple")
        self.assertEqual(
            {Path(k).name: v for k, v in hits.items()},
            {"a.md": [(1, "Apple pie")], "b.MD": [(2, "APPLE tart")]},
        )
        self.assertEqual(self.fm.search_folder(self.docs, "apple", case_sensitive=True), {})
        self.assertEqual(self.fm.search_folder(self.docs, ""), {})


if __name__ == "__main__":
    unittest.main()
