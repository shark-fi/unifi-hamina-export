#pragma once
#include <stdint.h>
#include <stdbool.h>

// One anchor's identity + radio configuration.
//
// The surveyed (x, y, floor) is the whole point of the board: it is the same
// coordinate the exporter writes for the co-located UniFi AP, so the FTM/BLE
// anchor map and the Hamina Wi-Fi plan share a single source of truth. The
// backend maps ble{uuid,major,minor} / wifi BSSID -> this coordinate.
typedef struct {
    // --- survey identity ---
    char    label[32];
    int32_t floor;
    float   x_m;              // metres
    float   y_m;              // metres

    // --- Wi-Fi FTM responder (SoftAP) ---
    char    wifi_ssid[32];    // final SSID, "<base>-<label>"
    char    wifi_password[64];
    uint8_t wifi_channel;

    // --- BLE iBeacon ---
    uint8_t  ble_uuid[16];
    uint16_t ble_major;
    uint16_t ble_minor;
    int8_t   ble_measured_power;   // RSSI at 1 m, dBm
} anchor_cfg_t;

// Populate `out` from Kconfig defaults, then override any keys present in the
// "anchor" NVS namespace (survey coordinate is expected to be provisioned there
// per unit at commissioning time). Never fails hard: missing NVS -> defaults.
void anchor_cfg_load(anchor_cfg_t *out);

// Persist the survey identity (label, floor, x, y, major, minor) to NVS.
// Radio credentials are left to provisioning; this is the commissioning write.
esp_err_t anchor_cfg_save_survey(const anchor_cfg_t *cfg);

// Log the full resolved config at boot.
void anchor_cfg_log(const anchor_cfg_t *cfg);
