# UniFi InnerSpace write API (reverse-engineered)

Phase 1 recon for the OpenIntent → InnerSpace importer (issue #2). Captured from
a real InnerSpace session via HAR (create plan, upload image, place/move devices,
draw walls of every material, add attenuation objects, custom types, reorder,
delete plan).

- **Base:** `/proxy/innerspace/api`
- **Auth:** cookie + `X-CSRF-Token` (same as the read path / `legacy_login`).
- **`?socketId=<uuid>`** query param on mutating calls — the caller's websocket
  session id. The HTTP response echoes **empty** `create/remove/update` arrays;
  the persisted result is broadcast over the websocket, so **verify by re-GETting
  `/project?mode=2D`**, not by reading the POST response.

## Create a floor plan (image + plan)

1. `POST /project/plan/upload?socketId=…` — `multipart/form-data`, field name
   **`2D`** = the PNG file.
   → `{"data":{"files":["/proxy/innerspace/<proj>/<uuid>.png"]}}`
2. `POST /project/plan?socketId=…` —
   `{"title": "...", "type": "real", "url": "<uploaded file url from step 1>"}`
   → `{"data":{"plan":{id,title,type,ordering,projectId,…},
              "shapes":[{type:"map", urlImage, scale:{x:1,y:1,z:1}, opacity:0.7, …}]}}`
   Creating the plan **auto-creates the `map` shape** referencing the image.

## The workhorse: `POST /shape/change?socketId=…`

Body: `{"mode":"2D", "create":[…], "update":[…], "remove":[…]}` — each array holds
shape objects. Used for devices, walls, attenuation objects, and map-shape edits.
Move/edit = put the full shape (new `position`) in `update`; add = `create`;
delete = `remove`.

### Shape: `wall`
```json
{ "id": "<uuid>", "planId": "<plan>", "projectId": "<proj>",
  "type": "wall", "variant": "concrete",
  "position": [ {"x":.., "y":.., "z":0}, {"x":.., "y":.., "z":0} ],
  "status": 0, "createdAt": "<iso>", "updatedAt": "<iso>" }
```
Built-in `variant` values (richer than the exporter's current `WALL_VARIANTS`):
`concrete`, `drywall`, `drywall_heavy`, `glass`, `glass_thin`, `brick`, `metal`,
`wood`, `door_wood`, `door_metal`, `door_glass`,
`window_1_pane`, `window_2_pane`, `window_3_pane`.
Custom wall → `"variant":"custom"` + `"wallTypeId":"<id>"` (see custom types).

### Shape: `device` (AP / client)
```json
{ "id": "<uuid>", "planId": "<plan>", "projectId": "<proj>",
  "type": "device", "title": "U7-Pro-Bedroom",
  "productId": "<innerspace catalog product id>",
  "meta": {"ip": "192.168.5.209", "mac": "9C05D6AEFFDC"},
  "mount": "ceiling",                         // or "wall" | "floor"
  "position": [ {"x":.., "y":.., "z":0} ],
  "rotation": { "base": {"w","x","y","z"}, "pov": {"w","x","y","z"} },
  "antenna": null, "parentId": null, "childIndex": null,
  "rackId": null, "rackOrder": null, "optimize": false,
  "status": 1, "createdAt": "<iso>", "updatedAt": "<iso>" }
```
- `productId` is the InnerSpace catalog id — maps the AP **model**. Read the
  catalog from `GET /project` → `products[]` (id + sku/name).
- `status`: `1` for adopted/real devices, `0`/`3` seen for others.
- Identity vs. real hardware is carried in `meta.mac`.

### Shape: `attenuationObject`
```json
{ "id": "<uuid>", "planId": "<plan>", "projectId": "<proj>",
  "type": "attenuationObject", "variant": "server_rack",
  "position": [ …closed polygon of {x,y,z}… ], "status": 0 }
```
Built-in variants: `car`, `cubicles`, `elevator`, `foliage_heavy`, `foliage_light`,
`human_crowd`, `machinery`, `server_rack`, `shelf_small`, `shelf_medium`,
`shelf_warehouse`, `truck`. Custom → `"variant":"custom"` + `"attenuationObjectTypeId"`.

## Custom types

- `POST /project/wall-type?socketId=…` —
  `{"create":[…], "update":[…], "remove":[]}` of wall-type objects:
  ```json
  { "id":"<uuid>", "name":"Test", "attenuation":8, "color":"#37f74e",
    "thicknessM":0.2, "thicknessPx":4, "topHeight":2.44, "bottomHeight":null,
    "transparent":true, "autoFillEnabled":true, "variant":"custom",
    "variantKey":"custom-<id>", "isCustom":true, "isDeleted":false,
    "renderOrder":0, "projectId":"<proj>" }
  ```
- `POST /project/attenuation-object-type?socketId=…` — same envelope, object
  fields: `id,name,attenuation,color,topHeight,bottomHeight,variant:"custom",
  variantKey,isCustom,isDeleted,renderOrder,projectId`.

## Obstacle side-car (`--obstacles`)

Hamina's OpenIntent export writes only walls + wall materials — never obstacle
geometry — so `openintent_import.py` accepts an optional side-car JSON of
obstacle/attenuation objects, merged onto the matching floorplan at import and
placed as `attenuationObject` shapes. See `obstacles.example.json`.

```json
{ "obstacles": [
  { "floorplan": "Basement", "material": "car",
    "unit": "meters", "rect": { "cx": 3.0, "cy": 5.0, "w": 2.0, "h": 4.5 } },
  { "floorplan": "Basement", "material": "metal shelving",
    "unit": "pixels", "polygon": [[120,80],[240,80],[240,160],[120,160]] }
] }
```

- `floorplan` — name as it appears in the export; omit if the export is single-floor.
- `material` — mapped to a built-in variant (above); many plain-English aliases
  resolve (`wardrobe`/`fridge`/`rack`/…), unknown → `cubicles` with a warning.
- `unit` — `pixels` (default) or `meters`, both from the image **top-left**
  corner, x right / y down — the axes you read off an image editor, *not* the
  bottom-left/y-up axes `wall_segments` use; the importer flips y on the way in.
  Metres are scaled by the floorplan's own px/m.
- geometry — a `polygon` (`[[x,y],…]` or `[{x,y},…]`, ≥3 pts) or axis-aligned
  `rect` `{cx,cy,w,h}`. A bare top-level list is also accepted.

Generate a starter side-car (correct floor names + each floor's pixel/metre
extents, one example `car` per floor) with
`openintent_import.py <export.zip> --dump-obstacle-template [OUT]` (OUT path, or
omit for stdout) — offline, no console needed.

## Plan management

- `PATCH /project/plan/order?socketId=…` — `[{"id":"<plan>","ordering":0}, …]`
- `DELETE /project/plan/<planId>?socketId=…` → returns the remaining `plans[]`.

## Coordinates

`position` is in InnerSpace **scene units** (image-centre origin), identical to the
read path. Invert `scene_to_pixels`:
`scene = (pixel - img/2) * scale + offset` — **no y-flip** (matches the verified
export direction).

## Open items for the writer (Phase 4)

1. **socketId** — obtain a real websocket session id, or test whether a random /
   omitted value is accepted (writes may still persist; the id is mainly for
   change broadcast/echo-suppression).
2. **Verify writes** by re-GETting `/project?mode=2D` (POST responses are empty).
3. **productId mapping** — build OpenIntent `model` → InnerSpace `productId` from
   the `products[]` catalog (reverse of `INNERSPACE_SKU_ALIASES`).
4. **Scale** — RESOLVED (captured from the real Set-Scale UI flow). A plan's
   `scale` shape must be **activated**, not merely created: re-send it via
   `POST /shape/change` with a **top-level `"type":"scale"`** marker in the
   `update` array — `{"mode":"2D","type":"scale","create":[],"update":[<scale
   shape>],"remove":[]}`. Creating the shape alone leaves the plan prompting
   "Set Scale"; the top-level `type` is what makes InnerSpace recompute the
   plan's metres/unit. A cosmetic `PATCH /project {"unit":"imperial"|"metric"}`
   fires alongside it in the UI but is not required for the scale to take.
5. **Safety** — write to a **new** plan by default, `--dry-run` first, never
   clobber an existing plan.
