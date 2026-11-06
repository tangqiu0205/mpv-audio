import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import apple_sdk
from common import ROOT, read_lock, sha256, write_json


def fake_bundles(root):
    result = {}
    for name in apple_sdk.ARCHITECTURES:
        dest = root / name
        (dest / "include/mpv").mkdir(parents=True)
        (dest / "include/mpv/client.h").write_text("/* identical public C API */", encoding="utf-8")
        (dest / "libmpv.dylib").write_bytes(b"native library")
        write_json(dest / "native-lock.json", read_lock(ROOT / "native-lock.json"))
        write_json(dest / "manifest.json", {"platform": name.replace("-", "/", 1), "license_profile": "lgpl",
                   "libraries": {"libmpv.dylib": sha256(dest / "libmpv.dylib")}})
        result[name] = dest
    return result


class AppleSDKTests(unittest.TestCase):
    def test_matching_source_locks_headers_and_library_sets_are_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundles = fake_bundles(Path(tmp))
            lock, names = apple_sdk.validate_inputs(bundles)
            self.assertEqual(names, ["libmpv.dylib"])
            self.assertEqual(lock["mpv"]["version"], "0.42.0")

    def test_both_architectures_are_required(self):
        with self.assertRaisesRegex(RuntimeError, "both macOS architectures"):
            apple_sdk.validate_inputs({})

    def test_changed_library_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundles = fake_bundles(Path(tmp))
            (bundles["darwin-arm64"] / "libmpv.dylib").write_bytes(b"modified")
            with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                apple_sdk.validate_inputs(bundles)

    def test_mismatched_source_locks_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundles = fake_bundles(Path(tmp))
            path = bundles["darwin-arm64"] / "native-lock.json"
            lock = read_lock(path)
            lock["ffmpeg"]["commit"] = "0" * 40
            write_json(path, lock)
            with self.assertRaisesRegex(RuntimeError, "different source locks"):
                apple_sdk.validate_inputs(bundles)

    def test_mismatched_headers_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundles = fake_bundles(Path(tmp))
            (bundles["darwin-arm64"] / "include/mpv/client.h").write_text("changed", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "headers differ"):
                apple_sdk.validate_inputs(bundles)

    def test_input_archive_cannot_escape_destination_or_add_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for filename in ("../outside", "/absolute", "mpv/../outside", "C:/absolute", "mpv\\outside"):
                with zipfile.ZipFile(root / "bad.zip", "w") as archive:
                    item = zipfile.ZipInfo()
                    item.filename = filename
                    archive.writestr(item, b"bad")
                with self.assertRaisesRegex(RuntimeError, "unsafe bundle"):
                    apple_sdk.extract_bundle(root / "bad.zip", root / "output")
            with zipfile.ZipFile(root / "bad.zip", "w") as archive:
                item = zipfile.ZipInfo("mpv/link")
                item.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(item, "../outside")
            with self.assertRaisesRegex(RuntimeError, "must not contain symlinks"):
                apple_sdk.extract_bundle(root / "bad.zip", root / "output")

    @unittest.skipIf(os.name == "nt", "native framework symlinks are exercised on Unix runners")
    def test_framework_zip_preserves_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "binary").write_bytes(b"library")
            (source / "link").symlink_to("binary")
            apple_sdk.zip_tree(source, root / "framework.zip")
            with zipfile.ZipFile(root / "framework.zip") as archive:
                self.assertTrue(stat.S_ISLNK(archive.getinfo("link").external_attr >> 16))
                self.assertEqual(archive.read("link"), b"binary")


if __name__ == "__main__":
    unittest.main()
