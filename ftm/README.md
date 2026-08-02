# Pi-side FTM ranging

An 802.11mc/az **FTM initiator** loop for a Linux Wi-Fi device (e.g. a WLAN Pi).
The Pi ranges against responder APs on its own radio and serves the latest
distances over HTTP as JSON, for a front-end (e.g. an iOS app) to consume. The
phone is never in the radio path — iOS exposes no Wi-Fi FTM API, so all ranging
happens on the Pi and the app is just a client.

Standard library only, Python 3.7+.

## Files

| File | What |
|---|---|
| `ftm_initiator.py` | The ranging loop + local JSON/SSE server. `--mock` runs with no radio. |
| `responders.example.json` | Example responder layout (BSSID, center freq, bandwidth, x/y). |
| `VALIDATION.md` | Step-by-step checklist to prove FTM works on the hardware. Start here. |

## Quick start (no hardware)

```bash
# single synthetic sweep
python3 ftm_initiator.py --responders responders.example.json --mock --once

# serve a live-shaped mock feed for the app to develop against
python3 ftm_initiator.py --responders responders.example.json --mock
curl -s localhost:8080/measurements
curl -sN localhost:8080/stream        # Server-Sent Events, one sweep per interval
```

## Live mode

```bash
# copy the example, fill in your real responders' BSSID / cf / bw / x / y
cp responders.example.json responders.json

python3 ftm_initiator.py --responders responders.json --iface wlan0 --once -v   # first real sweep
python3 ftm_initiator.py --responders responders.json --iface wlan0 --interval 1 # the loop
```

Mock and live emit **identical JSON shapes**, so a client built against the mock
feed works unchanged against real measurements.

## Endpoints

| Method / path | Returns |
|---|---|
| `GET /` | plain-text status summary |
| `GET /measurements` | latest measurement per responder, as JSON |
| `GET /stream` | Server-Sent Events; one JSON sweep per interval |
| `GET /healthz` | `ok` |

Measurement record:

```json
{
  "mac": "00:11:22:33:44:01", "name": "AP-1",
  "status": 0, "success": true,
  "rtt_psec": 19616, "distance_m": 2.94, "rssi": -49,
  "x": 0.0, "y": 0.0, "error": null, "timestamp": 1785652813.15
}
```

## Notes

- `iw`'s FTM output field names vary by version. If live distances come back
  `null` while raw `iw` clearly prints them, adjust the `_STATUS_RE` /
  `_RTT_RE` / `_DIST_RE` regexes in `ftm_initiator.py` to match your build
  (`VALIDATION.md` Phase 2 captures the strings you need).
- This tool does **not** trilaterate — it emits distances plus each responder's
  `x`/`y`; computing a position is the client's job.
- Feasibility hinges on the radio: run `VALIDATION.md` Phase 0 first. The WLAN
  Pi Go's Intel BE200 is hardware-ready for FTM but Linux driver enablement is
  currently unreliable — see the checklist's fallback options.
