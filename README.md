# unifi-hamina-export

Two standalone stdlib-only tools for a UniFi wireless estate:

- **`unifi_export.py`** — UniFi floor plans, AP placements and walls into an
  OpenIntent zip that Hamina Network Planner imports directly.
- **`radius_userid.py`** — FreeRADIUS/eduroam logins into Palo Alto User-ID,
  so firewall policy can match `UNI\jdoe` instead of an address.
  ([guide](docs/palo-alto-user-id.md))

## unifi_export.py

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

## Notes

- Wall attenuation values in `WALL_VARIANTS` are defaults; Hamina maps wall
  types onto its own library, so the label matters more than the number.
- Ceiling-mounted APs use the plan's own ceiling height; others use
  `--ap-height` (default 2.5 m).
- TLS verification is off by default for local consoles (self-signed certs).
  `--verify-tls` enables it.
- Every call `unifi_export.py` makes is a GET.

## radius_userid.py — eduroam identities in Palo Alto policy

The firewall never sees an 802.1X exchange, so it cannot know who is behind
an address. RADIUS accounting does. This bridge reads FreeRADIUS accounting
and pushes login/logout entries into PAN-OS through the User-ID XML API.

```bash
python3 radius_userid.py run \
    --detail '/var/log/freeradius/radacct/userid/detail-*' \
    --firewall https://fw1.example.edu --api-key-file /etc/radius-userid/api.key \
    --realm-map example.edu=EXAMPLE --timeout 480 \
    --state /var/lib/radius-userid/state.json
```

Reads accounting from tailed detail files, from its own RADIUS accounting
listener (`--listen`), or both. `test`, `show` and `keygen` subcommands cover
proving the API path before any of it is wired up; `--dry-run` prints the
uid-messages instead of sending them.

The eduroam catch: with PEAP/TTLS the NAS only ever sees `anonymous@realm`.
For **your own users** the fix is a `Class` attribute carrying the inner
identity out to accounting — config in [`freeradius/`](freeradius/). For
**visiting users** the real name is deliberately never disclosed outside their
home institution, so the bridge maps them per realm
(`eduroam-visitor@fh-koeln.de`) and can tag them for dynamic user groups.

Wireless accounting often carries no `Framed-IP-Address`. Interim updates fix
most of it; `--unifi-host` lets the bridge resolve the client MAC against the
UniFi controller's client table for the rest.

Full walkthrough — FreeRADIUS config, the API admin role, zone settings,
policy examples, the syslog alternative, troubleshooting and the privacy
implications — in [docs/palo-alto-user-id.md](docs/palo-alto-user-id.md).

```bash
python3 -m unittest discover tests   # 49 tests, no network
```

## Disclaimer

This is an independent, unofficial project. It is not affiliated with,
endorsed by, or supported by Ubiquiti Inc., Hamina Wireless Oy, Palo Alto
Networks Inc., the FreeRADIUS project or the GÉANT eduroam service. UniFi,
InnerSpace, Hamina, PAN-OS and User-ID are trademarks of their respective
owners.

The InnerSpace endpoints and data model documented here are **undocumented
internal APIs**, determined by observing the InnerSpace web application in
order to interoperate with it. They carry no stability guarantee and may
change or disappear in any UniFi release — if an update breaks this tool,
that is expected, not a defect on Ubiquiti's part.

`unifi_export.py` only ever issues HTTP GET requests. It reads floor plans,
device placements and radio state; it never creates, modifies or deletes
anything on a console. The write endpoints that exist in the InnerSpace API
are deliberately not called.

`radius_userid.py` uses the documented PAN-OS User-ID XML API, which is a
write interface by nature: it submits IP-to-user mappings and, optionally,
user tags. It does not read, submit or commit firewall configuration, and its
only other call is the read-only `show user ip-user-mapping` used by the
`show` subcommand. Its UniFi lookups are GETs against the client table.

Mapping network activity to named people has legal and institutional
consequences that are yours, not this tool's. Read the privacy section of
[docs/palo-alto-user-id.md](docs/palo-alto-user-id.md) before deploying it.

Use it on equipment you own or are authorised to administer. It requires
credentials for that console and offers no way to reach one you cannot
already log in to. As stated in the [license](LICENSE), the software is
provided "as is", without warranty of any kind.

## License

[MIT](LICENSE) © 2026 Mark Houtz
