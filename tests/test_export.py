"""Regression tests for the two failure modes this exporter is prone to.

Both bugs these cover shipped looking correct: the counts were right, so the
export reported success, and only a diff against a real Hamina export revealed
that the *values* were wrong. Nothing raised. That is the shape of defect worth
pinning down here -- structure present, content silently wrong.

Stdlib only, no test framework to install:

    python3 -m unittest discover -s tests -v
"""
import argparse
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
)

# Hamina's built-in wall types, read off its own wall-type picker. It drops a
# segment whose wall_type it does not recognise rather than defaulting it, so a
# label outside this set (plus whatever custom types the project defines) loses
# the wall silently.
HAMINA_WALL_TYPES = {
    "Brick", "Concrete", "Cubicle", "Door (Glass)", "Door (Metal)",
    "Door (Wooden)", "Drywall", "Drywall (Heavy)", "Elevator", "Glass",
    "Glass (Thin)", "Metal", "Railing", "Window", "Window (Tinted)", "Wood",
}
# Hamina types with no built-in InnerSpace variant to store them in. A wall
# drawn as a *custom* InnerSpace type keeps its name and round-trips exactly
# (see CustomWallTypes); one mapped onto a built-in on the way in can only come
# back as that built-in. Cubicle and Elevator are attenuation objects in
# InnerSpace rather than walls, so they land on the nearest wall material.
DEGRADES_TO = {
    "Railing": "Drywall",
    "Cubicle": "Drywall",
    "Elevator": "Metal",
    "Window (Tinted)": "Window",
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

    def test_every_label_is_a_hamina_wall_type(self):
        """The whole point: a label Hamina does not offer is a lost wall."""
        for variant, (label, _att) in ux.WALL_VARIANTS.items():
            self.assertIn(label, HAMINA_WALL_TYPES,
                          f"{variant!r} exports as {label!r}, which is not a "
                          f"Hamina wall type and will be dropped")

    def test_shared_labels_invert_to_the_first_variant(self):
        """Hamina draws no distinction between window pane counts, so all three
        pane variants share a label. The inverse must still be deterministic
        and land on the single-pane variant."""
        self.assertEqual(WALL_LABEL_TO_VARIANT["Window"], "window_1_pane")
        for label in {lbl for lbl, _ in ux.WALL_VARIANTS.values()}:
            self.assertIn(label, WALL_LABEL_TO_VARIANT,
                          f"{label!r} missing from the inverse")

    def test_hamina_types_survive_the_round_trip(self):
        """Hamina -> InnerSpace variant -> back out returns the same label,
        except where InnerSpace has no variant to store the material in."""
        for wall_type in sorted(HAMINA_WALL_TYPES):
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
        self.assertEqual(_wall_label({"variant": "door_wood"}, self.TYPES),
                         "Door (Wooden)")


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


class _FakeHttp:
    def __init__(self, *a, **kw):
        pass

    def request(self, method, url, raw=False):
        return 200, {"Content-Type": "image/png"}, _png(1000, 600)


class SwitchExportEndToEnd(unittest.TestCase):
    """Drive run_innerspace over a synthetic project and read the zip back."""

    def _run(self, **overrides):
        args = argparse.Namespace(
            host="https://console", username="u", password="p",
            verify_tls=False, no_radio=True, no_walls=False, no_switches=False,
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

    def test_switch_gets_a_csv_row_typed_usw(self):
        _data, rows = self._run()
        types = sorted(r["type"] for r in rows)
        self.assertEqual(types, ["uap", "usw"])
        sw_row = next(r for r in rows if r["type"] == "usw")
        self.assertEqual(sw_row["ip"], "192.168.5.3")
        self.assertEqual(sw_row["rstate_5g"], "")


if __name__ == "__main__":
    unittest.main()
