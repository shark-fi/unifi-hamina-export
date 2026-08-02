#pragma once
#include "anchor.h"

// Bring up the SoftAP with the 802.11mc FTM *responder* enabled. After this
// returns, initiator STAs can run FTM ranging sessions against this anchor's
// BSSID and derive distance from the timestamps.
//
// Responder is almost entirely a config flag (wifi_ap_config_t.ftm_responder):
// there is no per-session responder API and no responder report event — the
// Wi-Fi firmware answers FTM frames autonomously once the AP is up.
void wifi_ftm_responder_start(const anchor_cfg_t *cfg);
