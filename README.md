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

`innerspace` also joins the Network app by MAC to fill in live channel, width
and TX power per radio (`--no-radio` to skip). `--no-walls` omits wall segments;
`--no-switches` omits switches; `--plan NAME` limits export to matching floors.

Switches placed on a floor plan export to OpenIntent's `switches[]` alongside
the APs, carrying position, model, IP, serial, copper and SFP port counts and
PoE budget. Only `name` is required by the schema, so a switch that the Network
app join doesn't match still exports as a positioned node. Cameras, door access
and other InnerSpace gear are not exported.

Each AP also carries `connected_switch` — `switch_name`, `switch_id` (the
uplink MAC) and `port` — read from its wired uplink in the Network app, giving
Hamina the AP-to-switch-port topology. `switch_name` resolves against
`switches[]` wherever the switch is placed, falling back to the Network app's
own label otherwise. A meshed AP gets no `connected_switch`: it has no switch
port, and inventing one would be worse than omitting it. This is AP metadata,
so it survives `--no-switches`.

`legacy` mode works only on Network versions that still have classic Maps.
Newer consoles return `api.err.InvalidObject` / `api.err.NotFound` — use
`innerspace`. `--unplaced` exports APs with radio config onto a placeholder
grid when no floor plans exist at all.

### Radios: live state, not configured intent

Radios come from the device's `radio_table_stats` (what is actually on air),
not `radio_table` (what was configured). The two disagree in ways that matter
to a prediction:

| | `radio_table` (configured) | `radio_table_stats` (live) |
|---|---|---|
| Width | requested `ht` — 80 MHz | operating `bw` — routinely 40 MHz |
| Channel | `"auto"` until RRM resolves it | the resolved number |
| TX power | equals `min_txpower`, the floor | the power actually in use |

On a real site every 5 GHz radio requested 80 MHz and ran at 40, and an
AFC standard-power outdoor AP ran 26 dBm where the configured value said 6 —
a 20 dB error in the direction that hides coverage holes.

The configured table is still used for `mimo_chains` (`nss`), and as a fallback
for channel and width when a device is offline and has no live state at all —
never for TX power, since the configured value is the floor and would silently
understate every radio.

A radio whose live `state` is not `RUN` (a disabled or wedged radio, typically
2.4 GHz) is **dropped**, with a warning naming it, so the plan doesn't predict
coverage that isn't there. `--include-down-radios` keeps them; the flag is
available on both `legacy` and `innerspace`.

`channel_assignment: AUTOMATIC` is emitted only when the controller still says
`"auto"`. Once RRM settles it writes the number back into `radio_table`, so an
int there can't distinguish a hand-pinned radio from a settled auto one — the
field is omitted rather than asserting `MANUAL` on a guess and stopping Hamina
from re-optimising.

## How InnerSpace works

Undocumented, reverse-engineered from the InnerSpace UI bundle; it can change
with any InnerSpace release. The exporter only ever reads. The optional
importer (below) writes, but only when you pass `--commit`.

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
(a write endpoint — used only by `openintent_import.py`, and only with
`--commit`; `unifi_export.py` never calls it).

### Data model

- `plans[]` — one per floor (`real` / `sample` / `blank` / `geo`)
- `shapes[]` — discriminated by `.type`, sharing `planId` + `position[]`:
  - `map` — floor-plan image (`urlImage`), offset, scale
  - `scale` — two points + real-world length; `.height` = ceiling height (m)
  - `device` — `productId`, `meta.mac`/`ip`, position, `mount`, rotation
  - `wall` — two points + `variant` (14 built-ins: concrete, drywall,
    drywall_heavy, glass, glass_thin, brick, metal, wood, door_wood,
    door_metal, door_glass, window_{1,2,3}_pane)
- `products[]` — `productId` → `sku` (`U7-Pro-Max`) and `category` (`wifi`)

Devices with `planId: null` are unplaced and skipped.

### Coordinates

Scene units **are image pixels, with the origin at the image centre and y
pointing up**. OpenIntent pixel coordinates keep y pointing up as well,
measured from the **bottom-left** corner — so converting only re-centres, with
no y-flip:

```
x_px = (x - map.position.x) / map.scale.x + image_width / 2
y_px = (y - map.position.y) / map.scale.y + image_height / 2
```

Three independent things confirm y is up-from-the-bottom rather than
down-from-the-top, which is worth recording because the opposite reading looks
just as plausible and both round-trip cleanly:

- an AP exported at `y = 0.63 × height` is physically in a room at the **top**
  of the plan — i.e. 0.37 down from the top
- the live-map renderer had to apply `y → height - y` before plotting into an
  SVG, which measures y down
- the `legacy` path converts classic Maps positions, which are top-left/y-down
  image pixels, with an explicit `y = image_height - y`

The **obstacle side-car** (`--obstacles`) is the deliberate exception: it is
authored by hand against the image, so its coordinates are top-left/y-down and
the importer flips them on the way in.

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

Wall segments carry a `wall_type` spelled the way Hamina spells it — `Drywall
(Heavy)`, `Door (Wooden)`, `Window` — because Hamina **drops** a segment whose
type it doesn't recognise rather than falling back to a default. `WALL_VARIANTS`
covers all 14 InnerSpace built-in variants using labels checked against
Hamina's own wall-type picker; anything outside it warns loudly instead of
exporting a guessed label.

A **custom** InnerSpace wall type (`variant: "custom"` plus a `wallTypeId`)
exports under its own name, so a wall drawn as `Fireplace` in Hamina, imported
as a matching custom type, comes back as `Fireplace`. Hamina types that get
mapped onto a built-in variant on the way in can only return as that built-in:
`Railing` and `Cubicle` → `Drywall`, `Elevator` → `Metal`, `Window (Tinted)` →
`Window`. Drawing them as custom types in InnerSpace avoids the loss.

The CSV alongside it carries name, model, MAC, IP, pixel and metre
coordinates, and five live columns per band (`_2g` / `_5g` / `_6g`):
`ch_*` channel, `bw_*` operating width, `txpw_*` TX power, `sta_*` associated
clients, and `rstate_*` — `RUN` when the radio is on air, `INIT` or blank when
it is configured but not serving.

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

- Wall attenuation values in `WALL_VARIANTS` are defaults and Hamina uses its
  own library anyway, so the label matters and the number does not — but the
  label has to match Hamina's spelling exactly, because an unrecognised
  `wall_type` is dropped, not defaulted.
- Ceiling-mounted APs use the plan's own ceiling height; others use
  `--ap-height` (default 2.5 m).
- TLS verification is off by default for local consoles (self-signed certs).
  `--verify-tls` enables it.
- Every call `unifi_export.py` makes is a GET. Writes happen only in
  `openintent_import.py`, and only when you pass `--commit`.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Stdlib only, no network, no console. They cover the two things that go wrong
here quietly — radios exported from configured intent rather than live state,
and wall types spelled in a vocabulary Hamina doesn't accept. Both shipped once
looking like clean exports, because the AP and wall *counts* were right and
only the values were wrong.

## Disclaimer

This is an independent, unofficial project. It is not affiliated with,
endorsed by, or supported by Ubiquiti Inc. or Hamina Wireless Oy. UniFi,
InnerSpace and Hamina are trademarks of their respective owners.

The InnerSpace endpoints and data model documented here are **undocumented
internal APIs**, determined by observing the InnerSpace web application in
order to interoperate with it. They carry no stability guarantee and may
change or disappear in any UniFi release — if an update breaks this tool,
that is expected, not a defect on Ubiquiti's part.

The exporter (`unifi_export.py`) only ever issues HTTP GET requests. It reads
floor plans, device placements and radio state; it never creates, modifies or
deletes anything on a console.

The importer (`openintent_import.py`) is the one component that writes, and
only to bring a Hamina floor plan back into InnerSpace. It defaults to a
**dry-run** that prints the exact calls it would make and writes nothing; a
write happens only when you pass `--commit`. Its writes are confined to
InnerSpace floor plans — creating a plan and its wall / device / obstacle
shapes, and replacing a plan it previously created with the same title. It
does not touch device configuration, network settings or anything outside the
floor plan it is importing.

Use it on equipment you own or are authorised to administer. It requires
credentials for that console and offers no way to reach one you cannot
already log in to. As stated in the [license](LICENSE), the software is
provided "as is", without warranty of any kind.

## License

[MIT](LICENSE) © 2026 Mark Houtz
