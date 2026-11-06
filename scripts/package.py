#!/usr/bin/env python3
"""Package tested bundles and generate a manifest of all release assets."""
import argparse
import json
from pathlib import Path
import zipfile

from common import ROOT, LIBRARY_NAMES, read_lock, sha256, write_json
from smoke import validate_report

PLATFORMS = ("linux-amd64", "linux-arm64", "darwin-amd64", "darwin-arm64", "windows-amd64", "windows-arm64")


def package(target):
    platform = target.replace("/", "-")
    bundle = ROOT / "build" / platform / "bundle" / "mpv"
    report = json.loads((bundle / "smoke-report.json").read_text(encoding="utf-8"))
    lock = read_lock(bundle / "native-lock.json")
    validate_report(report, lock["mpv"]["version"])
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    if manifest["platform"] != target:
        raise RuntimeError("bundle platform mismatch")
    if manifest.get("license_profile") != "lgpl":
        raise RuntimeError("bundle must use the LGPL build profile")
    sdk = json.loads((bundle / "sdk-report.json").read_text(encoding="utf-8"))
    if sdk.get("c_compile_link_run") is not True:
        raise RuntimeError("C SDK compile/link/runtime test is missing")
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
            sdk = json.loads(stream.read("mpv/sdk-report.json"))
            for required in ("mpv/include/mpv/client.h", "mpv/lib/pkgconfig/mpv.pc",
                             "mpv/" + LIBRARY_NAMES[platform.split("-")[0]]):
                if required not in stream.namelist():
                    raise RuntimeError(f"missing SDK file in {platform}: {required}")
        if manifest["platform"].replace("/", "-") != platform or plan != lock:
            raise RuntimeError(f"inconsistent build plan in {platform}")
        validate_report(smoke, lock["mpv"]["version"])
        if manifest.get("license_profile") != "lgpl" or sdk.get("c_compile_link_run") is not True:
            raise RuntimeError(f"LGPL profile or C SDK test is missing in {platform}")
        assets[platform] = {"asset": archive.name, "sha256": sha256(archive)}
    apple_archive = directory / "libmpv_macos.xcframework.zip"
    with zipfile.ZipFile(apple_archive) as stream:
        apple = json.loads(stream.read("apple-sdk.json"))
        apple_lock = json.loads(stream.read("native-lock.json"))
        if "libmpv_macos.xcframework/Info.plist" not in stream.namelist():
            raise RuntimeError("XCFramework metadata is missing")
    if (apple_lock != lock or apple.get("framework_compile_link_run") is not True
            or set(apple.get("architectures", [])) != {"arm64", "x86_64"}
            or apple.get("expected_mpv_version") != lock["mpv"]["version"]
            or apple.get("license_profile") != "lgpl"):
        raise RuntimeError("universal XCFramework validation is incomplete")
    assets["darwin-universal"] = {"asset": apple_archive.name, "sha256": sha256(apple_archive)}
    write_json(directory / "release.json", {"schema": 1, "tag": lock["release"]["tag"],
               "mpv_version": lock["mpv"]["version"], "profile": "audio", "license_profile": "lgpl",
               "channel": "candidate", "assets": assets})
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
