idf_component_register(
    SRCS
        "app_main.c"
        "anchor.c"
        "wifi_ftm.c"
        "ble_beacon.c"
    INCLUDE_DIRS "."
    REQUIRES
        nvs_flash
        esp_wifi
        esp_netif
        esp_event
        bt
)
