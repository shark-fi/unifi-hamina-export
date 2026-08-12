"""Fitting a path-loss model from (distance, RSSI) pairs.

The fit itself is ordinary regression and hard to get wrong. What is easy to
get wrong is believing it: measurements clustered at one distance produce a
confident exponent that the data never tested, and nothing in the residual
says so.

    python3 -m unittest discover -s tests -v
"""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathloss_calibrate import fit_pathloss, norm_mac


def synth(a1m, n, distances, noise=None):
    noise = noise or [0.0] * len(distances)
    return [(d, a1m - 10.0 * n * math.log10(d) + e)
            for d, e in zip(distances, noise)]


class Fit(unittest.TestCase):
    def test_exact_measurements_recover_the_model(self):
        pts = synth(-40.0, 3.0, [1.0, 3.0, 10.0, 30.0])
        a1m, n, rms, span = fit_pathloss(pts)
        self.assertAlmostEqual(a1m, -40.0, places=6)
        self.assertAlmostEqual(n, 3.0, places=6)
        self.assertLess(rms, 1e-9)

    def test_a_realistic_exponent_is_recovered(self):
        """Indoors is nearer 3-4 than free space's 2."""
        for exp in (2.0, 2.7, 3.5, 4.2):
            _a, n, _r, _s = fit_pathloss(synth(-45.0, exp, [2.0, 5.0, 12.0, 25.0]))
            self.assertAlmostEqual(n, exp, places=6)

    def test_noise_shows_up_in_the_residual(self):
        pts = synth(-40.0, 3.0, [1.5, 4.0, 9.0, 20.0], noise=[3, -4, 2, -2])
        _a, n, rms, _s = fit_pathloss(pts)
        self.assertGreater(rms, 1.0, "a bad fit must not report as clean")
        self.assertLess(abs(n - 3.0), 1.0)

    def test_clustered_distances_are_flagged_by_the_span(self):
        """The number that says "do not trust this exponent".

        Four sensors all roughly 5 m from their gateway fit a line through a
        cluster. The slope is whatever the noise says, the residual is small,
        and the exponent looks calibrated. Only the span reveals it.
        """
        tight = fit_pathloss(synth(-40.0, 3.0, [4.8, 5.0, 5.2, 5.4]))[3]
        wide = fit_pathloss(synth(-40.0, 3.0, [1.0, 4.0, 12.0, 30.0]))[3]
        self.assertLess(tight, 0.4, "should trip the warning")
        self.assertGreater(wide, 1.0, "should not")

    def test_identical_distances_are_refused_rather_than_fitted(self):
        with self.assertRaises(ValueError) as cm:
            fit_pathloss([(5.0, -50.0), (5.0, -52.0), (5.0, -49.0)])
        self.assertIn("same distance", str(cm.exception))

    def test_one_measurement_cannot_fit_two_parameters(self):
        with self.assertRaises(ValueError):
            fit_pathloss([(5.0, -50.0)])

    def test_zero_and_negative_distances_are_dropped(self):
        pts = [(0.0, -30.0)] + synth(-40.0, 3.0, [2.0, 8.0, 20.0])
        _a, n, _r, _s = fit_pathloss(pts)
        self.assertAlmostEqual(n, 3.0, places=6)


class MacNormalisation(unittest.TestCase):
    def test_every_shape_the_two_apis_use(self):
        for raw in ("1C:0B:8B:D6:36:DA", "1c0b8bd636da", "1C-0B-8B-D6-36-DA"):
            self.assertEqual(norm_mac(raw), "1C0B8BD636DA")

    def test_missing_is_empty_not_an_error(self):
        self.assertEqual(norm_mac(None), "")


if __name__ == "__main__":
    unittest.main()
