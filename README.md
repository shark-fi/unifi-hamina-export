# unifi-hamina-export

Export UniFi access-point data — including floor plans, AP placements and wall
geometry — into an [OpenIntent 2.0](https://github.com/google/openintent) zip
that [Hamina Network Planner](https://www.hamina.com/) imports directly.

Hamina has no public write API and no UniFi integration (it supports Mist,
Meraki, Aruba, Arista, Extreme, Ruckus and Catalyst Center). OpenIntent is the
supported way in: Hamina imports floor plans with scale, AP locations, models,
channels and transmit power from an OpenIntent zip.

Single file, standard library only, no dependencies.

## Quick start

```bash
python3 unifi_export.py innerspace --host https://192.168.1.1 -u <local-admin> \
    --openintent hq.zip -o aps.csv
```

Then import `hq.zip` into Hamina as an OpenIntent file.

Use a **local admin account** (UniFi: Admins & Users → "Restrict to local
access only"). A ui.com cloud account hits MFA and cannot log in from a script.

## Modes

| Mode | Source | Gives you |
|---|---|---|
| `innerspace` | UniFi InnerSpace app (`/proxy/innerspace`) | **Floor plans, AP placements, walls** — the one you want |
| `legacy` | Internal controller API | Classic Maps positions + radio state (older Network versions only) |
| `local` | Official Network Integration API | AP inventory per site |
| `cloud` | Site Manager API (`api.ui.com`) | AP inventory across cloud-connected consoles |
| `probe` | — | Diagnostics: which floor-plan APIs a console exposes |

`innerspace` also joins the Network app by MAC to fill in live channel and TX
power per radio (`--no-radio` to skip). `--no-walls` omits wall segments;
`--plan NAME` limits export to matching floors.

`legacy` mode works only on Network versions that still have classic Maps.
Newer consoles return `api.err.InvalidObject` / `api.err.NotFound` — use
`innerspace`. `--unplaced` exports APs with radio config onto a placeholder
grid when no floor plans exist at all.

## How InnerSpace works

Undocumented, reverse-engineered from the InnerSpace UI bundle. Read-only use
only; it can change with any InnerSpace release.

InnerSpace is a separate controller app (port 17080, `apiPrefix:
/proxy/innerspace/`). Its UI is not served from the console — it loads from
Ubiquiti's public CDN, which `GET /api/system` reveals:

```json
{"type":"controller","port":17080,
 "ui":{"baseUrl":"/innerspace/","apiPrefix":"/proxy/innerspace/"},
 "uiCdn":"https://cdn.pkg.svc.ui.com/innerspace-ui/1.3.16-1d05615764",
 "uiVersion":"1.3.16"}
```

`GET <uiCdn>/swai.js` (~10 MB, no auth) contains the API layer.

### The API

`GET /proxy/innerspace/api/project?mode=2D` returns the entire project.
Others: `/api/project/plan{,/upload,/order}`, `/api/project/wall-type`,
`/api/project/attenuation-object-type`, `/api/stats`, and `/api/shape/change`
(a write endpoint — this tool never calls it).

### Data model

- `plans[]` — one per floor (`real` / `sample` / `blank` / `geo`)
- `shapes[]` — discriminated by `.type`, sharing `planId` + `position[]`:
  - `map` — floor-plan image (`urlImage`), offset, scale
  - `scale` — two points + real-world length; `.height` = ceiling height (m)
  - `device` — `productId`, `meta.mac`/`ip`, position, `mount`, rotation
  - `wall` — two points + `variant` (concrete/drywall/glass/metal/door_metal)
- `products[]` — `productId` → `sku` (`U7-Pro-Max`) and `category` (`wifi`)

Devices with `planId: null` are unplaced and skipped.

### Coordinates

Scene units **are image pixels, with the origin at the image centre and y
pointing up**. Converting to OpenIntent (origin top-left, y down):

```
x_px = (x - map.position.x) / map.scale.x + image_width / 2
y_px = image_height / 2 - (y - map.position.y) / map.scale.y
```

Metres per pixel comes from the user's scale line:

```
m_per_unit = scale.scale / distance(scale.position[0], scale.position[1])
m_per_px   = m_per_unit * map.scale.x
```

## Output

The zip holds `openintent.json` plus `images/` (floor-plan images, referenced
as `file://images/...`). Output validates against the official OpenIntent 2.0
schema. Zip conventions follow
[oiconvert](https://github.com/yourwificz/oiconvert), the community Ekahau →
OpenIntent converter tested against Hamina's importer.

The CSV alongside it carries name, model, MAC, IP, pixel and metre
coordinates, and per-band channel / TX power.

## Reverse: import Hamina → UniFi InnerSpace

`openintent_import.py` goes the other way: it reads a Hamina OpenIntent zip and
writes it into an InnerSpace project — floor-plan image, walls (with materials),
AP placements, and real-world scale. Full write-API reference:
[`docs/INNERSPACE_WRITE_API.md`](docs/INNERSPACE_WRITE_API.md).

```bash
# offline dry-run — parse + preview the exact API calls, no console needed
python3 openintent_import.py home.zip --project-json innerspace_project.json

# live dry-run against the console (fetches the product catalog itself)
python3 openintent_import.py home.zip --host https://192.168.1.1 \
    --username <local-admin> --no-verify-tls

# actually write it in
python3 openintent_import.py home.zip --host https://192.168.1.1 \
    --username <local-admin> --no-verify-tls --commit
```

It defaults to a **dry-run** (prints the calls it *would* make); nothing is
written until `--commit`. Same local-admin login note as above. Writes need a
CSRF token, taken automatically from the login cookie.

### How APs resolve — name *and* model do different jobs

Hamina's OpenIntent export carries no MACs, and InnerSpace places a device only
by matching an **adopted** device's MAC. So the importer uses two keys:

| Aspect | Resolved by | If no match |
|---|---|---|
| Which physical AP it binds to, and placement | AP **name** vs your adopted UniFi devices (case/punctuation-insensitive) | placed with a synthesized placeholder MAC — shown, but not tied to live hardware |
| Product type (icon, radios, antenna) | AP **model** vs the InnerSpace product catalog (with alias + `-internal`/`-external` suffix stripping) | AP is **skipped** — can't place without a product id |

Practical upshot: **keep your Hamina AP names identical to the UniFi device
names** and they bind to the real APs; the model just has to exist in the
InnerSpace catalog. The run prints how many matched (`matched N AP(s) to adopted
MACs by name`) and lists anything skipped.

### Walls, scale, re-import

- **Walls** carry their material: each OpenIntent `wall_type` maps to an
  InnerSpace variant (concrete, drywall, drywall_heavy, door_wood/glass/metal,
  window, brick, …); unrecognized labels default to drywall with a warning.
- **Scale** is set automatically from the export's metre dimensions, so plans
  come in pre-scaled (no "Set Scale" prompt). `--unit imperial|metric` sets the
  project display unit (default imperial).
- **Re-import in place** is the default: re-running replaces the previous
  same-titled plan(s) *after* the new one is fully written (a mid-run failure
  never loses data), which also sweeps up duplicates from earlier runs. Pass
  `--no-replace` to always create a fresh plan instead.

### Obstacles side-car

Hamina's export omits obstacle geometry (walls + materials only), so obstacles
(cars, shelving, appliances, foliage, …) are supplied in an optional side-car
JSON and placed as attenuation objects:

```bash
# scaffold a starter side-car with your floor names + dimensions
python3 openintent_import.py home.zip --dump-obstacle-template obstacles.json
# ...edit it, then place them on import
python3 openintent_import.py home.zip --host … --commit --obstacles obstacles.json
```

Each entry takes a `floorplan`, a `material` (many plain-English aliases
resolve), a `unit` (`pixels` or `meters`, from the image top-left), and either a
`rect {cx,cy,w,h}` or an explicit `polygon`. See
[`obstacles.example.json`](obstacles.example.json) and the write-API doc for the
full format.

## Notes

- Wall attenuation values in `WALL_VARIANTS` are defaults; Hamina maps wall
  types onto its own library, so the label matters more than the number.
- Ceiling-mounted APs use the plan's own ceiling height; others use
  `--ap-height` (default 2.5 m).
- TLS verification is off by default for local consoles (self-signed certs).
  `--verify-tls` enables it.
- Every call this tool makes is a GET.
