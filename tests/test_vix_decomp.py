"""vix_decomp.py 단위 테스트 — 네트워크 없이 돈다.

스크립트의 자체 점검(--self-test)은 이론가로 만든 가상 SPX 체인에서 만기 선택, VIX 수준, 30일 곡선,
요인 여섯 개(곡선 따라 이동·평행 이동·풋 스큐·콜 스큐·하방 볼록성·상방 볼록성)가 알려진 답과 맞는지 확인한다.
여기서는 그 점검과 함께 보조 함수 몇 개를 따로 확인한다.
"""
import datetime as dt
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import vix_decomp as vd  # noqa: E402


class SelfTest(unittest.TestCase):
    def test_self_test_passes(self):
        self.assertEqual(vd.self_test(), 0)


class Helpers(unittest.TestCase):
    def test_quote_time_is_spx_option_close(self):
        self.assertEqual(vd.quote_time("20261113"), dt.datetime(2026, 11, 13, 16, 15))

    def test_quote_time_early_close(self):
        try:
            import exchange_calendars  # noqa: F401
        except ImportError:
            self.skipTest("exchange_calendars 없음")
        self.assertEqual(vd.quote_time("20261127"), dt.datetime(2026, 11, 27, 13, 15))   # 추수감사절 다음 날: 13:00 폐장

    def test_weights_reproduce_30_day_interpolation(self):
        # 두 만기의 변동성이 같으면 보간 결과도 그 값이어야 한다
        m1, m2 = 27.99 * 1440, 34.72 * 1440
        self.assertAlmostEqual(vd.blend(0.04, 0.04, m1, m2), 20.0, places=9)
        a1, a2 = vd.weights(m1, m2)
        self.assertAlmostEqual(a1 + a2, 1.0, places=9)
        self.assertTrue(a1 > a2 > 0)                                                    # 30일에 더 가까운 근월의 비중이 크다

    def test_strip_var_matches_hand_calculation(self):
        K, Q = np.array([90.0, 100.0, 110.0]), np.array([1.0, 4.0, 1.5])
        s2, c = vd.strip_var(K, Q, 101.0, 0.1, 100.0)
        e = np.exp(vd.R * 0.1)
        hand = 2 / 0.1 * e * (10 / 90 ** 2 * 1.0 + 10 / 100 ** 2 * 4.0 + 10 / 110 ** 2 * 1.5) - (101.0 / 100.0 - 1) ** 2 / 0.1
        self.assertAlmostEqual(s2, hand, places=12)
        self.assertAlmostEqual(c.sum() - (101.0 / 100.0 - 1) ** 2 / 0.1, s2, places=12)

    def test_bins_conserve_total_and_split_across_edges(self):
        # 풋 3개, K0, 콜 2개. 델타 곡선을 직접 주고 합계 보존과 경계 분할을 확인한다
        K = np.array([80.0, 90.0, 95.0, 100.0, 105.0, 110.0])
        cp = np.array([-1.0, -1.0, -1.0, 0.0, 1.0, 1.0])
        v = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        dcurve = ((np.array([80.0, 90.0, 95.0, 100.0]), np.array([0.02, 0.10, 0.30, 0.49])),
                  (np.array([100.0, 105.0, 110.0]), np.array([0.51, 0.30, 0.10])))
        bins = vd._to_bins(K, cp, v, dcurve, k0_extra=-0.5)
        self.assertEqual(bins.shape, (2, vd.NB))
        self.assertAlmostEqual(bins.sum(), v.sum() - 0.5, places=12)
        self.assertAlmostEqual(bins[0].sum(), 1 + 2 + 3 + 1.75, places=12)              # K0 몫(4 - 0.5)의 절반이 풋 쪽
        # 행사가 90의 풋은 85~92.5 구간을 맡는다 → 델타 0.06~0.20 → 5단위 구간 네 개에 걸친다
        only90 = vd._to_bins(K, cp, np.array([0.0, 1.0, 0.0, 0.0, 0.0, 0.0]), dcurve)
        self.assertTrue((only90[0, 1:4] > 0).all() and only90[0, 4:].sum() == 0)
        self.assertAlmostEqual(only90[0, 2], 0.05 / 0.14, places=9)                     # 0.10~0.15가 차지하는 몫

    def test_groups_use_10_delta_boundary(self):
        bins = np.arange(20, dtype=float).reshape(2, 10)
        g = vd.groups(bins)
        self.assertEqual(g["put_wing"], 0 + 1)
        self.assertEqual(g["call_wing"], 10 + 11)
        self.assertEqual(g["put_body"] + g["put_wing"], bins[0].sum())

    def test_atm_strike_is_forward_rounded_to_5(self):
        # 30일 선도가의 5단위 반올림. 값은 실제 날짜의 30일 선도가와 Cboe 도구(atmPairs)의 등가격 행사가
        self.assertEqual(vd.atm_strike(7722.01), 7720)                  # 2026-11-04
        self.assertEqual(vd.atm_strike(7364.33), 7365)                  # 2026-11-05
        self.assertEqual(vd.atm_strike(7564.17), 7565)                  # 2026-11-13

    def test_forward30_interpolates_terms_to_30_days(self):
        lv = {"terms": [{"minutes": 25 * 1440, "F": 5000.0}, {"minutes": 32 * 1440, "F": 5007.0}]}
        self.assertAlmostEqual(vd.forward30(lv), 5005.0, places=9)
        self.assertTrue(np.isnan(vd.forward30({"terms": []})))

    def test_atm_comes_from_forward_when_available(self):
        lv = {"terms": [{"minutes": 25 * 1440, "F": 5003.0}, {"minutes": 32 * 1440, "F": 5010.0}]}
        self.assertEqual(vd.atm_strike(vd.forward30(lv)), 5010)        # 30일 선도가 5008 → 5010(지수 종가와 무관)

    def test_factors_need_spot_and_curve(self):
        lv = {"vix": 20.0, "curve": None}
        self.assertIn("error", vd.cboe_factors(lv, lv, 5000.0, 5000.0))
        lv = {"vix": 20.0, "curve": (np.array([4000.0, 5000.0, 6000.0]), np.array([0.3, 0.2, 0.15]))}
        self.assertIn("error", vd.cboe_factors(lv, lv, float("nan"), 5000.0))
        self.assertIn("범위 밖", vd.cboe_factors(lv, lv, 5000.0, 7000.0)["error"])

    def test_report_separates_usual_side_move_from_excess(self):
        # 등가격 IV 변화 +2.0에 풋 쪽 +1.2(0.6배), 콜 쪽 -0.6(-0.3배)이 평소 관계. 그보다 풋 쪽이 +0.5 큰 날
        rows = [{"date": f"d{i}", "vix": 20.0, "sticky": 0.5, "parallel": 0.5, "put_skew": 0.3, "down_conv": 0.4, "call_skew": -0.3, "up_conv": 0.0}
                for i in range(9)]
        lv = {"date": "20261113", "vix": 21.0, "var": 0.0441, "bins": np.full((2, vd.NB), 0.0441 / 20), "a": (0.6, 0.4), "curve": None,
              "terms": [{"exp": __import__("pandas").Timestamp("2026-12-11"), "root": "SPXW", "minutes": 28 * 1440, "sigma2": 0.04, "F": 5000.0, "K0": 5000.0,
                         "n_put": 10, "n_call": 10, "k_lo": 4000.0, "k_hi": 6000.0, "iv_nan": 0, "k0_shift": False}] * 2}
        f = {"sticky": 1.2, "parallel": 0.8, "put_skew": 0.7, "down_conv": 1.0, "call_skew": -0.5, "up_conv": -0.1, "resid": 0.0,
             "atm": {"strike_prev": 5000.0, "strike": 4950.0, "iv_prev": 15.0, "iv_prev_at_new": 16.2, "iv": 17.0},
             "premium": {k: 0.0 for k in vd.FACTORS[2:]}, "premium_prev": {k: 0.0 for k in vd.FACTORS[2:]}}
        prev = dict(lv, date="20261112", vix=18.0, curve=(np.array([4000.0, 6000.0]), np.array([0.3, 0.1])))
        lv = dict(lv, curve=(np.array([4000.0, 6000.0]), np.array([0.32, 0.12])))
        st = {"rows": rows + [dict(rows[0], date="20261113", **{k: f[k] for k in vd.FACTORS})], "closes": {},
              "last": {"date": "20261113", "level": lv, "factors": f, "spot": 4950.0, "close": {},
                       "prev": {"date": "20261112", "lv": prev, "spot": 5000.0, "close": {}}}}
        res = vd.summary(st)
        self.assertAlmostEqual(res["side"]["put"]["usual"], 1.2)
        self.assertAlmostEqual(res["side"]["put"]["excess"], 0.5)
        self.assertAlmostEqual(res["side"]["call"]["excess"], 0.0)
        self.assertAlmostEqual(res["side"]["put"]["typ"], 0.1)            # 과거 9번: 0.7 - 0.6 x 1.0
        self.assertIn("평소 관계보다 +0.50", vd.report(res))

    def test_ticks_cover_range_with_round_steps(self):
        t, step = vd._ticks(0.0, 4.9)
        self.assertEqual(t, [0.0, 2.0, 4.0])
        t, step = vd._ticks(-0.16, 0.04)
        self.assertIn(0.0, t)
        self.assertTrue(all(abs(x / step - round(x / step)) < 1e-9 for x in t))

    def test_number_format(self):
        self.assertEqual(vd._num(0.6), "+0.60")
        self.assertEqual(vd._num(-0.164), "−0.16")
        self.assertEqual(vd._num(-0.003), "0.00")
        self.assertEqual(vd._num(2.0, 0, sign=False), "2")


def _exps(rows):
    import pandas as pd
    return pd.DataFrame({"exp": pd.to_datetime([e for e, _ in rows]), "root": [r for _, r in rows]})


class OtherUnderlyings(unittest.TestCase):
    """VIX 옵션(VVIX 방식)·GLD 옵션(GVZ 방식)의 만기 선택과 이름표, Cboe 공식값 끼워 넣기."""

    def test_vix_options_use_first_two_monthlies_with_8_days(self):
        e = _exps([("2026-11-18", "VIX"), ("2026-11-24", "VIXW"), ("2026-12-09", "VIXW"), ("2026-12-16", "VIX"), ("2027-01-20", "VIX")])
        got = [(f"{x:%m-%d}", r) for x, r in vd.pick_terms(e, "20261113", "VIX")]
        self.assertEqual(got, [("12-16", "VIX"), ("01-20", "VIX")])        # 11-18은 5일 남아 건너뛰고, 주간물(VIXW)은 쓰지 않는다
        got = [(f"{x:%m-%d}", r) for x, r in vd.pick_terms(e, "20261109", "VIX")]
        self.assertEqual(got, [("11-18", "VIX"), ("12-16", "VIX")])        # 9일 남으면 근월로 쓴다

    def test_gld_uses_monthlies_around_30_days_or_extrapolates(self):
        e = _exps([("2026-11-16", "GLD"), ("2026-11-20", "GLD"), ("2026-11-27", "GLD"), ("2026-12-11", "GLD"), ("2026-12-18", "GLD"),
                   ("2027-01-15", "GLD")])
        got = [f"{x:%m-%d}" for x, _ in vd.pick_terms(e, "20261102", "GLD")]
        self.assertEqual(got, ["11-20", "12-18"])                          # 월물(셋째 금요일) 18일·46일
        got = [f"{x:%m-%d}" for x, _ in vd.pick_terms(e, "20261116", "GLD")]
        self.assertEqual(got, ["12-18", "01-15"])                          # 근월 월물이 4일 남음 → 30일 넘는 월물 둘로 바깥 보간

    def test_spx_rule_unchanged(self):
        e = _exps([("2026-12-11", "SPXW"), ("2026-12-16", "SPXW"), ("2026-12-18", "SPX"), ("2026-12-18", "SPXW")])
        self.assertEqual([(f"{x:%m-%d}", r) for x, r in vd.pick_terms(e, "20261113")], [("12-11", "SPXW"), ("12-18", "SPX")])

    def test_atm_step_per_underlying(self):
        self.assertEqual(vd.atm_strike(19.73, vd.SPECS["VIX"]["step"]), 19.5)
        self.assertEqual(vd.atm_strike(354.6, vd.SPECS["GLD"]["step"]), 355.0)

    def _res(self, spec="SPX"):
        f = {"sticky": 1.98, "parallel": 1.14, "put_skew": 0.43, "call_skew": -0.55, "down_conv": 0.73, "up_conv": -0.12}
        return {"date": "20261116", "prev": "20261113", "spec": spec, "source": "own", "flags": [], "vix": 22.91, "vix_prev": 19.30, "chg": 3.61,
                "spx": 7416.59, "spx_prev": 7561.67, "spx_is_close": True, "spx_prev_is_close": True, "factors": f,
                "atm": {"strike_prev": 7565.0, "strike": 7425.0, "iv_prev": 14.97, "iv_prev_at_new": 16.96, "iv": 18.09},
                "premium": {k: 0.0 for k in vd.FACTORS[2:]}, "premium_prev": {k: 0.0 for k in vd.FACTORS[2:]},
                "terms": [{"exp": "2026-12-11", "root": "SPXW", "days": 25.0, "vol": 23.2, "weight": 0.2, "F": 7420.0, "K0": 7420.0, "n_put": 1,
                           "n_call": 1, "k_lo": 2800.0, "k_hi": 8800.0}],
                "points": {k: 1.0 for k in vd.GROUPS}, "share": {k: 0.25 for k in vd.GROUPS}}

    def test_apply_official_replaces_factors_and_keeps_own(self):
        res = self._res()
        k = np.array([7000.0, 7440.0, 8000.0])
        off = {"factors": {"sticky": 1.79, "parallel": 1.30, "put_skew": -0.06, "call_skew": 0.44, "down_conv": -0.14, "up_conv": 0.28},
               "atm": {"strike_prev": 7565.0, "strike": 7440.0, "iv_prev": 14.98, "iv": 18.07, "iv_prev_at_new": 16.77},
               "curves": {"20261113": (k, np.array([20.0, 16.8, 13.0])), "20261116": (k, np.array([21.0, 18.1, 14.0]))},
               "closes": {"20261113": {"spx": 7561.67, "vix": 19.30}, "20261116": {"spx": 7416.59, "vix": 22.91}}}
        hist = [{f: 0.1 * (i + 1) for f in vd.FACTORS} for i in range(10)]
        out = vd.apply_official(res, off, hist)
        self.assertEqual(out["source"], "cboe")
        self.assertAlmostEqual(out["factors"]["call_skew"], 0.44)
        self.assertAlmostEqual(out["own_factors"]["call_skew"], -0.55)
        self.assertAlmostEqual(out["chg_official"], 3.61, places=9)
        self.assertAlmostEqual(out["factor_typ"]["sticky"], 0.55, places=9)
        self.assertNotIn("premium", out)
        self.assertIn("Cboe 공식값", vd.report(out))
        self.assertFalse(out["flags"], out["flags"])

    def test_apply_official_falls_back_with_flag(self):
        out = vd.apply_official(self._res(), {"error": "Cboe 공식값을 받지 못함(URLError)"}, [])
        self.assertEqual(out["source"], "own")
        self.assertTrue(any("자체 계산 값으로 대신함" in x for x in out["flags"]))

    def test_report_and_labels_follow_underlying(self):
        for spec, head, col in (("VIX", "VVIX식 분해", "VVIX"), ("GLD", "GVZ식 분해", "GVZ")):
            r = self._res(spec)
            self.assertIn(head, vd.report(r))
            L = vd._labels("ko", r)
            self.assertEqual(L["vix"], col)
            self.assertEqual(L["spx"], spec)


if __name__ == "__main__":
    unittest.main()
