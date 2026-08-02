# Variant B — PD + Re-Source PoE Board (WiFi-FTM / BLE Anchor)

The companion to [`poe-passthrough-ble-board.md`](./poe-passthrough-ble-board.md)
(variant A, the passive tap). Same mission — an inline anchor between switch and
UniFi AP running an ESP32 combo radio as an FTM responder + BLE beacon — but a
fundamentally different power topology:

- The board is a **real PoE Powered Device (PD)** on its upstream port.
- The board is a **real PoE Power Sourcing Equipment (PSE)** on its downstream
  port, re-injecting power to the AP.

That makes it **standalone** (it powers up with nothing downstream), gives it a
**deterministic power budget** (it owns the negotiation on both sides), and lets
it **feed a maxed-out class-4 AP** — the three things variant A cannot promise.
The cost is real: more silicon, more power, and an architectural consequence you
have to accept before anything else.

---

## 0. The consequence you must accept first

**Re-sourcing means two independent PoE domains, and that forces active data
regeneration.**

In variant A there is *one* PoE domain end-to-end: the pairs are straight copper
through the board, PoE common-mode DC flows switch→AP, and the board just steals
from the headroom. You cannot re-source on top of that, because a straight
copper trace passes DC common-mode straight through — you can *tap* it but you
can't *break* it.

To create a second, independently-negotiated PoE domain (our PSE → AP) you must
**break DC continuity on the pairs** between input and output while still passing
the differential data. The only clean way to pass 2.5 GBASE-T differential AC
but block DC is **magnetics** — and two sets of magnetics back-to-back need an
active driver between them. So variant B necessarily includes:

```
                       ┌───────── active data regeneration ─────────┐
 RJ45_in ── mag ──► 2.5G PHY_A ══(MAC-to-MAC / repeater)══ 2.5G PHY_B ──► mag ── RJ45_out
   │(magnetics, PoE CT)                                              (magnetics, PoE CT)│
   │                                                                                    │
   ▼ input PoE domain                                              output PoE domain ▼
 PD front-end ──► VPoE_in ──┬─► iso flyback ─► 3V3 ─► ESP32-C5    PSE front-end (sources AP)
                            └─► PSE supply rail ─────────────────────────► (drives AP CT)
```

There is no "passive data, active power" version of a true re-source. If you
want passive data, you're back to variant A. **Accept the two PHYs, or don't
re-source.** (Bonus: because there's now an active switch/PHY in the data path,
the anchor *can* report over a wired path instead of spending WiFi airtime — see
§7.)

---

## 1. Requirements delta vs variant A

| # | Variant A (passive tap) | Variant B (this doc) |
|---|---|---|
| PoE domains | one, end-to-end | **two** (PD in, PSE out) |
| Upstream role | invisible tap | **802.3bt PD** (recommended) |
| Downstream role | untouched passthrough | **802.3at PSE** to the AP |
| Data path | straight traces (passive) | **PHY_A ↔ PHY_B** (active) |
| Runs standalone? | no (needs downstream PD) | **yes** |
| Feeds a maxed class-4 AP? | no | **yes** (with bt upstream) |
| Self power | ~2 W | ~5–7 W (radio + 2 PHYs + losses) |

---

## 2. The power budget — read this before choosing an upstream class

Conservation of energy is the whole story: **you cannot hand a full PoE+
(25.5 W) AP its power *and* run the board from a PoE+ (25.5 W) input.** You must
ingest more than you re-source. That means **802.3bt (PoE++) upstream** if you
want a full-power AP downstream.

Overhead to subtract from whatever you ingest:

| Consumer | Budget |
|---|---|
| 2× 2.5 G PHY (data regeneration) | ~1.5–2 W |
| ESP32-C5 radio incl. WiFi-TX peaks | ~2 W |
| Isolated flyback + housekeeping losses | ~1–2 W |
| PSE conversion / pass loss | ~2–3 W |
| **Total overhead** | **~7 W** |

### Upstream class → what you can feed downstream

| Upstream PD | Guaranteed at PD | − ~7 W overhead | Downstream AP you can feed |
|---|---|---|---|
| **802.3bt Type 3, Class 6** | 51 W | ~44 W | Any PoE+ AP (25.5 W) with huge margin; even a light bt AP |
| **802.3bt Type 3, Class 5** | 40 W | ~33 W | Any PoE+ AP (25.5 W) comfortably ✅ **recommended** |
| **802.3bt Type 4, Class 7/8** | 62 / 71 W | ~55 / 64 W | Overkill; leaves headroom for a full bt AP |
| 802.3at Type 2, Class 4 | 25.5 W | ~18–19 W | **Constrained** — AP must be ≤ ~18 W (802.3af-class or a light AP) |

**Recommendation:** target **802.3bt Type 3, Class 5 (40 W) upstream, re-source
802.3at PoE+ (25.5 W) downstream + radio.** This is the sweet spot: standard bt
port on modern UniFi Pro switches, comfortable margin, feeds any PoE+ AP.

Keep the at-in / reduced-out row only for deployments where the drop is
at-only *and* the AP is genuinely low-power.

---

## 3. Upstream PD front-end

- **Magnetics:** 2.5 GBASE-T + PoE MagJack (center-tapped, multi-gig rated) on
  the input; PD taps the center taps for input PoE.
- **Bridge:** 4-pair diode bridge (Alt A / Alt B / 4-pair agnostic), low-Vf.
- **PD controller:** an 802.3bt (Type 3/4) PD interface — e.g. TI `TPS2372`
  (Type 4), Silicon Labs `Si34xx`, or equivalent bt PD IC. Handles the
  detection signature, bt classification (advertise Class 5/6), inrush, hotswap
  FET, and UVLO. Output = `VPoE_in` (~50–57 V) on the input PoE domain.
- **Surge/TVS** on the input PI per 802.3 (e.g. SMAJ58A + bulk).

Unlike variant A there is **no transparency requirement** — the board *is* the
PD, so it presents its own signature deliberately and negotiates its own class.

---

## 4. Downstream PSE front-end (the re-source)

- **PSE controller:** single/low-port-count PSE — e.g. TI `TPS23861` (I²C,
  4-port, up to ~30 W/port, plenty for one PoE+ AP), or a bt PSE like
  `TPS2388x` / Microchip `PD69xxx` if you ever want to feed a bt AP.
- **PSE supply:** source the downstream port from `VPoE_in`. If input sags near
  the PSE's 50 V minimum under load, add a small regulator to hold a clean
  ~53 V PSE bus; otherwise pass `VPoE_in` directly through the PSE hotswap FET.
- **Detection/classification of the AP** happens on the **downstream** pairs'
  common mode via the output MagJack center taps — completely normal PSE
  behaviour, independent of the differential data passing through PHY_B.

### 4.1 Budget-aware PSE (don't over-promise)

The PSE must **never advertise more power than the board ingested minus
overhead.** If the board is only a Type 2 (25.5 W) PD, it must *not* let the AP
negotiate a full 25.5 W class — it can't back it, and the AP will brown out and
trip PSE fault loops. Set the PSE's **per-port power limit** (I²C on TPS23861-
class parts) to `P_in − ~7 W`, and advertise a matching class. With the
recommended Class-5 (40 W) bt upstream this never binds — the point is to make
the firmware enforce it so an at-only deployment fails *safe* (grants a lower
class) instead of oscillating.

---

## 5. Data path — active 2.5 G regeneration

- **Two 2.5 GBASE-T PHYs** (e.g. Marvell `88Q`/`88E21xx`, Realtek `RTL822x`,
  Broadcom) connected **MAC-to-MAC** (SGMII/USXGMII back-to-back), or a **2-port
  2.5 G switch/repeater IC** if you prefer a single part with management.
- Each PHY sits on the PHY side of its port's magnetics; PoE couples on the RJ45
  side via the MagJack center taps — the PHYs never see PoE.
- Length-match and impedance-control (100 Ω diff) the short PHY↔MagJack runs;
  this is far more forgiving than variant A's passive straight-through because
  the PHYs re-clock and re-drive the link.
- Power the PHYs from the flyback's low-voltage rails (their ~1.5–2 W is already
  in the §2 budget).

---

## 6. Radio subsystem

Identical to variant A: **ESP32-C5-MINI-1** (dual-band WiFi-6 + BLE, FTM
responder, pre-certified module; C6-MINI-1 as the 2.4 G fallback). Powered from
the isolated flyback's 3V3. See variant A §4 and §9 for the FTM-responder / BLE-
beacon firmware notes — unchanged here.

---

## 7. Bonus: wired reporting

Because there's an active switch/PHY in the data path, the anchor can push its
FTM/BLE telemetry over the **wired** link instead of consuming WiFi airtime.
The ESP32-C5/C6 has no Ethernet MAC, so realise this with an **SPI-Ethernet
controller (W5500/LAN8651)** hung off the SoC and a spare port on the 2-port
switch IC — optional, but attractive for a fixed infrastructure anchor. Omit it
and the radio simply backhauls over WiFi as in variant A.

---

## 8. Isolation & protection

- **Two floating PoE domains** (input PD, output PSE) separated by the two sets
  of magnetics — this is the *natural* topology, each side floats like a normal
  cable segment. No special co-referencing gymnastics needed.
- **Radio/logic** galvanically isolated from the PoE domains via the flyback
  transformer (1500 Vrms boundary).
- **Surge/TVS** on both the PD input PI and the PSE output.
- Creepage/clearance ≥ ~2.5–3 mm around 48–57 V nets and their surge
  excursions.

---

## 9. Starter BOM delta (on top of variant A's radio/USB/UI blocks)

| Block | Ref | Description | Representative part | Qty |
|---|---|---|---|---|
| PD front-end | U10 | 802.3bt (Type 3/4) PD interface controller | TI TPS2372 / SiLabs Si34xx | 1 |
| PD front-end | D10–D17 | 4-pair input bridge (low-Vf Schottky) | PMEG-series array | 8 |
| PSE front-end | U11 | PoE PSE controller (I²C, per-port limit) | TI TPS23861 (at) / TPS2388x (bt) | 1 |
| PSE front-end | Q10 | PSE pass/hotswap FET | 100 V N-FET | 1 |
| Data | U12, U13 | 2.5 GBASE-T PHY (×2, MAC-to-MAC) | Marvell 88Q / Realtek RTL822x | 2 |
| Data (alt) | U12 | 2-port 2.5 G switch/repeater IC | vendor 2.5 G switch | (1, alt) |
| Magnetics | J10, J11 | 2.5 G + PoE MagJack (center-tapped) | multi-gig PoE MagJack | 2 |
| DC-DC | U14 | Isolated PoE flyback → 3V3/PHY rails | LT8302 / MAX17690 + xfmr | 1 |
| Supply | — | Optional PSE-bus regulator (~53 V) | wide-Vin buck/boost | 0–1 |
| Protection | TVS10/11 | Surge clamp, PD input + PSE output | SMAJ58A | 2 |
| Wired (opt) | U15 | SPI-Ethernet for wired telemetry | W5500 / LAN8651 | 0–1 |

Plus the variant-A radio module, USB-C flashing, LED, buttons, and Tag-Connect.

---

## 10. When to choose B over A

Choose **B (this doc)** when any of these hold:
- The AP is a high-draw / full class-4 device, or on a long run from an at-only
  upstream (A's headroom is too tight).
- The board must power up and run **with nothing downstream** (commissioning,
  or the AP is offline).
- You have **802.3bt** available upstream and want a deterministic, spec-clean
  power contract on both sides.
- You want the **wired-telemetry** option (§7).

Stay with **A (passive tap)** when:
- Upstream is bt or the AP draws well under class-4 (plenty of headroom).
- You want the lowest cost, lowest part count, and lowest power.
- Data-path simplicity ("bump in the wire", no PHYs) matters more than
  standalone operation.

---

## 11. Open items / next spin

1. **Pick the data-regeneration path** — dual discrete PHYs (MAC-to-MAC) vs a
   single 2-port 2.5 G switch IC. This drives cost, power, and firmware most.
2. Lock the **upstream class** (recommend bt Class 5) and prove the §2 budget on
   the bench with real WiFi-TX peaks + both PHYs active.
3. Implement and test **budget-aware PSE** class capping (§4.1) — verify an
   at-only input grants a *lower* downstream class cleanly instead of oscillating.
4. Confirm PSE-bus regulation is/ isn't needed (does `VPoE_in` stay above the
   PSE 50 V minimum under full AP load?).
5. EMC: two PHYs + PSE switching is a busier board than A — plan the pre-scan
   and shielding early.

---

*Design document, not a released schematic. §0 (two domains ⇒ active data) and
§2 (bt-in to feed at-out) are the two things to internalise before committing —
they're why B exists and why it costs more than A.*
