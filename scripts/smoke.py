#!/usr/bin/env python3
"""Exercise the relocated bundle through libmpv's C API without a sound device."""
import argparse
import ctypes as C
import json
import math
import os
from pathlib import Path
import re
import struct
import tempfile
import time
import wave

from common import ROOT, LIBRARY_NAMES, host_platform, read_lock, write_json


class Event(C.Structure):
    _fields_ = [("id", C.c_int), ("error", C.c_int), ("userdata", C.c_uint64), ("data", C.c_void_p)]


def spectrum_filter():
    channels = "FL FR FC LFE BL BR FLC FRC BC SL SR TC TFL TFC TFR TBL TBC TBR LFE2 TSL TSR BFC BFL BFR DL DR".split()
    graph = "aformat=sample_rates=44100:channel_layouts=stereo,asplit=2[dry][s];[s]pan=mono|c0=0.5*c0+0.5*c1,asplit=24"
    graph += "".join(f"[s{b}]" for b in range(24)) + ";"
    for b in range(24):
        low, high = 40 * 350 ** (b / 24), 40 * 350 ** ((b + 1) / 24)
        graph += f"[s{b}]bandpass=f={math.sqrt(low * high):.3f}:t=h:w={high - low:.3f}[b{b}];"
    graph += "[dry]" + "".join(f"[b{b}]" for b in range(24))
    graph += f"join=inputs=25:channel_layout={'+'.join(channels)}:map=0.0-FL|0.1-FR"
    graph += "".join(f"|{b + 1}.0-{name}" for b, name in enumerate(channels[2:]))
    graph += ",astats=metadata=1:reset=1:measure_perchannel=RMS_level:measure_overall=none,pan=stereo|c0=c0|c1=c1"
    return "@spectrum:lavfi=[" + graph + "]"


class Client:
    def __init__(self, library):
        self.dll_dir = os.add_dll_directory(str(library.parent)) if os.name == "nt" else None
        self.api = C.CDLL(str(library))
        signatures = {
            "mpv_create": (C.c_void_p, []),
            "mpv_set_option_string": (C.c_int, [C.c_void_p, C.c_char_p, C.c_char_p]),
            "mpv_initialize": (C.c_int, [C.c_void_p]),
            "mpv_command": (C.c_int, [C.c_void_p, C.POINTER(C.c_char_p)]),
            "mpv_get_property_string": (C.c_void_p, [C.c_void_p, C.c_char_p]),
            "mpv_free": (None, [C.c_void_p]),
            "mpv_wait_event": (C.POINTER(Event), [C.c_void_p, C.c_double]),
            "mpv_terminate_destroy": (None, [C.c_void_p]),
            "mpv_error_string": (C.c_char_p, [C.c_int]),
        }
        for name, (result, args) in signatures.items():
            fn = getattr(self.api, name)
            fn.restype, fn.argtypes = result, args
        self.handle = None

    def check(self, result):
        if result < 0:
            raise RuntimeError(self.api.mpv_error_string(result).decode())

    def open(self, options):
        self.handle = self.api.mpv_create()
        if not self.handle:
            raise RuntimeError("mpv_create failed")
        defaults = {"config": "no", "terminal": "no", "vid": "no", "audio-display": "no", "idle": "yes"}
        for name, value in (defaults | options).items():
            self.check(self.api.mpv_set_option_string(self.handle, name.encode(), value.encode()))
        self.check(self.api.mpv_initialize(self.handle))

    def get(self, name):
        ptr = self.api.mpv_get_property_string(self.handle, name.encode())
        if not ptr:
            return ""
        try:
            return C.string_at(ptr).decode()
        finally:
            self.api.mpv_free(ptr)

    def load(self, path):
        args = (C.c_char_p * 4)(b"loadfile", str(path).encode(), b"replace", None)
        self.check(self.api.mpv_command(self.handle, args))

    def wait_end(self):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            event = self.api.mpv_wait_event(self.handle, 0.1).contents
            if event.id == 7:
                # mpv_event_end_file starts with reason and error (two ints).
                reason, error = C.cast(event.data, C.POINTER(C.c_int * 2)).contents
                self.check(error)
                if reason != 0:
                    raise RuntimeError(f"unexpected end-file reason: {reason}")
                return
        raise TimeoutError("native playback did not finish")

    def close(self):
        if self.handle:
            self.api.mpv_terminate_destroy(self.handle)
            self.handle = None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--lock", type=Path, default=ROOT / "native-lock.json")
    args = parser.parse_args()
    bundle = args.bundle.resolve()
    lock = read_lock(args.lock)
    library = bundle / LIBRARY_NAMES[host_platform().split("/")[0]]
    with tempfile.TemporaryDirectory() as tmp:
        source, pcm = Path(tmp) / "tone.wav", Path(tmp) / "sample.pcm"
        with wave.open(str(source), "wb") as out:
            out.setparams((2, 2, 44100, 0, "NONE", "not compressed"))
            out.writeframes(b"".join(struct.pack("<hh", int(12000 * math.sin(2 * math.pi * 997 * n / 44100)),
                                               int(12000 * math.sin(2 * math.pi * 1601 * n / 44100))) for n in range(88200)))
        client = Client(library)
        try:
            client.open({"ao": "null", "af": spectrum_filter()})
            version = client.get("mpv-version")
            if not re.search(r"(?:^|\s)v?" + re.escape(lock["mpv"]["version"]) + r"(?:$|[-\s])", version):
                raise RuntimeError(f"wrong runtime mpv version: {version}")
            client.load(source)
            client.wait_end()
        finally:
            client.close()
        try:
            client.open({"ao": "pcm", "ao-pcm-file": str(pcm), "ao-pcm-waveheader": "no",
                         "audio-format": "s16", "audio-samplerate": "16000", "audio-channels": "mono"})
            client.load(source)
            client.wait_end()
        finally:
            client.close()
        data = pcm.read_bytes()
        if len(data) < 60000 or not any(data):
            raise RuntimeError(f"invalid recognition PCM output: {len(data)} bytes")
    write_json(bundle / "smoke-report.json", {"mpv_version": version, "wav_decode": True,
               "spectrum_filter": True, "pcm_sampling": True})
    print("Native smoke tests passed:", version)


if __name__ == "__main__":
    main()
