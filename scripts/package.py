#!/usr/bin/env python3
"""Package tested bundles and generate a manifest of all release assets."""
import argparse
import json
from pathlib import Path
import zipfile

from common import ROOT, read_lock, sha256, write_json

PLATFORMS = ("linux-amd64", "linux-arm64", "darwin-amd64", "darwin-arm64", "windows-amd64", "windows-arm64")


def package(target):
    platform = target.replace("/", "-")
    bundle = ROOT / "build" / platform / "bundle" / "mpv"
    if not (bundle / "smoke-report.json").exists():
        raise RuntimeError("native smoke test report is missing")
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    if manifest["platform"] != target:
        raise RuntimeError("bundle platform mismatch")
    output = ROOT / "dist"
    output.mkdir(exist_ok=True)
    archive = output / f"mpv-audio-{platform}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as stream:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                stream.write(path, path.relative_to(bundle.parent).as_posix())
    return archive


def release_manifest(directory, lock_path):
    lock = read_lock(lock_path)
    assets = {}
    for platform in PLATFORMS:
        archive = directory / f"mpv-audio-{platform}.zip"
        if not archive.is_file() or not (directory / f"sources-{platform}.tar.gz").is_file():
            raise RuntimeError(f"missing runtime or source archive for {platform}")
        with zipfile.ZipFile(archive) as stream:
            manifest = json.loads(stream.read("mpv/manifest.json"))
            plan = json.loads(stream.read("mpv/native-lock.json"))
            smoke = json.loads(stream.read("mpv/smoke-report.json"))
        if manifest["platform"].replace("/", "-") != platform or plan != lock:
            raise RuntimeError(f"inconsistent build plan in {platform}")
        if not all(smoke.get(key) is True for key in ("wav_decode", "spectrum_filter", "pcm_sampling")):
            raise RuntimeError(f"incomplete smoke tests in {platform}")
        assets[platform] = {"asset": archive.name, "sha256": sha256(archive)}
    write_json(directory / "release.json", {"schema": 1, "tag": lock["release"]["tag"],
               "mpv_version": lock["mpv"]["version"], "profile": "audio", "assets": assets})
    files = sorted(p for p in directory.iterdir() if p.is_file() and p.name != "SHA256SUMS")
    (directory / "SHA256SUMS").write_text("".join(f"{sha256(p)}  {p.name}\n" for p in files), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform")
    parser.add_argument("--release-dir", type=Path)
    parser.add_argument("--lock", type=Path, default=ROOT / "native-lock.json")
    args = parser.parse_args()
    if args.release_dir:
        release_manifest(args.release_dir, args.lock)
    elif args.platform:
        print(package(args.platform))
    else:
        parser.error("specify --platform or --release-dir")


if __name__ == "__main__":
    main()
