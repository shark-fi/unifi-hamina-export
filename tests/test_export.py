"""Regression tests for the two failure modes this exporter is prone to.

Both bugs these cover shipped looking correct: the counts were right, so the
export reported success, and only a diff against a real Hamina export revealed
that the *values* were wrong. Nothing raised. That is the shape of defect worth
pinning down here -- structure present, content silently wrong.

Stdlib only, no test framework to install:

    python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unifi_export as ux
from openintent_import import (
    INNERSPACE_WALL_VARIANTS, WALL_LABEL_TO_VARIANT, wall_variant,
)

# Wall types seen in a real Hamina OpenIntent export -- the only labels we can
# claim Hamina accepts. It drops a segment whose wall_type it does not
# recognise rather than defaulting it, so a label outside its vocabulary loses
# the wall silently.
HAMINA_OBSERVED = {
    "Concrete", "Drywall", "Drywall (Heavy)", "Door (Wooden)", "Door (Metal)",
    "Door (Glass)", "Window", "Railing", "Fireplace",
}
# Hamina types with no InnerSpace variant: the material is lost on the way in,
# and the exporter can only pass on what InnerSpace stored. 'Brick' has not
# been seen in a Hamina export, so whether that wall survives import is
# unverified -- confirm against a plan containing a fireplace before treating
# it as safe.
UNREPRESENTABLE = {"Railing": "Drywall", "Fireplace": "Brick"}
UNVERIFIED_TARGETS = {"Brick"}


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

    def test_labels_are_distinct(self):
        """openintent_import.py inverts WALL_VARIANTS; duplicate labels would
        silently drop entries from that inverse."""
        labels = [label for label, _att in ux.WALL_VARIANTS.values()]
        self.assertEqual(len(labels), len(set(labels)), "duplicate wall labels")
        self.assertEqual(len(WALL_LABEL_TO_VARIANT), len(ux.WALL_VARIANTS),
                         "inverse lost entries to a label collision")

    def test_hamina_types_survive_the_round_trip(self):
        """Hamina -> InnerSpace variant -> back out returns the same label,
        except where InnerSpace has no variant to store the material in."""
        for wall_type in sorted(HAMINA_OBSERVED):
            variant = wall_variant(wall_type)
            self.assertIn(variant, ux.WALL_VARIANTS,
                          f"{wall_type!r} maps to variant {variant!r}, which "
                          f"has no label")
            back = ux.WALL_VARIANTS[variant][0]
            expected = UNREPRESENTABLE.get(wall_type, wall_type)
            self.assertEqual(back, expected,
                             f"{wall_type!r} returned as {back!r}")

    def test_representable_types_return_a_label_hamina_accepts(self):
        """Anything Hamina can round-trip must come back in its own
        vocabulary. Degrade targets are excluded only where we have not
        verified the target label against a real export."""
        for wall_type in sorted(HAMINA_OBSERVED):
            back = ux.WALL_VARIANTS[wall_variant(wall_type)][0]
            if back in UNVERIFIED_TARGETS:
                continue
            self.assertIn(back, HAMINA_OBSERVED,
                          f"{wall_type!r} returns as {back!r}, which Hamina "
                          f"will drop")


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


if __name__ == "__main__":
    unittest.main()
