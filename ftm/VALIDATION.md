# FTM ranging — validation checklist

Goal: prove the Pi can act as an 802.11mc/az FTM **initiator** on its own radio
and feed distances to a client. Work top to bottom; **stop at the first hard
NO** — later steps can't succeed if an earlier one fails. Phase 0 is the
go/no-go gate for the WLAN Pi Go's Intel BE200.

Legend:  ✅ pass · ⚠️ works-but-degraded · ❌ blocker

---

## Phase 0 — Does the radio even expose FTM? (10 minutes, decides everything)

- [ ] **Shell + root on the device.** You can `ssh` in (or open a console) and
      run commands as root. The Go is a more locked-down, app-driven appliance
      than the classic WLAN Pi — if you can't get a root shell or load
      modules, stop and use a classic WLAN Pi instead.
- [ ] **Identify the adapter and driver.**
      ```bash
      lspci -k | grep -iA3 network   # or: lsusb ; ls /sys/class/net
      ethtool -i wlan0 | grep -e driver -e version -e firmware
      ```
      Expect `driver: iwlwifi` and a BE200-class firmware. Record kernel
      (`uname -r`) and firmware versions — FTM support is version-sensitive.
- [ ] **Capability is advertised.** This is the gate:
      ```bash
      iw phy | grep -iE "FTM|peer measurement|ranging"
      ```
      - ✅ You see `FTM responder`, `peer measurement`, or ranging caps → proceed.
      - ❌ Nothing prints → the driver/firmware is **not exposing FTM**. No
        amount of app work fixes this. Options: newer/older kernel + iwlwifi
        combo, a firmware with FTM enabled, or swap to a known-good adapter
        (AX210 / AX200 / 8260) on a classic WLAN Pi. Re-run this step after
        each change.
- [ ] **`iw` supports the measurement subcommand.**
      ```bash
      iw dev wlan0 measurement --help 2>&1 | head
      ```
      - ❌ "unknown command" → rebuild `iw` (≥ 4.14) with measurement support.

> If Phase 0 ends in ❌ on the BE200, that's the answer to "can the Go do it" —
> not with this radio/driver today. Everything below assumes Phase 0 passed.

---

## Phase 1 — Stand up a responder to range against

FTM needs two ends. You need at least one known responder before any real
measurement means anything.

- [ ] **A responder exists.** One of:
      - An AP configured as an FTM responder (`ftm_responder=1` in `hostapd`,
        or the vendor's "Fine Timing Measurement / 802.11mc responder" toggle).
      - A second Linux box running `hostapd` with `ftm_responder=1`.
- [ ] **Record each responder's exact BSSID, channel and center frequency
      (MHz) and bandwidth.** These go straight into `responders.json`
      (`mac`, `cf`, `bw`). A wrong `cf` = guaranteed timeout.
- [ ] **Responder is on air.** `iw dev wlan0 scan | grep -iA20 <bssid>` shows
      the BSS on the expected channel.
- [ ] **(Recommended) Fixed positions.** Measure each responder's `x`/`y` in
      metres on a shared floor grid. Distances alone don't give a position —
      the client trilaterates from these.

---

## Phase 2 — First real measurement (raw `iw`, no tool yet)

Prove the radio ranges before involving this repo's code.

- [ ] **Interface up, not associated to something that blocks it.**
      ```bash
      ip link set wlan0 up
      ```
- [ ] **Write a one-line config** (`/tmp/ftm.cfg`), substituting your values:
      ```
      00:11:22:33:44:01 bw=40 cf=5180 retries=5 asap
      ```
- [ ] **Fire a request:**
      ```bash
      iw dev wlan0 measurement ftm_request /tmp/ftm.cfg
      ```
      - ✅ A peer block with `status`/`fail_reason: 0` and a distance/RTT.
      - ⚠️ Intermittent timeouts (~50% is common on Intel FTM) → still a pass;
        raise `retries`, keep initiator and responder in line of sight, retry.
      - ❌ Every attempt fails / no peer block → responder not really in
        responder mode, wrong `cf`, regulatory/channel mismatch (6 GHz and DFS
        are common culprits), or the driver advertises FTM but can't run it.
- [ ] **Copy the raw output into a note.** The exact field names
      (`rtt_avg` vs `rtt`, `distance_avg` mm vs `distance` cm) tell you whether
      the tool's parser regexes need adjusting for your `iw` build.

---

## Phase 3 — The tool in mock mode (no hardware needed; can start on day one)

Build and wire the client in parallel with hardware bring-up.

- [ ] `python3 ftm_initiator.py --responders responders.example.json --mock --once`
      prints a JSON sweep with `success: true` and moving `distance_m`.
- [ ] Server + endpoints:
      ```bash
      python3 ftm_initiator.py --responders responders.example.json --mock &
      curl -s localhost:8080/healthz          # -> ok
      curl -s localhost:8080/measurements     # -> JSON, one entry per responder
      curl -s -N localhost:8080/stream        # -> SSE "data: {...}" every interval
      ```
- [ ] **iOS app consumes the feed.** Point the app at
      `http://<pi-ip>:8080/stream` (or `/measurements` polling) and confirm it
      renders distances/positions from the mock data. The mock JSON is
      byte-identical in shape to live data, so a client that works here works
      live.

---

## Phase 4 — The tool in live mode

- [ ] Put your **real** responders in `responders.json` (BSSID, `cf`, `bw`,
      `x`, `y`).
- [ ] Single live sweep:
      ```bash
      python3 ftm_initiator.py --responders responders.json --iface wlan0 --once -v
      ```
      - ✅ Real `distance_m` per responder, `status: 0`.
      - ❌ `status` non-zero or `error: "no result from iw"` for everyone →
        drop back to Phase 2 raw `iw`; the tool only wraps it.
      - ⚠️ Distances present but the parser shows `null` distance while raw
        `iw` clearly printed one → your `iw` output uses field names the
        regexes miss. Adjust `_STATUS_RE` / `_RTT_RE` / `_DIST_RE` in
        `ftm_initiator.py` to match the strings from Phase 2, re-run.
- [ ] Run the loop and watch `/`:
      ```bash
      python3 ftm_initiator.py --responders responders.json --iface wlan0 --interval 1 -v
      curl -s localhost:8080/          # status page updates each sweep
      ```

---

## Phase 5 — Accuracy & stability sanity

- [ ] **Static ground truth.** Place the Pi at a tape-measured distance from
      one responder. Compare reported `distance_m` — expect metre-ish error and
      a fixed bias; note the bias (FTM commonly needs a per-pair calibration
      offset).
- [ ] **Success rate.** Over ~100 sweeps, `sweeps_with_errors` should be a
      minority. Persistent high failure = LoS/channel/responder issue, not the
      tool.
- [ ] **Multi-AP position.** With ≥3 responders and known `x`/`y`, feed
      distances to the client's trilateration and eyeball the computed point
      against where the Pi physically is.
- [ ] **Soak.** Let the loop run 30+ minutes; confirm no memory growth, no
      wedged interface, SSE clients stay connected.

---

## If Phase 0 or 2 blocks on the BE200

The architecture is sound; the radio is the risk. In priority order:

1. **Try a different kernel + iwlwifi + firmware combo.** FTM enablement is
   notoriously version-specific; "latest" is not always "works".
2. **Swap the initiator radio.** Intel **AX210 / AX200** have the longest
   Linux FTM track record; the older **8260** is documented working. The Go's
   BE200 is fixed, so this likely means moving to a **classic WLAN Pi** with a
   swappable adapter — same tool, same app, proven radio.
3. **Keep the client on the mock feed** until a radio passes Phase 2, so the
   iOS side ships regardless of hardware timing.
