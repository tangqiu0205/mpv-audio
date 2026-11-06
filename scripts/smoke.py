#!/usr/bin/env python3
"""Test relocated libmpv using synthetic fixtures, without a sound device or private repositories."""
import argparse
from contextlib import contextmanager
import ctypes as C
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import math
import os
from pathlib import Path
import re
import ssl
import tempfile
from threading import Thread
import time

from common import ROOT, LIBRARY_NAMES, host_platform, read_lock, write_json
from fixtures import FORMATS, REQUIRED_FORMATS


class Event(C.Structure):
    _fields_ = [("id", C.c_int), ("error", C.c_int), ("userdata", C.c_uint64), ("data", C.c_void_p)]


class Node(C.Structure):
    pass


class NodeList(C.Structure):
    _fields_ = [("num", C.c_int), ("values", C.POINTER(Node)), ("keys", C.POINTER(C.c_char_p))]


class NodeValue(C.Union):
    _fields_ = [("string", C.c_char_p), ("list", C.POINTER(NodeList)),
                ("integer", C.c_int64), ("number", C.c_double), ("flag", C.c_int)]


Node._fields_ = [("value", NodeValue), ("format", C.c_int)]


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


def has_spectrum(metadata):
    keys = [f"lavfi.astats.{channel}.RMS_level" for channel in range(3, 27)]
    if not all(key in metadata for key in keys):
        return False
    levels = [float(metadata[key]) for key in keys]
    if any(math.isnan(level) or level == math.inf for level in levels):
        raise RuntimeError("invalid spectrum metadata")
    return any(math.isfinite(level) and level > -60 for level in levels)


class Client:
    def __init__(self, library):
        self.dll_dir = os.add_dll_directory(str(library.parent)) if os.name == "nt" else None
        self.api = C.CDLL(str(library))
        signatures = {
            "mpv_create": (C.c_void_p, []),
            "mpv_set_option_string": (C.c_int, [C.c_void_p, C.c_char_p, C.c_char_p]),
            "mpv_set_property_string": (C.c_int, [C.c_void_p, C.c_char_p, C.c_char_p]),
            "mpv_initialize": (C.c_int, [C.c_void_p]),
            "mpv_command": (C.c_int, [C.c_void_p, C.POINTER(C.c_char_p)]),
            "mpv_get_property_string": (C.c_void_p, [C.c_void_p, C.c_char_p]),
            "mpv_get_property": (C.c_int, [C.c_void_p, C.c_char_p, C.c_int, C.POINTER(Node)]),
            "mpv_free_node_contents": (None, [C.POINTER(Node)]),
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

    def set(self, name, value):
        self.check(self.api.mpv_set_property_string(self.handle, name.encode(), value.encode()))

    def get(self, name):
        ptr = self.api.mpv_get_property_string(self.handle, name.encode())
        if not ptr:
            return ""
        try:
            return C.string_at(ptr).decode()
        finally:
            self.api.mpv_free(ptr)

    def metadata(self):
        node = Node()
        result = self.api.mpv_get_property(self.handle, b"af-metadata/spectrum", 6, C.byref(node))
        if result in (-10, -11):  # Filter chain may not have produced a frame yet.
            return {}
        self.check(result)
        try:
            if node.format != 8 or not node.value.list:
                raise RuntimeError("spectrum property is not a node map")
            values = node.value.list.contents
            return {values.keys[n].decode(): values.values[n].value.string.decode()
                    for n in range(values.num) if values.values[n].format == 1}
        finally:
            self.api.mpv_free_node_contents(C.byref(node))

    def load(self, path):
        args = (C.c_char_p * 4)(b"loadfile", str(path).encode(), b"replace", None)
        self.check(self.api.mpv_command(self.handle, args))

    def wait_end(self, require_spectrum=False):
        deadline = time.monotonic() + 20
        observed = False
        while time.monotonic() < deadline:
            event = self.api.mpv_wait_event(self.handle, 0.05).contents
            if require_spectrum and not observed:
                observed = has_spectrum(self.metadata())
            if event.id == 7:
                # mpv_event_end_file starts with reason and error (two ints).
                reason, error = C.cast(event.data, C.POINTER(C.c_int * 2)).contents
                self.check(error)
                if reason != 0:
                    raise RuntimeError(f"unexpected end-file reason: {reason}")
                if require_spectrum and not observed:
                    raise RuntimeError("decoded audio did not expose 24-band spectrum metadata")
                return
        raise TimeoutError("native playback did not finish")

    def close(self):
        if self.handle:
            self.api.mpv_terminate_destroy(self.handle)
            self.handle = None


@contextmanager
def local_stream(directory, tls=False):
    handler = partial(SimpleHTTPRequestHandler, directory=str(directory))
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        if tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(directory / "test-cert.pem", directory / "test-key.pem")
            server.socket = context.wrap_socket(server.socket, server_side=True)
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield f"{'https' if tls else 'http'}://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            worker.join()


def validate_pcm(data):
    if len(data) < 60000 or len(data) % 2 or not any(data):
        raise RuntimeError(f"invalid PCM output: {len(data)} bytes")


def test_bundle(library, fixtures, expected_version):
    client = Client(library)
    observed_formats = []
    version = ""
    for name, source in [("wav", fixtures / "tone.wav"), *[(name, fixtures / spec[0]) for name, spec in FORMATS.items()]]:
        print("Testing decode and spectrum:", name, flush=True)
        try:
            client.open({"ao": "null", "af": spectrum_filter()})
            version = client.get("mpv-version")
            if not re.search(r"(?:^|\s)v?" + re.escape(expected_version) + r"(?:$|[-\s])", version):
                raise RuntimeError(f"wrong runtime mpv version: {version}, expected {expected_version}")
            client.set("volume", "25")
            if float(client.get("volume")) != 25:
                raise RuntimeError("volume was not applied")
            client.set("pause", "yes")
            if client.get("pause") != "yes":
                raise RuntimeError("pause was not applied")
            client.set("pause", "no")
            client.load(source)
            client.wait_end(require_spectrum=True)
            observed_formats.append(name)
        finally:
            client.close()
    with local_stream(fixtures) as source:
        try:
            client.open({"ao": "null", "af": spectrum_filter()})
            client.load(source + "/live.m3u8")
            client.wait_end(require_spectrum=True)
        finally:
            client.close()
    with local_stream(fixtures, tls=True) as source:
        try:
            # Test encrypted transport against a local self-signed endpoint;
            # public CA/hostname verification is deliberately a separate concern.
            client.open({"ao": "null", "af": spectrum_filter(), "tls-verify": "no"})
            client.load(source + "/tone.flac")
            client.wait_end(require_spectrum=True)
        finally:
            client.close()
    with tempfile.TemporaryDirectory() as tmp:
        pcm = Path(tmp) / "sample.pcm"
        try:
            client.open({"ao": "pcm", "ao-pcm-file": str(pcm), "ao-pcm-waveheader": "no",
                         "audio-format": "s16", "audio-samplerate": "16000", "audio-channels": "mono"})
            client.load(fixtures / "tone.wav")
            client.wait_end()
        finally:
            client.close()
        validate_pcm(pcm.read_bytes())
    return {"schema": 2, "mpv_version": version, "expected_mpv_version": expected_version,
            "formats": observed_formats, "wav_decode": True, "spectrum_filter": True,
            "spectrum_metadata": True, "pcm_sampling": True, "hls_http": True, "https_local": True, "pause_volume": True}


def validate_report(report, expected_version):
    checks = ("wav_decode", "spectrum_filter", "spectrum_metadata", "pcm_sampling", "hls_http", "https_local", "pause_volume")
    if (report.get("schema") != 2 or report.get("expected_mpv_version") != expected_version
            or not all(report.get(key) is True for key in checks)
            or set(report.get("formats", [])) != set(REQUIRED_FORMATS)):
        raise RuntimeError("incomplete native regression report")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--lock", type=Path, default=ROOT / "native-lock.json")
    args = parser.parse_args()
    bundle = args.bundle.resolve()
    lock = read_lock(args.lock)
    library = bundle / LIBRARY_NAMES[host_platform().split("/")[0]]
    report = test_bundle(library, args.fixtures.resolve(), lock["mpv"]["version"])
    validate_report(report, lock["mpv"]["version"])
    write_json(bundle / "smoke-report.json", report)
    print("Native regressions passed:", report["mpv_version"])


if __name__ == "__main__":
    main()
