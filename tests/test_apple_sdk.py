import json
import os
from pathlib import Path
import stat
import shutil
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
        for folder in ("examples", "lib/pkgconfig", "licenses"):
            (dest / folder).mkdir(parents=True)
        (dest / "BUILD-README.md").write_text("SDK", encoding="utf-8")
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

    def test_architecture_specific_dependencies_are_preserved_and_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundles = fake_bundles(root)
            arm = bundles["darwin-arm64"]
            helper = arm / "libbrotlidec.1.dylib"
            helper.write_bytes(b"arm64 helper")
            manifest = json.loads((arm / "manifest.json").read_text(encoding="utf-8"))
            manifest["libraries"][helper.name] = sha256(helper)
            write_json(arm / "manifest.json", manifest)

            def tools(*args):
                if args[0] == "lipo":
                    path = Path(args[-1])
                    if path.parent == bundles["darwin-arm64"] or path.name == helper.name:
                        return "arm64"
                    if path.parent == bundles["darwin-amd64"]:
                        return "x86_64"
                    return "arm64 x86_64"
                architecture, path = args[2], Path(args[-1])
                imports = f"{path}:\n\t@loader_path/{path.name} (compatibility version 1.0.0)\n"
                if path.name == "libmpv.dylib" and architecture == "arm64":
                    imports += "\t@loader_path/libbrotlidec.1.dylib (compatibility version 1.0.0)\n"
                return imports

            def checked(*args):
                if args[0] == "lipo":
                    shutil.copy2(args[2], args[-1])

            with patch.object(apple_sdk, "run", side_effect=tools), patch.object(apple_sdk, "checked", side_effect=checked):
                dest = apple_sdk.merge_bundles(bundles, root / "work")
            merged = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(merged["library_architectures"]["libmpv.dylib"], ["arm64", "x86_64"])
            self.assertEqual(merged["library_architectures"][helper.name], ["arm64"])
            self.assertEqual((dest / helper.name).read_bytes(), b"arm64 helper")

    def test_each_dependency_must_supply_the_requested_architecture(self):
        libraries = {"libmpv.dylib": ["arm64", "x86_64"], "libbrotlidec.1.dylib": ["arm64"]}
        output = "libmpv.dylib:\n\t@loader_path/libbrotlidec.1.dylib (compatibility version 1.0.0)\n"
        with patch.object(apple_sdk, "run", return_value=output):
            with self.assertRaisesRegex(RuntimeError, "missing dependency architecture"):
                apple_sdk.validate_dependency_architectures(Path("bundle"), libraries)

    def test_build_prefix_dependencies_are_rejected(self):
        output = "libmpv.dylib:\n\t/build/prefix/libavutil.dylib (compatibility version 1.0.0)\n"
        with patch.object(apple_sdk, "run", return_value=output):
            with self.assertRaisesRegex(RuntimeError, "non-relocatable dependency"):
                apple_sdk.validate_dependency_architectures(Path("bundle"), {"libmpv.dylib": ["arm64"]})

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
