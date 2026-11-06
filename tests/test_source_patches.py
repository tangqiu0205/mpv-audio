from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_native
from common import ROOT, read_lock, sha256

COREAUDIO_BLOCK = """if features['audiounit'] or features['coreaudio'] or features['avfoundation']
    sources += files('audio/out/ao_coreaudio_chmap.c',
                     'audio/out/ao_coreaudio_utils.c')
endif
"""


class SourcePatchTests(unittest.TestCase):
    def test_coreaudio_patch_adds_helpers_without_enabling_cocoa(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            (source / "meson.build").write_text(COREAUDIO_BLOCK, encoding="utf-8")
            spec = read_lock(ROOT / "native-lock.json")["mpv"]
            records = build_native.apply_source_patches(spec, source, os.environ.copy())
            text = (source / "meson.build").read_text(encoding="utf-8")
            self.assertIn("if not features['cocoa']\n        sources += files('osdep/utils-mac.c')", text)
            self.assertNotIn("-Dcocoa=enabled", text)
            patch = ROOT / "patches/mpv-coreaudio-utils.patch"
            self.assertEqual(records, [{"file": patch.name, "sha256": sha256(patch)}])
            self.assertEqual((source / "mpv-audio-patches" / patch.name).read_bytes(), patch.read_bytes())
            self.assertTrue((source / "mpv-audio-patches/manifest.json").is_file())

    def test_changed_upstream_context_is_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            original = "# upstream replaced the audio build section\n"
            (source / "meson.build").write_text(original, encoding="utf-8")
            spec = read_lock(ROOT / "native-lock.json")["mpv"]
            with self.assertRaises(subprocess.CalledProcessError):
                build_native.apply_source_patches(spec, source, os.environ.copy())
            self.assertEqual((source / "meson.build").read_text(encoding="utf-8"), original)
            self.assertFalse((source / "mpv-audio-patches").exists())

    def test_all_libmpv_targets_reject_undefined_link_symbols(self):
        for platform in ("linux", "darwin", "windows"):
            self.assertIn("-Db_lundef=true", build_native.mpv_options(platform))


if __name__ == "__main__":
    unittest.main()
