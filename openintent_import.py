#!/usr/bin/env python3
"""Import a Hamina OpenIntent 2.0 export into UniFi InnerSpace (reverse of unifi_export.py).

Reads an OpenIntent zip (floorplans + walls + APs) and writes it into an
InnerSpace project as a NEW plan: upload image -> create plan -> create wall
shapes -> create device shapes. See docs/INNERSPACE_WRITE_API.md and issue #2.

Pipeline:  parse (OpenIntent)  ->  map (coords / model / wall)  ->  write (InnerSpace)

SAFETY: defaults to --dry-run, which prints the exact API calls it WOULD make
(no writes). Pass --commit to actually write. Even --commit only ever CREATEs a
new plan; it never edits or deletes existing plans.

Examples:
    # offline dry-run (no console needed): map + preview the calls, using a saved
    # project dump for the product catalog
    python3 openintent_import.py home.zip --project-json innerspace_project.json

    # live dry-run against the console (fetches the catalog itself)
    python3 openintent_import.py home.zip --host https://192.168.1.1 \
        --username admin --no-verify-tls

    # actually write it in
    python3 openintent_import.py home.zip --host https://192.168.1.1 \
        --username admin --no-verify-tls --commit
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import io
import json
import os
import sys
from collections import defaultdict
import urllib.error
import urllib.request
import uuid
import zipfile

# Reuse the exporter's HTTP client, login, image sniffing, and the wall/model
# tables so auth + mappings stay single-sourced. (Importing unifi_export does not
# run its CLI — main() is guarded by __main__.)
from unifi_export import (
    Http, legacy_login, image_size, WALL_VARIANTS, INNERSPACE_SKU_ALIASES,
)

INNERSPACE_API = "/proxy/innerspace/api"
# OpenIntent wall_type label -> InnerSpace variant (reverse of the exporter's own
# labels; exact match tried first).
WALL_LABEL_TO_VARIANT = {label: variant for variant, (label, _att) in WALL_VARIANTS.items()}
# model (as the exporter emits it) -> InnerSpace SKU (reverse of the export aliases).
MODEL_TO_SKU = {model: sku for sku, model in INNERSPACE_SKU_ALIASES.items()}

# The full InnerSpace built-in wall variant set (docs/INNERSPACE_WRITE_API.md) —
# richer than the exporter's 8. These are the canonical write targets.
INNERSPACE_WALL_VARIANTS = {
    "concrete", "drywall", "drywall_heavy", "glass", "glass_thin", "brick",
    "metal", "wood", "door_wood", "door_metal", "door_glass",
    "window_1_pane", "window_2_pane", "window_3_pane",
}
# Common OpenIntent/Hamina wall-material phrasings -> InnerSpace variant. Keys are
# normalized (lowercase, non-alphanumeric collapsed to single spaces).
_WALL_ALIASES = {
    "concrete": "concrete", "cinder block": "concrete", "cinderblock": "concrete",
    "cmu": "concrete", "solid wall": "concrete",
    "fireplace": "brick", "chimney": "brick", "masonry": "brick", "stone": "brick",
    "drywall": "drywall", "gypsum": "drywall", "gypsum board": "drywall",
    "plasterboard": "drywall", "sheetrock": "drywall", "plaster": "drywall",
    "interior wall": "drywall", "partition": "drywall",
    "drywall heavy": "drywall_heavy", "heavy drywall": "drywall_heavy",
    "thick drywall": "drywall_heavy", "double drywall": "drywall_heavy",
    "glass": "glass", "glass thin": "glass_thin", "thin glass": "glass_thin",
    "brick": "brick",
    "metal": "metal", "steel": "metal", "sheet metal": "metal",
    "wood": "wood", "wooden": "wood", "plywood": "wood", "timber": "wood",
    "wood door": "door_wood", "wooden door": "door_wood", "door wood": "door_wood",
    "metal door": "door_metal", "door metal": "door_metal", "steel door": "door_metal",
    "glass door": "door_glass", "door glass": "door_glass",
    "window": "window_1_pane", "single pane window": "window_1_pane",
    "single pane": "window_1_pane", "window 1 pane": "window_1_pane",
    "1 pane window": "window_1_pane",
    "double pane window": "window_2_pane", "double pane": "window_2_pane",
    "window 2 pane": "window_2_pane", "2 pane window": "window_2_pane",
    "triple pane window": "window_3_pane", "triple pane": "window_3_pane",
    "window 3 pane": "window_3_pane", "3 pane window": "window_3_pane",
}

# InnerSpace built-in attenuation-object variants (docs/INNERSPACE_WRITE_API.md).
INNERSPACE_ATTEN_VARIANTS = {
    "car", "cubicles", "elevator", "foliage_heavy", "foliage_light",
    "human_crowd", "machinery", "server_rack", "shelf_small", "shelf_medium",
    "shelf_warehouse", "truck",
}
# Common obstacle/attenuation-area phrasings -> InnerSpace variant (normalized keys).
_ATTEN_ALIASES = {
    "car": "car", "vehicle": "car", "truck": "truck", "lorry": "truck",
    "cubicles": "cubicles", "cubicle": "cubicles", "office cubicles": "cubicles",
    "elevator": "elevator", "lift": "elevator",
    "foliage": "foliage_heavy", "foliage heavy": "foliage_heavy",
    "heavy foliage": "foliage_heavy", "trees": "foliage_heavy", "tree": "foliage_heavy",
    "foliage light": "foliage_light", "light foliage": "foliage_light",
    "bushes": "foliage_light", "shrubs": "foliage_light",
    "human crowd": "human_crowd", "crowd": "human_crowd", "people": "human_crowd",
    "machinery": "machinery", "machine": "machinery", "equipment": "machinery",
    "server rack": "server_rack", "rack": "server_rack", "servers": "server_rack",
    "shelf": "shelf_medium", "shelving": "shelf_medium",
    "shelf small": "shelf_small", "small shelf": "shelf_small",
    "shelf medium": "shelf_medium", "medium shelf": "shelf_medium",
    "shelf warehouse": "shelf_warehouse", "warehouse shelf": "shelf_warehouse",
}

_ISO = "2026-01-01T00:00:00.000Z"   # placeholder timestamp; server stamps its own


# --- UniFi OS write auth: X-CSRF-Token -----------------------------------
def apply_csrf(http_) -> str | None:
    """UniFi OS requires X-CSRF-Token on every mutating request. The token is
    the `csrfToken` claim inside the `TOKEN` session-cookie JWT. Extract it and
    put it on the shared headers so all writes carry it. (GET-only tools like
    the exporter never needed this.)"""
    token = None
    for c in http_.jar:
        if c.name == "TOKEN" and c.value and c.value.count(".") >= 2:
            payload = c.value.split(".")[1]
            payload += "=" * (-len(payload) % 4)  # pad base64url
            try:
                claims = json.loads(base64.urlsafe_b64decode(payload))
                token = claims.get("csrfToken") or token
            except Exception:
                pass
    if not token:  # fall back to whatever login stashed from the response header
        token = http_.extra_headers.get("X-CSRF-Token")
    if token:
        http_.extra_headers["X-CSRF-Token"] = token
    return token


# --- Phase 2: parse the OpenIntent export --------------------------------
def _load_openintent(path: str):
    """Load the OpenIntent doc + an image reader from a .zip or a bare .json.

    Hamina names the JSON `openIntent_<Project>.json` (not `openintent.json`) and
    adds `export-warnings.json`, so find the OpenIntent doc by CONTENT (has
    `floorplans`/`openintent_version`), not by filename. Returns (data, reader)
    where reader(rel_path) -> image bytes or None."""
    if path.lower().endswith(".zip"):
        zf = zipfile.ZipFile(path)
        names = zf.namelist()
        data = None
        for n in names:
            if not n.lower().endswith(".json"):
                continue
            try:
                j = json.loads(zf.read(n))
            except Exception:
                continue
            if isinstance(j, dict) and ("floorplans" in j or "openintent_version" in j):
                data = j
                break
        if data is None:
            raise RuntimeError("no OpenIntent JSON (with 'floorplans') in %s" % path)

        def reader(rel):
            base = rel.split("/")[-1]
            for n in names:
                if n == rel or n.lstrip("./") == rel.lstrip("./") or n.split("/")[-1] == base:
                    if n.lower().endswith((".png", ".jpg", ".jpeg")):
                        return zf.read(n)
            return None
        return data, reader

    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    root = os.path.dirname(os.path.abspath(path))

    def reader(rel):
        rel = rel.lstrip("./")
        base = rel.split("/")[-1]
        for cand in (os.path.join(root, rel), os.path.join(root, "images", base),
                     os.path.join(root, base)):
            if os.path.exists(cand):
                with open(cand, "rb") as fh:
                    return fh.read()
        return None
    return data, reader


def parse_openintent(zip_path: str) -> dict:
    """Return {floorplans:[{name,image_name,image_bytes,img_w,img_h,walls,aps}]}.

    walls: [{wall_type,start:(x,y),end:(x,y)}]   (pixel coords)
    aps:   [{name,model,model_original,mac,x,y}] (pixel coords)
    """
    data, reader = _load_openintent(zip_path)
    by_name: dict[str, dict] = {}
    for fp in data.get("floorplans", []):
        name = fp.get("name") or "Plan"
        dims = fp.get("dimensions") or [{}]
        px = next((d for d in dims if d.get("unit") == "pixels"), dims[0])
        img_w = float(px.get("width") or 0)
        img_h = float(px.get("length") or 0)
        mm = next((d for d in dims if d.get("unit") == "meters"), None)
        width_m = float(mm["width"]) if mm and mm.get("width") else None
        ceiling_m = float(mm["height"]) if mm and mm.get("height") else 2.5
        image_name, image_bytes = _read_image(reader, fp)
        by_name[name] = {
            "name": name, "image_name": image_name, "image_bytes": image_bytes,
            "img_w": img_w, "img_h": img_h,
            "width_m": width_m, "ceiling_m": ceiling_m,
            "walls": [_wall(w) for w in (fp.get("wall_segments") or [])],
            "atten": _attenuation_objects(fp),
            "aps": [],
        }
    # attach APs to their floorplan
    for ap in data.get("accesspoints", []):
        fpn = ap.get("floorplan_name")
        target = by_name.get(fpn) or (next(iter(by_name.values())) if by_name else None)
        if target is None:
            continue
        px = _ap_pixel(ap)
        if px is None:
            continue
        mac = (ap.get("mac_address") or "").replace(":", "").upper()
        synth = not mac
        if synth:                      # Hamina exports omit MACs; InnerSpace won't
            mac = _synth_mac(ap.get("name") or "AP")   # place a device without one
        target["aps"].append({
            "name": ap.get("name") or "AP",
            "model": (ap.get("model") or "").lower(),
            "model_original": ap.get("model_original") or ap.get("model") or "",
            "mac": mac, "mac_synth": synth,
            "x": px[0], "y": px[1],
        })
    return {"floorplans": list(by_name.values())}


def _read_image(reader, fp):
    uri = fp.get("map_uri") or ""
    rel = uri.split("file://", 1)[-1] if uri.startswith("file://") else uri
    if not rel:
        return None, None
    return rel.split("/")[-1], reader(rel)


def _wall(w):
    return {
        "wall_type": w.get("wall_type") or "",
        "start": (float(w["start_point"]["x"]), float(w["start_point"]["y"])),
        "end": (float(w["end_point"]["x"]), float(w["end_point"]["y"])),
    }


def _synth_mac(name: str) -> str:
    """A stable, locally-administered placeholder MAC (12 hex, uppercase) derived
    from the AP name, for exports (like Hamina's) that omit MACs. `02` prefix =
    locally-administered/unicast; deterministic so re-imports stay stable."""
    return "02" + hashlib.md5(name.encode("utf-8")).hexdigest().upper()[:10]


def _ap_pixel(ap):
    for c in ap.get("coordinates") or []:
        xyz = c.get("coordinate_xyz") or {}
        if xyz.get("unit") == "pixels":
            return float(xyz["x"]), float(xyz["y"])
    return None


# OpenIntent has no standardized obstacle key across Hamina versions, so probe
# the likely ones and accept several polygon encodings. Adjust when a real
# export with attenuation objects is available.
_ATTEN_KEYS = ("attenuation_objects", "attenuationObjects", "attenuation_areas",
               "attenuationAreas", "obstacles", "attenuation_zones")


def _attenuation_objects(fp) -> list:
    raw = None
    for k in _ATTEN_KEYS:
        if fp.get(k):
            raw = fp[k]
            break
    out = []
    for obj in (raw or []):
        pts = _polygon_pixels(obj)
        if len(pts) < 3:
            continue
        material = (obj.get("type") or obj.get("material") or obj.get("variant")
                    or obj.get("name") or "")
        out.append({"material": material, "polygon": pts})
    return out


def _polygon_pixels(obj) -> list:
    """Pull a pixel polygon out of an obstacle, tolerating several encodings:
    polygon/points/vertices/coordinates of {x,y} or {coordinate_xyz:{x,y,unit}}."""
    for key in ("polygon", "points", "vertices", "coordinates", "shape"):
        seq = obj.get(key)
        if not isinstance(seq, list) or not seq:
            continue
        pts = []
        for p in seq:
            if not isinstance(p, dict):
                continue
            xyz = p.get("coordinate_xyz") or p
            if xyz.get("unit") not in (None, "pixels"):
                continue  # prefer pixel coords; skip metre-only entries
            if "x" in xyz and "y" in xyz:
                pts.append((float(xyz["x"]), float(xyz["y"])))
        if len(pts) >= 3:
            return pts
    return []


# --- Phase 3: map to InnerSpace ------------------------------------------
def to_scene(px: float, py: float, img_w: float, img_h: float) -> tuple[float, float]:
    """Invert scene_to_pixels for a fresh plan (identity scale, zero offset):
    pixel = scene + img/2  ->  scene = pixel - img/2.  No y-flip (matches export)."""
    return round(px - img_w / 2.0, 4), round(py - img_h / 2.0, 4)


def _norm(s: str) -> str:
    """lowercase, collapse any run of non-alphanumerics to a single space."""
    return " ".join("".join(c if c.isalnum() else " " for c in s.lower()).split())


def wall_variant(wall_type: str, misses: set | None = None) -> str:
    """OpenIntent wall label -> InnerSpace variant, robust across phrasings.

    Order: exact exporter label -> alias table -> already-a-variant ->
    keyword heuristic -> default 'drywall' (recorded in `misses` if given)."""
    if not wall_type:
        return "drywall"
    if wall_type in WALL_LABEL_TO_VARIANT:            # exporter's own labels
        return WALL_LABEL_TO_VARIANT[wall_type]
    n = _norm(wall_type)
    if n in _WALL_ALIASES:
        return _WALL_ALIASES[n]
    underscored = n.replace(" ", "_")
    if underscored in INNERSPACE_WALL_VARIANTS:       # label already a variant
        return underscored
    if "window" in n:                                 # keyword heuristics
        return ("window_3_pane" if ("3" in n or "triple" in n)
                else "window_2_pane" if ("2" in n or "double" in n)
                else "window_1_pane")
    if "door" in n:
        return ("door_glass" if "glass" in n
                else "door_metal" if ("metal" in n or "steel" in n)
                else "door_wood")
    for kw, var in (("concrete", "concrete"), ("brick", "brick"),
                    ("glass", "glass"), ("metal", "metal"), ("steel", "metal"),
                    ("wood", "wood"), ("timber", "wood"),
                    ("drywall", "drywall"), ("gypsum", "drywall")):
        if kw in n:
            return var
    if misses is not None:
        misses.add(wall_type)
    return "drywall"


def find_product_id(ap: dict, products: list) -> str | None:
    """Map an OpenIntent AP model to an InnerSpace catalog productId."""
    wanted = {
        (ap.get("model_original") or "").lower(),
        (ap.get("model") or "").lower(),
        MODEL_TO_SKU.get(ap.get("model") or "", "").lower(),
    }
    # strip radio-variant suffixes the InnerSpace catalog doesn't carry
    # (e.g. u7-pro-outdoor-internal -> u7-pro-outdoor, matching sku U7-Pro-Outdoor)
    for m in list(wanted):
        for suf in ("-internal", "-external", "-inner", "-outer", "-int", "-ext"):
            if m.endswith(suf):
                wanted.add(m[: -len(suf)])
    wanted.discard("")
    for p in products:
        for field in ("sku", "name", "shortname", "title", "abbrev", "model"):
            v = str(p.get(field) or "").lower()
            if v and v in wanted:
                return p.get("id")
    return None


def atten_variant(material: str, misses: set | None = None) -> str:
    """OpenIntent obstacle material -> InnerSpace attenuation-object variant."""
    if not material:
        if misses is not None:
            misses.add("(unnamed)")
        return "cubicles"
    n = _norm(material)
    if n in _ATTEN_ALIASES:
        return _ATTEN_ALIASES[n]
    underscored = n.replace(" ", "_")
    if underscored in INNERSPACE_ATTEN_VARIANTS:
        return underscored
    for kw, var in (("server", "server_rack"), ("rack", "server_rack"),
                    ("cubicle", "cubicles"), ("elevator", "elevator"),
                    ("lift", "elevator"), ("foliage", "foliage_heavy"),
                    ("tree", "foliage_heavy"), ("bush", "foliage_light"),
                    ("crowd", "human_crowd"), ("people", "human_crowd"),
                    ("machine", "machinery"), ("equipment", "machinery"),
                    ("shelf", "shelf_medium"), ("shelv", "shelf_medium"),
                    ("truck", "truck"), ("car", "car"), ("vehicle", "car")):
        if kw in n:
            return var
    if misses is not None:
        misses.add(material)
    return "cubicles"


def atten_shape(plan_id, project_id, variant, points_scene):
    """An `attenuationObject` shape (closed polygon), matched to the captured
    write shape."""
    poly = [{"x": x, "y": y, "z": 0} for (x, y) in points_scene]
    if poly and poly[0] != poly[-1]:
        poly.append(dict(poly[0]))            # close the ring, as InnerSpace does
    return {
        "id": str(uuid.uuid4()), "planId": plan_id, "projectId": project_id,
        "type": "attenuationObject", "variant": variant, "status": 0,
        "position": poly, "createdAt": _ISO, "updatedAt": _ISO,
    }


def scale_shape(plan_id, project_id, img_w, width_m, ceiling_m):
    """A `scale` shape that sets the plan's real-world scale so coverage renders
    accurately (no "Set Scale" prompt). A full-width scale line spanning `img_w`
    px carries `width_m` metres, giving metres/px = width_m/img_w — the same
    scale the OpenIntent file was built with. Fresh plans have map scale 1 and
    origin-centre, so scene x runs -img_w/2 .. +img_w/2."""
    half = round(img_w / 2.0, 4)
    return {
        "id": str(uuid.uuid4()), "planId": plan_id, "projectId": project_id,
        "type": "scale", "status": 0,
        "position": [{"x": -half, "y": 0, "z": 0}, {"x": half, "y": 0, "z": 0}],
        "scale": round(width_m, 4), "height": ceiling_m,
        "computedScale": None, "defaultScale": False, "defaultHeight": False,
        "createdAt": _ISO, "updatedAt": _ISO,
    }


def wall_shape(plan_id, project_id, variant, a, b):
    return {
        "id": str(uuid.uuid4()), "planId": plan_id, "projectId": project_id,
        "type": "wall", "variant": variant, "status": 0,
        "position": [{"x": a[0], "y": a[1], "z": 0}, {"x": b[0], "y": b[1], "z": 0}],
        "createdAt": _ISO, "updatedAt": _ISO,
    }


def device_shape(plan_id, project_id, ap, product_id, scene):
    return {
        "id": str(uuid.uuid4()), "planId": plan_id, "projectId": project_id,
        "type": "device", "title": ap["name"], "productId": product_id,
        "meta": {"mac": ap["mac"]} if ap["mac"] else {},
        "mount": "ceiling",
        "position": [{"x": scene[0], "y": scene[1], "z": 0}],
        "rotation": {"base": {"w": 0, "x": 0, "y": 0, "z": 1},
                     "pov": {"w": 0, "x": 0, "y": 0, "z": 1}},
        "antenna": None, "parentId": None, "childIndex": None,
        "rackId": None, "rackOrder": None, "optimize": False,
        "status": 1, "createdAt": _ISO, "updatedAt": _ISO,
    }


def _remove_ready(s):
    """Sanitize a shape fetched from GET /project so it passes /shape/change's
    'remove' validation. InnerSpace validates each removed shape as a full
    object; shapes read back from an existing project can carry incomplete
    rotation quaternions (e.g. missing rotation.base.w), which trips a
    'rotation.base.w Required' 400. Fill any missing quaternion component with 0.
    """
    s = dict(s)
    rot = s.get("rotation")
    if isinstance(rot, dict):
        out = dict(rot)
        for key in ("base", "pov"):
            q = dict(rot.get(key) or {})
            for c in ("w", "x", "y", "z"):
                q.setdefault(c, 0.0)
            out[key] = q
        s["rotation"] = out
    return s


# --- Phase 4: write (dry-run by default) ---------------------------------
class Writer:
    """Emits InnerSpace API calls. dry_run prints them; commit sends them."""

    def __init__(self, http_, base, socket_id, dry_run=True):
        self.http = http_
        self.base = base
        self.sid = socket_id
        self.dry = dry_run
        self.n = 0

    def _url(self, path):
        return "%s%s%s?socketId=%s" % (self.base, INNERSPACE_API, path, self.sid)

    def call(self, method, path, body):
        self.n += 1
        url = self._url(path)
        if self.dry:
            print("\n[%d] %s %s%s?socketId=%s" % (self.n, method, INNERSPACE_API, path, self.sid))
            print(json.dumps(body, indent=2)[:2000])
            return {}
        _, _, resp = self.http.request(method, url, body=body)
        return resp or {}

    def upload_image(self, name, blob):
        """multipart POST /project/plan/upload (field '2D'). Returns the file url."""
        self.n += 1
        if self.dry:
            print("\n[%d] POST %s/project/plan/upload?socketId=%s  (multipart form-data)"
                  % (self.n, INNERSPACE_API, self.sid))
            print('  field "2D" = %s (%s bytes)' % (name or "map.png", len(blob or b"")))
            return "file://<uploaded-%s>" % (name or "map.png")
        boundary = "----uhlImport%s" % uuid.uuid4().hex
        body = _multipart(boundary, "2D", name or "map.png", blob or b"")
        req_headers = {"Content-Type": "multipart/form-data; boundary=%s" % boundary}
        url = self._url("/project/plan/upload")
        h = {**self.http.extra_headers, **req_headers}
        req = urllib.request.Request(url, data=body, headers=h, method="POST")
        try:
            with self.http.opener.open(req, timeout=60) as r:
                out = json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            raise RuntimeError("image upload -> HTTP %s %s" % (e.code, detail))
        return (out.get("data", {}).get("files") or [None])[0]

    def create_plan(self, title, file_url):
        body = {"title": title, "type": "real", "url": file_url}
        resp = self.call("POST", "/project/plan", body)
        if self.dry:
            return "<new-plan-id>", "<project-id>"
        plan = resp.get("data", {}).get("plan", {})
        return plan.get("id"), plan.get("projectId")

    def shape_create(self, shapes):
        return self.call("POST", "/shape/change",
                         {"mode": "2D", "create": shapes, "update": [], "remove": []})

    def set_unit(self, unit):
        """PATCH /project {"unit": ...} — commits the project display unit.
        Captured immediately before the scale `shape/change`; the plan is only
        treated as scaled once the unit is committed alongside the scale shape."""
        return self.call("PATCH", "/project", {"unit": unit})

    def set_scale(self, scale_obj):
        """Activate a plan's scale. Creating the `scale` shape alone does NOT
        make InnerSpace recompute the plan's active metres/unit — that only
        happens when the shape is re-sent in the `update` array with a
        top-level `"type":"scale"` marker (captured from the real Set-Scale
        UI flow), preceded by the set_unit() PATCH. Without this the plan keeps
        prompting 'Set Scale'."""
        return self.call("POST", "/shape/change",
                         {"mode": "2D", "type": "scale",
                          "create": [], "update": [scale_obj], "remove": []})

    def shape_remove(self, shapes):
        return self.call("POST", "/shape/change",
                         {"mode": "2D", "create": [], "update": [],
                          "remove": [_remove_ready(s) for s in shapes]})

    def delete_plan(self, plan_id):
        self.n += 1
        if self.dry:
            print("\n[%d] DELETE %s/project/plan/%s?socketId=%s"
                  % (self.n, INNERSPACE_API, plan_id, self.sid))
            return
        self.http.request("DELETE", "%s%s/project/plan/%s?socketId=%s"
                          % (self.base, INNERSPACE_API, plan_id, self.sid))


def _multipart(boundary, field, filename, blob):
    ctype = "image/jpeg" if blob[:3] == b"\xff\xd8\xff" else "image/png"
    head = (
        "--%s\r\n"
        'Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
        "Content-Type: %s\r\n\r\n" % (boundary, field, filename, ctype)
    ).encode()
    tail = ("\r\n--%s--\r\n" % boundary).encode()
    return head + blob + tail


# --- catalog (product list + projectId + adopted device name->MAC) --------
def load_catalog(args, http_, base):
    if args.project_json:
        body = json.load(open(args.project_json))
        data = body.get("data", body)
    else:
        data = http_.get_json("%s%s/project?mode=2D" % (base, INNERSPACE_API)).get("data", {})
    products = data.get("products") or []
    pid = ((data.get("project") or {}).get("id")
           or (data.get("plans") or [{}])[0].get("projectId") or "<project-id>")
    # existing device shapes carry the real adopted MAC + title; build name->MAC
    # so imported APs (which Hamina exports WITHOUT MACs) resolve to real hardware.
    name_to_mac = {}
    for s in data.get("shapes") or []:
        if s.get("type") == "device":
            mac = ((s.get("meta") or {}).get("mac") or "").replace(":", "").upper()
            title = s.get("title") or ""
            if mac and title:
                name_to_mac[_norm(title)] = mac
    # existing plans by exact title -> [ids], for re-import-in-place (replace)
    plans_by_title = {}
    for pl in data.get("plans") or []:
        if pl.get("id"):
            plans_by_title.setdefault(pl.get("title") or "", []).append(pl["id"])
    # shapes grouped by plan, so replacing a plan also removes its shapes (else
    # deleting the plan orphans them -> duplicate APs in the device list)
    shapes_by_plan = defaultdict(list)
    for s in data.get("shapes") or []:
        if s.get("planId"):
            shapes_by_plan[s["planId"]].append(s)
    return pid, products, name_to_mac, plans_by_title, dict(shapes_by_plan)


def _plan_title(override, name, limit=32):
    """InnerSpace caps plan titles at 32 chars. Use --plan-title as-is (trimmed)
    if given; otherwise '<name> (imported)', truncating the name to fit."""
    if override:
        return override[:limit]
    suffix = " (imported)"
    room = limit - len(suffix)
    base = name if len(name) <= room else name[:room].rstrip("- ")
    return base + suffix


# --- orchestration -------------------------------------------------------
def run(args):
    project = parse_openintent(args.openintent)
    fps = project["floorplans"]
    print("parsed %d floorplan(s), %d wall(s), %d AP(s) from %s"
          % (len(fps), sum(len(f["walls"]) for f in fps),
             sum(len(f["aps"]) for f in fps), args.openintent))

    http_, base, need_login = None, "", not args.project_json or args.commit
    if need_login:
        base = args.host.rstrip("/")
        http_ = Http(verify=args.verify_tls)
        pw = args.password or getpass.getpass("password for %s: " % args.username)
        legacy_login(http_, base, args.username, pw)
        csrf = apply_csrf(http_)
        print("auth: X-CSRF-Token %s" % ("acquired" if csrf else "NOT FOUND (writes may 403)"))
    project_id, products, name_to_mac, plans_by_title, shapes_by_plan = load_catalog(
        args, http_, base)
    print("catalog: %d product(s), %d adopted device MAC(s); projectId=%s"
          % (len(products), len(name_to_mac), project_id))
    replace = not args.no_replace

    # resolve MAC-less imported APs to real adopted hardware by name (Hamina omits
    # MACs; InnerSpace places a device by matching its MAC to an adopted AP).
    n_matched = 0
    for fp in fps:
        for ap in fp["aps"]:
            if ap.get("mac_synth"):
                real = name_to_mac.get(_norm(ap["name"]))
                if real:
                    ap["mac"], ap["mac_synth"], ap["mac_matched"] = real, False, True
                    n_matched += 1
    if n_matched:
        print("matched %d AP(s) to adopted MACs by name" % n_matched)

    socket_id = str(uuid.uuid4())
    w = Writer(http_, base, socket_id, dry_run=not args.commit)
    print("\n=== %s ===" % ("COMMIT (writing to InnerSpace)" if args.commit
                            else "DRY-RUN (no writes; showing planned calls)"))

    skipped = []
    for fp in fps:
        title = _plan_title(args.plan_title, fp["name"])
        old_ids = plans_by_title.get(title, []) if replace else []
        verb = "replace" if old_ids else "new plan"
        print("\n--- floorplan '%s' -> %s '%s' (%gx%g px) ---"
              % (fp["name"], verb, title, fp["img_w"], fp["img_h"]))
        file_url = w.upload_image(fp["image_name"], fp["image_bytes"])
        plan_id, proj_id = w.create_plan(title, file_url)
        proj_id = proj_id if proj_id and proj_id != "<project-id>" else project_id

        # set the plan scale first so coverage renders accurately (no "Set Scale").
        # Two steps, mirroring the real UI: (1) create the `scale` shape on the
        # plan, then (2) re-send it via /shape/change with a top-level
        # "type":"scale" marker, which is what actually triggers InnerSpace to
        # recompute the plan's metres/unit.
        if fp.get("width_m"):
            sc = scale_shape(plan_id, proj_id, fp["img_w"],
                             fp["width_m"], fp["ceiling_m"])
            w.shape_create([sc])
            w.set_unit(args.unit)
            w.set_scale(sc)
        else:
            print("  (no metres dimension in the export -> scale left unset)")

        wall_misses: set = set()
        mat_counts: dict = {}
        walls = []
        for wl in fp["walls"]:
            variant = wall_variant(wl["wall_type"], wall_misses)
            mat_counts[variant] = mat_counts.get(variant, 0) + 1
            walls.append(wall_shape(
                plan_id, proj_id, variant,
                to_scene(*wl["start"], fp["img_w"], fp["img_h"]),
                to_scene(*wl["end"], fp["img_w"], fp["img_h"])))
        if walls:
            print("  wall materials: " + ", ".join(
                "%s×%d" % (v, n) for v, n in sorted(mat_counts.items())))
            if wall_misses:
                print("  (unrecognized wall label(s) defaulted to drywall: %s)"
                      % ", ".join(sorted(wall_misses)))
            w.shape_create(walls)

        # attenuation objects (obstacles: racks, cubicles, foliage, ...)
        atten_misses: set = set()
        acounts: dict = {}
        attens = []
        for ao in fp.get("atten") or []:
            variant = atten_variant(ao["material"], atten_misses)
            acounts[variant] = acounts.get(variant, 0) + 1
            attens.append(atten_shape(
                plan_id, proj_id, variant,
                [to_scene(px, py, fp["img_w"], fp["img_h"]) for px, py in ao["polygon"]]))
        if attens:
            print("  attenuation objects: " + ", ".join(
                "%s×%d" % (v, n) for v, n in sorted(acounts.items())))
            if atten_misses:
                print("  (unrecognized obstacle label(s) defaulted to cubicles: %s)"
                      % ", ".join(sorted(atten_misses)))
            w.shape_create(attens)

        devices = []
        for ap in fp["aps"]:
            pid = find_product_id(ap, products)
            if not pid:
                skipped.append("%s (%s)" % (ap["name"], ap["model_original"]))
                continue
            devices.append(device_shape(
                plan_id, proj_id, ap, pid,
                to_scene(ap["x"], ap["y"], fp["img_w"], fp["img_h"])))
        if devices:
            n_synth = sum(1 for ap in fp["aps"] if ap.get("mac_synth"))
            print("  devices: %d AP(s)%s" % (
                len(devices),
                " (%d with synthesized placeholder MAC)" % n_synth if n_synth else ""))
            w.shape_create(devices)

        # re-import in place: only AFTER the new plan is fully written do we delete
        # the old same-titled plan(s), so a mid-run failure never loses your data
        # (also sweeps up duplicates accumulated from earlier runs).
        if old_ids:
            print("  replacing %d existing plan(s) titled '%s'" % (len(old_ids), title))
            for oid in old_ids:
                old_shapes = shapes_by_plan.get(oid, [])
                if old_shapes:                 # remove its shapes first, else the
                    w.shape_remove(old_shapes)  # plan delete orphans them
                w.delete_plan(oid)

    print("\n=== %d call(s) %s ===" % (w.n, "sent" if args.commit else "previewed"))
    if skipped:
        print("skipped %d AP(s) with no InnerSpace product match: %s"
              % (len(skipped), ", ".join(skipped)))
        print("  -> add the model->productId mapping (see products[] in the catalog)")


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("openintent", help="OpenIntent 2.0 .zip exported from Hamina")
    p.add_argument("--host", default="", help="console URL, e.g. https://192.168.1.1")
    p.add_argument("--username", default="")
    p.add_argument("--password", default="")
    p.add_argument("--verify-tls", dest="verify_tls", action="store_true", default=False)
    p.add_argument("--no-verify-tls", dest="verify_tls", action="store_false")
    p.add_argument("--project-json", help="saved /project dump (offline dry-run catalog)")
    p.add_argument("--plan-title", help="override the new plan title")
    p.add_argument("--unit", choices=["imperial", "metric"], default="imperial",
                   help="project display unit committed when setting scale "
                        "(default: imperial)")
    p.add_argument("--no-replace", action="store_true",
                   help="always create a new plan; do NOT replace/refresh an "
                        "existing plan with the same title (old behavior)")
    p.add_argument("--commit", action="store_true",
                   help="actually write (default is a dry-run preview)")
    args = p.parse_args()
    if args.commit and not args.host:
        p.error("--commit needs --host / --username / --password")
    if not args.project_json and not args.host:
        p.error("provide --project-json (offline) or --host (live) for the catalog")
    try:
        run(args)
    except RuntimeError as e:
        print("error:", e, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
