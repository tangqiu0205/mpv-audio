"""Shared build metadata; importing this module has no external side effects."""
import hashlib
import json
import platform
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
LIBRARY_NAMES = {"windows": "mpv-2.dll", "linux": "libmpv.so.2", "darwin": "libmpv.dylib"}


def run(*args, cwd=None, env=None):
    return subprocess.check_output(args, cwd=cwd, env=env, text=True).strip()


def checked(*args, cwd=None, env=None):
    print("+", " ".join(map(str, args)), flush=True)
    subprocess.run(list(map(str, args)), cwd=cwd, env=env, check=True)


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
