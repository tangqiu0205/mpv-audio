"""Copy the complete native dependency closure, refusing unresolved libraries."""
import os
from pathlib import Path
import re
import shutil

from common import LIBRARY_NAMES, checked, run, sha256

LINUX_SYSTEM = re.compile(r"^(ld-linux|lib(c|m|dl|pthread|rt|resolv|util|anl)\.so)")


def macos_minimum_versions(commands):
    versions = []
    for command in commands.split("Load command"):
        if re.search(r"cmd LC_BUILD_VERSION\b", command):
            field = "minos"
        elif re.search(r"cmd LC_VERSION_MIN_MACOSX\b", command):
            field = "version"
        else:
            continue
        versions.extend(re.findall(rf"(?m)^\s*{field}\s+(\d+\.\d+(?:\.\d+)?)", command))
    return versions


def version_tuple(version):
    parts = tuple(map(int, version.split(".")))
    return parts + (0,) * (3 - len(parts))


def macos_dependencies(library, prefix, brew, env):
    commands = run("otool", "-l", str(library), env=env)
    rpaths = []
    for command in commands.split("Load command"):
        if re.search(r"cmd LC_RPATH\b", command):
            match = re.search(r"(?m)^\s*path (.+?) \(offset", command)
            if match:
                rpaths.append(match[1].replace("@loader_path", str(library.parent)))
    result = []
    for line in run("otool", "-L", str(library), env=env).splitlines()[1:]:
        dep = line.strip().split(" (", 1)[0]
        if dep.startswith(("/usr/lib/", "/System/")):
            continue
        if dep.startswith("@loader_path/"):
            candidates = [library.parent / dep[len("@loader_path/"):]]
        elif dep.startswith("@rpath/"):
            suffix = dep[len("@rpath/"):]
            candidates = [Path(p) / suffix for p in rpaths] + [prefix / "lib" / suffix, brew / "lib" / suffix]
        elif dep.startswith("@"):
            raise RuntimeError(f"unsupported install name: {library}: {dep}")
        else:
            candidates = [Path(dep)]
        actual = next((p for p in candidates if p.exists()), None)
        if actual is None:
            raise RuntimeError(f"unresolved dependency: {library}: {dep}")
        if actual.resolve() != library:
            result.append((dep, actual))
    return commands, result


def linux_dependencies(library, env):
    output = run("ldd", str(library), env=env)
    if "not found" in output:
        raise RuntimeError(f"unresolved dependencies for {library}:\n{output}")
    result = []
    for line in output.splitlines():
        match = re.search(r"^\s*(\S+) => (/\S+)", line)
        if match and not LINUX_SYSTEM.match(match[1]):
            result.append((match[1], Path(match[2])))
    return result


def windows_dependencies(library, prefix, env):
    import pefile
    with pefile.PE(str(library)) as image:
        descriptors = list(getattr(image, "DIRECTORY_ENTRY_IMPORT", []))
        descriptors += list(getattr(image, "DIRECTORY_ENTRY_DELAY_IMPORT", []))
        names = [entry.dll.decode("ascii") for entry in descriptors]
    system = Path(env.get("SYSTEMROOT") or env["SystemRoot"]) / "System32"
    mingw = Path(run("cygpath", "-m", env["MINGW_PREFIX"], env=env))
    result = []
    for name in names:
        if name.lower().startswith(("msys-", "cygwin")):
            raise RuntimeError(f"MSYS/Cygwin runtime is not redistributable as a native Windows build: {name}")
        if name.lower().startswith(("api-ms-", "ext-ms-")) or (system / name).exists():
            continue
        actual = next((p / name for p in (prefix / "bin", mingw / "bin") if (p / name).exists()), None)
        if actual is None:
            raise RuntimeError(f"unresolved Windows dependency: {library}: {name}")
        result.append((name, actual))
    return result


def collect_package_notice(source, real, dest, goos, env):
    """Record exact supporting-library packages and their available license files."""
    if goos == "linux":
        # Resolve usr-merge aliases before querying the package database.
        candidates = [str(source), str(real)]
        if str(real).startswith("/usr/"):
            candidates.append(str(real)[4:])
        package = None
        for candidate in candidates:
            import subprocess
            try:
                owner = run("dpkg-query", "-S", candidate, env=env)
                package = owner.split(": ", 1)[0].splitlines()[0]
                break
            except subprocess.CalledProcessError:
                continue
        if package is None:
            raise RuntimeError(f"cannot identify Debian package for {source}")
        info = run("dpkg-query", "-W", "-f=${Package}\t${Version}\t${source:Package}\t${source:Version}", package, env=env)
        notice = Path("/usr/share/doc") / package.split(":")[0] / "copyright"
        if not notice.exists():
            raise RuntimeError(f"missing license for {package}")
        shutil.copy2(notice, dest / (package.replace(":", "-") + ".txt"))
        return package, info
    if goos == "darwin":
        parts = real.parts
        if "Cellar" not in parts:
            raise RuntimeError(f"non-system dependency is not a Homebrew formula: {real}")
        index = parts.index("Cellar")
        formula, version = parts[index + 1:index + 3]
        cellar = Path(*parts[:index + 3])
        folder = dest / formula
        folder.mkdir(exist_ok=True)
        for item in cellar.iterdir():
            if item.is_file() and (re.match(r"(?i)(copying|license|copyright|authors)", item.name) or item.name == "INSTALL_RECEIPT.json"):
                shutil.copy2(item, folder / item.name)
        info = run("brew", "info", "--json=v2", formula, env=env)
        (folder / "formula.json").write_text(info + "\n", encoding="utf-8")
        return formula, {"version": version, "metadata": "licenses/packages/" + formula + "/formula.json"}
    posix = run("cygpath", "-u", str(source), env=env)
    package = run("pacman", "-Qoq", posix, env=env)
    if "\n" in package:
        raise RuntimeError(f"ambiguous package for {source}: {package}")
    info = run("pacman", "-Qi", package, env=env)
    (dest / (package + ".txt")).write_text(info + "\n", encoding="utf-8")
    mingw = Path(run("cygpath", "-m", env["MINGW_PREFIX"], env=env))
    # Pacman package names have an environment prefix; licenses normally don't.
    stem = re.sub(r"^mingw-w64-(?:ucrt-x86_64|clang-aarch64)-", "", package)
    license_dir = mingw / "share/licenses" / stem
    if not license_dir.exists():
        raise RuntimeError(f"missing MSYS2 license directory: {license_dir}")
    shutil.copytree(license_dir, dest / stem, dirs_exist_ok=True)
    return package, info


def bundle_libraries(library, dest, prefix, goos, minimum, env):
    brew = Path(run("brew", "--prefix", env=env)) if goos == "darwin" else None
    pending = [(library, LIBRARY_NAMES[goos])]
    seen = {}
    packages = {}
    package_dir = dest / "licenses/packages"
    package_dir.mkdir(parents=True)
    while pending:
        source, name = pending.pop()
        real = source.resolve(strict=True)
        key = name.lower() if goos == "windows" else name
        if key in seen:
            if seen[key] != real:
                raise RuntimeError(f"conflicting libraries named {name}")
            continue
        seen[key] = real
        target = dest / name
        shutil.copy2(real, target)
        target.chmod(0o755)
        if goos == "linux":
            dependencies = linux_dependencies(real, env)
        elif goos == "darwin":
            commands, dependencies = macos_dependencies(real, prefix, brew, env)
            versions = macos_minimum_versions(commands)
            if not versions:
                raise RuntimeError(f"missing deployment target in {real}")
            if any(version_tuple(v) > version_tuple(minimum) for v in versions):
                raise RuntimeError(f"{real} requires macOS {versions}, configured minimum is {minimum}")
            checked("install_name_tool", "-id", "@loader_path/" + name, str(target), env=env)
            for old, actual in dependencies:
                checked("install_name_tool", "-change", old, "@loader_path/" + actual.name, str(target), env=env)
        else:
            dependencies = windows_dependencies(real, prefix, env)
        for old, actual in dependencies:
            pending.append((actual, old if goos != "darwin" else actual.name))
        if not real.is_relative_to(prefix.resolve()):
            package, info = collect_package_notice(source, real, package_dir, goos, env)
            packages[package] = info
    if goos == "linux":
        for name in seen:
            checked("patchelf", "--set-rpath", "$ORIGIN", str(dest / name), env=env)
    if goos == "darwin":
        for name in seen:
            checked("codesign", "--force", "--sign", "-", str(dest / name), env=env)
    return {"packages": packages, "libraries": {p.name: sha256(p) for p in dest.iterdir() if p.is_file()}}
