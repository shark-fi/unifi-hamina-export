#include "anchor.h"

#include <string.h>
#include <stdio.h>

#include "esp_err.h"
#include "esp_log.h"
#include "nvs.h"
#include "sdkconfig.h"

static const char *TAG = "anchor";
static const char *NVS_NS = "anchor";

// Parse up to 16 bytes from a 32-char hex string. Non-hex / short input leaves
// the remaining bytes zeroed; this is a config convenience, not a validator.
static void parse_uuid(const char *hex, uint8_t out[16])
{
    memset(out, 0, 16);
    for (int i = 0; i < 16; i++) {
        unsigned byte = 0;
        if (sscanf(hex + i * 2, "%2x", &byte) != 1) {
            break;
        }
        out[i] = (uint8_t)byte;
    }
}

static void load_defaults(anchor_cfg_t *c)
{
    memset(c, 0, sizeof(*c));

    strncpy(c->label, CONFIG_ANCHOR_LABEL, sizeof(c->label) - 1);
    c->floor = CONFIG_ANCHOR_FLOOR;
    c->x_m   = CONFIG_ANCHOR_X_MM / 1000.0f;
    c->y_m   = CONFIG_ANCHOR_Y_MM / 1000.0f;

    snprintf(c->wifi_ssid, sizeof(c->wifi_ssid), "%s-%s",
             CONFIG_ANCHOR_WIFI_SSID, CONFIG_ANCHOR_LABEL);
    strncpy(c->wifi_password, CONFIG_ANCHOR_WIFI_PASSWORD,
            sizeof(c->wifi_password) - 1);
    c->wifi_channel = CONFIG_ANCHOR_WIFI_CHANNEL;

    parse_uuid(CONFIG_ANCHOR_BLE_UUID, c->ble_uuid);
    c->ble_major          = CONFIG_ANCHOR_BLE_MAJOR;
    c->ble_minor          = CONFIG_ANCHOR_BLE_MINOR;
    c->ble_measured_power = CONFIG_ANCHOR_BLE_TXPOWER;
}

// Override selected survey keys from NVS if a commissioning write left them.
static void overlay_nvs(anchor_cfg_t *c)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) != ESP_OK) {
        ESP_LOGI(TAG, "no NVS survey data, using Kconfig defaults");
        return;
    }

    size_t len = sizeof(c->label);
    nvs_get_str(h, "label", c->label, &len);
    nvs_get_i32(h, "floor", &c->floor);

    int32_t x_mm, y_mm;
    if (nvs_get_i32(h, "x_mm", &x_mm) == ESP_OK) c->x_m = x_mm / 1000.0f;
    if (nvs_get_i32(h, "y_mm", &y_mm) == ESP_OK) c->y_m = y_mm / 1000.0f;

    uint16_t u16;
    if (nvs_get_u16(h, "major", &u16) == ESP_OK) c->ble_major = u16;
    if (nvs_get_u16(h, "minor", &u16) == ESP_OK) c->ble_minor = u16;

    nvs_close(h);
    ESP_LOGI(TAG, "applied NVS survey overrides");
}

void anchor_cfg_load(anchor_cfg_t *out)
{
    load_defaults(out);
    overlay_nvs(out);
}

esp_err_t anchor_cfg_save_survey(const anchor_cfg_t *cfg)
{
    nvs_handle_t h;
    esp_err_t err = nvs_open(NVS_NS, NVS_READWRITE, &h);
    if (err != ESP_OK) return err;

    nvs_set_str(h, "label", cfg->label);
    nvs_set_i32(h, "floor", cfg->floor);
    nvs_set_i32(h, "x_mm", (int32_t)(cfg->x_m * 1000.0f));
    nvs_set_i32(h, "y_mm", (int32_t)(cfg->y_m * 1000.0f));
    nvs_set_u16(h, "major", cfg->ble_major);
    nvs_set_u16(h, "minor", cfg->ble_minor);

    err = nvs_commit(h);
    nvs_close(h);
    return err;
}

void anchor_cfg_log(const anchor_cfg_t *c)
{
    ESP_LOGI(TAG, "anchor '%s'  floor=%ld  x=%.2fm  y=%.2fm",
             c->label, (long)c->floor, c->x_m, c->y_m);
    ESP_LOGI(TAG, "  wifi: ssid='%s' ch=%u %s",
             c->wifi_ssid, c->wifi_channel,
             c->wifi_password[0] ? "wpa2" : "open");
    ESP_LOGI(TAG, "  ble:  major=%u minor=%u txpwr=%d",
             c->ble_major, c->ble_minor, c->ble_measured_power);
}
