# Starter BOM — PoE+ Pass-Through WiFi-FTM / BLE Anchor Board

Representative parts, not a released BOM. Passives (decoupling, resistors, most
caps) are summarised, not enumerated. See
[`poe-passthrough-ble-board.md`](./poe-passthrough-ble-board.md) for the design
rationale behind each choice.

| Block | Ref | Description | Representative part | Qty | Notes |
|---|---|---|---|---|---|
| Radio | U1 | WiFi-6 dual-band + BLE combo module (FTM capable) | Espressif ESP32-C5-MINI-1 | 1 | Pre-certified module w/ PCB antenna; ESP32-C6-MINI-1 = 2.4G-only fallback |
| Power tap | T1–T4 | Center-tap coupling choke for PoE extraction | Multi-gig PoE choke (Pulse/Bourns/Würth/Halo) | 4 | **MUST be ≥2.5GBASE-T rated** — biggest SI risk item |
| Power tap | D1–D8 | 4-pair polarity/Alt-agnostic rectifier bridge | Low-Vf Schottky array (e.g. PMEG series) | 8 | Two full-wave bridges; low Vf at ~50 mA |
| Power gate | Q1/U2 | UVLO load switch + inrush limit | 100 V load switch or FET + UVLO comparator | 1 | Enable ~38–40 V so board is high-Z during PoE detect/class |
| DC-DC | U3 | 48 V → 3.3 V wide-Vin buck (~0.7 A) | TI LMR38010 / LMR16006 | 1 | Non-isolated primary; isolated LT8302 + xfmr optional |
| Protection | TVS1 | 48 V rail transient/surge clamp | SMBJ58A | 1 | On tapped VPoE node |
| Protection | C_bulk | VPoE bulk + HF decoupling | ≥100 V X7R + electrolytic/poly | several | Ride-through for WiFi TX current peaks |
| Connector | J1 | Shielded RJ45 IN (from PoE+ switch), no magnetics | Shielded RJ45 jack | 1 | Magnetics live in PSE/AP; tap chokes are discrete |
| Connector | J2 | Shielded RJ45 OUT (to UniFi AP), no magnetics | Shielded RJ45 jack | 1 | Straight-through 100 Ω diff pairs |
| Program | J3 | USB-C receptacle (flash/provision) | USB-C 2.0 receptacle | 1 | Native USB-Serial-JTAG on ESP32; ORed into 3V3 |
| Program | ESD1 | USB-C ESD protection array | TI TPD2E / equiv | 1 | Port ESD |
| Program | J4 | Tag-Connect programming/UART footprint | TC2030 pads | 1 | No-connector debug fallback |
| UI | LED1 | Status LED | 0603 LED + resistor | 1 | Link/anchor status |
| UI | SW1–SW2 | BOOT / RESET tact switches | SMD tact | 2 | Bring-up |
| Filter | CMC1 | Common-mode choke / filtering on tap return | CMC + caps | 1 | Keep buck switching noise off 2.5G common mode |
