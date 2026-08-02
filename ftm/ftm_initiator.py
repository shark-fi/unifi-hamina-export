#!/usr/bin/env python3
"""ftm_initiator.py — Pi-side 802.11mc/az FTM ranging loop with a local JSON feed.

Runs an FTM (Fine Timing Measurement) *initiator* loop on a Linux Wi-Fi
interface — e.g. the Intel adapter in a WLAN Pi — ranging against a set of
responder access points, and serves the latest per-responder measurements over
HTTP so a front-end (e.g. an iOS app) can consume them. The phone never touches
Wi-Fi ranging: the Pi does the FTM, the app just reads distances.

Real ranging shells out to `iw dev <iface> measurement ftm_request <cfg>` and
parses the peer results. `--mock` skips the radio entirely and emits synthetic,
moving measurements so the client can be built and tested without working FTM
hardware.

Endpoints (all CORS-open for local app development):
    GET /             plain-text status summary
    GET /measurements latest measurement per responder, as JSON
    GET /stream       Server-Sent Events; one JSON sweep per interval
    GET /healthz      "ok"

Standard library only. Python 3.7+.
"""

import argparse
import json
import math
import os
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# --------------------------------------------------------------------------- #
# Responder configuration
# --------------------------------------------------------------------------- #

class Responder:
    """One FTM responder AP we range against."""

    def __init__(self, mac, cf, bw=20, retries=5, asap=True, name=None,
                 x=None, y=None):
        self.mac = mac.lower()
        self.cf = int(cf)            # center frequency in MHz (e.g. 5180)
        self.bw = int(bw)            # bandwidth in MHz (20/40/80/160)
        self.retries = int(retries)
        self.asap = bool(asap)
        self.name = name or mac
        self.x = x                   # optional known position, passed through
        self.y = y                   # so the client can trilaterate

    def iw_config_line(self):
        """A single line for the `iw ... ftm_request` config file."""
        line = "%s bw=%d cf=%d retries=%d" % (self.mac, self.bw, self.cf,
                                              self.retries)
        if self.asap:
            line += " asap"
        return line

    def meta(self):
        return {"mac": self.mac, "name": self.name, "cf": self.cf,
                "bw": self.bw, "x": self.x, "y": self.y}


def load_responders(path):
    with open(path) as fh:
        raw = json.load(fh)
    if not isinstance(raw, list) or not raw:
        raise ValueError("responders file must be a non-empty JSON array")
    out = []
    for i, r in enumerate(raw):
        try:
            out.append(Responder(**r))
        except TypeError as exc:
            raise ValueError("responder #%d is malformed: %s" % (i, exc))
    return out


# --------------------------------------------------------------------------- #
# Measurement result
# --------------------------------------------------------------------------- #

def make_result(responder, status, rtt_psec=None, distance_m=None, rssi=None,
                error=None):
    """Build one JSON-serialisable measurement record."""
    return {
        "mac": responder.mac,
        "name": responder.name,
        "status": status,           # 0 == success; anything else is a failure
        "success": status == 0,
        "rtt_psec": rtt_psec,
        "distance_m": distance_m,
        "rssi": rssi,
        "x": responder.x,
        "y": responder.y,
        "error": error,
        "timestamp": time.time(),
    }


# --------------------------------------------------------------------------- #
# Real FTM via `iw`
# --------------------------------------------------------------------------- #

_MAC_RE = re.compile(r"^\s*([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})\b")
# Tolerate both mainline iw (fail_reason / rtt_avg / distance_avg, mm) and the
# older patched builds (Status / rtt / distance, cm). Units are captured when
# present and normalised to metres; absent units are assumed to be millimetres.
_STATUS_RE = re.compile(r"(?:fail_reason|status)\s*[:=]\s*(-?\d+)", re.I)
_RTT_RE = re.compile(r"rtt(?:_avg)?\s*[:=]\s*(-?\d+)\s*(psec|ps)?", re.I)
_DIST_RE = re.compile(r"distance(?:_avg)?\s*[:=]\s*(-?\d+)\s*(mm|cm|m)?", re.I)
_RSSI_RE = re.compile(r"rssi\s*[:=]\s*(-?\d+)", re.I)


def _to_meters(value, unit):
    unit = (unit or "mm").lower()
    if unit == "cm":
        return value / 100.0
    if unit == "m":
        return float(value)
    return value / 1000.0  # mm (mainline iw default)


def parse_iw_ftm(output, responders):
    """Parse `iw ... ftm_request` output into per-responder results.

    iw's exact field names vary by version, so this is deliberately tolerant.
    If it ever stops matching, run with -v and adjust the regexes above against
    your build's real output — that's the single most likely thing to break.
    """
    by_mac = {r.mac: r for r in responders}
    results = {}

    # Split into per-peer blocks, each introduced by a bare MAC line.
    blocks = []
    current = None
    for line in output.splitlines():
        m = _MAC_RE.match(line)
        if m and (":" not in line.split(m.group(1), 1)[1][:2]):
            # New peer block header (a MAC not followed immediately by a value)
            current = {"mac": m.group(1).lower(), "lines": []}
            blocks.append(current)
        elif current is not None:
            current["lines"].append(line)

    for block in blocks:
        mac = block["mac"]
        responder = by_mac.get(mac)
        if responder is None:
            continue
        text = "\n".join(block["lines"])
        sm = _STATUS_RE.search(text)
        status = int(sm.group(1)) if sm else 1
        rtt = None
        dist = None
        rssi = None
        rm = _RTT_RE.search(text)
        if rm:
            rtt = int(rm.group(1))
        dm = _DIST_RE.search(text)
        if dm:
            dist = _to_meters(int(dm.group(1)), dm.group(2))
        rsm = _RSSI_RE.search(text)
        if rsm:
            rssi = int(rsm.group(1))
        results[mac] = make_result(responder, status, rtt, dist, rssi)

    # Any responder with no block at all counts as a failed sweep.
    for r in responders:
        results.setdefault(r.mac, make_result(r, status=255,
                                              error="no result from iw"))
    return results


def run_iw_ftm(iw_path, iface, responders, verbose=False):
    """Issue one FTM request for all responders and return parsed results."""
    cfg = "\n".join(r.iw_config_line() for r in responders) + "\n"
    tmp = tempfile.NamedTemporaryFile("w", suffix=".ftm", delete=False)
    try:
        tmp.write(cfg)
        tmp.close()
        cmd = [iw_path, "dev", iface, "measurement", "ftm_request", tmp.name]
        if verbose:
            print("[ftm] %s\n%s" % (" ".join(cmd), cfg.rstrip()),
                  file=sys.stderr)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        out = proc.stdout + "\n" + proc.stderr
        if verbose:
            print("[ftm] rc=%d\n%s" % (proc.returncode, out.rstrip()),
                  file=sys.stderr)
        if proc.returncode != 0 and not out.strip():
            raise RuntimeError("iw exited %d with no output" % proc.returncode)
        return parse_iw_ftm(out, responders)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Mock FTM (no radio required)
# --------------------------------------------------------------------------- #

class MockField:
    """Synthesises a tag drifting through the responder layout.

    Distances are the true geometric range to a slowly circling virtual tag,
    plus Gaussian noise, with an occasional dropped measurement — enough to
    exercise the client's happy path, jitter smoothing and failure handling.
    """

    def __init__(self, responders, seed=1, fail_rate=0.05, noise_m=0.4):
        self.responders = responders
        self.fail_rate = fail_rate
        self.noise_m = noise_m
        self.rng = random.Random(seed)
        xs = [r.x for r in responders if r.x is not None]
        ys = [r.y for r in responders if r.y is not None]
        self.cx = sum(xs) / len(xs) if xs else 0.0
        self.cy = sum(ys) / len(ys) if ys else 0.0
        span = 1.0
        if xs and ys:
            span = max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
        self.radius = span * 0.35
        self.t0 = time.time()

    def _tag_xy(self):
        theta = (time.time() - self.t0) * 0.2  # slow orbit, rad/s
        return (self.cx + self.radius * math.cos(theta),
                self.cy + self.radius * math.sin(theta))

    def sweep(self):
        tx, ty = self._tag_xy()
        results = {}
        for r in self.responders:
            if self.rng.random() < self.fail_rate:
                results[r.mac] = make_result(r, status=1,
                                             error="mock: dropped burst")
                continue
            if r.x is None or r.y is None:
                true_d = 3.0 + self.rng.random() * 5.0
            else:
                true_d = math.hypot(tx - r.x, ty - r.y)
            d = max(0.1, true_d + self.rng.gauss(0, self.noise_m))
            rtt = int(d / 3e8 * 2 * 1e12)  # metres -> round-trip picoseconds
            rssi = int(-40 - 20 * math.log10(max(d, 0.5)))
            results[r.mac] = make_result(r, 0, rtt_psec=rtt,
                                         distance_m=round(d, 3), rssi=rssi)
        return results


# --------------------------------------------------------------------------- #
# Shared state + ranging loop
# --------------------------------------------------------------------------- #

class State:
    def __init__(self):
        self._lock = threading.Lock()
        self._latest = {}     # mac -> latest result
        self._seq = 0
        self._sweeps = 0
        self._errors = 0

    def update(self, results):
        with self._lock:
            self._latest.update(results)
            self._seq += 1
            self._sweeps += 1
            if any(not r["success"] for r in results.values()):
                self._errors += 1

    def snapshot(self):
        with self._lock:
            return {
                "seq": self._seq,
                "sweeps": self._sweeps,
                "sweeps_with_errors": self._errors,
                "measurements": list(self._latest.values()),
            }

    def seq(self):
        with self._lock:
            return self._seq


def ranging_loop(state, responders, args, stop_event):
    mock = MockField(responders) if args.mock else None
    while not stop_event.is_set():
        start = time.time()
        try:
            if mock is not None:
                results = mock.sweep()
            else:
                results = run_iw_ftm(args.iw, args.iface, responders,
                                     args.verbose)
            state.update(results)
        except Exception as exc:  # keep the loop alive; report per-sweep
            failed = {r.mac: make_result(r, status=254, error=str(exc))
                      for r in responders}
            state.update(failed)
            if args.verbose:
                print("[ftm] sweep failed: %s" % exc, file=sys.stderr)
        elapsed = time.time() - start
        stop_event.wait(max(0.0, args.interval - elapsed))


# --------------------------------------------------------------------------- #
# HTTP server
# --------------------------------------------------------------------------- #

def make_handler(state, args):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            if args.verbose:
                super().log_message(*a)

        def _send_bytes(self, body, code=200, ctype="application/json"):
            # Always send Content-Length so HTTP/1.1 keep-alive connections
            # know when the body ends (otherwise clients hang).
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, obj, code=200):
            self._send_bytes(json.dumps(obj).encode("utf-8"), code)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/healthz":
                self._send_bytes(b"ok", ctype="text/plain")
            elif path == "/measurements":
                self._send_json(state.snapshot())
            elif path == "/stream":
                self._stream()
            elif path == "/":
                self._status_page()
            else:
                self._send_json({"error": "not found"}, code=404)

        def _status_page(self):
            snap = state.snapshot()
            lines = ["ftm_initiator  mode=%s  iface=%s" %
                     ("mock" if args.mock else "live", args.iface),
                     "sweeps=%d errors=%d" %
                     (snap["sweeps"], snap["sweeps_with_errors"]), ""]
            for m in snap["measurements"]:
                d = "%.2f m" % m["distance_m"] if m["distance_m"] is not None \
                    else "--"
                lines.append("%-16s %-18s %s  status=%s" %
                             (m["name"], m["mac"], d, m["status"]))
            body = ("\n".join(lines) + "\n").encode("utf-8")
            self._send_bytes(body, ctype="text/plain; charset=utf-8")

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            last = -1
            try:
                while True:
                    cur = state.seq()
                    if cur != last:
                        last = cur
                        payload = json.dumps(state.snapshot())
                        self.wfile.write(("data: %s\n\n" % payload)
                                         .encode("utf-8"))
                        self.wfile.flush()
                    time.sleep(max(0.05, args.interval / 2.0))
            except (BrokenPipeError, ConnectionResetError):
                return

    return Handler


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--responders", required=True,
                   help="JSON array of responder APs (see responders.example.json)")
    p.add_argument("--iface", default="wlan0",
                   help="Wi-Fi interface to range from (default: wlan0)")
    p.add_argument("--iw", default="iw", help="path to the iw binary")
    p.add_argument("--interval", type=float, default=1.0,
                   help="seconds between sweeps (default: 1.0)")
    p.add_argument("--port", type=int, default=8080,
                   help="HTTP port for the JSON feed (default: 8080)")
    p.add_argument("--bind", default="0.0.0.0",
                   help="address to bind the HTTP server (default: 0.0.0.0)")
    p.add_argument("--mock", action="store_true",
                   help="emit synthetic measurements; no radio required")
    p.add_argument("--once", action="store_true",
                   help="run a single sweep, print JSON to stdout, and exit")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    responders = load_responders(args.responders)

    if args.once:
        if args.mock:
            results = MockField(responders).sweep()
        else:
            results = run_iw_ftm(args.iw, args.iface, responders, args.verbose)
        print(json.dumps({"measurements": list(results.values())}, indent=2))
        return 0

    state = State()
    stop_event = threading.Event()
    loop = threading.Thread(target=ranging_loop,
                            args=(state, responders, args, stop_event),
                            daemon=True)
    loop.start()

    server = ThreadingHTTPServer((args.bind, args.port),
                                 make_handler(state, args))
    print("ftm_initiator: mode=%s iface=%s serving on http://%s:%d "
          "(GET /measurements, /stream)" %
          ("mock" if args.mock else "live", args.iface, args.bind, args.port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
