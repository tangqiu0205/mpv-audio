"""Shared build metadata; importing this module has no external side effects."""
import hashlib
import json
import platform
from pathlib import Path
import os
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
LIBRARY_NAMES = {"windows": "mpv-2.dll", "linux": "libmpv.so.2", "darwin": "libmpv.dylib"}


def command_args(args, env=None):
    args = list(map(str, args))
    # Windows CreateProcess can otherwise resolve against the parent's PATH,
    # ignoring the toolchain PATH passed through env (notably bash.exe/WSL).
    path = (env if env is not None else os.environ).get("PATH", "")
    executable = shutil.which(args[0], path=path)
    if executable is None:
        raise FileNotFoundError(f"required tool not found in build PATH: {args[0]}")
    return [executable, *args[1:]]


def run(*args, cwd=None, env=None):
    return subprocess.check_output(command_args(args, env), cwd=cwd, env=env, text=True).strip()


def checked(*args, cwd=None, env=None):
    command = command_args(args, env)
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def windows_environment(env):
    env = env.copy()
    root = Path(run("cygpath", "-m", "/", env=env))
    mingw = Path(run("cygpath", "-m", env["MINGW_PREFIX"], env=env))
    if not (root / "usr/bin/bash.exe").is_file():
        raise RuntimeError(f"MSYS2 Bash is missing under {root}")
    env["PATH"] = os.pathsep.join([str(mingw / "bin"), str(root / "usr/bin"), env["PATH"]])
    return env


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def host_platform():
    system = {"Windows": "windows", "Darwin": "darwin", "Linux": "linux"}[platform.system()]
    arch = {"AMD64": "amd64", "x86_64": "amd64", "arm64": "arm64", "ARM64": "arm64", "aarch64": "arm64"}[platform.machine()]
    return f"{system}/{arch}"


def read_lock(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema") != 1:
        raise ValueError("unsupported lock schema")
    return data


def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
