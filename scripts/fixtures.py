#!/usr/bin/env python3
"""Generate synthetic audio locally; the generator is never shipped with the library."""
import argparse
import math
from pathlib import Path
import struct
import wave

from common import checked, run, write_json

FORMATS = {
    "mp3": ("tone.mp3", ["-c:a", "libmp3lame", "-b:a", "128k"]),
    "aac-mono": ("aac-mono.aac", ["-ac", "1", "-ar", "32000", "-c:a", "aac", "-f", "adts"]),
    "aac-stereo": ("aac-stereo.aac", ["-ar", "48000", "-c:a", "aac", "-f", "adts"]),
    "flac": ("tone.flac", ["-c:a", "flac"]),
    "vorbis-mono": ("vorbis-mono.ogg", ["-ac", "1", "-ar", "32000", "-c:a", "libvorbis"]),
    "vorbis-stereo": ("vorbis-stereo.ogg", ["-ar", "48000", "-c:a", "libvorbis"]),
    "opus-mono": ("opus-mono.ogg", ["-ac", "1", "-ar", "24000", "-c:a", "libopus", "-b:a", "48k"]),
    "opus-stereo": ("opus-stereo.ogg", ["-ar", "48000", "-c:a", "libopus", "-b:a", "96k"]),
}
REQUIRED_FORMATS = ("wav", *FORMATS)


def generate(directory):
    directory.mkdir(parents=True, exist_ok=False)
    source = directory / "tone.wav"
    with wave.open(str(source), "wb") as out:
        out.setparams((2, 2, 44100, 0, "NONE", "not compressed"))
        out.writeframes(b"".join(struct.pack("<hh", int(12000 * math.sin(2 * math.pi * 997 * n / 44100)),
                                           int(12000 * math.sin(2 * math.pi * 1601 * n / 44100))) for n in range(88200)))
    for filename, options in FORMATS.values():
        checked("ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(source),
                "-vn", *options, str(directory / filename))
    checked("ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(source),
            "-vn", "-c:a", "aac", "-f", "hls", "-hls_time", "1", "-hls_list_size", "0",
            "-hls_segment_filename", str(directory / "segment-%03d.ts"), str(directory / "live.m3u8"))
    # Ephemeral test credentials only: never included in runtime/source releases.
    checked("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
            "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost",
            "-keyout", str(directory / "test-key.pem"), "-out", str(directory / "test-cert.pem"))
    write_json(directory / "fixtures.json", {"source": "synthetic two-second 997/1601 Hz stereo tone",
               "generator": run("ffmpeg", "-version").splitlines()[0], "formats": list(REQUIRED_FORMATS)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    generate(args.output.resolve())


if __name__ == "__main__":
    main()
