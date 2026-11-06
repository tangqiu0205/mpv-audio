#!/usr/bin/env python3
"""Resolve a stable upstream release without changing the checked-in baseline."""
import argparse
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request

from common import ROOT, read_lock, write_json

API = "https://api.github.com"


def api(path, optional=False):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "mpv-audio"}
    token = os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(API + path, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if optional and error.code == 404:
            return None
        raise


def stable_version(tag):
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise ValueError(f"not a stable mpv release tag: {tag!r}")
    return tag[1:]


def resolve(version, revision, repository):
    if not re.fullmatch(r"[1-9]\d*", revision):
        raise ValueError("revision must be a positive integer")
    upstream = "/repos/mpv-player/mpv"
    release = api(upstream + ("/releases/tags/v" + version if version else "/releases/latest"))
    version = stable_version(release["tag_name"])
    if release["draft"] or release["prerelease"]:
        raise ValueError("refusing draft or prerelease")
    tag = f"libmpv-audio-{version}-r{revision}"
    existing = api(f"/repos/{repository}/releases/tags/{tag}", optional=True)
    if existing is not None:
        if existing["draft"]:
            raise RuntimeError(f"{tag} has an unfinished draft; inspect it before retrying")
        return None
    # A tag without a release may belong to a failed/manual publication.
    if api(f"/repos/{repository}/git/ref/tags/{tag}", optional=True) is not None:
        raise RuntimeError(f"tag {tag} already exists without a published release")
    ref = api(upstream + "/git/ref/tags/" + release["tag_name"])["object"]
    for _ in range(5):
        if ref["type"] == "commit":
            break
        if ref["type"] != "tag":
            raise ValueError("upstream tag does not resolve to a commit")
        ref = api(upstream + "/git/tags/" + ref["sha"])["object"]
    else:
        raise ValueError("too many nested annotated tags")
    lock = read_lock(ROOT / "native-lock.json")
    lock["mpv"].update(version=version, tag=release["tag_name"], commit=ref["sha"])
    lock["release"] = {"tag": tag, "repository": repository}
    return lock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="")
    parser.add_argument("--revision", default="1")
    parser.add_argument("--repository", required=True)
    args = parser.parse_args()
    lock = resolve(args.version, args.revision, args.repository)
    outputs = {"build": "false"}
    if lock:
        output = ROOT / "build"
        output.mkdir(exist_ok=True)
        write_json(output / "native-lock.json", lock)
        outputs = {"build": "true", "tag": lock["release"]["tag"]}
        print(json.dumps(lock, indent=2))
    else:
        print("Latest requested release is already published; nothing to do.")
    if os.environ.get("GITHUB_OUTPUT"):
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
            for key, value in outputs.items():
                stream.write(f"{key}={value}\n")


if __name__ == "__main__":
    main()
