#pragma once
#include "anchor.h"

// Start a non-connectable iBeacon advertisement carrying the anchor identity
// (UUID = deployment, major = site/floor, minor = anchor id). Coarse presence
// + a stable key the backend maps back to the surveyed coordinate.
//
// Runs the NimBLE host on its own FreeRTOS task; returns once advertising has
// been scheduled to start (on stack sync).
void ble_beacon_start(const anchor_cfg_t *cfg);
