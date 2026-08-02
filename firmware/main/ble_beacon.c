#include "ble_beacon.h"

#include <string.h>

#include "esp_log.h"
#include "nvs_flash.h"

#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "host/ble_hs.h"
#include "host/util/util.h"
#include "services/gap/ble_svc_gap.h"

static const char *TAG = "ble_beacon";

// Cached copy so the sync callback (fires on the host task) can build the frame.
static anchor_cfg_t s_cfg;
static uint8_t      s_own_addr_type;

// Assemble the 30-byte Apple iBeacon advertising payload.
//   AD1  flags:        02 01 06
//   AD2  mfr data:     1A FF 4C00 02 15 <16 uuid> <2 major> <2 minor> <1 power>
static int build_ibeacon(const anchor_cfg_t *c, uint8_t *buf)
{
    int i = 0;
    buf[i++] = 0x02; buf[i++] = 0x01; buf[i++] = 0x06;          // flags

    buf[i++] = 0x1A;                                            // len
    buf[i++] = 0xFF;                                            // mfr specific
    buf[i++] = 0x4C; buf[i++] = 0x00;                          // Apple 0x004C
    buf[i++] = 0x02; buf[i++] = 0x15;                          // iBeacon, 21B

    memcpy(&buf[i], c->ble_uuid, 16); i += 16;
    buf[i++] = c->ble_major >> 8;  buf[i++] = c->ble_major & 0xFF;
    buf[i++] = c->ble_minor >> 8;  buf[i++] = c->ble_minor & 0xFF;
    buf[i++] = (uint8_t)c->ble_measured_power;
    return i;                                                  // == 30
}

static void start_advertising(void)
{
    uint8_t adv[30];
    int len = build_ibeacon(&s_cfg, adv);

    if (ble_gap_adv_set_data(adv, len) != 0) {
        ESP_LOGE(TAG, "adv_set_data failed");
        return;
    }

    struct ble_gap_adv_params p = {
        .conn_mode = BLE_GAP_CONN_MODE_NON,   // non-connectable beacon
        .disc_mode = BLE_GAP_DISC_MODE_NON,   // undirected, non-discoverable
        .itvl_min  = BLE_GAP_ADV_ITVL_MS(200),
        .itvl_max  = BLE_GAP_ADV_ITVL_MS(300),
    };

    int rc = ble_gap_adv_start(s_own_addr_type, NULL, BLE_HS_FOREVER,
                               &p, NULL, NULL);
    if (rc != 0) {
        ESP_LOGE(TAG, "adv_start rc=%d", rc);
        return;
    }
    ESP_LOGI(TAG, "iBeacon advertising (major=%u minor=%u)",
             s_cfg.ble_major, s_cfg.ble_minor);
}

static void on_sync(void)
{
    ble_hs_util_ensure_addr(0);
    if (ble_hs_id_infer_auto(0, &s_own_addr_type) != 0) {
        ESP_LOGE(TAG, "no usable BLE address");
        return;
    }
    start_advertising();
}

static void on_reset(int reason)
{
    ESP_LOGW(TAG, "nimble reset, reason=%d", reason);
}

static void host_task(void *param)
{
    nimble_port_run();            // returns only at nimble_port_stop()
    nimble_port_freertos_deinit();
}

void ble_beacon_start(const anchor_cfg_t *cfg)
{
    s_cfg = *cfg;

    if (nimble_port_init() != ESP_OK) {
        ESP_LOGE(TAG, "nimble_port_init failed");
        return;
    }

    ble_hs_cfg.sync_cb  = on_sync;
    ble_hs_cfg.reset_cb = on_reset;

    ble_svc_gap_init();
    ble_svc_gap_device_name_set(cfg->label);

    nimble_port_freertos_init(host_task);
}
