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
import io
import json
import sys
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
# OpenIntent wall_type label -> InnerSpace variant (reverse of WALL_VARIANTS).
WALL_LABEL_TO_VARIANT = {label: variant for variant, (label, _att) in WALL_VARIANTS.items()}
# model (as the exporter emits it) -> InnerSpace SKU (reverse of the export aliases).
MODEL_TO_SKU = {model: sku for sku, model in INNERSPACE_SKU_ALIASES.items()}
# OpenIntent band label -> nothing needed on import; radios are InnerSpace-derived.

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
def parse_openintent(zip_path: str) -> dict:
    """Return {floorplans:[{name,image_name,image_bytes,img_w,img_h,walls,aps}]}.

    walls: [{wall_type,start:(x,y),end:(x,y)}]   (pixel coords)
    aps:   [{name,model,model_original,mac,x,y}] (pixel coords)
    """
    zf = zipfile.ZipFile(zip_path)
    data = json.loads(zf.read("openintent.json"))
    by_name: dict[str, dict] = {}
    for fp in data.get("floorplans", []):
        name = fp.get("name") or "Plan"
        dims = fp.get("dimensions") or [{}]
        px = next((d for d in dims if d.get("unit") == "pixels"), dims[0])
        img_w = float(px.get("width") or 0)
        img_h = float(px.get("length") or 0)
        image_name, image_bytes = _read_image(zf, fp)
        by_name[name] = {
            "name": name, "image_name": image_name, "image_bytes": image_bytes,
            "img_w": img_w, "img_h": img_h,
            "walls": [_wall(w) for w in (fp.get("wall_segments") or [])],
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
        target["aps"].append({
            "name": ap.get("name") or "AP",
            "model": (ap.get("model") or "").lower(),
            "model_original": ap.get("model_original") or ap.get("model") or "",
            "mac": (ap.get("mac_address") or "").replace(":", "").upper(),
            "x": px[0], "y": px[1],
        })
    return {"floorplans": list(by_name.values())}


def _read_image(zf, fp):
    uri = fp.get("map_uri") or ""
    rel = uri.split("file://", 1)[-1] if uri.startswith("file://") else uri
    if rel and rel in zf.namelist():
        return rel.split("/")[-1], zf.read(rel)
    return None, None


def _wall(w):
    return {
        "wall_type": w.get("wall_type") or "",
        "start": (float(w["start_point"]["x"]), float(w["start_point"]["y"])),
        "end": (float(w["end_point"]["x"]), float(w["end_point"]["y"])),
    }


def _ap_pixel(ap):
    for c in ap.get("coordinates") or []:
        xyz = c.get("coordinate_xyz") or {}
        if xyz.get("unit") == "pixels":
            return float(xyz["x"]), float(xyz["y"])
    return None


# --- Phase 3: map to InnerSpace ------------------------------------------
def to_scene(px: float, py: float, img_w: float, img_h: float) -> tuple[float, float]:
    """Invert scene_to_pixels for a fresh plan (identity scale, zero offset):
    pixel = scene + img/2  ->  scene = pixel - img/2.  No y-flip (matches export)."""
    return round(px - img_w / 2.0, 4), round(py - img_h / 2.0, 4)


def wall_variant(wall_type: str) -> str:
    """OpenIntent wall label -> InnerSpace variant."""
    if wall_type in WALL_LABEL_TO_VARIANT:
        return WALL_LABEL_TO_VARIANT[wall_type]
    guess = wall_type.strip().lower().replace(" ", "_")
    return guess or "drywall"


def find_product_id(ap: dict, products: list) -> str | None:
    """Map an OpenIntent AP model to an InnerSpace catalog productId."""
    wanted = {
        (ap.get("model_original") or "").lower(),
        (ap.get("model") or "").lower(),
        MODEL_TO_SKU.get(ap.get("model") or "", "").lower(),
    }
    wanted.discard("")
    for p in products:
        for field in ("sku", "name", "shortname", "title", "abbrev", "model"):
            v = str(p.get(field) or "").lower()
            if v and v in wanted:
                return p.get("id")
    return None


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


def _multipart(boundary, field, filename, blob):
    ctype = "image/jpeg" if blob[:3] == b"\xff\xd8\xff" else "image/png"
    head = (
        "--%s\r\n"
        'Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
        "Content-Type: %s\r\n\r\n" % (boundary, field, filename, ctype)
    ).encode()
    tail = ("\r\n--%s--\r\n" % boundary).encode()
    return head + blob + tail


# --- catalog (product list + projectId) ----------------------------------
def load_catalog(args, http_, base):
    if args.project_json:
        body = json.load(open(args.project_json))
        data = body.get("data", body)
        products = data.get("products") or []
        pid = ((data.get("project") or {}).get("id")
               or (data.get("plans") or [{}])[0].get("projectId"))
        return pid or "<project-id>", products
    data = http_.get_json("%s%s/project?mode=2D" % (base, INNERSPACE_API)).get("data", {})
    products = data.get("products") or []
    pid = ((data.get("project") or {}).get("id")
           or (data.get("plans") or [{}])[0].get("projectId"))
    return pid, products


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
    project_id, products = load_catalog(args, http_, base)
    print("catalog: %d product(s); projectId=%s" % (len(products), project_id))

    socket_id = str(uuid.uuid4())
    w = Writer(http_, base, socket_id, dry_run=not args.commit)
    print("\n=== %s ===" % ("COMMIT (writing to InnerSpace)" if args.commit
                            else "DRY-RUN (no writes; showing planned calls)"))

    skipped = []
    for fp in fps:
        title = args.plan_title or ("%s (imported)" % fp["name"])
        print("\n--- floorplan '%s' -> new plan '%s' (%gx%g px) ---"
              % (fp["name"], title, fp["img_w"], fp["img_h"]))
        file_url = w.upload_image(fp["image_name"], fp["image_bytes"])
        plan_id, proj_id = w.create_plan(title, file_url)
        proj_id = proj_id if proj_id and proj_id != "<project-id>" else project_id

        walls = [wall_shape(plan_id, proj_id, wall_variant(wl["wall_type"]),
                            to_scene(*wl["start"], fp["img_w"], fp["img_h"]),
                            to_scene(*wl["end"], fp["img_w"], fp["img_h"]))
                 for wl in fp["walls"]]
        if walls:
            w.shape_create(walls)

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
            w.shape_create(devices)

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
