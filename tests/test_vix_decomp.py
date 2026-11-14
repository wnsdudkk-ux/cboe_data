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

    def test_atm_strike_is_nearest_25(self):
        self.assertEqual(vd.atm_strike(7688.62), 7700)                  # Cboe 도구의 2026-11-04 값과 맞는 규칙
        self.assertEqual(vd.atm_strike(7353.73), 7350)
        self.assertEqual(vd.atm_strike(7561.67), 7550)

    def test_factors_need_spot_and_curve(self):
        lv = {"vix": 20.0, "curve": None}
        self.assertIn("error", vd.cboe_factors(lv, lv, 5000.0, 5000.0))
        lv = {"vix": 20.0, "curve": (np.array([4000.0, 5000.0, 6000.0]), np.array([0.3, 0.2, 0.15]))}
        self.assertIn("error", vd.cboe_factors(lv, lv, float("nan"), 5000.0))
        self.assertIn("범위 밖", vd.cboe_factors(lv, lv, 5000.0, 7000.0)["error"])

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


if __name__ == "__main__":
    unittest.main()
