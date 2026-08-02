# FTM-responder + BLE-beacon anchor firmware

ESP-IDF firmware skeleton for the anchor radio on the PoE pass-through boards
([variant A](../docs/poe-passthrough-ble-board.md) /
[variant B](../docs/poe-passthrough-ble-board-resource-variant.md)). It turns an
**ESP32-C5 / C6** into a fixed positioning anchor that:

- answers **802.11mc FTM** ranging as a Wi-Fi **responder** (SoftAP), and
- broadcasts a non-connectable **iBeacon** carrying the anchor's identity.

Both roles share one radio; Wi-Fi/BLE software coexistence handles airtime.

The anchor's identity keys back to its **surveyed coordinate** — the same
coordinate the exporter in this repo writes for the co-located UniFi AP — so the
positioning anchors and the Hamina Wi-Fi plan share one source of truth.

## Layout

```
firmware/
├── CMakeLists.txt          top-level IDF project
├── sdkconfig.defaults      FTM + NimBLE + coex baseline
├── partitions.csv          nvs / phy / factory
└── main/
    ├── app_main.c          boot: nvs → load cfg → wifi → ble
    ├── anchor.{c,h}        identity/coordinate, Kconfig defaults + NVS overlay
    ├── wifi_ftm.{c,h}      SoftAP with ap.ftm_responder = true
    ├── ble_beacon.{c,h}    NimBLE non-connectable iBeacon
    └── Kconfig.projbuild    per-anchor menuconfig options
```

## Build & flash

```bash
. $IDF_PATH/export.sh
cd firmware
idf.py set-target esp32c6      # or esp32c5 for dual-band / 5 GHz FTM
idf.py menuconfig              # set "FTM/BLE Anchor configuration"
idf.py build flash monitor
```

Requires ESP-IDF **v5.x**. This is a skeleton — it compiles into the two radio
roles and logs identity; it is not a finished product. See the TODOs below.

## Configuration

Per-anchor values live under **`FTM/BLE Anchor configuration`** in `menuconfig`
(coordinate, floor, SSID/channel, iBeacon UUID/major/minor). At commissioning,
a factory NVS write to the `anchor` namespace overrides the survey keys
(`label`, `floor`, `x_mm`, `y_mm`, `major`, `minor`) per unit without a rebuild.

## Notes on the two roles

- **FTM responder** is essentially one flag — `wifi_ap_config_t.ftm_responder`.
  There is no per-session responder API and no responder-side report event; the
  Wi-Fi firmware answers FTM frames on its own once the AP is up. The *initiator*
  (a phone or another ESP32) drives sessions and gets the distance. HT40 is
  requested for finer range resolution where the channel allows.
- **iBeacon** is a fixed 30-byte manufacturer-data frame. `minor` is the anchor
  id, `major` the site/floor, UUID the deployment — the backend maps that tuple
  to the surveyed coordinate.

## Not yet implemented (intentional skeleton gaps)

- **Provisioning UI** — writing the survey coordinate + Wi-Fi credentials over
  BLE GATT or the console, then `anchor_cfg_save_survey()` + reboot.
- **Telemetry backhaul** — APSTA STA join (variant A) or wired SPI-Ethernet
  (variant B) to report FTM/BLE observations upstream.
- **BLE scanning** — observing asset tags (the observer role is already compiled
  in via `BT_NIMBLE_ROLE_OBSERVER`).
- **Health/OTA** — status LED behaviour, watchdog wiring, OTA partition split.
