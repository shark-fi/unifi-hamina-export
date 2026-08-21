"""Sanity-check the wall segments in an OpenIntent zip before importing it.

Reads every floor plan in the archive and reports, per plan:

  * the plan's pixel dimensions and the x/y range the segments actually span,
    so geometry that has drifted out of the drawing is obvious;
  * endpoints outside the plan bounds, zero-length segments, and exact
    duplicates — the three shapes Hamina renders badly or silently drops;
  * a tally of every `wall_type` string in use.

That last count is the one to read first. Hamina discards wall types it does not
recognise, without a warning, so a label that is merely plausible comes back as
no wall at all. Check the tally against a zip Hamina itself exported *recently* —
not against the names in its wall picker, and not against an older export: the
vocabulary has changed between versions before now.

Note the pixel dimension is `length`, not `height`; `height` is the ceiling.

    python3 wallcheck.py path/to/openintent.zip     # defaults to /tmp/oi.zip
"""

import zipfile, json, sys, math, collections
z = zipfile.ZipFile(sys.argv[1] if len(sys.argv) > 1 else "/tmp/oi.zip")
docs = [json.loads(z.read(n)) for n in z.namelist() if n.endswith(".json")]

def find(o, key, out):
    if isinstance(o, dict):
        if key in o and isinstance(o[key], list): out.append(o)
        for v in o.values(): find(v, key, out)
    elif isinstance(o, list):
        for v in o: find(v, key, out)

plans = []
for d in docs: find(d, "wall_segments", plans)
for fp in plans:
    segs = fp["wall_segments"]
    xs = [p[k]["x"] for p in segs for k in ("start_point", "end_point")]
    ys = [p[k]["y"] for p in segs for k in ("start_point", "end_point")]
    dims = fp.get("dimensions") or []
    px = next((d for d in dims if d.get("unit") == "pixels"), {})
    zero = sum(1 for s in segs
               if math.dist((s["start_point"]["x"], s["start_point"]["y"]),
                            (s["end_point"]["x"], s["end_point"]["y"])) < 0.5)
    outside = sum(1 for s in segs for k in ("start_point", "end_point")
                  if not (0 <= s[k]["x"] <= (px.get("width") or 1e9)
                          and 0 <= s[k]["y"] <= (px.get("length") or 1e9)))
    dup = len(segs) - len({(round(s["start_point"]["x"]), round(s["start_point"]["y"]),
                            round(s["end_point"]["x"]), round(s["end_point"]["y"]))
                           for s in segs})
    print(f"\n{fp.get('name')}: {len(segs)} segments")
    print(f"  plan px   : {px.get('width')} x {px.get('length')}")
    print(f"  x range   : {min(xs):.1f} .. {max(xs):.1f}")
    print(f"  y range   : {min(ys):.1f} .. {max(ys):.1f}")
    print(f"  endpoints outside the plan: {outside}")
    print(f"  zero-length segments      : {zero}")
    print(f"  exact duplicate segments  : {dup}")
    print(f"  labels: {dict(collections.Counter(s['wall_type'] for s in segs))}")
