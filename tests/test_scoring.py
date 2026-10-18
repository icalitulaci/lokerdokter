"""The seven reference cases from specialist-demand-dashboard-prompt.md.

Each case states its pillar D situation in words; the inputs below are chosen to produce it
(national median M = 1.0 per 100k unless the case needs something else).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scoring import (NO_POPULATION, area_catchment, area_custom, area_global, area_national,  # noqa: E402
                     score_pair, two_step_catchment)


class ReferenceCases(unittest.TestCase):
    def test_1_general_hospital_no_anesthesiologist(self):
        r = score_pair(specialty="Sp.An", general=True, tiers={}, counts={"Sp.An": 0},
                       peer_median=2, kab_total=0, density=0.0, national_median=1.0, kab_name="X")
        self.assertEqual((r["A"], r["B"], r["C"], r["D"]), (35, 0, 15, 20))
        self.assertEqual(r["total"], 70)
        self.assertEqual(r["label"], "High")

    def test_2_kmk_heart_madya_no_cardiologist(self):
        r = score_pair(specialty="Sp.JP", general=True, tiers={"jantung": "Madya"}, counts={"Sp.JP": 0},
                       peer_median=1, kab_total=3, density=0.4, national_median=1.0, kab_name="X")
        self.assertEqual((r["A"], r["B"], r["C"], r["D"]), (0, 30, 15, 12))
        self.assertEqual(r["total"], 57)
        self.assertEqual(r["label"], "High")  # hard-gap override lifts Moderate to High

    def test_3_single_internist_80_beds(self):
        r = score_pair(specialty="Sp.PD", general=True, tiers={}, counts={"Sp.PD": 1},
                       peer_median=2, kab_total=40, density=1.5, national_median=1.0, kab_name="X")
        self.assertEqual((r["A"], r["B"], r["C"], r["D"]), (12, 0, 7.5, 0))
        self.assertEqual(r["total"], 19.5)
        self.assertEqual(r["label"], "Low")

    def test_4_relevance_gate_blocks_cardiac_surgeon(self):
        r = score_pair(specialty="Sp.BTKV", general=True, tiers={}, counts={"Sp.BTKV": 0},
                       peer_median=0, kab_total=0, density=0.0, national_median=0.1, kab_name="X")
        self.assertEqual((r["A"], r["B"], r["C"], r["D"]), (0, 0, 0, 0))
        self.assertEqual(r["total"], 0)
        self.assertEqual(r["label"], "Low")

    def test_5_either_group_satisfied_by_nuclear_medicine(self):
        counts = {"Sp.Onk.Rad": 0, "Sp.KN": 1}
        for s in ("Sp.Onk.Rad", "Sp.KN"):
            r = score_pair(specialty=s, general=True, tiers={"kanker": "Utama"}, counts=counts,
                           peer_median=0, kab_total=1, density=1.0, national_median=1.0, kab_name="X")
            self.assertEqual(r["B"], 0, s)

    def test_6_stroke_paripurna_no_rehab(self):
        r = score_pair(specialty="Sp.KFR", general=True, tiers={"stroke": "Paripurna"}, counts={"Sp.KFR": 0},
                       peer_median=1, kab_total=10, density=1.2, national_median=1.0, kab_name="X")
        self.assertEqual((r["A"], r["B"], r["C"], r["D"]), (0, 30, 15, 0))
        self.assertEqual(r["total"], 45)
        self.assertEqual(r["label"], "High")

    def test_7_rsia_kia_madya_no_pediatric_surgeon(self):
        r = score_pair(specialty="Sp.BA", general=False, tiers={"kia": "Madya"}, counts={"Sp.BA": 0},
                       peer_median=0, kab_total=0, density=0.0, national_median=0.1, kab_name="X")
        self.assertEqual((r["A"], r["B"], r["C"], r["D"]), (0, 30, 0, 20))
        self.assertEqual(r["total"], 50)
        self.assertEqual(r["label"], "High")


class ExtraRules(unittest.TestCase):
    def test_either_group_both_missing_scores_both(self):
        counts = {"Sp.Onk.Rad": 0, "Sp.KN": 0}
        for s in ("Sp.Onk.Rad", "Sp.KN"):
            r = score_pair(specialty=s, general=True, tiers={"kanker": "Utama"}, counts=counts,
                           peer_median=0, kab_total=1, density=1.0, national_median=1.0, kab_name="X")
            self.assertEqual(r["B"], 30, s)

    def test_single_specialist_at_utama_scores_10(self):
        r = score_pair(specialty="Sp.JP", general=True, tiers={"jantung": "Utama"}, counts={"Sp.JP": 1},
                       peer_median=1, kab_total=5, density=2.0, national_median=1.0, kab_name="X")
        self.assertEqual(r["B"], 10)

    def test_missing_population_marks_d_unavailable(self):
        r = score_pair(specialty="Sp.An", general=True, tiers={}, counts={"Sp.An": 0},
                       peer_median=2, kab_total=0, density=None, national_median=1.0, kab_name="X")
        self.assertIsNone(r["D"])
        self.assertEqual(r["total"], 50)
        self.assertTrue(any("no population data" in x for x in r["reasons"]))

    def test_total_capped_at_100(self):
        r = score_pair(specialty="Sp.An", general=True, tiers={"kia": "Madya"}, counts={"Sp.An": 0},
                       peer_median=3, kab_total=0, density=0.0, national_median=1.0, kab_name="X")
        self.assertEqual(r["total"], 100)


class AreaMethods(unittest.TestCase):
    """The four settings for pillar D, each at kab level, then through the relevance gate."""

    def test_custom_matches_original_rule(self):
        self.assertEqual(area_custom("Sp.An", 0, 0.0, 1.0, "X")[0], 20)
        self.assertEqual(area_custom("Sp.An", 3, 0.4, 1.0, "X")[0], 12)
        self.assertEqual(area_custom("Sp.An", 3, 0.8, 1.0, "X")[0], 6)
        self.assertEqual(area_custom("Sp.An", 3, 1.0, 1.0, "X"), (0, None))

    def test_national_basic_specialist(self):
        self.assertEqual(area_national("Sp.Rad", 0, 0, 1, 1, [], "X")[0], 20)
        self.assertEqual(area_national("Sp.Rad", 2, 0, 1, 1, [], "X")[0], 12)  # only private hospitals have one
        r = area_national("Sp.Rad", 4, 2, 3, 1, [], "X")
        self.assertEqual(r[0], 6)
        self.assertIn("1 of 3 public hospitals", r[1][0])
        self.assertEqual(area_national("Sp.Rad", 4, 4, 3, 0, [], "X"), (0, None))

    def test_national_kmk_specialist_only_where_targeted(self):
        self.assertEqual(area_national("Sp.JP", 0, 0, 0, 0, [], "X"), (0, None))
        r = area_national("Sp.JP", 0, 0, 1, 1, [("jantung", "Madya"), ("jantung", "Utama")], "X")
        self.assertEqual(r[0], 20)
        self.assertIn("Utama", r[1][0])

    def test_global_single_and_group_ratio(self):
        self.assertEqual(area_global("Sp.An", 5, 2.0, 30, "X")[0], 12)  # WFSA 5 per 100k
        self.assertEqual(area_global("Sp.An", 5, 4.0, 30, "X")[0], 6)
        self.assertEqual(area_global("Sp.An", 5, 5.0, 1, "X"), (0, None))
        self.assertEqual(area_global("Sp.B", 5, 50.0, 9.0, "X")[0], 12)  # Lancet SAO 20 per 100k, group density
        self.assertEqual(area_global("Sp.B", 5, 0.1, 25.0, "X"), (0, None))
        self.assertEqual(area_global("Sp.B", 0, 0.0, 25.0, "X")[0], 20)

    def test_global_without_published_ratio_is_skipped(self):
        pts, reason = area_global("Sp.PD", 0, 0.0, 0.0, "X")
        self.assertIsNone(pts)
        self.assertIn("No internationally recommended ratio", reason[0])

    def test_catchment_uses_national_average(self):
        self.assertEqual(area_catchment("Sp.KN", 0.0, 0.05, "X")[0], 20)
        self.assertEqual(area_catchment("Sp.KN", 0.02, 0.05, "X")[0], 12)
        self.assertEqual(area_catchment("Sp.KN", 0.04, 0.05, "X")[0], 6)
        self.assertEqual(area_catchment("Sp.KN", 0.05, 0.05, "X"), (0, None))
        self.assertEqual(area_catchment("Sp.KN", 0.0, 0, "X"), (0, None))  # nobody in the country has one

    def test_two_step_catchment(self):
        # Hospital h1 (10 doctors) serves areas a and b; h2 (2 doctors) serves only b.
        acc = two_step_catchment({"a": 100_000, "b": 300_000, "c": 50_000},
                                 {"h1": ["a", "b"], "h2": ["b"]}, {"h1": 10, "h2": 2})
        self.assertAlmostEqual(acc["a"], 2.5)
        self.assertAlmostEqual(acc["b"], 2.5 + 2 / 3)
        self.assertEqual(acc["c"], 0)
        # Everyone's access, weighted by population, adds back up to every doctor.
        self.assertAlmostEqual((acc["a"] * 1 + acc["b"] * 3 + acc["c"] * 0.5), 12)

    def test_gate_and_missing_population(self):
        args = dict(specialty="Sp.JP", general=True, tiers={}, counts={"Sp.JP": 0}, peer_median=0)
        self.assertEqual(score_pair(**args, area=(20, ("x", "x")))["D"], 0)  # gate fails: no floor, KMK or peers
        r = score_pair(**{**args, "peer_median": 1}, area=(20, ("x", "x")))
        self.assertEqual((r["D"], r["total"]), (20, 35))
        self.assertEqual(r["reasons"][-1], "x")
        self.assertIsNone(score_pair(**args, area=NO_POPULATION)["D"])


if __name__ == "__main__":
    unittest.main()
