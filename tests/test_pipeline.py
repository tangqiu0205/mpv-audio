import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_native
import bundle
from common import ROOT, read_lock, write_json
import package
import resolve_release
import smoke
import fixtures


def passing_report(version="0.42.0"):
    return {"schema": 2, "mpv_version": "mpv v" + version, "expected_mpv_version": version,
            "formats": list(fixtures.REQUIRED_FORMATS), "wav_decode": True,
            "spectrum_filter": True, "spectrum_metadata": True, "pcm_sampling": True,
            "hls_http": True, "pause_volume": True}


class BuildProfileTests(unittest.TestCase):
    def test_audio_only_ffmpeg_profile_has_network_and_spectrum(self):
        options = build_native.ffmpeg_options("linux", "amd64", "/prefix")
        self.assertIn("--disable-everything", options)
        self.assertIn("--disable-autodetect", options)
        self.assertIn("--disable-programs", options)
        self.assertIn("--enable-gnutls", options)
        self.assertTrue({"aac", "mp3float", "vorbis", "opus", "flac"} <= set(build_native.DECODERS))
        self.assertNotIn("h264", build_native.DECODERS)
        self.assertTrue({"astats", "bandpass", "join", "pan", "aresample"} <= set(build_native.FILTERS))
        self.assertIn("https", build_native.PROTOCOLS)
        self.assertIn("hls", build_native.DEMUXERS)

    def test_native_audio_backends_are_explicitly_enabled(self):
        for goos, output in (("linux", "pulse"), ("darwin", "coreaudio"), ("windows", "wasapi")):
            options = build_native.mpv_options(goos)
            self.assertIn(f"-D{output}=enabled", options)
            self.assertIn("-Dgl=disabled", options)
            self.assertIn("-Dcplayer=false", options)

    def test_os_native_tls_and_windows_arm_compiler(self):
        self.assertIn("--enable-securetransport", build_native.ffmpeg_options("darwin", "arm64", "/p"))
        options = build_native.ffmpeg_options("windows", "arm64", "/p")
        self.assertIn("--enable-schannel", options)
        self.assertIn("--arch=aarch64", options)
        self.assertIn("--cc=clang", options)

    def test_moved_source_tag_is_rejected_before_submodules(self):
        spec = read_lock(ROOT / "native-lock.json")["mpv"]
        with patch.object(build_native, "checked") as checked, patch.object(build_native, "run", return_value="0" * 40):
            with self.assertRaisesRegex(RuntimeError, "tag moved"):
                build_native.clone_source(spec, Path("unused"), {})
            self.assertEqual(checked.call_count, 1)

    def test_wrong_host_is_rejected_before_building(self):
        with patch.object(build_native, "host_platform", return_value="linux/amd64"):
            with self.assertRaisesRegex(RuntimeError, "native build required"):
                build_native.build("windows/arm64", ROOT / "native-lock.json")

    def test_ffmpeg_validation_rejects_video_decoder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config_components.h").write_text("#define CONFIG_H264_DECODER 1\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "unexpected FFmpeg decoder"):
                build_native.verify_ffmpeg_profile(root)

    def test_libplacebo_minimum_matches_locked_mpv(self):
        lock = read_lock(ROOT / "native-lock.json")
        self.assertEqual(len(lock["mpv"]["commit"]), 40)
        self.assertGreaterEqual(bundle.version_tuple(lock["libplacebo"]["version"]), (7, 387, 0))

    def test_spectrum_graph_has_24_bands_and_valid_join(self):
        graph = smoke.spectrum_filter()
        self.assertEqual(graph.count("bandpass="), 24)
        self.assertIn("[b23]join=inputs=25", graph)
        self.assertNotIn("],join", graph)
        self.assertIn("astats=metadata=1", graph)


class StandaloneRegressionTests(unittest.TestCase):
    def test_pipeline_uses_local_regression_tools(self):
        lock = read_lock(ROOT / "native-lock.json")
        self.assertEqual(set(lock), {"schema", "mpv", "ffmpeg", "libplacebo", "libass", "macos_minimum"})
        workflow = (ROOT / ".github/workflows/native-release.yml").read_text(encoding="utf-8")
        build_job = workflow.split("  build:\n", 1)[1].split("\n  publish:", 1)[0]
        self.assertNotIn("repository:", build_job)
        self.assertIn("scripts/fixtures.py", build_job)
        self.assertIn("scripts/smoke.py", build_job)

    def test_synthetic_fixture_generator_covers_every_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "fixtures"
            with patch.object(fixtures, "checked") as checked, patch.object(fixtures, "run", return_value="ffmpeg version test"):
                fixtures.generate(output)
            self.assertEqual(checked.call_count, len(fixtures.FORMATS) + 1)
            self.assertTrue((output / "tone.wav").exists())
            for call in checked.call_args_list:
                self.assertEqual(call.args[0], "ffmpeg")
                self.assertIn("-vn", call.args)
            self.assertIn("-hls_segment_filename", checked.call_args_list[-1].args)

    def test_spectrum_requires_real_values_for_all_24_bands(self):
        metadata = {f"lavfi.astats.{channel}.RMS_level": "-20" for channel in range(3, 27)}
        self.assertTrue(smoke.has_spectrum(metadata))
        del metadata["lavfi.astats.26.RMS_level"]
        self.assertFalse(smoke.has_spectrum(metadata))
        metadata["lavfi.astats.26.RMS_level"] = "NaN"
        with self.assertRaisesRegex(RuntimeError, "invalid spectrum"):
            smoke.has_spectrum(metadata)

    def test_silent_or_truncated_pcm_fails(self):
        for data in (b"", b"\0" * 64000, b"\1" * 100, b"\1" * 64001):
            with self.assertRaisesRegex(RuntimeError, "invalid PCM"):
                smoke.validate_pcm(data)
        smoke.validate_pcm(b"\1\0" * 32000)

    def test_publication_requires_hls_every_codec_and_matching_version(self):
        smoke.validate_report(passing_report(), "0.42.0")
        for change in ({"hls_http": False}, {"formats": ["wav"]}, {"spectrum_metadata": False},
                       {"expected_mpv_version": "0.38.0"}, {"schema": 1}):
            with self.assertRaisesRegex(RuntimeError, "incomplete native regression"):
                smoke.validate_report(passing_report() | change, "0.42.0")


class DependencyTests(unittest.TestCase):
    def test_macos_tool_version_is_not_deployment_target(self):
        commands = """Load command 0
      cmd LC_BUILD_VERSION
 platform 1
    minos 15.0
      sdk 26.0
   ntools 1
     tool 3
  version 1115.7.3
Load command 1
      cmd LC_SOURCE_VERSION
  version 1000.0
"""
        self.assertEqual(bundle.macos_minimum_versions(commands), ["15.0"])

    def test_legacy_macos_deployment_target(self):
        self.assertEqual(bundle.macos_minimum_versions("Load command 0\n cmd LC_VERSION_MIN_MACOSX\n version 13.0\n sdk 15.0"), ["13.0"])
        self.assertEqual(bundle.version_tuple("15.0"), bundle.version_tuple("15.0.0"))

    def test_linux_missing_dependency_is_fatal(self):
        with patch.object(bundle, "run", return_value="libavcodec.so => not found"):
            with self.assertRaisesRegex(RuntimeError, "unresolved"):
                bundle.linux_dependencies(Path("libmpv.so"), {})

    def test_linux_excludes_glibc_but_keeps_cpp_and_ffmpeg(self):
        output = """libc.so.6 => /lib/libc.so.6 (0x0)
libm.so.6 => /lib/libm.so.6 (0x0)
libavcodec.so.62 => /prefix/lib/libavcodec.so.62 (0x0)
libstdc++.so.6 => /lib/libstdc++.so.6 (0x0)"""
        with patch.object(bundle, "run", return_value=output):
            deps = bundle.linux_dependencies(Path("libmpv.so"), {})
        self.assertEqual([name for name, _ in deps], ["libavcodec.so.62", "libstdc++.so.6"])


class ResolveTests(unittest.TestCase):
    def test_rejects_nonstable_tags(self):
        for tag in ("v0.43.0-rc1", "master", "v0.42", "../v0.42.0"):
            with self.assertRaises(ValueError):
                resolve_release.stable_version(tag)
        self.assertEqual(resolve_release.stable_version("v0.42.0"), "0.42.0")

    def test_new_release_resolves_annotated_tag_without_changing_other_pins(self):
        upstream = {"tag_name": "v0.43.0", "draft": False, "prerelease": False}
        with patch.object(resolve_release, "api", side_effect=[upstream, None, None,
                          {"object": {"type": "tag", "sha": "tag"}},
                          {"object": {"type": "commit", "sha": "a" * 40}}]):
            lock = resolve_release.resolve("", "1", "owner/repo")
        self.assertEqual(lock["mpv"]["version"], "0.43.0")
        self.assertEqual(lock["mpv"]["commit"], "a" * 40)
        self.assertEqual(lock["ffmpeg"], read_lock(ROOT / "native-lock.json")["ffmpeg"])
        self.assertEqual(lock["release"]["tag"], "libmpv-audio-0.43.0-r1")

    def test_published_release_is_never_overwritten(self):
        upstream = {"tag_name": "v0.42.0", "draft": False, "prerelease": False}
        with patch.object(resolve_release, "api", side_effect=[upstream, {"draft": False}]):
            self.assertIsNone(resolve_release.resolve("", "1", "owner/repo"))

    def test_partial_publication_requires_manual_inspection(self):
        upstream = {"tag_name": "v0.42.0", "draft": False, "prerelease": False}
        for responses in ([upstream, {"draft": True}], [upstream, None, {"object": {}}]):
            with patch.object(resolve_release, "api", side_effect=responses):
                with self.assertRaises(RuntimeError):
                    resolve_release.resolve("", "1", "owner/repo")

    def test_revision_is_validated_before_api_calls(self):
        with patch.object(resolve_release, "api") as api:
            for value in ("0", "-1", "1;echo", "r2"):
                with self.assertRaises(ValueError):
                    resolve_release.resolve("", value, "owner/repo")
            api.assert_not_called()


class PackageTests(unittest.TestCase):
    def test_missing_platform_prevents_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "missing runtime or source"):
                package.release_manifest(Path(tmp), ROOT / "native-lock.json")

    def test_all_platforms_and_sources_are_checksummed(self):
        lock = read_lock(ROOT / "native-lock.json")
        lock["release"] = {"tag": "libmpv-audio-0.42.0-r1", "repository": "owner/repo"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(root / "native-lock.json", lock)
            for platform in package.PLATFORMS:
                with zipfile.ZipFile(root / f"mpv-audio-{platform}.zip", "w") as archive:
                    archive.writestr("mpv/manifest.json", json.dumps({"platform": platform.replace("-", "/", 1)}))
                    archive.writestr("mpv/native-lock.json", json.dumps(lock))
                    archive.writestr("mpv/smoke-report.json", json.dumps(passing_report()))
                (root / f"sources-{platform}.tar.gz").write_bytes(b"source")
            package.release_manifest(root, root / "native-lock.json")
            metadata = json.loads((root / "release.json").read_text(encoding="utf-8"))
            self.assertEqual(len(metadata["assets"]), 6)
            checksums = (root / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(checksums), 14)
            self.assertTrue(all(len(line.split()[0]) == 64 for line in checksums))


if __name__ == "__main__":
    unittest.main()
