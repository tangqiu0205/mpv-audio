import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_native
import common
import sdk


class ToolchainTests(unittest.TestCase):
    def test_child_command_is_resolved_using_explicit_build_path(self):
        with patch.object(common.shutil, "which", return_value="/msys/usr/bin/bash.exe") as which:
            command = common.command_args(["bash", "configure"], {"PATH": "build-tools"})
        which.assert_called_once_with("bash", path="build-tools")
        self.assertEqual(command, ["/msys/usr/bin/bash.exe", "configure"])

    def test_missing_build_tool_is_explicit(self):
        with patch.object(common.shutil, "which", return_value=None):
            with self.assertRaisesRegex(FileNotFoundError, "required tool"):
                common.command_args(["bash"], {"PATH": ""})

    def test_msys_tools_precede_windows_system_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "usr/bin").mkdir(parents=True)
            (root / "usr/bin/bash.exe").touch()
            mingw = root / "ucrt64"
            original = {"PATH": "windows-system-tools", "MINGW_PREFIX": "/ucrt64"}
            with patch.object(common, "run", side_effect=[str(root), str(mingw)]):
                env = common.windows_environment(original)
            self.assertEqual(env["PATH"].split(os.pathsep)[:2], [str(mingw / "bin"), str(root / "usr/bin")])
            self.assertEqual(original["PATH"], "windows-system-tools")


class SDKTests(unittest.TestCase):
    def test_pkg_config_is_relocatable_and_uses_api_version(self):
        for goos in ("linux", "darwin", "windows"):
            pc = sdk.pkg_config_text("2.5.0", goos)
            self.assertIn("prefix=${pcfiledir}/../..", pc)
            self.assertIn("Version: 2.5.0", pc)
            self.assertNotIn("/build/", pc)
        self.assertIn("-l:libmpv.so.2", sdk.pkg_config_text("2.5.0", "linux"))

    def test_headers_and_example_are_staged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix, dest = root / "prefix", root / "bundle"
            (prefix / "include/mpv").mkdir(parents=True)
            (prefix / "include/mpv/client.h").write_text("/* C API */", encoding="utf-8")
            (prefix / "lib/pkgconfig").mkdir(parents=True)
            (prefix / "lib/pkgconfig/mpv.pc").write_text("Version: 2.5.0\n", encoding="utf-8")
            sdk.stage_sdk(prefix, dest, "linux", "amd64", {})
            self.assertTrue((dest / "include/mpv/client.h").is_file())
            self.assertTrue((dest / "examples/version.c").is_file())
            self.assertIn("Version: 2.5.0", (dest / "lib/pkgconfig/mpv.pc").read_text(encoding="utf-8"))

    def test_core_components_explicitly_disable_gpl_and_nonfree(self):
        options = build_native.ffmpeg_options("linux", "amd64", "/prefix")
        self.assertIn("--disable-gpl", options)
        self.assertIn("--disable-nonfree", options)
        self.assertIn("-Dgpl=false", build_native.mpv_options("linux"))

    def test_configured_gpl_is_rejected_even_with_audio_only_decoders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config_components.h").write_text("#define CONFIG_MP3FLOAT_DECODER 1\n", encoding="utf-8")
            (root / "config.h").write_text("#define CONFIG_GPL 1\n#define CONFIG_NONFREE 0\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "CONFIG_GPL=0"):
                build_native.verify_ffmpeg_profile(root)


if __name__ == "__main__":
    unittest.main()
