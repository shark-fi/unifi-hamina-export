#!/usr/bin/env python3
"""pathloss_calibrate.py — fit a building's 2.4 GHz path loss from Protect sensors.

UniFi Protect sensors report the BLE signal strength of their link to a
SuperLink gateway, and both ends are already placed on an InnerSpace floor plan.
That is a permanently installed set of (distance, RSSI) measurements: several
transmitters at fixed known positions, in the building you care about, costing
nothing to collect.

    python3 pathloss_calibrate.py --host https://192.168.60.223 -u marko

The fit gives RSSI(d) = A - 10*n*log10(d).

WHAT TRANSFERS, AND WHAT DOES NOT. The exponent n describes the building --
walls, clutter, how fast signal decays -- and applies to any 2.4 GHz link in it,
so it is worth putting in SENSOR_PATHLOSS_EXPONENT for the Wi-Fi locate feature.
The intercept A does NOT: it is BLE's reference level at 10 dBm, and a Wi-Fi AP
transmits around 20. Take n from here and A from rssi_sensor.py --calibrate.

Read-only. Standard library only, Python 3.9+.
"""
import argparse
import getpass
import json
import math
import sys

from unifi_export import Http, legacy_login

INNERSPACE_API = "/proxy/innerspace/api"
PROTECT_API = "/proxy/protect/api"


def norm_mac(mac):
    return (mac or "").replace(":", "").replace("-", "").upper()


# --------------------------------------------------------------------------- #
# The fit
# --------------------------------------------------------------------------- #

def fit_pathloss(points):
    """Least-squares fit of RSSI = A - 10*n*log10(d) over (distance_m, rssi).

    Returns (rssi_at_1m, exponent, rms_residual_db, log_span). Linear in
    log10(d), so this is an ordinary regression -- no solver needed.

    `log_span` is the spread of log10(distance) across the inputs and is the
    number to check before believing the exponent. Distance is the only thing
    constraining the slope: measurements all taken at roughly the same range
    fit a line through a cluster, which reports a confident exponent that the
    data never actually tested.
    """
    pts = [(d, r) for d, r in points if d and d > 0]
    if len(pts) < 2:
        raise ValueError("need at least 2 measurements at different distances")
    xs = [math.log10(d) for d, _ in pts]
    ys = [r for _, r in pts]
    n = len(pts)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        raise ValueError("every measurement is at the same distance; the "
                         "exponent is unconstrained")
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    intercept = my - slope * mx
    rms = math.sqrt(sum((y - (intercept + slope * x)) ** 2
                        for x, y in zip(xs, ys)) / n)
    return intercept, -slope / 10.0, rms, max(xs) - min(xs)


# --------------------------------------------------------------------------- #
# Console data
# --------------------------------------------------------------------------- #

def device_positions(http_, base):
    """mac -> (plan_id, x_scene, y_scene), plus plan_id -> metres per scene unit.

    Metres come straight from the scale line: a coordinate difference in scene
    units times metres-per-unit IS the distance in metres. Going via pixels
    would add the image fetch, the centring offset and the plan's own scale
    factor, all of which cancel for a difference.
    """
    data = http_.get_json("%s%s/project?mode=2D" % (base, INNERSPACE_API)).get("data", {})
    by_plan = {}
    for s in data.get("shapes") or []:
        if s.get("planId"):
            by_plan.setdefault(s["planId"], []).append(s)

    mpu = {}
    for pid, group in by_plan.items():
        scale = next((s for s in group if s.get("type") == "scale"), None)
        if not scale:
            continue
        p = scale.get("position") or []
        if len(p) >= 2 and scale.get("scale"):
            dist = math.hypot(p[1]["x"] - p[0]["x"], p[1]["y"] - p[0]["y"])
            if dist:
                mpu[pid] = float(scale["scale"]) / dist

    pos = {}
    for s in data.get("shapes") or []:
        if s.get("type") != "device":
            continue
        mac = norm_mac((s.get("meta") or {}).get("mac"))
        pt = (s.get("position") or [{}])[0]
        if mac and pt.get("x") is not None:
            pos[mac] = (s.get("planId"), float(pt["x"]), float(pt["y"]),
                        s.get("title") or "")
    plans = {p["id"]: (p.get("title") or p["id"]) for p in data.get("plans") or []}
    return pos, mpu, plans


def protect_links(http_, base):
    """[(sensor_name, sensor_mac, gateway_mac, rssi)] for every BLE-linked sensor."""
    boot = http_.get_json("%s%s/bootstrap" % (base, PROTECT_API))
    gateways = {}
    for key in ("linkstations", "bridges"):
        for g in boot.get(key) or []:
            if g.get("id"):
                gateways[g["id"]] = (norm_mac(g.get("mac")), g.get("name") or key)
    out = []
    for s in boot.get("sensors") or []:
        wcs = s.get("wirelessConnectionState") or {}
        rssi = ((wcs.get("signalState") or {}).get("signalStrength")
                if wcs.get("signalState") else None)
        if rssi is None:
            bcs = s.get("bluetoothConnectionState") or {}
            rssi = bcs.get("signalStrength")
        gw = gateways.get(wcs.get("bridge"))
        if rssi is None or not gw:
            continue
        out.append((s.get("name") or s.get("mac"), norm_mac(s.get("mac")),
                    gw[0], gw[1], float(rssi)))
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", required=True, help="console URL")
    p.add_argument("-u", "--username", required=True, help="local admin user")
    p.add_argument("-p", "--password", help="password (prompted if omitted)")
    p.add_argument("--verify-tls", action="store_true", default=False)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    a = p.parse_args(argv)

    base = a.host.rstrip("/")
    http_ = Http(verify=a.verify_tls)
    legacy_login(http_, base, a.username,
                 a.password or getpass.getpass("password for %s: " % a.username))

    pos, mpu, plans = device_positions(http_, base)
    links = protect_links(http_, base)
    if not links:
        print("No BLE-linked sensors reporting signal strength. Nothing to fit.",
              file=sys.stderr)
        return 1

    rows, skipped = [], []
    for name, smac, gmac, gname, rssi in links:
        sp, gp = pos.get(smac), pos.get(gmac)
        if not sp:
            skipped.append((name, "sensor is not placed on a floor plan")); continue
        if not gp:
            skipped.append((name, "its gateway (%s) is not placed" % gname)); continue
        if sp[0] != gp[0]:
            # Ignoring floors would report a horizontal distance for a link
            # that goes through a ceiling, which biases the exponent low.
            skipped.append((name, "sensor and gateway are on different plans "
                                  "(%s vs %s)" % (plans.get(sp[0], "?"),
                                                  plans.get(gp[0], "?")))); continue
        m = mpu.get(sp[0])
        if not m:
            skipped.append((name, "plan '%s' has no scale set"
                            % plans.get(sp[0], "?"))); continue
        d = math.hypot(sp[1] - gp[1], sp[2] - gp[2]) * m
        rows.append({"sensor": name, "gateway": gname, "plan": plans.get(sp[0], "?"),
                     "distance_m": round(d, 2), "rssi": rssi})

    for name, why in skipped:
        print("skipped %-28s %s" % (name, why), file=sys.stderr)
    if len(rows) < 2:
        print("Only %d usable measurement(s); need at least 2 at different "
              "distances." % len(rows), file=sys.stderr)
        return 1

    a1m, exponent, rms, span = fit_pathloss([(r["distance_m"], r["rssi"])
                                             for r in rows])
    for r in rows:
        pred = a1m - 10.0 * exponent * math.log10(max(r["distance_m"], 1e-9))
        r["residual_db"] = round(r["rssi"] - pred, 1)

    if a.json:
        print(json.dumps({"measurements": rows, "rssi_at_1m": round(a1m, 1),
                          "exponent": round(exponent, 2), "rms_db": round(rms, 1),
                          "log10_distance_span": round(span, 2)}, indent=2))
        return 0

    print("\n%-28s %-22s %9s %7s %9s" % ("SENSOR", "GATEWAY", "DIST (m)",
                                         "RSSI", "RESIDUAL"))
    print("-" * 80)
    for r in rows:
        print("%-28s %-22s %9.2f %7.0f %+9.1f"
              % (r["sensor"][:28], r["gateway"][:22], r["distance_m"],
                 r["rssi"], r["residual_db"]))
    print("\nfitted:  rssi_at_1m = %.1f dBm   exponent = %.2f   rms = %.1f dB"
          % (a1m, exponent, rms))

    # The exponent is only as trustworthy as the range of distances behind it.
    if span < 0.4:                      # < ~2.5x between nearest and furthest
        print("\nWARNING: every measurement is at a similar distance "
              "(log10 span %.2f). The exponent is barely constrained by this "
              "data — treat it as a guess, not a calibration." % span)
    elif rms > 6.0:
        print("\nWARNING: %.1f dB rms is a poor fit. Check that the placements "
              "are accurate and that no sensor is behind something the others "
              "are not." % rms)
    else:
        print("\nSENSOR_PATHLOSS_EXPONENT=%.2f" % exponent)

    print("\nThe EXPONENT describes this building and applies to any 2.4 GHz\n"
          "link in it. The INTERCEPT does not transfer: it is BLE's reference\n"
          "at 10 dBm, and an AP transmits around 20. Get SENSOR_RSSI_AT_1M from\n"
          "rssi_sensor.py --calibrate with the actual radios.\n"
          "Distances are horizontal only — mounting heights are not modelled.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:
        print("error:", e, file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
