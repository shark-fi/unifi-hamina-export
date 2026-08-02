#include "wifi_ftm.h"

#include <string.h>

#include "esp_log.h"
#include "esp_mac.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_event.h"

static const char *TAG = "wifi_ftm";

static void wifi_event_handler(void *arg, esp_event_base_t base,
                               int32_t id, void *data)
{
    if (base != WIFI_EVENT) {
        return;
    }
    switch (id) {
    case WIFI_EVENT_AP_START: {
        uint8_t mac[6] = {0};
        esp_wifi_get_mac(WIFI_IF_AP, mac);
        ESP_LOGI(TAG, "FTM responder up, BSSID %02x:%02x:%02x:%02x:%02x:%02x",
                 mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
        break;
    }
    case WIFI_EVENT_AP_STACONNECTED:
        ESP_LOGI(TAG, "station associated (may range against us)");
        break;
    default:
        break;
    }
}

void wifi_ftm_responder_start(const anchor_cfg_t *cfg)
{
    ESP_ERROR_CHECK(esp_netif_init());
    esp_netif_create_default_wifi_ap();

    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL, NULL));

    wifi_config_t ap = { 0 };
    strncpy((char *)ap.ap.ssid, cfg->wifi_ssid, sizeof(ap.ap.ssid) - 1);
    ap.ap.ssid_len       = strlen(cfg->wifi_ssid);
    ap.ap.channel        = cfg->wifi_channel;
    ap.ap.max_connection = 4;

    if (cfg->wifi_password[0] != '\0') {
        strncpy((char *)ap.ap.password, cfg->wifi_password,
                sizeof(ap.ap.password) - 1);
        ap.ap.authmode = WIFI_AUTH_WPA2_PSK;
    } else {
        ap.ap.authmode = WIFI_AUTH_OPEN;
    }

    // The one flag that makes this an FTM anchor.
    ap.ap.ftm_responder = true;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_AP));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ap));

    // HT40 gives finer FTM range resolution than HT20 where the channel allows.
    // Ignore failure (channel/regdomain may not permit 40 MHz here).
    esp_wifi_set_bandwidth(WIFI_IF_AP, WIFI_BW_HT40);

    ESP_ERROR_CHECK(esp_wifi_start());

    // NOTE (backhaul): to also report telemetry over Wi-Fi, switch to
    // WIFI_MODE_APSTA and add a STA config + IP event handling. The FTM
    // responder keeps working in APSTA. Variant-B boards can instead backhaul
    // over the wired path and leave this AP-only.
}
