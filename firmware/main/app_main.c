// FTM-responder + BLE-beacon anchor firmware — entry point.
//
// Boot sequence:
//   1. NVS + default event loop (shared by Wi-Fi and NimBLE).
//   2. Load anchor identity (Kconfig defaults, overlaid by commissioned NVS).
//   3. Bring up the SoftAP FTM responder.
//   4. Start the iBeacon advertisement.
//
// Wi-Fi and BLE share one radio; software coexistence (enabled in
// sdkconfig.defaults) arbitrates airtime between FTM exchanges and beacons.

#include "esp_log.h"
#include "esp_event.h"
#include "nvs_flash.h"

#include "anchor.h"
#include "wifi_ftm.h"
#include "ble_beacon.h"

static const char *TAG = "app";

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

    ESP_ERROR_CHECK(esp_event_loop_create_default());

    anchor_cfg_t cfg;
    anchor_cfg_load(&cfg);
    anchor_cfg_log(&cfg);

    wifi_ftm_responder_start(&cfg);   // Wi-Fi first: it owns netif/coex init
    ble_beacon_start(&cfg);

    ESP_LOGI(TAG, "anchor '%s' running (FTM responder + iBeacon)", cfg.label);
    // TODO(provisioning): expose survey coordinate + credentials over BLE GATT
    // or the console, then anchor_cfg_save_survey() and reboot. Until then the
    // coordinate comes from Kconfig / a factory NVS write.
}
