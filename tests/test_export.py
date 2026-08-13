"""Regression tests for the two failure modes this exporter is prone to.

Both bugs these cover shipped looking correct: the counts were right, so the
export reported success, and only a diff against a real Hamina export revealed
that the *values* were wrong. Nothing raised. That is the shape of defect worth
pinning down here -- structure present, content silently wrong.

Stdlib only, no test framework to install:

    python3 -m unittest discover -s tests -v
"""
import argparse
import contextlib
import io
import json
import os
import struct
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unifi_export as ux
from openintent_import import (
    INNERSPACE_WALL_VARIANTS, WALL_LABEL_TO_VARIANT, wall_variant,
    load_obstacle_sidecar, to_scene as oi_to_scene, _plan_title,
    _synth_mac, _is_placeholder_mac, run_purge, classify_device_shapes,
)


def oi_sidecar(sidecar, floorplans):
    """load_obstacle_sidecar takes a path; feed it one from a dict."""
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "obstacles.json")
        with open(p, "w") as f:
            json.dump(sidecar, f)
        return load_obstacle_sidecar(p, floorplans)

# The wall_type strings Hamina WRITES in its own OpenIntent export, read out of
# a real one. Hamina drops a segment whose wall_type it does not recognise, and
# it matches on these -- not on the display names in its wall-type picker.
#
# This set used to be the picker names, and this file asserted against them. It
# passed while the exporter shipped labels Hamina rejected: a 122-wall plan
# round-tripped and came back with 13, the only ones whose picker name and
# library name happen to be identical ("Concrete"). A green suite proved
# consistency with a list nobody had checked against the product.
#
# Add a name here only from an export that contains it.
HAMINA_EXPORT_WALL_TYPES = {
    "Brick wall", "Concrete", "Door Interior", "Dry wall", "Exterior Window",
    "Metal door / wall",
}
# Labels the exporter emits that no Hamina export has been seen to contain.
# They came from the picker, which is exactly the source that proved wrong for
# the six above, so each is a wall Hamina may silently drop.
UNVERIFIED_LABELS = {
    "Drywall (Heavy)", "Glass", "Glass (Thin)", "Metal", "Wood", "Door (Glass)",
}
# Hamina types with no built-in InnerSpace variant to store them in. A wall
# drawn as a *custom* InnerSpace type keeps its name and round-trips exactly
# (see CustomWallTypes); one mapped onto a built-in on the way in can only come
# back as that built-in. Cubicle and Elevator are attenuation objects in
# InnerSpace rather than walls, so they land on the nearest wall material.
DEGRADES_TO = {
    "Railing": "Dry wall",
    "Cubicle": "Dry wall",
    "Elevator": "Metal",
    "Window (Tinted)": "Exterior Window",
}


class WallVocabulary(unittest.TestCase):
    """Wall labels must be spelled the way Hamina spells them."""

    def test_every_innerspace_variant_has_a_label(self):
        """A variant missing here falls through to .title() of its own name --
        'Drywall_Heavy' -- which Hamina drops."""
        missing = INNERSPACE_WALL_VARIANTS - set(ux.WALL_VARIANTS)
        self.assertEqual(missing, set(),
                         f"InnerSpace variants with no OpenIntent label: "
                         f"{sorted(missing)}")

    def test_no_label_looks_like_a_title_cased_variant(self):
        """The old fallback produced 'Window_1_Pane'; nothing should now."""
        for variant, (label, _att) in ux.WALL_VARIANTS.items():
            self.assertNotIn("_", label,
                             f"{variant!r} exports as {label!r}, which looks "
                             f"like the .title() fallback, not a Hamina type")

    def test_every_label_is_confirmed_or_declared_unverified(self):
        """A label is either seen in a Hamina export or flagged as a guess.

        The failure this guards is not a typo -- it is confidence. The previous
        version of this test asserted against Hamina's PICKER names and passed
        while the exporter dropped 109 of 122 walls on a real plan.
        """
        for variant, (label, _att) in ux.WALL_VARIANTS.items():
            self.assertIn(
                label, HAMINA_EXPORT_WALL_TYPES | UNVERIFIED_LABELS,
                f"{variant!r} exports as {label!r}, which is neither confirmed "
                f"from a Hamina export nor declared unverified. Read the label "
                f"out of an export before adding it.")

    def test_the_six_confirmed_labels_are_exactly_what_hamina_emits(self):
        """Byte-for-byte, because 'Dry wall' vs 'Drywall' loses every wall."""
        self.assertEqual(ux.WALL_VARIANTS["concrete"][0], "Concrete")
        self.assertEqual(ux.WALL_VARIANTS["drywall"][0], "Dry wall")
        self.assertEqual(ux.WALL_VARIANTS["brick"][0], "Brick wall")
        self.assertEqual(ux.WALL_VARIANTS["door_wood"][0], "Door Interior")
        self.assertEqual(ux.WALL_VARIANTS["door_metal"][0], "Metal door / wall")
        self.assertEqual(ux.WALL_VARIANTS["window_1_pane"][0], "Exterior Window")

    def test_a_real_export_round_trips_every_wall(self):
        """The 122-wall plan that exposed this, as counts per label.

        In: what Hamina wrote. Out: what we send back. Any label that changes
        spelling is a wall Hamina will not recognise.
        """
        hamina_out = {"Concrete": 13, "Metal door / wall": 3, "Dry wall": 47,
                      "Door Interior": 15, "Brick wall": 32,
                      "Exterior Window": 12}
        returned = {}
        for label, n in hamina_out.items():
            variant = wall_variant(label)
            self.assertIn(variant, ux.WALL_VARIANTS,
                          f"{label!r} has no InnerSpace variant to store it in")
            back = ux.WALL_VARIANTS[variant][0]
            returned[back] = returned.get(back, 0) + n
        self.assertEqual(returned, hamina_out,
                         "every wall must come back under the label it went in "
                         "with, or Hamina drops it")

    def test_shared_labels_invert_to_the_first_variant(self):
        """Hamina draws no distinction between window pane counts, so all three
        pane variants share a label. The inverse must still be deterministic
        and land on the single-pane variant."""
        self.assertEqual(WALL_LABEL_TO_VARIANT["Exterior Window"],
                         "window_1_pane")
        for label in {lbl for lbl, _ in ux.WALL_VARIANTS.values()}:
            self.assertIn(label, WALL_LABEL_TO_VARIANT,
                          f"{label!r} missing from the inverse")

    def test_hamina_types_survive_the_round_trip(self):
        """Hamina -> InnerSpace variant -> back out returns the same label,
        except where InnerSpace has no variant to store the material in."""
        for wall_type in sorted(HAMINA_EXPORT_WALL_TYPES):
            variant = wall_variant(wall_type)
            if variant not in ux.WALL_VARIANTS:
                continue        # Cubicle/Elevator are attenuation objects
            back = ux.WALL_VARIANTS[variant][0]
            expected = DEGRADES_TO.get(wall_type, wall_type)
            self.assertEqual(back, expected,
                             f"{wall_type!r} returned as {back!r}")


def _wall_label(shape, wall_types):
    """The exporter's wall-label decision, isolated from the plan loop."""
    variant = shape.get("variant")
    if variant == "custom":
        return ((wall_types.get(shape.get("wallTypeId"), {})
                 .get("name") or "").strip() or "Wall")
    if variant in ux.WALL_VARIANTS:
        return ux.WALL_VARIANTS[variant][0]
    return str(variant or "Wall").title()


class CustomWallTypes(unittest.TestCase):
    """A custom InnerSpace wall carries variant 'custom' and its real name in
    wallTypes. Reading only the variant exported every one of them as
    'Custom', which Hamina drops."""

    TYPES = {"wt-1": {"id": "wt-1", "name": "Fireplace", "isCustom": True}}

    def test_custom_wall_keeps_its_name(self):
        label = _wall_label({"variant": "custom", "wallTypeId": "wt-1"},
                            self.TYPES)
        self.assertEqual(label, "Fireplace")

    def test_custom_wall_does_not_export_as_the_word_custom(self):
        label = _wall_label({"variant": "custom", "wallTypeId": "wt-1"},
                            self.TYPES)
        self.assertNotEqual(label, "Custom")

    def test_unnamed_custom_wall_falls_back(self):
        label = _wall_label({"variant": "custom", "wallTypeId": "missing"}, {})
        self.assertEqual(label, "Wall")

    def test_builtin_variant_still_uses_the_table(self):
        """A built-in variant takes its label from the table, not the project's
        wall-type names — and the table now spells them Hamina's way."""
        self.assertEqual(_wall_label({"variant": "door_wood"}, self.TYPES),
                         "Door Interior")


def _dev(stats, table=None, **kw):
    d = {"name": "AP", "model": "U7PRO", "radio_table_stats": stats,
         "radio_table": table if table is not None else []}
    d.update(kw)
    return d


# tx_power in radio_table is the configured floor (it equals min_txpower), not
# the power in use -- the whole reason live state has to win.
CONFIGURED = [{"radio": "ng", "channel": 6, "ht": 20, "nss": 2, "tx_power": 6,
               "min_txpower": 6},
              {"radio": "na", "channel": 36, "ht": 80, "nss": 2, "tx_power": 6,
               "min_txpower": 6}]
LIVE = [{"radio": "ng", "state": "RUN", "channel": 6, "bw": 20, "tx_power": 6,
         "num_sta": 3},
        {"radio": "na", "state": "RUN", "channel": 36, "bw": 40, "tx_power": 26,
         "num_sta": 9}]


class LiveRadioState(unittest.TestCase):
    """Radios come from radio_table_stats, not radio_table."""

    def test_operating_width_beats_configured_width(self):
        """Configured ht 80 while bw reports 40: 80 would have Hamina predict
        twice the channel width that is actually on air."""
        r = {x["band"]: x for x in ux.oi_radios(_dev(LIVE, CONFIGURED))}
        self.assertEqual(r["FREQ_5GHZ"]["channel_width"], "40_MHz")

    def test_operating_power_beats_configured_floor(self):
        r = {x["band"]: x for x in ux.oi_radios(_dev(LIVE, CONFIGURED))}
        self.assertEqual(r["FREQ_5GHZ"]["transmit_power"], 26)

    def test_configured_power_is_never_a_fallback(self):
        """With no live power the field is omitted. Falling back to
        radio_table would understate the radio by up to 20 dB."""
        stats = [{"radio": "na", "state": "RUN", "channel": 36, "bw": 40}]
        r = ux.oi_radios(_dev(stats, CONFIGURED))[0]
        self.assertNotIn("transmit_power", r)

    def test_down_radio_is_dropped_and_ids_stay_contiguous(self):
        stats = [{"radio": "ng", "state": "INIT", "last_channel": 0},
                 {"radio": "na", "state": "RUN", "channel": 36, "bw": 40,
                  "tx_power": 26}]
        radios = ux.oi_radios(_dev(stats, CONFIGURED))
        self.assertEqual([r["band"] for r in radios], ["FREQ_5GHZ"])
        self.assertEqual([r["id"] for r in radios], list(range(len(radios))))

    def test_include_down_keeps_it(self):
        stats = [{"radio": "ng", "state": "INIT", "last_channel": 0},
                 {"radio": "na", "state": "RUN", "channel": 36, "bw": 40,
                  "tx_power": 26}]
        radios = ux.oi_radios(_dev(stats, CONFIGURED), include_down=True)
        self.assertEqual([r["band"] for r in radios],
                         ["FREQ_2.4GHZ", "FREQ_5GHZ"])

    def test_offline_device_falls_back_to_configured_channel_and_width(self):
        """No live state at all: better a configured plan than no AP. ht is a
        string on some models, so the width must coerce."""
        table = [{"radio": "na", "channel": 108, "ht": "80", "nss": 4,
                  "tx_power": 6}]
        r = ux.oi_radios(_dev([], table))[0]
        self.assertEqual(r["channel"], 108)
        self.assertEqual(r["channel_width"], "80_MHz")
        self.assertEqual(r["mimo_chains"], 4)
        self.assertNotIn("transmit_power", r)

    def test_auto_channel_resolves_from_live_state(self):
        table = [{"radio": "na", "channel": "auto", "ht": 80, "nss": 2}]
        stats = [{"radio": "na", "state": "RUN", "channel": 36, "bw": 40,
                  "tx_power": 26}]
        r = ux.oi_radios(_dev(stats, table))[0]
        self.assertEqual(r["channel"], 36)
        self.assertEqual(r["channel_assignment"], "AUTOMATIC")

    def test_settled_channel_does_not_claim_manual(self):
        """RRM writes the resolved channel back into radio_table, so an int
        there cannot prove the radio was pinned by hand. Asserting MANUAL would
        stop Hamina re-optimising it."""
        r = ux.oi_radios(_dev(LIVE, CONFIGURED))[0]
        self.assertNotIn("channel_assignment", r)


class CsvRadioColumns(unittest.TestCase):
    """CSV radio columns come from live stats in every mode."""

    def test_all_columns_populate(self):
        """The innerspace path once filled these from the exported OpenIntent
        radios, which carry no bw, state or client count -- so bw_*, sta_* and
        rstate_* came out empty on every row."""
        row = ux.blank_row()
        ux.fill_radio_columns(row, _dev(LIVE))
        self.assertEqual(row["bw_5g"], 40)
        self.assertEqual(row["txpw_5g"], 26)
        self.assertEqual(row["sta_5g"], 9)
        self.assertEqual(row["rstate_5g"], "RUN")

    def test_down_radio_is_still_reported(self):
        """A radio dropped from the export is exactly the one worth seeing in
        a diagnostic CSV, and rstate_* is what says it is down."""
        stats = [{"radio": "ng", "state": "INIT", "last_channel": 0}]
        row = ux.blank_row()
        ux.fill_radio_columns(row, _dev(stats))
        self.assertEqual(row["rstate_2g"], "INIT")
        self.assertEqual(row["ch_2g"], "")

    def test_unjoined_device_leaves_columns_blank(self):
        row = ux.blank_row()
        ux.fill_radio_columns(row, {})
        self.assertEqual(row["rstate_5g"], "")

    def test_every_band_has_a_full_column_set(self):
        for band in ("2g", "5g", "6g"):
            for prefix in ("ch", "bw", "txpw", "sta", "rstate"):
                self.assertIn(f"{prefix}_{band}", ux.CSV_COLUMNS)


SWITCH_DEV = {
    "name": "USW Pro Max 24 PoE", "serial": "F4E2C6AABBCC",
    "total_max_power": 400,
    "port_table": ([{"port_idx": i, "media": "GE"} for i in range(1, 25)]
                   + [{"port_idx": i, "media": "SFP+"} for i in range(25, 27)]),
}


class Switches(unittest.TestCase):
    """Switches sit in the InnerSpace project already positioned; they were
    filtered out one line before they could be used."""

    def _sw(self, dev=None, sku="USW-Pro-Max-24-PoE"):
        coords = [{"coordinate_xyz": {"x": 10.0, "y": 20.0, "unit": "pixels"}}]
        return ux.oi_switch(dev if dev is not None else SWITCH_DEV, sku,
                            "Upstairs", coords, "USW Pro Max 24 PoE",
                            "192.168.5.3")

    def test_name_is_always_present(self):
        """The only field the OpenIntent 2.0 schema requires."""
        self.assertEqual(self._sw()["name"], "USW Pro Max 24 PoE")
        self.assertEqual(self._sw(dev={}, sku="")["name"],
                         "USW Pro Max 24 PoE")

    def test_position_and_identity_carry_over(self):
        sw = self._sw()
        self.assertEqual(sw["floorplan_name"], "Upstairs")
        self.assertEqual(sw["coordinates"][0]["coordinate_xyz"]["x"], 10.0)
        self.assertEqual(sw["ip_address"], "192.168.5.3")
        self.assertEqual(sw["serial_number"], "F4E2C6AABBCC")
        self.assertEqual(sw["sku"], "USW-Pro-Max-24-PoE")

    def test_ports_split_copper_from_sfp(self):
        sw = self._sw()
        self.assertEqual(sw["copper_port_count"], 24)
        self.assertEqual(sw["modular_port_count"], 2)
        self.assertEqual(sw["poe_budget"], 400)

    def test_ports_without_media_count_as_copper(self):
        """Older firmware omits `media`; those are RJ45, not SFP."""
        dev = {"port_table": [{"port_idx": 1}, {"port_idx": 2}]}
        sw = self._sw(dev=dev)
        self.assertEqual(sw["copper_port_count"], 2)
        self.assertNotIn("modular_port_count", sw)

    def test_unjoined_switch_still_exports(self):
        """No Network-app match: it is still a positioned node, not a drop."""
        sw = self._sw(dev={})
        self.assertEqual(sw["floorplan_name"], "Upstairs")
        for absent in ("serial_number", "poe_budget", "copper_port_count"):
            self.assertNotIn(absent, sw)


def _png(w, h):
    """Enough PNG for image_size(): signature + IHDR width/height."""
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
            + struct.pack(">II", w, h) + b"\x08\x06\x00\x00\x00" + b"\x00" * 8)


PROJECT = {"data": {
    "project": {"title": "Home", "unit": "imperial"},
    "plans": [{"id": "p1", "title": "Upstairs"}],
    "products": [
        {"id": "prod-ap", "sku": "U7-Pro", "category": "wifi"},
        {"id": "prod-sw", "sku": "USW-Pro-Max-24-PoE", "category": "switching"},
        {"id": "prod-cam", "sku": "UVC-G5-Turret-Ultra",
         "category": "camera_security"},
    ],
    "shapes": [
        {"type": "map", "planId": "p1", "urlImage": "/img.png",
         "position": [{"x": 0, "y": 0, "z": 0}], "scale": {"x": 1, "y": 1}},
        {"type": "scale", "planId": "p1", "scale": 10.0, "height": 2.5,
         "position": [{"x": 0, "y": 0, "z": 0}, {"x": 100, "y": 0, "z": 0}]},
        {"type": "wall", "planId": "p1", "variant": "drywall",
         "position": [{"x": -100, "y": -100, "z": 0},
                      {"x": 100, "y": -100, "z": 0}]},
        {"type": "device", "planId": "p1", "productId": "prod-ap",
         "title": "U7-Pro-Bedroom", "mount": "ceiling",
         "meta": {"mac": "9C05D6AEFFDC", "ip": "192.168.5.209"},
         "position": [{"x": 10, "y": 20, "z": 0}]},
        {"type": "device", "planId": "p1", "productId": "prod-sw",
         "title": "USW Pro Max 24 PoE", "mount": "wall",
         "meta": {"mac": "F4E2C6EAEADF", "ip": "192.168.5.3"},
         "position": [{"x": -30, "y": 40, "z": 0}]},
        {"type": "device", "planId": "p1", "productId": "prod-cam",
         "title": "Doorbell", "meta": {"mac": "AABBCCDDEEFF"},
         "position": [{"x": 0, "y": 0, "z": 0}]},
    ],
}}


# The Network app's own label for the switch differs from the InnerSpace shape
# title on purpose: connected_switch.switch_name references switches[], so the
# name we exported has to win over this one.
NETWORK_DEVICES = [
    {"mac": "9c:05:d6:ae:ff:dc", "model": "U7PRO", "name": "U7-Pro-Bedroom",
     "radio_table_stats": LIVE, "radio_table": CONFIGURED,
     "uplink": {"type": "wire", "uplink_mac": "f4:e2:c6:ea:ea:df",
                "uplink_remote_port": 5,
                "uplink_device_name": "USW-Pro-Max-24-PoE"}},
    dict(SWITCH_DEV, mac="f4:e2:c6:ea:ea:df", model="USWPROMAX24POE"),
]


class _FakeHttp:
    def __init__(self, *a, **kw):
        pass

    def request(self, method, url, raw=False):
        return 200, {"Content-Type": "image/png"}, _png(1000, 600)

    def get_json(self, url):
        if url.endswith("/self/sites"):
            return {"data": [{"name": "default"}]}
        if url.endswith("/stat/device"):
            return {"data": NETWORK_DEVICES}
        return {"data": []}


class ConnectedSwitch(unittest.TestCase):
    """AP -> switch port topology, from the wired uplink."""

    WIRED = {"uplink": {"type": "wire", "uplink_mac": "f4:e2:c6:ea:ea:df",
                        "uplink_remote_port": 5,
                        "uplink_device_name": "Network label"}}

    def test_placed_switch_name_wins_over_the_network_label(self):
        cs = ux.oi_connected_switch(
            self.WIRED, {"F4E2C6EAEADF": "USW Pro Max 24 PoE"})
        self.assertEqual(cs["switch_name"], "USW Pro Max 24 PoE")

    def test_falls_back_to_the_network_label(self):
        """An unplaced switch still records real topology."""
        cs = ux.oi_connected_switch(self.WIRED, {})
        self.assertEqual(cs["switch_name"], "Network label")

    def test_port_is_a_string(self):
        """UniFi reports an int; the schema types this field as a string."""
        cs = ux.oi_connected_switch(self.WIRED, {})
        self.assertEqual(cs["port"], "5")

    def test_switch_id_carries_the_mac(self):
        cs = ux.oi_connected_switch(self.WIRED, {})
        self.assertEqual(cs["switch_id"], "f4:e2:c6:ea:ea:df")

    def test_wireless_uplink_gets_no_switch(self):
        """A meshed AP has no switch port; claiming one invents topology."""
        dev = {"uplink": {"type": "wireless", "uplink_mac": "aa:bb:cc:dd:ee:ff"}}
        self.assertIsNone(ux.oi_connected_switch(dev, {}))

    def test_no_uplink_at_all(self):
        self.assertIsNone(ux.oi_connected_switch({}, {}))
        self.assertIsNone(ux.oi_connected_switch({"uplink": {}}, {}))

    def test_absent_port_still_yields_the_switch(self):
        dev = {"uplink": {"type": "wire", "uplink_mac": "f4:e2:c6:ea:ea:df"}}
        cs = ux.oi_connected_switch(dev, {})
        self.assertNotIn("port", cs)
        self.assertEqual(cs["switch_id"], "f4:e2:c6:ea:ea:df")


class SwitchExportEndToEnd(unittest.TestCase):
    """Drive run_innerspace over a synthetic project and read the zip back."""

    def _run(self, **overrides):
        args = argparse.Namespace(
            host="https://console", username="u", password="p",
            verify_tls=False, no_radio=False, no_walls=False, no_switches=False,
            plan=None, ap_height=2.5, include_down_radios=False)
        with tempfile.TemporaryDirectory() as tmp:
            proj = os.path.join(tmp, "project.json")
            with open(proj, "w") as f:
                json.dump(PROJECT, f)
            args.project_json = proj
            args.openintent = os.path.join(tmp, "out.zip")
            for k, v in overrides.items():
                setattr(args, k, v)
            real_http, real_login = ux.Http, ux.legacy_login
            ux.Http, ux.legacy_login = _FakeHttp, lambda *a, **kw: None
            try:
                rows = ux.run_innerspace(args)
            finally:
                ux.Http, ux.legacy_login = real_http, real_login
            with zipfile.ZipFile(args.openintent) as zf:
                return json.load(zf.open("openintent.json")), rows

    def test_switch_reaches_the_openintent_zip(self):
        data, _rows = self._run()
        self.assertEqual([s["name"] for s in data["switches"]],
                         ["USW Pro Max 24 PoE"])
        self.assertEqual(len(data["accesspoints"]), 1)

    def test_switch_is_positioned_on_the_same_plan_as_the_ap(self):
        data, _rows = self._run()
        sw, ap = data["switches"][0], data["accesspoints"][0]
        self.assertEqual(sw["floorplan_name"], ap["floorplan_name"])
        self.assertIn("coordinates", sw)

    def test_non_network_gear_is_still_skipped(self):
        """A camera is not a switch; only 'switching' comes through."""
        data, _rows = self._run()
        names = [s["name"] for s in data["switches"]]
        self.assertNotIn("Doorbell", names)

    def test_no_switches_flag_omits_the_key(self):
        data, rows = self._run(no_switches=True)
        self.assertNotIn("switches", data)
        self.assertEqual([r["type"] for r in rows], ["uap"])

    def test_ap_references_the_exported_switch_by_name(self):
        data, _rows = self._run()
        cs = data["accesspoints"][0]["connected_switch"]
        names = [s["name"] for s in data["switches"]]
        self.assertIn(cs["switch_name"], names,
                      "connected_switch must reference a switch in switches[]")
        self.assertEqual(cs["port"], "5")

    def test_switch_port_counts_come_from_the_network_join(self):
        data, _rows = self._run()
        sw = data["switches"][0]
        self.assertEqual(sw["copper_port_count"], 24)
        self.assertEqual(sw["modular_port_count"], 2)
        self.assertEqual(sw["poe_budget"], 400)

    def test_switch_gets_a_csv_row_typed_usw(self):
        _data, rows = self._run()
        types = sorted(r["type"] for r in rows)
        self.assertEqual(types, ["uap", "usw"])
        sw_row = next(r for r in rows if r["type"] == "usw")
        self.assertEqual(sw_row["ip"], "192.168.5.3")
        self.assertEqual(sw_row["rstate_5g"], "")


class YConvention(unittest.TestCase):
    """OpenIntent pixel y measures UP from the bottom-left.

    Nothing catches this being wrong through a Hamina -> InnerSpace -> Hamina
    round trip, because the same convention applies in both directions. It only
    shows when something draws the coordinates against the image."""

    def test_scene_to_pixels_does_not_flip_y(self):
        """A scene point above the image centre must land in the upper half of
        OpenIntent's y range, not the lower."""
        shape = {"position": [{"x": 0, "y": 0}], "scale": {"x": 1, "y": 1}}
        _x, y = ux.scene_to_pixels({"x": 0, "y": 100}, shape, 1000.0, 600.0)
        self.assertEqual(y, 400.0)

    def test_scene_to_pixels_is_its_own_inverse_via_to_scene(self):
        shape = {"position": [{"x": 0, "y": 0}], "scale": {"x": 1, "y": 1}}
        px, py = ux.scene_to_pixels({"x": 30, "y": -80}, shape, 1000.0, 600.0)
        self.assertEqual(oi_to_scene(px, py, 1000.0, 600.0), (30.0, -80.0))

    def test_obstacle_sidecar_y_is_measured_from_the_top(self):
        """The side-car is authored against the image, so an obstacle near the
        top of the plan must end up near the top -- which means its y is
        flipped into OpenIntent's bottom-up space on the way in."""
        floorplans = [{"name": "Upstairs", "img_w": 1000.0, "img_h": 600.0}]
        sidecar = {"obstacles": [{
            "floorplan": "Upstairs", "material": "car", "unit": "pixels",
            "polygon": [[100, 50], [200, 50], [200, 100], [100, 100]]}]}
        parsed = oi_sidecar(sidecar, floorplans)
        ys = [y for _x, y in parsed["Upstairs"][0]["polygon"]]
        # authored at y 50..100 from the top of a 600px image
        self.assertEqual(sorted(ys), [500.0, 500.0, 550.0, 550.0])

    def test_obstacle_x_is_untouched(self):
        floorplans = [{"name": "Upstairs", "img_w": 1000.0, "img_h": 600.0}]
        sidecar = {"obstacles": [{
            "floorplan": "Upstairs", "material": "car", "unit": "pixels",
            "polygon": [[100, 50], [200, 50], [200, 100]]}]}
        parsed = oi_sidecar(sidecar, floorplans)
        xs = sorted(x for x, _y in parsed["Upstairs"][0]["polygon"])
        self.assertEqual(xs, [100.0, 200.0, 200.0])


if __name__ == "__main__":
    unittest.main()


class PlanTitle(unittest.TestCase):
    """Plan titles must survive the round trip Hamina -> UniFi -> Hamina.

    Hamina matches a vendor floor to its own map BY NAME ("floor plans must
    match!"). A decorated or clipped title stops the plan resolving back to the
    map it came from, and Hamina reports it as an unattributable import error
    that names nothing useful. This used to append " (imported)", spending 11 of
    InnerSpace's 32 characters and truncating every name longer than 21.
    """

    def test_name_is_used_verbatim(self):
        self.assertEqual(_plan_title(None, "Floor-Plan-Size-Upstairs"),
                         "Floor-Plan-Size-Upstairs")

    def test_no_decoration_is_added(self):
        for name in ("Basement", "Level 3", "Floor-Plan-Size-Basement"):
            self.assertEqual(_plan_title(None, name), name)
            self.assertNotIn("(imported)", _plan_title(None, name))

    def test_name_at_the_limit_is_untouched(self):
        name = "X" * 32
        self.assertEqual(_plan_title(None, name), name)

    def test_over_limit_is_clipped_and_warned(self):
        name = "Y" * 40
        err = io.StringIO()
        stderr, sys.stderr = sys.stderr, err
        try:
            title = _plan_title(None, name)
        finally:
            sys.stderr = stderr
        self.assertEqual(len(title), 32)
        self.assertIn("no longer match a Hamina map", err.getvalue())

    def test_override_still_wins(self):
        self.assertEqual(_plan_title("Custom", "Anything"), "Custom")
        self.assertEqual(len(_plan_title("Z" * 40, "Anything")), 32)


class PasswordResolution(unittest.TestCase):
    """--password puts the secret in `ps` for every user on the host.

    The bridge runs this exporter as a scheduled subprocess, so the password was
    on that command line continuously. UNIFI_PASSWORD is the way to pass it; the
    flag stays for compatibility and for one-off interactive runs.
    """

    def setUp(self):
        self.env = os.environ.get("UNIFI_PASSWORD")
        os.environ.pop("UNIFI_PASSWORD", None)

    def tearDown(self):
        os.environ.pop("UNIFI_PASSWORD", None)
        if self.env is not None:
            os.environ["UNIFI_PASSWORD"] = self.env

    def _args(self, password=None):
        return argparse.Namespace(password=password, username="admin")

    def test_flag_wins(self):
        os.environ["UNIFI_PASSWORD"] = "from-env"
        self.assertEqual(ux.resolve_password(self._args("from-flag")), "from-flag")

    def test_environment_is_used_when_no_flag(self):
        os.environ["UNIFI_PASSWORD"] = "from-env"
        self.assertEqual(ux.resolve_password(self._args()), "from-env")

    def test_no_password_and_no_terminal_exits_with_a_reason(self):
        """A non-interactive caller must not block forever on an invisible
        prompt — that is how a scheduled job hangs until someone notices."""
        err = io.StringIO()
        stderr, sys.stderr = sys.stderr, err
        try:
            with self.assertRaises(SystemExit):
                ux.resolve_password(self._args())
        finally:
            sys.stderr = stderr
        self.assertIn("UNIFI_PASSWORD", err.getvalue())


class DeviceIdentitySurvivesTheExport(unittest.TestCase):
    """The MAC is the only thing tying an OpenIntent AP to real hardware.

    Dropping it does not fail: the AP is exported, imported, and placed. It just
    gets a synthesized MAC on the way in, so the console ends up holding a
    placeholder next to the adopted AP of the same name -- a duplicate in the
    device list, with the real device still offered as available to add. Counts
    right, contents wrong, nothing raised.
    """

    def test_mac_is_carried_into_the_openintent_ap(self):
        ap = ux.oi_ap({"name": "U7-Pro-Furnace", "model": "U7PRO",
                       "mac": "78:8a:20:dd:7b:74"}, "Plan", None)
        self.assertEqual(ap.get("mac_address"), "78:8a:20:dd:7b:74")

    def test_case_and_spacing_are_normalised(self):
        """The importer upper-cases and strips colons; feed it one shape."""
        ap = ux.oi_ap({"name": "AP", "model": "U7PRO",
                       "mac": " 78:8A:20:DD:7B:74 "}, "Plan", None)
        self.assertEqual(ap.get("mac_address"), "78:8a:20:dd:7b:74")

    def test_no_mac_means_no_field_rather_than_an_empty_one(self):
        """An empty string is a value; the importer must see absence."""
        ap = ux.oi_ap({"name": "AP", "model": "U7PRO"}, "Plan", None)
        self.assertNotIn("mac_address", ap)


class PlaceholderMacDetection(unittest.TestCase):
    """What --purge-placeholders selects on.

    Locally administered is the whole safety argument: real UniFi hardware is
    globally administered without exception, so a placeholder can be told from
    adopted kit by one bit -- no name matching, no heuristics, no chance of
    removing something real.
    """

    def test_a_synthesized_mac_is_recognised(self):
        self.assertTrue(_is_placeholder_mac(_synth_mac("U7-Pro-Furnace")))

    def test_it_is_stable_so_reimports_do_not_multiply(self):
        self.assertEqual(_synth_mac("DK Kitchen 7 AP"), _synth_mac("DK Kitchen 7 AP"))

    def test_real_ubiquiti_macs_are_never_selected(self):
        # every Ubiquiti OUI in the field: the 0x02 bit is clear in all of them
        for mac in ("788A20AABBCC", "F492BFAABBCC", "802AA846AF10",
                    "245A4CAABBCC", "E43883AABBCC", "687251AABBCC"):
            self.assertFalse(_is_placeholder_mac(mac), mac)

    def test_the_bit_is_what_counts_not_the_prefix(self):
        """Written after a fabricated "real" MAC in this very test failed it.

        AA:BB:CC looks like a plausible address and is locally administered, so
        it would be selected. Nothing here reads the OUI -- only the bit.
        """
        for mac in ("AABBCCDD7B74", "02ABCDEF0123", "0A0000000000",
                    "0600000000000"[:12]):
            self.assertTrue(_is_placeholder_mac(mac), mac)

    def test_junk_is_not_treated_as_a_placeholder(self):
        """Unparseable must fail closed -- keeping a stray beats deleting real kit."""
        for mac in ("", "zz", "!", "0"):
            self.assertFalse(_is_placeholder_mac(mac), repr(mac))


class PurgeSelection(unittest.TestCase):
    """What --purge-placeholders will and will not remove.

    Modelled on a real project that had 55 device shapes: five APs appearing
    twice -- once placed, once stranded with no planId after a plan was deleted
    out from under its shapes -- alongside 39 Protect and Access devices that
    are simply not placed and must never be touched.
    """

    PLANS = [{"id": "P1", "title": "Basement"}, {"id": "P2", "title": "Upstairs"}]

    @staticmethod
    def shape(title, mac, plan):
        return {"type": "device", "title": title, "planId": plan,
                "meta": {"mac": mac}}

    def purge(self, shapes):
        """run_purge against an offline project dump; returns what it printed."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "project.json")
            with open(path, "w") as f:
                json.dump({"data": {"plans": self.PLANS, "shapes": shapes}}, f)
            args = argparse.Namespace(
                project_json=path, commit=False, host="", username="",
                password="", verify_tls=False)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                run_purge(args)
            return buf.getvalue()

    def test_a_stranded_copy_of_a_placed_device_is_removed(self):
        out = self.purge([
            self.shape("U7-Pro-Furnace", "8C3066647B74", "P1"),
            self.shape("U7-Pro-Furnace", "8C3066647B74", None),
        ])
        self.assertIn("1 removable shape(s); 1 left alone", out)
        self.assertIn("duplicate of a placed device", out)

    def test_a_device_listed_once_is_never_removed(self):
        """The console's ordinary "available to add" entry.

        U6P-Furnace was unplaced and had no second copy; selecting it would have
        deleted a legitimate entry the user explicitly wanted kept.
        """
        out = self.purge([self.shape("U6P-Furnace", "FCECDAFFE998", None)])
        self.assertIn("0 removable shape(s); 1 left alone", out)

    def test_devices_from_other_applications_are_left_alone(self):
        """Protect cameras and Access readers are unplaced and not adopted in
        the Network app. An earlier audit called all 39 of them orphans."""
        out = self.purge([
            self.shape("Front Doorbell", "F4E2C60EA138", None),
            self.shape("Backyard PTZ", "28704E1FF02B", None),
            self.shape("Water Softener", "9041B2344F8A", None),
        ])
        self.assertIn("0 removable shape(s); 3 left alone", out)

    def test_a_placed_device_is_never_removed(self):
        out = self.purge([self.shape("MHz-Home", "68D79A50EAF1", "P1")])
        self.assertIn("0 removable shape(s); 1 left alone", out)

    def test_the_whole_project_shape(self):
        """Five duplicates, one placeholder, everything else untouched."""
        aps = [("U7 Pro Outdoor", "942A6F5C41DC", "P2"),
               ("U7-Pro-Apartment", "F4E2C6FF8C3D", "P1"),
               ("U7-Pro-Bedroom", "9C05D6AEFFDC", "P2"),
               ("U7-Pro-Furnace", "8C3066647B74", "P1"),
               ("U7-Pro-Max-Kitchen", "942A6F32ADDE", "P2")]
        shapes = []
        for name, mac, plan in aps:
            shapes.append(self.shape(name, mac, plan))
            shapes.append(self.shape(name, mac, None))     # the stranded copy
        shapes.append(self.shape("U6P-Furnace", "FCECDAFFE998", None))
        shapes.append(self.shape("Front Doorbell", "F4E2C60EA138", None))
        shapes.append(self.shape("ghost", _synth_mac("ghost"), None))

        out = self.purge(shapes)
        self.assertIn("6 removable shape(s); 7 left alone", out)
        for kept in ("U6P-Furnace", "Front Doorbell"):
            self.assertNotIn(kept, out, "%s must survive" % kept)


class PlaceholderCountReporting(unittest.TestCase):
    """The per-plan summary printed just before you decide to --commit.

    It said "2 AP(s) (3 with synthesized placeholder MAC)" on a real import.
    More placeholders than devices cannot happen, and a number that is visibly
    impossible costs you the ability to trust the ones beside it.
    """

    @staticmethod
    def count(aps, placed_names):
        """The corrected expression: count over what is actually created."""
        placed = [a for a in aps if a["name"] in placed_names]
        return sum(1 for a in placed if a.get("mac_synth")), len(placed)

    def test_an_ap_skipped_for_its_model_is_not_counted(self):
        aps = [{"name": "AP1", "mac_synth": True},
               {"name": "AP2", "mac_synth": True},
               {"name": "AP4", "mac_synth": True}]        # no product match
        n_synth, n_dev = self.count(aps, {"AP1", "AP2"})
        self.assertEqual((n_synth, n_dev), (2, 2))
        self.assertLessEqual(n_synth, n_dev, "placeholders cannot exceed devices")

    def test_matched_aps_do_not_inflate_the_count(self):
        aps = [{"name": "AP1", "mac_synth": False},
               {"name": "AP2", "mac_synth": True}]
        n_synth, n_dev = self.count(aps, {"AP1", "AP2"})
        self.assertEqual((n_synth, n_dev), (1, 2))


class SharedRemovableRule(unittest.TestCase):
    """The audit and the purge must describe one project identically.

    They once reported 8 device shapes and 5 for the same console, which read
    as a filtering bug in one of them. It was neither: the project changed
    between the runs. The rule now lives in one function both import, so that
    explanation is the ONLY one left when the numbers differ.
    """

    PLANS = [{"id": "P1", "title": "Downstairs"}, {"id": "P2", "title": "Upstairs"}]

    @staticmethod
    def dev(title, mac, plan):
        return {"type": "device", "title": title, "planId": plan,
                "meta": {"mac": mac}}

    def classify(self, shapes):
        return classify_device_shapes({"plans": self.PLANS, "shapes": shapes})

    def test_a_stranded_copy_of_a_placed_device_is_removable(self):
        rem, kept, total = self.classify([
            self.dev("AP", "A89C6C2AAA7D", None),
            self.dev("AP", "A89C6C2AAA7D", "P2")])
        self.assertEqual((len(rem), len(kept), total), (1, 1, 2))
        self.assertEqual(rem[0][1], "duplicate of a placed device")
        self.assertIsNone(rem[0][0]["planId"], "must remove the plan-less copy")

    def test_a_placeholder_is_removable_even_when_placed(self):
        rem, _kept, _t = self.classify([self.dev("X", "7EACFC138E98", "P1")])
        self.assertEqual([w for _s, w in rem], ["placeholder"])

    def test_a_device_listed_once_is_never_removable(self):
        rem, kept, _t = self.classify([
            self.dev("Only copy", "A89C6C2AA992", "P1"),
            self.dev("Unplaced, no twin", "A89C6CA05806", None)])
        self.assertEqual((len(rem), len(kept)), (0, 2))

    def test_the_totals_always_reconcile(self):
        """removable + kept == total, whatever the input."""
        for shapes in ([], [self.dev("a", "A89C6C2AAA7D", None)],
                       [self.dev("a", "7E00000000AA", "P1"),
                        self.dev("b", "A89C6C2AA992", "P2"),
                        self.dev("b", "A89C6C2AA992", None)]):
            rem, kept, total = self.classify(shapes)
            self.assertEqual(len(rem) + len(kept), total)

    def test_non_device_shapes_are_not_counted(self):
        rem, kept, total = self.classify([
            {"type": "wall", "planId": "P1"}, {"type": "scale", "planId": "P1"},
            self.dev("AP", "A89C6C2AA992", "P1")])
        self.assertEqual(total, 1, "walls and scale lines are not devices")
        self.assertEqual((len(rem), len(kept)), (0, 1))
