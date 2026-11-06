#!/usr/bin/env python3
"""Stage relocatable C SDK files and compile a real consumer against the bundle."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from common import ROOT, LIBRARY_NAMES, checked, read_lock, run, windows_environment, write_json


def pkg_config_text(api_version, goos):
    library = "-l:libmpv.so.2" if goos == "linux" else "-lmpv"
    return ("prefix=${pcfiledir}/../..\nlibdir=${prefix}\nincludedir=${prefix}/include\n\n"
            "Name: mpv-audio\nDescription: Audio-focused libmpv shared C API\n"
            f"Version: {api_version}\nLibs: -L${{libdir}} {library}\nCflags: -I${{includedir}}\n")


def stage_sdk(prefix, dest, goos, arch, env):
    headers = prefix / "include/mpv"
    if not (headers / "client.h").is_file():
        raise RuntimeError("installed libmpv C headers are missing")
    shutil.copytree(headers, dest / "include/mpv")
    shutil.copytree(ROOT / "examples", dest / "examples")
    config = (prefix / "lib/pkgconfig/mpv.pc").read_text(encoding="utf-8")
    match = re.search(r"(?m)^Version:\s*(\S+)", config)
    if not match:
        raise RuntimeError("libmpv pkg-config API version is missing")
    pc = dest / "lib/pkgconfig"
    pc.mkdir(parents=True)
    (pc / "mpv.pc").write_text(pkg_config_text(match[1], goos), encoding="utf-8")
    if goos == "windows":
        import pefile
        with pefile.PE(str(dest / LIBRARY_NAMES[goos])) as image:
            exports = sorted(entry.name.decode("ascii") for entry in image.DIRECTORY_ENTRY_EXPORT.symbols
                             if entry.name and entry.name.startswith(b"mpv_"))
        if not exports:
            raise RuntimeError("libmpv has no C API exports")
        definition = dest / "mpv.def"
        definition.write_text("LIBRARY mpv-2.dll\nEXPORTS\n" + "\n".join(exports) + "\n", encoding="utf-8")
        tool = shutil.which("llvm-dlltool", path=env["PATH"]) or shutil.which("dlltool", path=env["PATH"])
        if tool is None:
            raise RuntimeError("dlltool is required to generate the SDK import library")
        # Regenerate it: the original import library refers to libmpv-2.dll,
        # while the portable runtime bundle deliberately uses mpv-2.dll.
        checked(tool, "-m", "arm64" if arch == "arm64" else "i386:x86-64",
                "-d", str(definition), "-D", "mpv-2.dll", "-l", str(dest / "libmpv.dll.a"), env=env)


def check_sdk(bundle):
    lock = read_lock(bundle / "native-lock.json")
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    goos = manifest["platform"].split("/")[0]
    env = os.environ.copy()
    if goos == "windows":
        env = windows_environment(env)
    compiler = ("clang" if manifest["platform"] == "windows/arm64" else "gcc") if goos == "windows" else "cc"
    with tempfile.TemporaryDirectory(prefix="mpv-sdk-") as tmp:
        program = Path(tmp) / ("version.exe" if goos == "windows" else "version")
        args = [compiler, str(bundle / "examples/version.c"), "-I" + str(bundle / "include"),
                "-L" + str(bundle), "-l:libmpv.so.2" if goos == "linux" else "-lmpv", "-o", str(program)]
        if goos != "windows":
            args += ["-Wl,-rpath," + str(bundle)]
        if goos == "linux":
            args += ["-Wl,-rpath-link," + str(bundle)]
        checked(*args, env=env)
        runtime_env = env.copy()
        runtime_env.pop("LD_LIBRARY_PATH", None)
        runtime_env.pop("DYLD_LIBRARY_PATH", None)
        if goos == "windows":
            system = Path(env.get("SYSTEMROOT") or env["SystemRoot"])
            runtime_env["PATH"] = os.pathsep.join([str(bundle), str(system / "System32"), str(system)])
        version = run(str(program), env=runtime_env)
    if not re.search(r"(?:^|\s)v?" + re.escape(lock["mpv"]["version"]) + r"(?:$|[-\s])", version):
        raise RuntimeError(f"C SDK example loaded the wrong library: {version}")
    write_json(bundle / "sdk-report.json", {"c_compile_link_run": True, "mpv_version": version})
    print("C SDK compile/link/runtime test passed:", version)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", type=Path, required=True)
    args = parser.parse_args()
    check_sdk(args.check.resolve())


if __name__ == "__main__":
    main()
