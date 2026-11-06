#!/usr/bin/env python3
"""Merge tested macOS bundles and create a self-contained universal XCFramework."""
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import shutil
import stat
import tempfile
import zipfile

from common import ROOT, checked, read_lock, run, sha256, write_json

ARCHITECTURES = {"darwin-arm64": "arm64", "darwin-amd64": "x86_64"}


def extract_bundle(archive, destination):
    with zipfile.ZipFile(archive) as stream:
        for item in stream.infolist():
            original = item.orig_filename
            path = PurePosixPath(original)
            if path.is_absolute() or ".." in path.parts or "\\" in original or ":" in original or "\x00" in original:
                raise RuntimeError(f"unsafe bundle archive path: {item.filename}")
            if stat.S_ISLNK(item.external_attr >> 16):
                raise RuntimeError("input runtime bundles must not contain symlinks")
            stream.extract(item, destination)
    return destination / "mpv"


def validate_inputs(bundles):
    if set(bundles) != set(ARCHITECTURES):
        raise RuntimeError("both macOS architectures are required")
    plans, libraries, headers = [], [], []
    for name, root in bundles.items():
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        if manifest["platform"].replace("/", "-") != name or manifest.get("license_profile") != "lgpl":
            raise RuntimeError(f"invalid macOS input bundle: {name}")
        plans.append(read_lock(root / "native-lock.json"))
        files = {p.name for p in root.glob("*.dylib")}
        if "libmpv.dylib" not in files:
            raise RuntimeError(f"libmpv.dylib is missing in {name}")
        if set(manifest["libraries"]) != files:
            raise RuntimeError(f"library manifest mismatch in {name}")
        for filename, digest in manifest["libraries"].items():
            if sha256(root / filename) != digest:
                raise RuntimeError(f"library checksum mismatch: {name}: {filename}")
        libraries.append(files)
        headers.append({p.relative_to(root / "include").as_posix(): p.read_bytes()
                        for p in (root / "include").rglob("*.h")})
    if plans[0] != plans[1]:
        raise RuntimeError("cannot merge macOS bundles from different source locks")
    if libraries[0] != libraries[1]:
        raise RuntimeError("macOS dependency closures differ; align dependency versions before merging")
    if not headers[0] or headers[0] != headers[1]:
        raise RuntimeError("macOS C SDK headers differ")
    return plans[0], sorted(libraries[0])


def zip_tree(source, archive):
    # Preserve framework symlinks; following them loses the canonical bundle layout.
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as stream:
        for path in sorted(source.rglob("*")):
            name = path.relative_to(source).as_posix()
            if path.is_symlink():
                item = zipfile.ZipInfo(name)
                item.create_system = 3
                item.external_attr = (stat.S_IFLNK | 0o777) << 16
                stream.writestr(item, os.readlink(path))
            elif path.is_file():
                stream.write(path, name)


def merge_bundles(bundles, work):
    lock, libraries = validate_inputs(bundles)
    dest = work / "universal/mpv"
    dest.mkdir(parents=True)
    first = bundles["darwin-arm64"]
    for folder in ("include", "examples", "lib"):
        shutil.copytree(first / folder, dest / folder)
    for filename in ("native-lock.json", "BUILD-README.md"):
        shutil.copy2(first / filename, dest / filename)
    for name, root in bundles.items():
        shutil.copytree(root / "licenses", dest / "licenses" / name)
    for filename in libraries:
        checked("lipo", "-create", *(str(root / filename) for root in bundles.values()), "-output", str(dest / filename))
        architectures = set(run("lipo", "-archs", str(dest / filename)).split())
        if architectures != {"arm64", "x86_64"}:
            raise RuntimeError(f"invalid universal architectures in {filename}: {architectures}")
        checked("codesign", "--force", "--sign", "-", str(dest / filename))
    metadata = {"platform": "darwin/universal", "license_profile": "lgpl", "mpv_version": lock["mpv"]["version"],
                "architectures": ["arm64", "x86_64"], "macos_minimum": lock["macos_minimum"],
                "libraries": {name: sha256(dest / name) for name in libraries},
                "inputs": {name: json.loads((root / "manifest.json").read_text(encoding="utf-8")) for name, root in bundles.items()}}
    write_json(dest / "manifest.json", metadata)
    return dest


def create_xcframework(bundle, work):
    lock = read_lock(bundle / "native-lock.json")
    framework = work / "mpv.framework"
    version = framework / "Versions/A"
    (version / "Resources").mkdir(parents=True)
    (version / "Frameworks").mkdir()
    (version / "Modules").mkdir()
    shutil.copytree(bundle / "include/mpv", version / "Headers")
    (version / "Modules/module.modulemap").write_text('framework module mpv {\n  umbrella header "client.h"\n  export *\n}\n', encoding="utf-8")
    metadata = {"CFBundleIdentifier": "io.github.tangqiu0205.mpv-audio", "CFBundleName": "mpv",
                "CFBundleExecutable": "mpv", "CFBundlePackageType": "FMWK",
                "CFBundleVersion": lock["mpv"]["version"], "CFBundleShortVersionString": lock["mpv"]["version"],
                "CFBundleSupportedPlatforms": ["MacOSX"], "LSMinimumSystemVersion": lock["macos_minimum"]}
    with (version / "Resources/Info.plist").open("wb") as stream:
        plistlib.dump(metadata, stream)
    libraries = sorted(bundle.glob("*.dylib"))
    for source in libraries:
        main = source.name == "libmpv.dylib"
        target = version / "mpv" if main else version / "Frameworks" / source.name
        shutil.copy2(source, target)
        checked("install_name_tool", "-id", "@rpath/mpv.framework/Versions/A/mpv" if main else "@loader_path/" + source.name, str(target))
        if main:
            for dependency in libraries:
                if dependency.name != "libmpv.dylib":
                    checked("install_name_tool", "-change", "@loader_path/" + dependency.name,
                            "@loader_path/Frameworks/" + dependency.name, str(target))
        checked("codesign", "--force", "--sign", "-", str(target))
    (framework / "Versions/Current").symlink_to("A", target_is_directory=True)
    for name in ("Headers", "Frameworks", "Resources", "Modules"):
        (framework / name).symlink_to("Versions/Current/" + name, target_is_directory=True)
    (framework / "mpv").symlink_to("Versions/Current/mpv")
    checked("codesign", "--force", "--sign", "-", str(framework))
    output = work / "libmpv_macos.xcframework"
    checked("xcodebuild", "-create-xcframework", "-framework", str(framework), "-output", str(output))
    return output


def check_framework(xcframework, example, expected_version):
    frameworks = list(xcframework.glob("*/mpv.framework"))
    if len(frameworks) != 1:
        raise RuntimeError("expected one universal macOS framework slice")
    parent = frameworks[0].parent
    env = os.environ.copy()
    env.pop("DYLD_LIBRARY_PATH", None)
    env.pop("LD_LIBRARY_PATH", None)
    with tempfile.TemporaryDirectory(prefix="mpv-framework-test-") as tmp:
        executable = Path(tmp) / "version"
        checked("cc", str(example), "-F" + str(parent), "-framework", "mpv", "-Wl,-rpath," + str(parent), "-o", str(executable), env=env)
        version = run(str(executable), env=env)
    if not re.search(r"(?:^|\s)v?" + re.escape(expected_version) + r"(?:$|[-\s])", version):
        raise RuntimeError(f"XCFramework consumer loaded the wrong library: {version}")
    return {"framework_compile_link_run": True, "architectures": ["arm64", "x86_64"],
            "mpv_version": version, "expected_mpv_version": expected_version, "license_profile": "lgpl"}


def prepare(inputs):
    work = ROOT / "build/apple-sdk"
    work.mkdir(parents=True, exist_ok=False)
    bundles = {name: extract_bundle(inputs / f"mpv-audio-{name}.zip", work / name) for name in ARCHITECTURES}
    print("Universal runtime:", merge_bundles(bundles, work))


def package():
    work = ROOT / "build/apple-sdk"
    bundle = work / "universal/mpv"
    # The universal runtime itself must have passed both test suites first.
    from smoke import validate_report
    lock = read_lock(bundle / "native-lock.json")
    validate_report(json.loads((bundle / "smoke-report.json").read_text(encoding="utf-8")), lock["mpv"]["version"])
    if json.loads((bundle / "sdk-report.json").read_text(encoding="utf-8")).get("c_compile_link_run") is not True:
        raise RuntimeError("universal C SDK test is missing")
    xcframework = create_xcframework(bundle, work)
    report = check_framework(xcframework, bundle / "examples/version.c", lock["mpv"]["version"])
    output = work / "release"
    output.mkdir()
    shutil.move(str(xcframework), output / xcframework.name)
    shutil.copytree(bundle / "licenses", output / "licenses")
    for name in ("native-lock.json", "BUILD-README.md", "manifest.json", "smoke-report.json", "sdk-report.json"):
        shutil.copy2(bundle / name, output / name)
    write_json(output / "apple-sdk.json", report)
    dest = ROOT / "dist"
    dest.mkdir(exist_ok=True)
    zip_tree(output, dest / "libmpv_macos.xcframework.zip")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path)
    parser.add_argument("--package", action="store_true")
    args = parser.parse_args()
    if args.inputs:
        prepare(args.inputs.resolve())
    elif args.package:
        package()
    else:
        parser.error("specify --inputs or --package")


if __name__ == "__main__":
    main()
