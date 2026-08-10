#!/usr/bin/env python3
"""
unifi_export.py — Export UniFi AP inventory (and floor-plan positions) to CSV
for semi-manual placement into a Hamina Network Planner project.

Three data sources, three subcommands:

  cloud    Site Manager API (api.ui.com, cloud API key from unifi.ui.com/api).
           Works from anywhere, even consoles behind CGNAT. Inventory only —
           no map positions, no radio detail.

  local    Official UniFi Network Integration API on the console
           (Network app 9.0+, API key from Settings -> Control Plane ->
           Integrations). Inventory per site. No map positions.

  legacy   Internal controller API (username/password). Richest output:
           AP x/y placement on UniFi floor-plan maps, map scale (upp),
           per-radio channel / tx power / client counts. Unofficial API —
           fields can shift between Network app versions.

           legacy mode can additionally emit an OpenIntent 2.0 zip
           (--openintent out.zip) that Hamina Network Planner imports
           directly: floor plans with scale + placed APs with model,
           channel, width, and TX power. No manual AP placement needed.

Examples:

  python3 unifi_export.py cloud  --api-key $UI_KEY -o aps.csv
  python3 unifi_export.py local  --host https://192.168.1.1 --api-key $KEY
  python3 unifi_export.py legacy --host https://192.168.1.1 -u admin
  python3 unifi_export.py legacy --host https://unifi.local -u admin \
          --site default --download-maps ./maps -o aps.csv
  python3 unifi_export.py legacy --host https://192.168.1.1 -u admin \
          --openintent hq.zip     # then drag hq.zip into Hamina

Stdlib only. Self-signed controller certs are accepted for local/legacy.
"""

import argparse
import csv
import datetime
import getpass
import os
import http.cookiejar
import json
import re
import ssl
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

CSV_COLUMNS = [
    "source", "site", "name", "model", "mac", "ip", "state", "type",
    "map_name", "x_px", "y_px", "map_upp", "x_m", "y_m",
    "ch_2g", "bw_2g", "txpw_2g", "sta_2g", "rstate_2g",
    "ch_5g", "bw_5g", "txpw_5g", "sta_5g", "rstate_5g",
    "ch_6g", "bw_6g", "txpw_6g", "sta_6g", "rstate_6g",
]


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


class Http:
    """Tiny urllib wrapper with cookie jar and optional TLS-verify skip."""

    def __init__(self, verify=True):
        self.jar = http.cookiejar.CookieJar()
        handlers = [urllib.request.HTTPCookieProcessor(self.jar)]
        if not verify:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self.opener = urllib.request.build_opener(*handlers)
        self.extra_headers = {}

    def request(self, method, url, body=None, headers=None, raw=False):
        h = {"Accept": "application/json", **self.extra_headers, **(headers or {})}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with self.opener.open(req, timeout=30) as resp:
                payload = resp.read()
                if raw:
                    return resp.status, dict(resp.headers), payload
                return resp.status, dict(resp.headers), json.loads(payload or b"null")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = " ".join(e.read().decode(errors="replace").split())
                if detail.startswith("<"):  # HTML error page — not useful
                    detail = ""
                detail = detail[:300]
            except Exception:
                pass
            raise RuntimeError(f"{method} {url} -> HTTP {e.code} {detail}".rstrip()) from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"{method} {url} -> {e.reason}") from None

    def get_json(self, url, headers=None):
        _, _, body = self.request("GET", url, headers=headers)
        return body


def blank_row():
    return {c: "" for c in CSV_COLUMNS}


# --------------------------------------------------------------------------
# cloud: Site Manager API (api.ui.com)
# --------------------------------------------------------------------------

def run_cloud(args):
    http_ = Http(verify=True)
    headers = {"X-API-KEY": args.api_key}
    base = os.environ.get("UNIFI_SM_BASE", "https://api.ui.com")

    def paged(path):
        """Yield items from a v1 endpoint, following nextToken if present."""
        token = None
        while True:
            url = f"{base}{path}"
            if token:
                sep = "&" if "?" in url else "?"
                url = f"{url}{sep}nextToken={urllib.parse.quote(token)}"
            body = http_.get_json(url, headers=headers)
            data = body.get("data") if isinstance(body, dict) else body
            for item in data or []:
                yield item
            token = (body or {}).get("nextToken") if isinstance(body, dict) else None
            if not token:
                break

    rows = []
    for host_entry in paged("/v1/devices"):
        host_name = (
            host_entry.get("hostName")
            or host_entry.get("hostId")
            or ""
        )
        for dev in host_entry.get("devices") or []:
            row = blank_row()
            row["source"] = "cloud"
            row["site"] = host_name
            row["name"] = dev.get("name") or ""
            row["model"] = dev.get("model") or dev.get("shortname") or ""
            row["mac"] = dev.get("mac") or ""
            row["ip"] = dev.get("ip") or ""
            row["state"] = dev.get("status") or ""
            row["type"] = dev.get("productLine") or ""
            rows.append(row)

    if not rows:
        print("warning: Site Manager API returned no devices "
              "(check the API key and that consoles are cloud-connected)",
              file=sys.stderr)
    return rows


# --------------------------------------------------------------------------
# local: official Network Integration API on the console
# --------------------------------------------------------------------------

def run_local(args):
    http_ = Http(verify=not args.insecure_skip_verify)
    headers = {"X-API-KEY": args.api_key}
    base = args.host.rstrip("/")
    prefix = f"{base}/proxy/network/integration/v1"

    def paged(path):
        offset, limit = 0, 200
        while True:
            sep = "&" if "?" in path else "?"
            body = http_.get_json(
                f"{prefix}{path}{sep}offset={offset}&limit={limit}",
                headers=headers,
            )
            data = body.get("data", []) if isinstance(body, dict) else (body or [])
            yield from data
            total = body.get("totalCount") if isinstance(body, dict) else None
            offset += len(data)
            if total is None or offset >= total or not data:
                break

    try:
        sites = list(paged("/sites"))
    except RuntimeError as e:
        die(f"Integration API not reachable ({e}).\n"
            "Requires Network app 9.0+ with an API key from "
            "Settings -> Control Plane -> Integrations.")

    rows = []
    for site in sites:
        site_id = site.get("id")
        site_name = site.get("name") or site_id
        for dev in paged(f"/sites/{site_id}/devices"):
            features = dev.get("features") or []
            is_ap = "accessPoint" in features if features else None
            if args.aps_only and is_ap is False:
                continue
            row = blank_row()
            row["source"] = "local"
            row["site"] = site_name
            row["name"] = dev.get("name") or ""
            row["model"] = dev.get("model") or ""
            row["mac"] = dev.get("macAddress") or dev.get("mac") or ""
            row["ip"] = dev.get("ipAddress") or dev.get("ip") or ""
            row["state"] = dev.get("state") or ""
            row["type"] = ",".join(features) if features else ""
            rows.append(row)
    return rows


# --------------------------------------------------------------------------
# legacy: internal controller API (username/password) — positions + radios
# --------------------------------------------------------------------------

RADIO_BAND = {"ng": "2g", "na": "5g", "6e": "6g", "ad": "6g"}
STATE_NAMES = {0: "offline", 1: "online", 4: "upgrading", 5: "provisioning",
               6: "heartbeat_missed", 9: "adopting"}


def legacy_login(http_, base, username, password):
    """Log in; return the API path prefix ('' classic, '/proxy/network' UniFi OS)."""
    # UniFi OS consoles (UDM/UDR/CloudKey Gen2 on UniFi OS) first
    try:
        status, headers, _ = http_.request(
            "POST", f"{base}/api/auth/login",
            body={"username": username, "password": password})
        csrf = headers.get("X-CSRF-Token") or headers.get("x-csrf-token")
        if csrf:
            http_.extra_headers["X-CSRF-Token"] = csrf
        return "/proxy/network"
    except RuntimeError as e:
        if "HTTP 401" in str(e) or "HTTP 403" in str(e):
            die("login rejected (bad username/password?). Use a local admin "
                "account, not a ui.com cloud account with MFA.")
        first = str(e)
    # Classic software controller / CloudKey Gen1. If this fails too, report BOTH
    # attempts: reporting only this one is actively misleading, since a UniFi OS
    # console fails here for the mundane reason that it has no /api/login at all.
    try:
        http_.request("POST", f"{base}/api/login",
                      body={"username": username, "password": password})
    except RuntimeError as e:
        die("could not log in to %s.\n  UniFi OS  (/api/auth/login): %s\n"
            "  classic   (/api/login):      %s\n"
            "  -> check the host really is the UniFi console (GET /api/system "
            "identifies one), and use a local admin account."
            % (base, first, e))
    return ""


def run_legacy(args):
    # default: skip TLS verification (self-signed certs are the norm)
    http_ = Http(verify=args.verify_tls)
    base = args.host.rstrip("/")
    password = args.password or getpass.getpass(f"password for {args.username}: ")
    prefix = legacy_login(http_, base, args.username, password)
    api = f"{base}{prefix}/api"

    if args.site:
        site_names = [args.site]
    else:
        sites = http_.get_json(f"{api}/self/sites").get("data", [])
        site_names = [s["name"] for s in sites]
        if not site_names:
            die("no sites visible to this account")

    rows = []
    site_data = []
    for site in site_names:
        maps = legacy_maps(http_, api, site)
        devices = http_.get_json(f"{api}/s/{site}/stat/device").get("data", [])
        site_data.append((site, maps, devices))

        for dev in devices:
            if args.aps_only and dev.get("type") != "uap":
                continue
            row = blank_row()
            row["source"] = "legacy"
            row["site"] = site
            row["name"] = dev.get("name") or ""
            row["model"] = dev.get("model") or ""
            row["mac"] = dev.get("mac") or ""
            row["ip"] = dev.get("ip") or ""
            state = dev.get("state")
            row["state"] = STATE_NAMES.get(state, str(state))
            row["type"] = dev.get("type") or ""

            # floor-plan placement
            map_id = dev.get("map_id")
            x, y = dev.get("x"), dev.get("y")
            if map_id and map_id in maps and x is not None and y is not None:
                m = maps[map_id]
                row["map_name"] = m.get("name") or map_id
                row["x_px"] = round(float(x), 2)
                row["y_px"] = round(float(y), 2)
                upp = m.get("upp")  # units per pixel, set when map is scaled
                if upp:
                    row["map_upp"] = round(float(upp), 6)
                    row["x_m"] = round(float(x) * float(upp), 2)
                    row["y_m"] = round(float(y) * float(upp), 2)

            # per-radio live state (channel / tx power / clients)
            for stat in dev.get("radio_table_stats") or []:
                band = RADIO_BAND.get(stat.get("radio"))
                if not band:
                    continue
                row[f"ch_{band}"] = stat.get("channel", "")
                row[f"bw_{band}"] = stat.get("bw", "")
                row[f"txpw_{band}"] = stat.get("tx_power", "")
                row[f"sta_{band}"] = stat.get("num_sta", "")
                # RUN = on air; INIT/other = configured but not serving
                row[f"rstate_{band}"] = stat.get("state", "")
            rows.append(row)

        if args.download_maps:
            download_maps(http_, base, prefix, site, maps, args.download_maps)

    if getattr(args, "openintent", None):
        build_openintent(http_, base, prefix, site_data, args)
    return rows


def legacy_maps(http_, api, site):
    """Fetch floor-plan maps; the collection name varies by Network version,
    and newer versions dropped classic Maps entirely (moved to UniFi Design
    Center). Returns {} when unavailable so the export continues without
    positions."""
    last_err = None
    for coll in ("rest/map", "list/map", "stat/map"):
        try:
            data = http_.get_json(f"{api}/s/{site}/{coll}").get("data", [])
            return {m["_id"]: m for m in data}
        except RuntimeError as e:
            last_err = e
    print(f"warning: no maps API on this Network version for site '{site}' "
          f"({last_err}). Classic Maps was removed in newer releases (floor "
          "plans live in UniFi Design Center now) — exporting inventory and "
          "radio data without positions.", file=sys.stderr)
    return {}


def fetch_map_image(http_, base, prefix, site, map_id):
    """Best-effort floor-plan image fetch; URL layout varies by version.
    Returns (bytes, '.png'|'.jpg') or (None, None)."""
    candidates = [
        f"{base}{prefix}/dl/map/{map_id}.png",
        f"{base}{prefix}/dl/map/{map_id}.jpg",
        f"{base}{prefix}/dl/map/{site}/{map_id}.png",
        f"{base}{prefix}/dl/map/{site}/{map_id}.jpg",
    ]
    for url in candidates:
        try:
            status, headers, payload = http_.request("GET", url, raw=True)
        except RuntimeError:
            continue
        ctype = headers.get("Content-Type", "")
        if status == 200 and ctype.startswith("image/") and payload:
            return payload, (".png" if "png" in ctype else ".jpg")
    return None, None


def download_maps(http_, base, prefix, site, maps, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    for map_id, m in maps.items():
        name = (m.get("name") or map_id).replace("/", "_")
        payload, ext = fetch_map_image(http_, base, prefix, site, map_id)
        if payload:
            path = os.path.join(out_dir, f"{site}_{name}{ext}")
            with open(path, "wb") as f:
                f.write(payload)
            print(f"saved map image: {path}")
        else:
            print(f"note: could not fetch image for map '{name}' "
                  "(export it from the UniFi map view instead)", file=sys.stderr)


# --------------------------------------------------------------------------
# innerspace: floor plans, AP placements and walls from the InnerSpace app
# --------------------------------------------------------------------------
#
# Reverse-engineered from the InnerSpace UI bundle (cdn.pkg.svc.ui.com/
# innerspace-ui/<ver>/swai.js) — undocumented and version-dependent, but
# read-only. GET /proxy/innerspace/api/project?mode=2D returns the whole
# project:
#   plans[]   one per floor (id, title, type)
#   shapes[]  discriminated by .type, all carrying planId + position[]:
#     map     the floor-plan image (urlImage), its offset + scale
#     scale   two points + real-world length -> metres per scene unit,
#             plus .height = ceiling height in metres
#     device  productId, meta.mac, position[0] {x,y,z}, mount, rotation
#     wall    two points + variant (concrete/drywall/glass/metal/door_metal)
#   products[] productId -> sku ("U7-Pro-Max") and category ("wifi")
#
# Scene coordinates are image pixels: origin at the image centre, y axis up.
# Image pixels -> OpenIntent: x + W/2, and H/2 - y to flip to y-down.

INNERSPACE_API = "/proxy/innerspace/api"

# InnerSpace SKU -> a model name Hamina's AP library accepts. Hamina silently
# DROPS access points whose model it cannot resolve, so a bad name here loses
# APs with no error. Verified against a real import: uap-ac-lite, uap-ac-iw,
# u6-enterprise and u7-pro-max resolve as-is; "uap-ac-m" did not (all five
# UAP-AC-M APs vanished), hence the alias to the full product name.
INNERSPACE_SKU_ALIASES = {
    "uap-ac-m": "uap-ac-mesh",
    "uap-ac-m-pro": "uap-ac-mesh-pro",
}

# InnerSpace wall variant -> (OpenIntent wall_type label, attenuation dB)
# Attenuation values are InnerSpace's own defaults for these materials; Hamina
# maps unknown wall types onto its own library, so the label matters most.
# Verified against a real import: 25 InnerSpace "concrete" walls exported as
# wall_segments (pixel start/end via scene_to_pixels, no y-flip) rendered
# correctly in Hamina, aligned with the APs, and drove the RF coverage sim.
WALL_VARIANTS = {
    "concrete": ("Concrete", 12.0),
    "drywall": ("Drywall", 3.0),
    "glass": ("Glass", 6.0),
    "metal": ("Metal", 20.0),
    "door_metal": ("Metal Door", 15.0),
    "door_wood": ("Wood Door", 3.0),
    "brick": ("Brick", 10.0),
    "wood": ("Wood", 4.0),
}


def innerspace_project(http_, base):
    body = http_.get_json(f"{base}{INNERSPACE_API}/project?mode=2D")
    data = body.get("data") if isinstance(body, dict) else None
    if not data or "shapes" not in data:
        raise RuntimeError("unexpected InnerSpace project payload "
                           f"(keys: {list(body or {})})")
    return data


def scene_to_pixels(pt, map_shape, img_w, img_h):
    """InnerSpace scene units -> OpenIntent pixel coordinates.

    Both systems have y pointing up — InnerSpace measures from the image
    centre, OpenIntent from the bottom-left corner — so this only re-centres.
    Do NOT flip y here: doing so mirrors every AP and wall about the middle of
    the floor plan (verified against a real import).
    """
    off = (map_shape.get("position") or [{}])[0]
    sc = map_shape.get("scale") or {}
    sx = float(sc.get("x") or 1) or 1
    sy = float(sc.get("y") or 1) or 1
    x = (float(pt["x"]) - float(off.get("x") or 0)) / sx + img_w / 2.0
    y = (float(pt["y"]) - float(off.get("y") or 0)) / sy + img_h / 2.0
    return x, y


def run_innerspace(args):
    http_ = Http(verify=args.verify_tls)
    base = args.host.rstrip("/")
    password = args.password or getpass.getpass(f"password for {args.username}: ")
    legacy_login(http_, base, args.username, password)

    if args.project_json:
        with open(args.project_json) as f:
            body = json.load(f)
        data = body.get("data", body)
    else:
        data = innerspace_project(http_, base)

    shapes = data.get("shapes") or []
    plans = {p["id"]: p for p in data.get("plans") or []}
    products = {p["id"]: p for p in data.get("products") or []}
    unit_imperial = (data.get("project") or {}).get("unit") == "imperial"

    by_plan = {}
    for s in shapes:
        pid = s.get("planId")
        if pid:
            by_plan.setdefault(pid, []).append(s)

    # optional: live radio state from the Network app, joined on MAC
    radio_by_mac = {}
    if not args.no_radio:
        try:
            sites = http_.get_json(f"{base}/proxy/network/api/self/sites")
            for site in sites.get("data", []):
                devs = http_.get_json(
                    f"{base}/proxy/network/api/s/{site['name']}/stat/device")
                for dev in devs.get("data", []):
                    mac = (dev.get("mac") or "").replace(":", "").upper()
                    if mac:
                        radio_by_mac[mac] = dev
            print(f"joined radio state for {len(radio_by_mac)} device(s) "
                  "from the Network app")
        except RuntimeError as e:
            print(f"note: could not read Network radio state ({e}); "
                  "exporting placement only", file=sys.stderr)

    floorplans, aps_out, images, rows = [], [], {}, []
    for pid, group in by_plan.items():
        plan = plans.get(pid) or {}
        title = plan.get("title") or pid
        if args.plan and args.plan.lower() not in title.lower():
            continue
        map_shape = next((s for s in group if s.get("type") == "map"), None)
        scale_shape = next((s for s in group if s.get("type") == "scale"), None)
        if not map_shape or not map_shape.get("urlImage"):
            print(f"skipping plan '{title}': no floor-plan image",
                  file=sys.stderr)
            continue

        img, ext = None, ".png"
        try:
            _s, hdrs, img = http_.request(
                "GET", base + map_shape["urlImage"], raw=True)
            ext = ".jpg" if "jpeg" in hdrs.get("Content-Type", "") else ".png"
        except RuntimeError as e:
            print(f"skipping plan '{title}': image fetch failed ({e})",
                  file=sys.stderr)
            continue
        size = image_size(img)
        if not size:
            print(f"skipping plan '{title}': unreadable image", file=sys.stderr)
            continue
        img_w, img_h = size

        # metres per scene unit from the user's scale line
        mpu, ceiling = 0.0, args.ap_height
        if scale_shape:
            p = scale_shape.get("position") or []
            if len(p) >= 2 and scale_shape.get("scale"):
                dist = ((p[1]["x"] - p[0]["x"]) ** 2 +
                        (p[1]["y"] - p[0]["y"]) ** 2) ** 0.5
                if dist:
                    mpu = float(scale_shape["scale"]) / dist
            if scale_shape.get("height"):
                ceiling = float(scale_shape["height"])
        sx = float((map_shape.get("scale") or {}).get("x") or 1) or 1
        mpp = mpu * sx  # metres per image pixel
        if not mpp:
            print(f"warning: plan '{title}' has no scale set in InnerSpace — "
                  "exporting pixels only; set the scale in Hamina after import",
                  file=sys.stderr)

        dims = [{"width": float(img_w), "length": float(img_h),
                 "unit": "pixels"}]
        if mpp:
            dims[0]["height"] = ceiling / mpp
            dims.append({"width": img_w * mpp, "length": img_h * mpp,
                         "height": ceiling, "unit": "meters"})

        fp = {"name": title, "project_name": (data.get("project") or {})
              .get("title") or "InnerSpace", "floor_id": pid, "rotation": 0,
              "map_uri": f"file://images/{safe_name(title)}{ext}",
              "dimensions": dims, "reference_markers": [],
              "coverage_areas": []}
        images[f"images/{safe_name(title)}{ext}"] = img

        # walls
        segs = []
        for s in group:
            if s.get("type") != "wall":
                continue
            pts = s.get("position") or []
            if len(pts) < 2:
                continue
            label, _att = WALL_VARIANTS.get(
                s.get("variant"), (str(s.get("variant") or "Wall").title(), 0))
            x1, y1 = scene_to_pixels(pts[0], map_shape, img_w, img_h)
            x2, y2 = scene_to_pixels(pts[1], map_shape, img_w, img_h)
            segs.append({"wall_type": label,
                         "start_point": {"x": round(x1, 2), "y": round(y1, 2)},
                         "end_point": {"x": round(x2, 2), "y": round(y2, 2)}})
        if segs and not args.no_walls:
            fp["wall_segments"] = segs
        floorplans.append(fp)

        # access points
        n_out = 0
        for s in group:
            if s.get("type") != "device":
                continue
            prod = products.get(s.get("productId")) or {}
            if prod.get("category") != "wifi":
                continue
            pts = s.get("position") or []
            if not pts:
                continue
            x_px, y_px = scene_to_pixels(pts[0], map_shape, img_w, img_h)
            if not (-1 <= x_px <= img_w + 1 and -1 <= y_px <= img_h + 1):
                print(f"warning: AP '{s.get('title')}' maps outside the image "
                      f"({x_px:.0f},{y_px:.0f} vs {img_w}x{img_h}) — "
                      "coordinate assumption may be wrong for this plan",
                      file=sys.stderr)
            z_m = ceiling if s.get("mount") == "ceiling" else args.ap_height
            coords = [{"coordinate_xyz": {
                "x": round(x_px, 2), "y": round(y_px, 2), "unit": "pixels",
                **({"z": round(z_m / mpp, 2)} if mpp else {})}}]
            if mpp:
                coords.append({"coordinate_xyz": {
                    "x": round(x_px * mpp, 3), "y": round(y_px * mpp, 3),
                    "z": round(z_m, 3), "unit": "meters"}})

            mac = (s.get("meta") or {}).get("mac") or ""
            dev = radio_by_mac.get(mac.replace(":", "").upper(), {})
            sku = prod.get("sku") or ""
            # radio state comes from the Network app when joined; otherwise
            # the InnerSpace SKU drives the default band set
            ap = oi_ap({**dev, "name": s.get("title") or sku,
                        "model": dev.get("model") or sku}, fp["name"], coords,
                       getattr(args, "include_down_radios", False))
            model = INNERSPACE_SKU_ALIASES.get(sku.lower(), sku.lower())
            ap["model"] = model
            ap["model_original"] = sku
            ap["antennas"] = [{"vendor": "ubiquiti", "model": model,
                               "bands": ap["antennas"][0]["bands"]}]
            if mac:
                m = mac.replace(":", "").lower()
                ap["mac_address"] = ":".join(m[i:i + 2]
                                             for i in range(0, len(m), 2))
            aps_out.append(ap)
            n_out += 1

            row = blank_row()
            row.update(source="innerspace", site=title,
                       name=ap["name"], model=sku, mac=ap.get("mac_address", ""),
                       ip=(s.get("meta") or {}).get("ip", ""),
                       type="uap", map_name=title,
                       x_px=round(x_px, 2), y_px=round(y_px, 2),
                       map_upp=round(mpp, 6) if mpp else "",
                       x_m=round(x_px * mpp, 2) if mpp else "",
                       y_m=round(y_px * mpp, 2) if mpp else "")
            for r in ap["dot11_radios"]:
                b = {"FREQ_2.4GHZ": "2g", "FREQ_5GHZ": "5g",
                     "FREQ_6GHZ": "6g"}[r["band"]]
                row[f"ch_{b}"] = r.get("channel", "")
                row[f"txpw_{b}"] = r.get("transmit_power", "")
            rows.append(row)

        print(f"plan '{title}': {n_out} AP(s), {len(segs)} wall(s), "
              f"{img_w}x{img_h}px"
              + (f", {mpp:.4f} m/px, ceiling {ceiling:.2f} m" if mpp else
                 ", no scale"))

    if args.openintent:
        if not aps_out and not floorplans:
            print("openintent: nothing to export", file=sys.stderr)
        else:
            data_out = {"openintent_version": "2.0.1",
                        "accesspoints": aps_out, "floorplans": floorplans}
            with zipfile.ZipFile(args.openintent, "w",
                                 zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("openintent.json", json.dumps(data_out, indent=2))
                for rel, blob in images.items():
                    zf.writestr(rel, blob)
            walls = sum(len(f.get("wall_segments") or []) for f in floorplans)
            print(f"openintent: wrote {args.openintent} — {len(floorplans)} "
                  f"floor plan(s), {len(aps_out)} AP(s), {walls} wall(s), "
                  f"{len(images)} image(s)")
    if unit_imperial:
        print("note: this project is set to imperial in InnerSpace; "
              "OpenIntent carries metres (Hamina converts for display).",
              file=sys.stderr)
    return rows


# --------------------------------------------------------------------------
# probe: discover which floor-plan/position APIs this console actually has
# --------------------------------------------------------------------------

def probe_paths(site, fp_id=None):
    """(label, path) candidates, GET-only and read-only.

    InnerSpace lives inside the Network app (UI route /network/<site>/
    innerspace/<uuid>), so its API is served under /proxy/network — modern
    Network features use the v2 API base /proxy/network/v2/api/site/<site>/.
    """
    v1 = f"/proxy/network/api/s/{site}"
    v2 = f"/proxy/network/v2/api/site/{site}"
    out = [
        ("UniFi OS: system info", "/api/system"),
        ("Network: sysinfo", f"{v1}/stat/sysinfo"),
        ("Network: classic maps (rest)", f"{v1}/rest/map"),
        ("Network v2: innerspace", f"{v2}/innerspace"),
        ("Network v2: innerspace/floorplans", f"{v2}/innerspace/floorplans"),
        ("Network v2: innerspaces", f"{v2}/innerspaces"),
        ("Network v2: floorplans", f"{v2}/floorplans"),
        ("Network v2: floorplan", f"{v2}/floorplan"),
        ("Network v2: maps", f"{v2}/maps"),
        ("Network v2: spatial", f"{v2}/spatial"),
        ("Network v1: rest/innerspace", f"{v1}/rest/innerspace"),
        ("Network v1: rest/floorplan", f"{v1}/rest/floorplan"),
    ]
    if fp_id:
        out += [
            ("Network v2: innerspace/<id>", f"{v2}/innerspace/{fp_id}"),
            ("Network v2: innerspace/floorplans/<id>",
             f"{v2}/innerspace/floorplans/{fp_id}"),
            ("Network v2: floorplans/<id>", f"{v2}/floorplans/{fp_id}"),
            ("Network v2: floorplan/<id>", f"{v2}/floorplan/{fp_id}"),
            ("Network v2: innerspace/<id>/devices",
             f"{v2}/innerspace/{fp_id}/devices"),
            ("Network v1: rest/innerspace/<id>", f"{v1}/rest/innerspace/{fp_id}"),
        ]
    return out


API_HINT = re.compile(
    r"""["'`](/?(?:proxy/network/)?(?:v2/)?api/(?:s|site)/[^"'`\s]{0,120})["'`]"""
    r"""|["'`]([^"'`\s]{0,80}(?:innerspace|floorplan|floor-plan)[^"'`\s]{0,80})["'`]""",
    re.I)


def crawl_bundles(http_, base, max_files=80, verbose=True):
    """Fetch the Network SPA's JS, following lazy-loaded chunk references.
    InnerSpace is a dynamically-loaded app, so its API calls live in chunks
    that index.html never mentions — we chase the chunk filenames that the
    webpack runtime embeds in the main bundle. Returns {url: text}."""
    try:
        _s, _h, html = http_.request("GET", f"{base}/network/", raw=True)
    except RuntimeError as e:
        raise RuntimeError(f"could not load the Network SPA: {e}")
    html = html.decode(errors="replace")
    roots = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html)
    roots += re.findall(r'["\']([^"\']+\.js)["\']', html)

    def absolutize(ref, from_url):
        if ref.startswith("http"):
            return ref
        if ref.startswith("/"):
            return base + ref
        return from_url.rsplit("/", 1)[0] + "/" + ref

    fetched, queue, seen = {}, [absolutize(r, base + "/network/x")
                                for r in roots], set()
    while queue and len(fetched) < max_files:
        url = queue.pop(0)
        if url in seen or not url.endswith(".js"):
            continue
        seen.add(url)
        try:
            _s, _h, blob = http_.request("GET", url, raw=True)
        except RuntimeError:
            continue
        text = blob.decode(errors="replace")
        fetched[url] = text
        if verbose:
            print(f"  fetched {url.rsplit('/', 1)[-1]} ({len(text)//1024} KB)",
                  file=sys.stderr)
        # chunk filenames referenced from this bundle (webpack emits these
        # as bare strings in its chunk-loading runtime)
        for ref in set(re.findall(r'["\']([\w~./-]{4,80}\.js)["\']', text)):
            cand = absolutize(ref, url)
            if cand not in seen:
                queue.append(cand)
        # webpack runtime: chunkId -> hash maps, reconstruct "<name>.<hash>.js"
        for name, h in list(set(re.findall(
                r'["\']([\w~-]{2,40})["\']\s*:\s*["\']([a-f0-9]{8,32})["\']',
                text)))[:400]:
            for pat in (f"{name}.{h}.js", f"{name}~{h}.js"):
                cand = absolutize(pat, url)
                if cand not in seen:
                    queue.append(cand)
    return fetched


def discover_api(http_, base, site, keyword="innerspace", context=200):
    """Mine the SPA (including lazy chunks) for how it talks to InnerSpace."""
    print(f"crawling the Network app's JS bundles for '{keyword}' "
          "(read-only)...\n", file=sys.stderr)
    fetched = crawl_bundles(http_, base)
    print(f"\nfetched {len(fetched)} bundle(s)\n")

    paths, snippets = {}, []
    kw = keyword.lower()
    for url, text in fetched.items():
        chunk = url.rsplit("/", 1)[-1]
        for m in API_HINT.finditer(text):
            frag = (m.group(1) or m.group(2) or "").strip()
            if 4 < len(frag) < 140 and "/" in frag and (
                    "api" in frag.lower() or kw in frag.lower()
                    or "floorplan" in frag.lower()):
                paths.setdefault(frag, chunk)
        # code context around API-ish uses of the keyword
        for m in re.finditer(kw, text, re.I):
            s = max(0, m.start() - context // 2)
            frag = text[s:m.start() + context // 2]
            if re.search(r"api|fetch|url|/v\d|http", frag, re.I):
                snippets.append((chunk, frag.replace("\n", " ")))

    api_paths = {k: v for k, v in paths.items()
                 if kw in k.lower() or "floorplan" in k.lower()
                 or "/api/" in k or "/v2/" in k}
    print(f"--- candidate API paths ({len(api_paths)}) ---")
    for frag, chunk in sorted(api_paths.items())[:60]:
        print(f"  {frag}    [{chunk}]")
    if not api_paths:
        print("  (none)")

    print(f"\n--- code context around '{keyword}' ({min(len(snippets), 25)} "
          f"of {len(snippets)}) ---")
    seen_frags = set()
    shown = 0
    for chunk, frag in snippets:
        key = frag[:60]
        if key in seen_frags:
            continue
        seen_frags.add(key)
        print(f"\n[{chunk}]\n  ...{frag}...")
        shown += 1
        if shown >= 25:
            break
    print("\nPaste this back to me — the URL construction in these snippets "
          "is what I need to wire the real endpoint into the export.")


def run_probe(args):
    http_ = Http(verify=args.verify_tls)
    base = args.host.rstrip("/")
    password = args.password or getpass.getpass(f"password for {args.username}: ")
    legacy_login(http_, base, args.username, password)
    site = args.site or "default"

    if args.discover:
        discover_api(http_, base, site, args.grep, args.context)
        return []
    if args.get:
        url = base + args.get if args.get.startswith("/") else args.get
        status, headers, payload = http_.request("GET", url, raw=True)
        sys.stdout.write(payload.decode(errors="replace"))
        return []

    print(f"probing {base} (site '{site}') — GET only, nothing is modified\n")
    hits = []
    for label, path in probe_paths(site, args.floorplan_id):
        url = f"{base}{path}"
        try:
            status, headers, payload = http_.request("GET", url, raw=True)
        except RuntimeError as e:
            msg = str(e)
            m = re.search(r"HTTP (\d{3})", msg)
            code = m.group(1) if m else "err"
            detail = ""
            if not m:  # transport-level failure — show it, it's informative
                detail = f"  <- {msg.split('-> ', 1)[-1][:80]}"
            print(f"  [{code:>3}] {label:<34} {path}{detail}")
            continue
        ctype = headers.get("Content-Type", "")
        size = len(payload or b"")
        note = ""
        if "json" in ctype:
            try:
                body = json.loads(payload)
                data = body.get("data") if isinstance(body, dict) else body
                if isinstance(data, list):
                    note = f"{len(data)} item(s)"
                elif isinstance(body, dict):
                    note = f"keys: {', '.join(list(body)[:6])}"
            except Exception:
                pass
        print(f"  [200] {label:<34} {path}  ({size}B {ctype.split(';')[0]}"
              f"{'; ' + note if note else ''})")
        hits.append((label, path, payload))

    print("\nIf a path returned 200 with floorplan data, send me the label. "
          "If everything 404s, run again with --discover to mine the "
          "InnerSpace UI's own JavaScript for the real endpoint.")
    if args.dump:
        os.makedirs(args.dump, exist_ok=True)
        for label, path, payload in hits:
            fn = safe_name(path.strip("/").replace("/", "_")) + ".json"
            with open(os.path.join(args.dump, fn), "wb") as f:
                f.write(payload or b"")
        print(f"saved {len(hits)} response(s) to {args.dump}/ — "
              "check for anything sensitive before sharing")
    return []


# --------------------------------------------------------------------------
# confirm-write: prove InnerSpace accepts scripted position writes
# --------------------------------------------------------------------------
#
# Answers the two Phase-4 unknowns in docs/INNERSPACE_WRITE_API.md:
#   (1) can a scripted caller post /shape/change with a random socketId and
#       have the write persist, or is a live websocket session id required?
#   (2) writes return an empty body -> verify by re-GET /project?mode=2D.
#
# Method: create a clearly-labelled scratch AP on an existing plan at (0,0),
# re-GET to confirm it persisted, MOVE it to (3,4), re-GET to confirm the new
# position persisted (this is the actual position write), then DELETE it.
# Net change to the project when it completes: none. Dry-run by default.

def _iso_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")


def run_confirm_write(args):
    http_ = Http(verify=args.verify_tls)
    base = args.host.rstrip("/")
    password = args.password or getpass.getpass(f"password for {args.username}: ")
    legacy_login(http_, base, args.username, password)
    api = f"{base}{INNERSPACE_API}"

    data = innerspace_project(http_, base)
    project_id = (data.get("project") or {}).get("id")
    plans = data.get("plans") or []
    products = data.get("products") or []
    if not project_id:
        die("no project id in the InnerSpace payload")
    if not plans:
        die("InnerSpace has no floor plans — create one in the UI first; this "
            "probe needs an existing plan to place a scratch device on")

    if args.plan:
        plan = next((p for p in plans
                     if args.plan.lower() in (p.get("title") or "").lower()), None)
        if not plan:
            die(f"no plan whose title contains '{args.plan}'")
    else:
        plan = plans[0]
    plan_id = plan["id"]

    wifi = [p for p in products if p.get("category") == "wifi"]
    product_id = (wifi or products or [{}])[0].get("id")
    if not product_id:
        die("no catalog product to reference for a device shape "
            "(products[] is empty)")

    socket_id = str(uuid.uuid4())
    dev_id = str(uuid.uuid4())
    p_start = {"x": 0.0, "y": 0.0, "z": 0}
    p_moved = {"x": 3.0, "y": 4.0, "z": 0}
    quat = {"w": 1, "x": 0, "y": 0, "z": 0}

    def device_shape(pos):
        now = _iso_now()
        return {
            "id": dev_id, "planId": plan_id, "projectId": project_id,
            "type": "device", "title": "SURFSCAN-WRITE-PROBE (safe to delete)",
            "productId": product_id, "meta": {"mac": "025355524650"},
            "mount": "ceiling", "position": [pos],
            "rotation": {"base": dict(quat), "pov": dict(quat)},
            "antenna": None, "parentId": None, "childIndex": None,
            "rackId": None, "rackOrder": None, "optimize": False,
            "status": 1, "createdAt": now, "updatedAt": now,
        }

    def shape_change(create=None, update=None, remove=None):
        body = {"mode": "2D", "create": create or [],
                "update": update or [], "remove": remove or []}
        return http_.request(
            "POST", f"{api}/shape/change?socketId={socket_id}", body=body)

    def probe_position():
        """Re-GET the project; return our device's position dict, or None."""
        for s in innerspace_project(http_, base).get("shapes") or []:
            if s.get("id") == dev_id:
                pts = s.get("position") or []
                return pts[0] if pts else None
        return None

    def at(pos, want):
        return (pos is not None
                and abs(float(pos["x"]) - want["x"]) < 0.01
                and abs(float(pos["y"]) - want["y"]) < 0.01)

    print(f"project        {project_id}")
    print(f"plan           {plan_id}  ('{plan.get('title')}')")
    print(f"product        {product_id}")
    print(f"scratch device {dev_id}")
    print(f"socketId       {socket_id}  (random UUID — the test is whether this")
    print( "               is accepted without a live websocket session)\n")

    if not args.live:
        print("DRY RUN — nothing is sent. Re-run with --live to execute.\n")
        print("--live would perform, on the plan above:")
        print("  1. POST /shape/change create=[AP @ (0,0)]  -> re-GET expects it present")
        print("  2. POST /shape/change update=[AP @ (3,4)]  -> re-GET expects it MOVED")
        print("  3. POST /shape/change remove=[AP]          -> re-GET expects it gone")
        print("\nNet change if run live: none (the scratch AP is created, moved, "
              "then deleted).")
        return []

    ok, created = True, False
    try:
        shape_change(create=[device_shape(p_start)])
        created = True
        if at(probe_position(), p_start):
            print("[PASS] create persisted — scratch AP present at (0,0)")
        else:
            ok = False
            print("[FAIL] create did NOT persist after re-GET.\n"
                  "       -> a random socketId is rejected; scripted writes "
                  "likely need a live websocket session id (see doc open item 1).")
            return []

        shape_change(update=[device_shape(p_moved)])
        if at(probe_position(), p_moved):
            print("[PASS] POSITION WRITE persisted — scratch AP moved to (3,4)")
        else:
            ok = False
            print("[FAIL] position update did NOT persist after re-GET.")
    finally:
        if created:
            try:
                shape_change(remove=[device_shape(p_moved)])
                if probe_position() is None:
                    print("[PASS] cleanup — scratch AP removed")
                else:
                    print(f"[WARN] scratch AP {dev_id} may remain — delete "
                          "'SURFSCAN-WRITE-PROBE' from the InnerSpace UI")
            except RuntimeError as e:
                print(f"[WARN] cleanup failed ({e}) — delete "
                      "'SURFSCAN-WRITE-PROBE' from the InnerSpace UI",
                      file=sys.stderr)

    print()
    if ok:
        print("CONFIRMED — InnerSpace accepts scripted position writes via "
              "POST /shape/change with a random socketId; verify each write by "
              "re-GETting /project?mode=2D.")
    else:
        print("NOT CONFIRMED — see the [FAIL] line(s) above.")
    return []


# --------------------------------------------------------------------------
# OpenIntent 2.0 export (zip importable by Hamina Network Planner)
# --------------------------------------------------------------------------

OI_BAND = {"ng": "FREQ_2.4GHZ", "na": "FREQ_5GHZ", "6e": "FREQ_6GHZ"}
OI_WIDTH = {20: "20_MHz", 40: "40_MHz", 80: "80_MHz",
            160: "160_MHz", 320: "320_MHz"}

# UniFi internal model codes -> marketing names (best effort; unknown codes
# pass through lowercased and can be fixed up in Hamina after import)
UNIFI_MODEL_NAMES = {
    "U7PG2": "uap-ac-pro", "U7LT": "uap-ac-lite", "U7LR": "uap-ac-lr",
    "U7HD": "uap-ac-hd", "U7SHD": "uap-ac-shd", "U7NHD": "uap-nanohd",
    "UFLHD": "uap-flexhd", "UHDIW": "uap-iw-hd", "U7IW": "uap-ac-iw",
    "U7MSH": "uap-ac-mesh", "U7MP": "uap-ac-mesh-pro",
    "UAL6": "u6-lite", "UAP6": "u6-lr", "UAP6MP": "u6-pro",
    "U6M": "u6-mesh", "U6IW": "u6-iw", "U6ENT": "u6-enterprise",
    "U6EXT": "u6-extender",
    "U7PRO": "u7-pro", "U7PROMAX": "u7-pro-max",
}


def image_size(data):
    """(width, height) from PNG/JPEG bytes, or None."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) > 24:
        w, h = struct.unpack(">II", data[16:24])
        return w, h
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seglen = struct.unpack(">H", data[i + 2:i + 4])[0]
            i += 2 + seglen
    return None


def safe_name(s):
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in s)


def oi_width(v):
    """OpenIntent width label from a UniFi width, which may be an int or a
    string ('80'); None if it is not a width we can express."""
    try:
        return OI_WIDTH[int(v)]
    except (TypeError, ValueError, KeyError):
        return None


def oi_radios(dev, include_down=False):
    """Radios from live state (radio_table_stats), falling back to the
    configured radio_table only where live state is missing.

    Live has to win. RRM moves radios at runtime, so radio_table is a
    statement of intent, not of fact: a radio configured ht 80 routinely
    operates at 40, `channel: "auto"` resolves to a number only in the live
    stats, and radio_table's tx_power is the configured floor (it equals
    min_txpower) while the stats carry the power actually in use. Exporting
    the configured values makes Hamina predict a network that is not on air.

    A radio whose live state is not RUN is not serving clients at all -- a
    disabled or wedged 2.4 GHz radio is common -- so it is dropped unless
    include_down, rather than seeding the plan with coverage that does not
    exist. Warnings name every radio dropped."""
    stats = {s.get("radio"): s for s in dev.get("radio_table_stats") or []}
    table = dev.get("radio_table") or [{"radio": r} for r in stats]
    who = dev.get("name") or dev.get("mac") or "AP"
    radios = []
    for rt in table:
        key = rt.get("radio")
        band = OI_BAND.get(key)
        if not band:
            continue
        st = stats.get(key) or {}

        state = st.get("state")
        if state and state != "RUN" and not include_down:
            print(f"warning: {who}: {band} radio ({key}) is in state "
                  f"{state}, not RUN — omitted from the export "
                  f"(--include-down-radios keeps it)")
            continue

        r = {"id": len(radios), "radio_function": "CLIENT_ACCESS",
             "band": band}

        ch = st.get("channel")
        if not isinstance(ch, int):
            ch = rt.get("channel")          # "auto" until RRM resolves it
        if isinstance(ch, int):
            r["channel"] = ch

        width = oi_width(st.get("bw")) or oi_width(rt.get("ht"))
        if width:
            r["channel_width"] = width

        # live only: radio_table's tx_power is the floor, not the operating
        # power, so a fallback there would silently understate every radio
        tp = st.get("tx_power")
        if isinstance(tp, (int, float)):
            r["transmit_power"] = tp

        nss = rt.get("nss")
        if isinstance(nss, int) and nss > 0:
            r["mimo_chains"] = nss

        # Only claim AUTOMATIC when the controller still says "auto". Once
        # RRM picks a channel it writes the number back into radio_table, so
        # an int here does not distinguish a hand-pinned radio from an
        # auto one that has already settled -- and asserting MANUAL on a
        # guess would stop Hamina re-optimising it. Omit when unsure.
        if rt.get("channel") == "auto":
            r["channel_assignment"] = "AUTOMATIC"
        radios.append(r)

    if table and not stats:
        print(f"warning: {who}: no live radio state (device offline?) — "
              f"exporting configured channels and widths, no tx power")
    return radios


def default_radios(model):
    """Radios to declare when no live radio state is available. OpenIntent
    requires a non-empty radio list, so infer the bands from the model: 6 GHz
    only for the WiFi 6E/7 lines."""
    m = (model or "").lower()
    bands = ["FREQ_2.4GHZ", "FREQ_5GHZ"]
    if re.search(r"\bu7\b|u7-|e7|-e7|u6-ent|enterprise|6e\b", m):
        bands.append("FREQ_6GHZ")
    return [{"id": i, "radio_function": "CLIENT_ACCESS", "band": b}
            for i, b in enumerate(bands)]


def oi_ap(dev, fp_name, coords, include_down=False):
    code = dev.get("model") or ""
    model = UNIFI_MODEL_NAMES.get(code, code.lower())
    radios = oi_radios(dev, include_down) or default_radios(code)
    bands = [{"band": r["band"]} for r in radios]
    ap = {
        "name": dev.get("name") or dev.get("mac") or "AP",
        "floorplan_name": fp_name,
        "manufacturer": "ubiquiti",
        "model": model,
        "model_original": code,
        "dot11_radios": radios,
        "antennas": [{"vendor": "ubiquiti", "model": model, "bands": bands}],
        "orientation": {"rotation": 0, "tilt": 0},
        "display_color": "#4687f0",
    }
    if coords:
        ap["coordinates"] = coords
    return ap


def build_openintent_unplaced(site_data, args):
    """No floor-plan positions available (classic Maps gone): emit APs with
    their real radio config on a placeholder floor plan, so Hamina imports the
    inventory + settings and you only drag them into place."""
    z_m = args.ap_height
    aps_out, floorplans = [], []
    for site, _maps, devices in site_data:
        aps = [d for d in devices if d.get("type") == "uap"]
        if not aps:
            continue
        fp_name = f"{site} - unplaced"
        # placeholder canvas: 50 m x 50 m at 100 px/m, APs on a grid
        px_per_m, side_m = 100.0, 50.0
        w = h = side_m * px_per_m
        floorplans.append({
            "name": fp_name, "project_name": site, "rotation": 0,
            "dimensions": [
                {"width": w, "length": h, "unit": "pixels",
                 "height": z_m * px_per_m},
                {"width": side_m, "length": side_m, "height": z_m,
                 "unit": "meters"}],
            "reference_markers": [], "coverage_areas": [],
        })
        cols = max(1, int(len(aps) ** 0.5 + 0.999))
        for i, dev in enumerate(aps):
            x_m = 5.0 + (i % cols) * 5.0
            y_m = 5.0 + (i // cols) * 5.0
            coords = [
                {"coordinate_xyz": {"x": x_m * px_per_m, "y": y_m * px_per_m,
                                    "z": z_m * px_per_m, "unit": "pixels"}},
                {"coordinate_xyz": {"x": x_m, "y": y_m, "z": z_m,
                                    "unit": "meters"}}]
            aps_out.append(oi_ap(dev, fp_name, coords,
                                 getattr(args, "include_down_radios", False)))

    if not aps_out:
        print("openintent: no APs found to export", file=sys.stderr)
        return
    data = {"openintent_version": "2.0.1",
            "accesspoints": aps_out, "floorplans": floorplans}
    with zipfile.ZipFile(args.openintent, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("openintent.json", json.dumps(data, indent=2))
    print(f"openintent: wrote {args.openintent} — {len(aps_out)} AP(s) with "
          f"radio config on {len(floorplans)} placeholder floor plan(s).")
    print("openintent: NO real positions/floor plans exist on this console, "
          "so APs are laid out on a blank 50x50 m grid — import into Hamina, "
          "swap in your own floor plan, and drag each AP into place.",
          file=sys.stderr)


def build_openintent(http_, base, prefix, site_data, args):
    multi_site = len(site_data) > 1
    z_m = args.ap_height
    floorplans, aps_out, images, skipped = [], [], {}, []

    if not any(maps for _s, maps, _d in site_data):
        if args.unplaced:
            build_openintent_unplaced(site_data, args)
        else:
            print("openintent: no floor plans on this console — rerun with "
                  "--unplaced to export APs + radio config onto a placeholder "
                  "plan for manual placement in Hamina", file=sys.stderr)
        return

    for site, maps, devices in site_data:
        placed = {}
        for dev in devices:
            if dev.get("type") != "uap":
                continue
            mid = dev.get("map_id")
            if (mid and mid in maps
                    and dev.get("x") is not None and dev.get("y") is not None):
                placed.setdefault(mid, []).append(dev)
            else:
                skipped.append(dev.get("name") or dev.get("mac") or "?")

        for mid, devs in placed.items():
            m = maps[mid]
            fp_name = m.get("name") or mid
            if multi_site:
                fp_name = f"{site} - {fp_name}"

            img, ext = fetch_map_image(http_, base, prefix, site, mid)
            size = image_size(img) if img else None
            if size:
                img_w, img_h = size
            elif m.get("width") and m.get("height"):
                img_w, img_h = float(m["width"]), float(m["height"])
            else:
                # no image, no stored dims: bound the canvas by AP extents
                img_w = max(float(d["x"]) for d in devs) * 1.1 + 100
                img_h = max(float(d["y"]) for d in devs) * 1.1 + 100
                print(f"warning: no image/dimensions for map '{fp_name}' — "
                      "estimating canvas from AP positions", file=sys.stderr)

            upp = float(m.get("upp") or 0)  # meters per pixel when map scaled
            dims = [{"width": float(img_w), "length": float(img_h),
                     "unit": "pixels"}]
            if upp:
                dims[0]["height"] = z_m / upp  # ceiling height in pixels
                dims.append({"width": img_w * upp, "length": img_h * upp,
                             "height": z_m, "unit": "meters"})
            else:
                print(f"warning: map '{fp_name}' has no scale (upp) set in "
                      "UniFi — Hamina will need the scale set manually",
                      file=sys.stderr)

            fp = {"name": fp_name, "project_name": site, "floor_id": mid,
                  "rotation": 0, "dimensions": dims,
                  "reference_markers": [], "coverage_areas": []}
            if img:
                rel = f"images/{safe_name(fp_name)}{ext}"
                fp["map_uri"] = f"file://{rel}"
                images[rel] = img
            floorplans.append(fp)

            for dev in devs:
                x = float(dev["x"])
                y = float(img_h) - float(dev["y"])  # OpenIntent origin: bottom-left
                coords = [{"coordinate_xyz": {
                    "x": x, "y": y, "unit": "pixels",
                    **({"z": z_m / upp} if upp else {})}}]
                if upp:
                    coords.append({"coordinate_xyz": {
                        "x": x * upp, "y": y * upp, "z": z_m,
                        "unit": "meters"}})
                aps_out.append(oi_ap(dev, fp_name, coords,
                                     getattr(args, "include_down_radios",
                                             False)))

    if not aps_out:
        print("openintent: no placed APs found — nothing to export "
              "(place APs on a UniFi floor plan first)", file=sys.stderr)
        return

    data = {"openintent_version": "2.0.1",
            "accesspoints": aps_out, "floorplans": floorplans}
    with zipfile.ZipFile(args.openintent, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("openintent.json", json.dumps(data, indent=2))
        for rel, blob in images.items():
            zf.writestr(rel, blob)
    print(f"openintent: wrote {args.openintent} — {len(floorplans)} floor "
          f"plan(s), {len(aps_out)} AP(s), {len(images)} image(s)")
    if skipped:
        print(f"openintent: skipped {len(skipped)} AP(s) not placed on any "
              f"map: {', '.join(skipped)}", file=sys.stderr)


# --------------------------------------------------------------------------

def write_csv(rows, out):
    fh = open(out, "w", newline="") if out != "-" else sys.stdout
    try:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    finally:
        if fh is not sys.stdout:
            fh.close()


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="mode", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-o", "--output", default="-",
                        help="output CSV path (default: stdout)")

    c = sub.add_parser("cloud", parents=[common],
                       help="Site Manager API (api.ui.com)")
    c.add_argument("--api-key", required=True,
                   help="cloud API key from unifi.ui.com/api")

    l = sub.add_parser("local", parents=[common],
                       help="official Integration API on the console")
    l.add_argument("--host", required=True,
                   help="console URL, e.g. https://192.168.1.1")
    l.add_argument("--api-key", required=True,
                   help="key from Settings -> Control Plane -> Integrations")
    l.add_argument("--verify-tls", dest="insecure_skip_verify",
                   action="store_false", default=True,
                   help="verify the console TLS cert (default: skip)")
    l.add_argument("--all-devices", dest="aps_only",
                   action="store_false", default=True,
                   help="include switches/gateways, not just APs")

    g = sub.add_parser("legacy", parents=[common],
                       help="internal controller API (map positions + radios)")
    g.add_argument("--host", required=True,
                   help="controller URL, e.g. https://192.168.1.1 or "
                        "https://ck:8443 for a classic controller")
    g.add_argument("-u", "--username", required=True,
                   help="local admin username (avoid ui.com accounts / MFA)")
    g.add_argument("-p", "--password",
                   help="password (prompted securely if omitted)")
    g.add_argument("--site", help="site short-name (default: all sites)")
    g.add_argument("--verify-tls", action="store_true", default=False,
                   help="verify the controller TLS cert (default: skip)")
    g.add_argument("--download-maps", metavar="DIR",
                   help="also try to save floor-plan images into DIR")
    g.add_argument("--openintent", metavar="OUT.zip",
                   help="also write an OpenIntent 2.0 zip (floor plans + "
                        "placed APs) importable directly into Hamina")
    g.add_argument("--ap-height", type=float, default=2.5, metavar="METERS",
                   help="AP mount height for OpenIntent export (default 2.5)")
    g.add_argument("--unplaced", action="store_true",
                   help="if the console has no floor plans, still export APs "
                        "+ radio config onto a placeholder plan")
    g.add_argument("--include-down-radios", action="store_true",
                   help="keep radios whose live state is not RUN (default: "
                        "drop them, so the plan does not predict coverage "
                        "from a disabled or wedged radio)")

    i = sub.add_parser("innerspace", parents=[common],
                       help="floor plans + AP placements + walls from the "
                            "InnerSpace app (best source; use this)")
    i.add_argument("--host", required=True, help="console URL")
    i.add_argument("-u", "--username", required=True, help="local admin user")
    i.add_argument("-p", "--password", help="password (prompted if omitted)")
    i.add_argument("--verify-tls", action="store_true", default=False,
                   help="verify the console TLS cert (default: skip)")
    i.add_argument("--openintent", metavar="OUT.zip",
                   help="write an OpenIntent 2.0 zip for Hamina import")
    i.add_argument("--plan", metavar="NAME",
                   help="only export plans whose title contains NAME")
    i.add_argument("--ap-height", type=float, default=2.5, metavar="METERS",
                   help="height for non-ceiling APs (default 2.5; ceiling "
                        "APs use the plan's own ceiling height)")
    i.add_argument("--no-walls", action="store_true",
                   help="omit wall segments from the OpenIntent export")
    i.add_argument("--no-radio", action="store_true",
                   help="skip the Network-app join for channel/tx power")
    i.add_argument("--include-down-radios", action="store_true",
                   help="keep radios whose live state is not RUN (default: "
                        "drop them, so the plan does not predict coverage "
                        "from a disabled or wedged radio)")
    i.add_argument("--project-json", metavar="FILE",
                   help="use a saved project.json instead of fetching")

    pr = sub.add_parser("probe", parents=[common],
                        help="report which floor-plan APIs this console has")
    pr.add_argument("--host", required=True, help="console URL")
    pr.add_argument("-u", "--username", required=True, help="local admin user")
    pr.add_argument("-p", "--password", help="password (prompted if omitted)")
    pr.add_argument("--site", help="site short-name (default: default)")
    pr.add_argument("--verify-tls", action="store_true", default=False,
                    help="verify the console TLS cert (default: skip)")
    pr.add_argument("--dump", metavar="DIR",
                    help="save successful JSON responses into DIR")
    pr.add_argument("--floorplan-id", metavar="UUID",
                    help="InnerSpace floor-plan UUID from the browser URL "
                         "(.../innerspace/<UUID>) — probes per-plan endpoints")
    pr.add_argument("--discover", action="store_true",
                    help="crawl the Network app's JS bundles (incl. lazy "
                         "chunks) for the API paths it actually calls")
    pr.add_argument("--grep", default="innerspace", metavar="WORD",
                    help="keyword for --discover (default: innerspace)")
    pr.add_argument("--context", type=int, default=200, metavar="CHARS",
                    help="context size around --grep hits (default 200)")
    pr.add_argument("--get", metavar="PATH",
                    help="fetch one path and print the raw body "
                         "(e.g. /proxy/network/v2/api/site/default/floorplan)")
    g.add_argument("--all-devices", dest="aps_only",
                   action="store_false", default=True,
                   help="include switches/gateways, not just APs")

    cw = sub.add_parser("confirm-write", parents=[common],
                        help="prove InnerSpace accepts scripted AP position "
                             "writes (creates+moves+deletes a scratch AP; "
                             "dry-run unless --live)")
    cw.add_argument("--host", required=True, help="console URL")
    cw.add_argument("-u", "--username", required=True, help="local admin user")
    cw.add_argument("-p", "--password", help="password (prompted if omitted)")
    cw.add_argument("--verify-tls", action="store_true", default=False,
                    help="verify the console TLS cert (default: skip)")
    cw.add_argument("--plan", metavar="NAME",
                    help="place the scratch AP on the plan whose title contains "
                         "NAME (default: the first plan)")
    cw.add_argument("--live", action="store_true",
                    help="actually send the writes (default: dry run, no POSTs)")

    args = p.parse_args()

    try:
        rows = {"cloud": run_cloud, "local": run_local, "legacy": run_legacy,
                "innerspace": run_innerspace, "probe": run_probe,
                "confirm-write": run_confirm_write}[args.mode](args)
    except RuntimeError as e:
        die(str(e))
    if args.mode in ("probe", "confirm-write"):
        return
    write_csv(rows, args.output)
    placed = sum(1 for r in rows if r["x_px"] != "")
    print(f"exported {len(rows)} devices ({placed} with map positions) "
          f"to {args.output if args.output != '-' else 'stdout'}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
