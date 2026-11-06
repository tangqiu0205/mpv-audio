#!/usr/bin/env python3
"""Build pinned mpv/FFmpeg sources with an audio-focused profile."""
import argparse
import os
from pathlib import Path
import re
import shutil
import tarfile

from common import ROOT, LIBRARY_NAMES, checked, host_platform, read_lock, run, sha256, windows_environment, write_json
from bundle import bundle_libraries
from sdk import stage_sdk

DECODERS = (
    "aac", "aac_fixed", "aac_latm", "mp3", "mp3float", "mp3adu", "mp3adufloat",
    "mp3on4", "mp3on4float", "mp2", "mp2float", "flac", "vorbis", "opus", "alac",
    "ac3", "ac3_fixed", "eac3", "wmav1", "wmav2", "wmapro", "wmalossless",
    "amrnb", "amrwb", "ape", "wavpack", "pcm_s16le", "pcm_s16be", "pcm_s24le",
    "pcm_s24be", "pcm_s32le", "pcm_s32be", "pcm_u8", "pcm_f32le", "pcm_f64le",
    "pcm_alaw", "pcm_mulaw",
)
DEMUXERS = (
    "aac", "mp3", "flac", "ogg", "hls", "mpegts", "mov", "matroska", "wav",
    "aiff", "asf", "ape", "ac3", "eac3", "amr", "au", "wv", "pcm_s16le", "pcm_s16be",
    "pcm_s24le", "pcm_s24be", "pcm_s32le", "pcm_s32be", "pcm_u8", "pcm_f32le", "pcm_f64le", "pcm_alaw", "pcm_mulaw",
)
FILTERS = (
    "abuffer", "abuffersink", "aformat", "asplit", "pan", "bandpass", "join",
    "astats", "aresample", "anull", "atempo", "volume", "atrim", "asetpts",
    "anullsrc", "afade", "alimiter", "equalizer", "highpass", "lowpass",
)
PARSERS = ("aac", "aac_latm", "mpegaudio", "flac", "opus", "vorbis", "ac3")
PROTOCOLS = ("file", "http", "https", "httpproxy", "tcp", "tls", "crypto", "data", "udp", "rtp")


def clone_source(spec, dest, env):
    checked("git", "clone", "--depth=1", "--branch", spec["tag"], spec["repository"], str(dest), env=env)
    actual = run("git", "-C", str(dest), "rev-parse", "HEAD", env=env)
    if actual != spec["commit"]:
        raise RuntimeError(f"source tag moved: {spec['repository']}: expected {spec['commit']}, got {actual}")
    checked("git", "-C", str(dest), "submodule", "update", "--init", "--recursive", "--depth=1", env=env)


def apply_source_patches(spec, source, env):
    records = []
    for name in spec.get("patches", []):
        patch = ROOT / "patches" / name
        checked("git", "-C", str(source), "apply", "--check", str(patch), env=env)
        checked("git", "-C", str(source), "apply", str(patch), env=env)
        records.append({"file": name, "sha256": sha256(patch)})
    if records:
        copies = source / "mpv-audio-patches"
        copies.mkdir()
        for record in records:
            shutil.copy2(ROOT / "patches" / record["file"], copies / record["file"])
        write_json(copies / "manifest.json", records)
    return records


def ffmpeg_options(goos, arch, prefix):
    options = [
        f"--prefix={prefix}", "--disable-autodetect", "--disable-everything",
        "--disable-programs", "--disable-doc", "--disable-debug", "--disable-static",
        "--disable-gpl", "--disable-nonfree",
        "--enable-shared", "--enable-pic", "--disable-asm", "--disable-avdevice",
        "--enable-avcodec", "--enable-avformat", "--enable-avfilter",
        "--enable-swresample", "--enable-swscale", "--enable-network",
        "--enable-decoder=" + ",".join(DECODERS),
        "--enable-demuxer=" + ",".join(DEMUXERS),
        "--enable-parser=" + ",".join(PARSERS),
        "--enable-filter=" + ",".join(FILTERS),
        "--enable-protocol=" + ",".join(PROTOCOLS),
    ]
    options.append({"linux": "--enable-gnutls", "darwin": "--enable-securetransport", "windows": "--enable-schannel"}[goos])
    if goos == "windows":
        options += ["--target-os=mingw32", "--arch=" + ("aarch64" if arch == "arm64" else "x86_64")]
        if arch == "arm64":
            options += ["--cc=clang", "--cxx=clang++", "--ld=clang", "--ar=llvm-ar", "--nm=llvm-nm", "--ranlib=llvm-ranlib"]
    return options


def mpv_options(goos):
    options = ["-Dlibmpv=true", "-Dcplayer=false", "-Dbuild-date=false", "-Dgpl=false",
               "-Dgl=disabled", "-Dlua=disabled", "-Dvector=enabled", "-Db_lundef=true"]
    options += {"linux": ["-Dalsa=enabled", "-Dpulse=enabled"],
                "darwin": ["-Dcoreaudio=enabled"],
                "windows": ["-Dwasapi=enabled", "-Dwin32-threads=enabled"]}[goos]
    return options


def verify_ffmpeg_profile(source):
    config = (source / "config_components.h").read_text(encoding="utf-8")
    enabled = set(re.findall(r"^#define CONFIG_(\w+)_DECODER 1$", config, re.MULTILINE))
    expected = {name.upper() for name in DECODERS}
    if not enabled or enabled - expected:
        raise RuntimeError(f"unexpected FFmpeg decoder configuration: {sorted(enabled - expected)}")
    license_config = (source / "config.h").read_text(encoding="utf-8")
    for name in ("GPL", "NONFREE"):
        if not re.search(rf"^#define CONFIG_{name} 0$", license_config, re.MULTILINE):
            raise RuntimeError(f"FFmpeg LGPL profile requires CONFIG_{name}=0")
    for name in ("AAC", "MP3FLOAT", "FLAC", "VORBIS", "OPUS", "PCM_S16LE"):
        if name not in enabled:
            raise RuntimeError(f"required decoder is missing: {name}")
    for name in ("HTTPS_PROTOCOL", "HLS_DEMUXER", "ASTATS_FILTER", "BANDPASS_FILTER", "JOIN_FILTER"):
        if f"#define CONFIG_{name} 1" not in config:
            raise RuntimeError(f"required FFmpeg component is missing: {name}")


def meson_build(name, source, work, prefix, options, env, shared=False):
    build = work / (name + "-build")
    args = ["meson", "setup", str(build), str(source), "--prefix=" + prefix.as_posix(),
            "--libdir=lib", "--buildtype=release", "--auto-features=disabled",
            "--wrap-mode=nodownload", "-Ddefault_library=" + ("shared" if shared else "static"),
            "-Db_staticpic=true", *options]
    checked(*args, env=env)
    checked("meson", "compile", "-C", str(build), "-j", str(min(os.cpu_count() or 2, 8)), env=env)
    checked("meson", "install", "-C", str(build), env=env)


def source_archive(work, output):
    # Ship patched source trees and pinned submodules; generated configs ship in the runtime bundle.
    with tarfile.open(output, "w:gz") as archive:
        for name in ("mpv", "ffmpeg", "libplacebo", "libass"):
            archive.add(work / name, arcname="sources/" + name,
                        filter=lambda member: None if ".git" in Path(member.name).parts else member)


def build(target, lock_path):
    if target != host_platform():
        raise RuntimeError(f"native build required: host={host_platform()} target={target}")
    goos, arch = target.split("/")
    lock = read_lock(lock_path)
    work = ROOT / "build" / target.replace("/", "-")
    if work.exists():
        raise RuntimeError(f"build directory exists; remove it explicitly before rebuilding: {work}")
    work.mkdir(parents=True)
    prefix = work / "prefix"
    env = os.environ.copy()
    env["PKG_CONFIG_PATH"] = str(prefix / "lib/pkgconfig") + os.pathsep + env.get("PKG_CONFIG_PATH", "")
    env["PATH"] = str(prefix / "bin") + os.pathsep + env["PATH"]
    if goos == "windows":
        env = windows_environment(env)
    if goos == "darwin":
        env["MACOSX_DEPLOYMENT_TARGET"] = lock["macos_minimum"]
        brew = Path(run("brew", "--prefix"))
        env["PKG_CONFIG_PATH"] += os.pathsep + str(brew / "lib/pkgconfig")
        env["PKG_CONFIG_PATH"] += os.pathsep + str(brew / "opt/icu4c/lib/pkgconfig")
    if goos == "linux":
        env["LD_LIBRARY_PATH"] = str(prefix / "lib")
    patches = {}
    for name in ("mpv", "ffmpeg", "libplacebo", "libass"):
        clone_source(lock[name], work / name, env)
        patches[name] = apply_source_patches(lock[name], work / name, env)

    output = ROOT / "dist"
    output.mkdir(exist_ok=True)
    source_archive(work, output / ("sources-" + target.replace("/", "-") + ".tar.gz"))
    meson_build("libplacebo", work / "libplacebo", work, prefix,
                ["-Ddemos=false", "-Dtests=false", "-Ddovi=disabled"], env, shared=True)
    meson_build("libass", work / "libass", work, prefix,
                ["-Drequire-system-font-provider=false"], env, shared=True)
    configure_prefix = prefix.as_posix()
    if goos == "windows":
        configure_prefix = run("cygpath", "-u", str(prefix))
    ff_options = ffmpeg_options(goos, arch, configure_prefix)
    checked("bash", "configure", *ff_options, cwd=work / "ffmpeg", env=env)
    verify_ffmpeg_profile(work / "ffmpeg")
    checked("make", "-j", str(min(os.cpu_count() or 2, 8)), cwd=work / "ffmpeg", env=env)
    checked("make", "install", cwd=work / "ffmpeg", env=env)
    mp_options = mpv_options(goos)
    meson_build("mpv", work / "mpv", work, prefix, mp_options, env, shared=True)

    if goos == "windows":
        libraries = list((prefix / "bin").glob("*mpv*.dll"))
        if len(libraries) != 1:
            raise RuntimeError(f"expected exactly one libmpv DLL: {libraries}")
        library = libraries[0]
    else:
        library = prefix / "lib" / LIBRARY_NAMES[goos]
    dest = work / "bundle" / "mpv"
    dest.mkdir(parents=True)
    metadata = bundle_libraries(library, dest, prefix, goos, lock["macos_minimum"], env)
    stage_sdk(prefix, dest, goos, arch, env)
    mpv_config = (work / "mpv-build/config.h").read_text(encoding="utf-8")
    if not re.search(r"^#define HAVE_GPL 0$", mpv_config, re.MULTILINE):
        raise RuntimeError("mpv LGPL profile requires HAVE_GPL=0")
    notices = dest / "licenses"
    for name in ("mpv", "ffmpeg", "libplacebo", "libass"):
        source = work / name
        folder = notices / name
        folder.mkdir(parents=True, exist_ok=True)
        found = False
        for item in source.iterdir():
            if item.is_file() and re.search(r"(?i)^(copying|license|copyright|authors)", item.name):
                shutil.copy2(item, folder / item.name)
                found = True
        if not found:
            raise RuntimeError(f"no license notices collected for {name}")
        if patches[name]:
            shutil.copytree(source / "mpv-audio-patches", folder / "patches")
    shutil.copy2(work / "mpv-build/config.h", notices / "mpv/config.h")
    shutil.copy2(work / "ffmpeg/config.h", notices / "ffmpeg/config.h")
    shutil.copy2(work / "ffmpeg/config_components.h", notices / "ffmpeg/config_components.h")
    metadata.update(platform=target, mpv_version=lock["mpv"]["version"], profile="audio",
                    license_profile="lgpl", supporting_licenses_review_required=True,
                    sources={name: lock[name] for name in ("mpv", "ffmpeg", "libplacebo", "libass")},
                    options={"ffmpeg": ff_options, "mpv": mp_options}, patches=patches,
                    macos_minimum=lock["macos_minimum"] if goos == "darwin" else None)
    write_json(dest / "manifest.json", metadata)
    shutil.copy2(lock_path, dest / "native-lock.json")
    shutil.copy2(ROOT / "README.md", dest / "BUILD-README.md")
    print("Built:", dest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--lock", type=Path, default=ROOT / "native-lock.json")
    args = parser.parse_args()
    build(args.platform, args.lock.resolve())


if __name__ == "__main__":
    main()
