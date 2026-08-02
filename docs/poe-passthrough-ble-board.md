# PoE+ Pass‑Through WiFi‑FTM / BLE Anchor Board

A small inline board that sits between a PoE+ switch and a UniFi access point.
It passes 2.5GBASE‑T data **and** PoE+ power straight through to the AP, and
taps a few hundred milliwatts off the PoE rail to run a combo WiFi‑6 + BLE SoC.
The radio acts as an **802.11mc Fine Timing Measurement (FTM) responder** and a
**BLE beacon/scanner**, giving you fixed, surveyed anchor points for indoor
positioning that line up with the AP locations you already export into Hamina.

> **Design intent in one line:** a "bump in the wire" — electrically almost
> invisible to the PSE↔AP link, mechanically a dongle you cable inline with the
> AP drop.

---

## 1. Requirements

| # | Requirement | Value |
|---|---|---|
| R1 | Input power | 802.3at (PoE+ Type 2), 42.5–57 V at the interface |
| R2 | Downstream power | Full PoE+ passed through, **passive** (no re‑negotiation) |
| R3 | Downstream data | 2.5GBASE‑T (NBASE‑T), all 4 pairs |
| R4 | Radio | WiFi‑6 with FTM (802.11mc RTT) **+** BLE 5.x, single SoC |
| R5 | Self power budget | ≤ 2 W worst case (target ~0.5–1 W typical) |
| R6 | Transparency | Must not corrupt PoE detection/classification of the AP |
| R7 | Deployment | Indoor, ceiling/plenum near the AP |

**Key consequence of R2 (passive tap):** the board draws its power from the DC
that is already on the pairs *because the downstream AP negotiated it*. The
board therefore only runs when a valid downstream PD is present and powered. If
you need the board to run with nothing downstream, it must become a real PD +
PSE (the "re‑source" architecture) — see §11, Rejected alternatives.

---

## 2. System block diagram

```
     RJ45 IN (from PoE+ switch)                         RJ45 OUT (to UniFi AP)
   ┌───────────────────────────┐                     ┌───────────────────────┐
   │ 4× diff pairs (2.5GBASE‑T)─┼──── controlled‑Z ───┼─► 4× diff pairs ──────►│  data passes
   │                           │     100Ω straight   │                       │  straight through
   │  per‑pair center‑tap      │                     │  per‑pair center‑tap  │
   │  power chokes  ○──┐       │                     │      ┌──○  chokes      │
   └───────────────────┼───────┘                     └──────┼──────────────────┘
                       │  PoE common‑mode DC (≈48 V)         │
                       └──────────────┬──────────────────────┘   DC bridged IN↔OUT
                                      │
                             ┌────────▼────────┐
                             │ 4‑pair polarity │  low‑Vf Schottky bridge
                             │ bridge (Alt A/B)│  (polarity‑independent)
                             └────────┬────────┘
                                      │ ~48 V (VPoE)
                             ┌────────▼────────┐
                             │  UVLO load sw   │  enable only ABOVE ~40 V, i.e.
                             │  + inrush limit │  AFTER the AP finishes detect/class
                             └────────┬────────┘
                                      │
                             ┌────────▼────────┐
                             │ 48→3.3 V buck   │  wide‑Vin (≥60 V) DC‑DC
                             │ (or iso flyback)│  ~2 W, ~85–90% eff
                             └────────┬────────┘
                                      │ 3V3
                             ┌────────▼──────────────────────────────┐
                             │  ESP32‑C5/C6‑MINI module               │
                             │   • WiFi‑6 (FTM responder)             │
                             │   • BLE 5.x (beacon / scanner)         │
                             │   • on‑module PCB antenna              │
                             └───────────────┬────────────────────────┘
                                             │
                              USB‑C (provision/flash) · status LED · boot/reset
```

---

## 3. The hard part: passive PoE tap that stays transparent

### 3.1 Why "transparent" is the whole game

PoE brings a port up in stages the board must not disturb:

1. **Detection** – the PSE applies 2.7–10.1 V and looks for the PD's ~25 kΩ
   signature. Any DC load the board presents here looks like a second
   (bad) signature and can make the PSE mis‑detect or refuse the AP.
2. **Classification** – the PSE applies ~15.5–20.5 V and measures the PD's
   class current. Again, board load here corrupts the AP's class.
3. **Power‑up / operation** – the PSE ramps to 44–57 V. Only *now* may the
   board draw current.

**Rule:** the tap front‑end must be **high‑impedance until the line is fully
powered.** Implement with a UVLO on the load switch that enables at
**≈ 38–40 V** — comfortably above the 20.5 V classification ceiling, so the
board is electrically absent during detection and classification and only wakes
up once the AP has successfully negotiated. This is what preserves R6.

### 3.2 Extracting DC from the pairs without breaking 2.5G data

2.5GBASE‑T uses all four pairs; there are no spare pairs to grab. Data is
passed **straight through** on controlled‑impedance 100 Ω differential traces
(no PHY, no magnetics in the through path — magnetics would need active silicon
on the far side to regenerate the link).

Power is lifted off the **common mode** of each pair using a center‑tapped
choke (autotransformer) per pair:

- Differential (data) sees the choke as a high, balanced impedance → minimal
  insertion loss / return loss if the choke is well‑matched.
- Common mode (PoE DC) appears at the center tap → routed to the bridge, and
  also bridged to the OUT connector's matching choke center taps so the AP gets
  its power.

Do this on **all four pairs at both connectors** so the design is agnostic to
Alt A / Alt B / 4‑pair delivery and to pair polarity.

> ⚠️ **2.5G signal‑integrity risk — call it out now.** Center‑tap chokes on a
> multi‑gig pair are the single biggest SI risk on this board. Use chokes
> **explicitly rated for ≥2.5GBASE‑T PoE coupling** (multi‑gig PoE magnetics
> from Pulse/Bourns/Würth/Halo), keep the tap stubs short and symmetric, and
> **validate with TDR + eye‑diagram / return‑loss** against the NBASE‑T mask
> before committing the layout. Budget a spin for this.

### 3.3 Power budget (the number that decides if this works)

802.3at Type 2 guarantees **25.5 W at the PD** (worst‑case 100 m Cat5e). The
board steals from the gap between what the PSE *port can source* and what the AP
*actually draws*:

```
headroom = P_PSE_port(available) − P_AP(actual) − P_cable_loss
board needs ≈ 2 W worst case  (P_board / η_dcdc ≈ 1.7 W / 0.85)
                → ≈ 40–50 mA off a 48 V rail
```

| Scenario | AP draw | Result |
|---|---|---|
| Typical WiFi‑6 UniFi AP on a short drop, PSE sources ~30 W | 13–20 W | 8–15 W headroom → **fine** |
| Upstream port is 802.3bt (many UniFi Pro switches) | any at‑class AP | huge headroom → **ideal** |
| AP is a true class‑4 25.5 W device on a 100 m worst‑case run | ~25.5 W | ~0 headroom → **marginal, avoid** |

**Deployment guidance:** the passive tap is comfortable when the upstream port
can source ≥ ~28–30 W (short cable) *or* is 802.3bt/PoE++, and the AP is not
pinned at full class‑4. For high‑draw APs on long runs, either feed the drop
from a bt port, or use the re‑source architecture (§11). Put this in the install
notes — it's a real constraint, not a footnote.

### 3.4 Behaviour on hot‑plug / MPS

- The board must **not** hold up the PSE's Maintain‑Power‑Signature on its own —
  if the AP is unplugged downstream, the port drops and the board de‑energises.
  That's correct and expected for a passive tap.
- Soft‑start the tap (inrush limit on the load switch) so waking the board never
  glitches the AP's rail or trips the PSE's overcurrent.

---

## 4. Radio subsystem

**Recommended part: `ESP32‑C5‑MINI‑1` module** (dual‑band WiFi‑6 2.4/5 GHz +
BLE 5, integrated PCB antenna, pre‑certified).

- **Why a module, not the bare SoC:** it ships with FCC/CE/IC modular
  certification and a tuned antenna — you inherit the RF cert and skip antenna
  tuning entirely. For a first board this saves weeks and a compliance spin.
- **Why C5 over C6:** 5 GHz + 80 MHz bandwidth gives materially better FTM
  ranging resolution than 2.4 GHz‑only. If you want the most mature, lowest‑risk
  option today, `ESP32‑C6‑MINI‑1` (2.4 GHz WiFi‑6 + BLE 5 + 802.15.4) is a drop‑in
  fallback with very well‑trodden FTM support — at reduced ranging accuracy.
- **FTM role:** ESP32 supports FTM as **responder** and **initiator**. As a
  fixed anchor it runs as an FTM **responder** (requires SoftAP mode on the
  ESP32) so client STAs can range against a known‑good, surveyed location.
- **BLE:** advertise iBeacon/Eddystone for coarse presence and to carry the
  anchor's identity/coordinates; optionally scan for asset tags. BLE also gives
  you a clean out‑of‑band provisioning channel.

Peripherals:
- **USB‑C** to the SoC's native USB‑Serial‑JTAG for flashing/provisioning on the
  bench (5 V from USB is isolated from the field power path — see §5).
- **Tag‑Connect (TC2030)** footprint as a no‑connector fallback for
  programming/UART.
- **Status LED** + **BOOT/RESET** buttons.

---

## 5. Power supply

**Primary: non‑isolated wide‑Vin buck, 48 V → 3.3 V.**
- e.g. TI `LMR38010` / `LMR16006` class (≥60 V input, integrated FET), sized for
  ~0.7 A at 3.3 V to cover WiFi TX current peaks.
- Board ground = PoE return. The mandated data isolation already lives in the
  PSE and AP magnetics, and the board has no user‑touchable conductive parts, so
  a non‑isolated buck is the simplest, cheapest, most efficient choice.
- **EMI caveat:** the buck's switching ground is the pairs' common mode.
  Contain it: input LC + common‑mode filtering, tight hot‑loop layout, and a
  ground‑referenced shield can keep switching noise off the 2.5G common mode
  (alien crosstalk / EMC).

**Option (choose if EMC is stringent or you want galvanic separation): isolated
flyback.**
- e.g. `LT8302` / `MAX17690` + a small transformer, or a pre‑qualified 3 W
  isolated brick. Fully decouples the radio ground from the 48 V return at the
  cost of a transformer, size, and ~5 pts efficiency.

Bench power: USB‑C 5 V feeds a small 5 V→3V3 LDO/buck for programming only,
ORed into 3V3 with an ideal‑diode so field power and USB never back‑feed.

---

## 6. Protection

| Node | Part | Purpose |
|---|---|---|
| VPoE (48 V) | TVS, e.g. SMBJ58A | Surge / transient clamp on the tapped rail |
| VPoE input | Bulk + HF caps, X7R rated ≥100 V | Ride‑through, decoupling |
| RJ45 shields | Bob‑Smith term + cap to chassis | Common‑mode / ESD path |
| USB‑C | USB TVS array (e.g. TPD2E) | Port ESD |
| Antenna/module RF | keepout + ground stitching | Per module guidelines |

Creepage/clearance: keep ≥ ~2.5–3 mm around the 48 V net (and its surge
excursions) to low‑voltage nets; slot the board under high‑pot nodes if the
enclosure demands it.

---

## 7. Connectors & mechanical

- **2× shielded RJ45 jacks, no integrated magnetics** (IN from switch, OUT to
  AP). Magnetics live off‑board (in PSE/AP); the tap chokes are discrete.
- Small inline enclosure with a short pigtail to the AP, or a flat "puck" that
  cable‑ties to the AP bracket. Keep the module's antenna edge at a board edge
  with keepout, away from the RJ45 metal and any ground plane under the antenna.
- Label IN/OUT clearly — polarity of *data* matters even though *power* is
  Alt‑agnostic.

---

## 8. PCB stackup & layout

- **4‑layer**: `Signal(top) / GND / Power / Signal(bottom)`.
- **100 Ω differential** for the four passthrough pairs, length‑matched
  within‑pair, generous pair‑to‑pair spacing, reference to solid GND, no splits
  under the pairs. Keep the tap‑choke stubs short/symmetric (§3.2).
- **Solid GND** under the switching converter; isolate the buck hot loop.
- **RF keepout** under the module antenna per Espressif's module layout guide;
  no copper, no traces, board‑edge placement.
- Star/segregate the 48 V domain from 3V3 and RF; single‑point chassis/GND tie.

---

## 9. Firmware notes (ESP‑IDF)

- **FTM responder:** enable in SoftAP mode (`esp_wifi_ftm` APIs); publish a
  stable SSID/BSSID per anchor. Clients call FTM against it; the anchor's
  surveyed coordinate is the reference point.
- **BLE beacon:** advertise anchor ID + coordinates (iBeacon major/minor or an
  Eddystone frame); optionally scan for tags and report over MQTT/Wi‑Fi.
- **Anchor identity ↔ survey:** the anchor coordinate is exactly the AP
  coordinate you already export from UniFi into Hamina — reuse that data so the
  FTM/BLE anchor map and the WiFi plan share one source of truth.
- **Provisioning:** BLE or USB‑C; store coordinate + role in NVS.

---

## 10. Starter BOM (key line items)

Full table: [`poe-passthrough-ble-board.bom.md`](./poe-passthrough-ble-board.bom.md).
Passives/decoupling are omitted from the summary below.

| Block | Part (representative) | Notes |
|---|---|---|
| Radio | Espressif ESP32‑C5‑MINI‑1 | dual‑band WiFi‑6 + BLE, FTM, pre‑certified |
| Tap chokes ×4 | Multi‑gig PoE center‑tap choke (Pulse/Bourns/Würth) | **must be ≥2.5GBASE‑T rated** |
| Rectifier | 4‑pair Schottky bridge (low‑Vf, e.g. PMEG series) | polarity/Alt‑agnostic |
| UVLO load switch | 100 V load switch or discrete FET + UVLO ~40 V | transparency gate |
| DC‑DC | TI LMR38010 (or isolated LT8302 + xfmr) | 48→3.3 V, ~0.7 A |
| Surge | SMBJ58A TVS | 48 V rail clamp |
| Connectors | 2× shielded RJ45, no magnetics | IN / OUT |
| USB | USB‑C receptacle + TPD2E ESD | flashing/provisioning |
| Misc | LED, 2× tact switch, TC2030 pads | bring‑up/debug |

---

## 11. Rejected / alternative architectures

- **PD + re‑source as PSE** (board negotiates PoE+ as a PD, then injects PoE to
  the AP as a PSE). *Pro:* runs standalone, deterministic power budget, works
  with a maxed class‑4 AP. *Con:* two negotiators, PSE controller + more
  magnetics, and 25.5 W now shared between AP and board. Choose this if you need
  the board up with nothing downstream, or must support high‑draw APs on long
  runs from an at‑only upstream.
- **Power‑in‑only end node** (no downstream data). Not applicable — the AP needs
  its data link.
- **BLE‑only / BLE Channel Sounding anchor** (no WiFi). Lower power and simpler,
  but drops true 802.11mc FTM. Reconsider if the WiFi TX peaks make the passive
  power budget too tight in practice.

---

## 12. Open items / next spin

1. **Prototype the tap‑choke SI first** — a 2‑port coupon (IN choke → OUT choke,
   pairs straight through) on real multi‑gig chokes, TDR + return loss vs the
   NBASE‑T mask. This gates everything else.
2. Confirm real WiFi‑TX current peaks of the chosen module; size the buck +
   input bulk to survive them without sagging the tap or nudging the PSE.
3. Measure worst‑case UniFi AP draw on the target switches to lock the §3.3
   deployment envelope.
4. Decide isolated vs non‑isolated after a first EMC pre‑scan.
5. Enclosure + antenna co‑design (keepout, metalwork, mounting).

---

*This is a design document, not a released schematic. Numbers here are
engineering targets to be validated on the bench — treat §3.3 (power budget) and
§3.2 (2.5G SI) as the two make‑or‑break items.*
