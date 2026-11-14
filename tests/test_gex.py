"""gex.py 단위 테스트 — 네트워크 없이 돈다.

스크립트의 자체 점검(--self-test)은 이론가로 만든 가상 SPX 체인에서 선도가, IV, 순 GEX, 기준 수준, 감마 플립,
콜 벽·풋 벽이 알려진 답과 맞는지 확인한다. 여기서는 그 점검과 함께 보조 함수, 추이 표, 실행 경로, 그림을 따로 확인한다.
"""
import io
import json
import os
import sys
import tarfile
import tempfile
import types
import unittest
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gex  # noqa: E402
from option_sentiment import prep  # noqa: E402

DATE, SPOT = "20260611", 5000.0


def chain(**kw):
    return prep(gex.synthetic(DATE, SPOT, **kw), "SPX", DATE)


def args(**kw):
    base = dict(archive_dir=None, date=None, history_csv=None, save_history=False, lookback=None, png=None, png_history=None, out_json=None,
                from_json=None, no_yf=True, self_test=False, closes_csv=None, require_close=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


class SelfTest(unittest.TestCase):
    def test_self_test_passes(self):
        self.assertEqual(gex.self_test(), 0)


class Calc(unittest.TestCase):
    def test_walls_ignore_the_in_the_money_side(self):
        # 종가 위 행사가에 풋을, 아래 행사가에 콜을 크게 쌓아도 벽은 외가격 쪽에서만 고른다
        df = chain(extra=((0, 5300.0, "P", 500000), (0, 4700.0, "C", 500000)))
        r = gex.day(df, DATE, SPOT)
        self.assertEqual(r["walls"], {"call": 5200.0, "put": 4800.0})

    def test_window_sums_add_up(self):
        r = gex.day(chain(), DATE, SPOT)
        t, _ = gex.build(chain(), DATE)
        self.assertEqual(sorted(t["dte"].unique()), [7, 28, 91])
        self.assertAlmostEqual(r["net"], r["call"] - r["put"], delta=1e-6 * r["gross"])
        self.assertAlmostEqual(r["gross"], r["call"] + r["put"], delta=1e-6 * r["gross"])
        self.assertTrue(abs(r["net7"]) < abs(r["net30"]) < abs(r["net"]))        # 만기 범위를 넓힐수록 같은 부호로 쌓인다
        self.assertAlmostEqual(r["pct"], 100 * r["net"] / r["gross"])

    def test_profile_passes_through_headline_and_flip(self):
        r = gex.day(chain(), DATE, SPOT)
        price, net = r["profile"]["price"], r["profile"]["net"]
        self.assertAlmostEqual(float(np.interp(SPOT, price, net)), r["net"], delta=2e-3 * r["gross"])
        self.assertAlmostEqual(float(np.interp(r["flip"], price, net)), 0.0, delta=2e-3 * r["gross"])
        self.assertAlmostEqual(price[0], SPOT * (1 - gex.SPAN))
        self.assertAlmostEqual(price[-1], SPOT * (1 + gex.SPAN))

    def test_missing_close_falls_back_to_quote_level(self):
        r = gex.day(chain(), DATE)
        self.assertAlmostEqual(r["spot"], r["ref"])
        self.assertAlmostEqual(r["net"], r["net_quote"], delta=1e-9 * r["gross"])

    def test_cboe_gamma_figure_uses_file_gamma(self):
        raw = gex.synthetic(DATE, SPOT)
        raw["Gamma"] = 0.001                                                         # 모든 계약의 감마가 0.001이면 순 GEX = 0.001 x 지수^2 x (콜 - 풋 미결제약정)
        df = prep(raw, "SPX", DATE)
        live = df[df["dte"] >= 1]
        oi = live.loc[live["cp"] == "C", "oi"].sum() - live.loc[live["cp"] == "P", "oi"].sum()
        self.assertAlmostEqual(gex.day(df, DATE, SPOT)["net_cboe"], 0.001 * SPOT * SPOT * oi, delta=1.0)

    def test_expiry_without_two_sided_quotes_is_dropped_and_counted(self):
        raw = gex.synthetic(DATE, SPOT)
        far = raw["Expiry"] == raw["Expiry"].max()
        raw.loc[far, ["Bid", "Ask"]] = 0.0                                           # 가장 먼 만기의 호가를 지운다
        t, miss = gex.build(prep(raw, "SPX", DATE), DATE)
        self.assertEqual(sorted(t["dte"].unique()), [7, 28])
        self.assertAlmostEqual(miss, 1 / 3, places=6)

    def test_nearest(self):
        self.assertEqual(gex.nearest([4800.0, 5100.0, 5600.0], 5000.0), 5100.0)
        self.assertTrue(np.isnan(gex.nearest([], 5000.0)))


class History(unittest.TestCase):
    def test_merge_replaces_same_date_and_sorts(self):
        a = gex.merge_history(pd.DataFrame(columns=gex.HIST_COLS), [{**dict.fromkeys(gex.HIST_COLS, 1.0), "date": "20260102"},
                                                                    {**dict.fromkeys(gex.HIST_COLS, 2.0), "date": "20260101"}])
        self.assertEqual(list(a["date"]), ["20260101", "20260102"])
        b = gex.merge_history(a, [{**dict.fromkeys(gex.HIST_COLS, 9.0), "date": "20260102"}, None])
        self.assertEqual(list(b["net_bn"]), [2.0, 9.0])

    def test_read_history_missing_file_gives_empty_table(self):
        h = gex.read_history("/nonexistent/gex_history.csv")
        self.assertEqual(list(h.columns), gex.HIST_COLS)
        self.assertEqual(len(h), 0)

    def test_summary_stats_and_flags(self):
        r = gex.day(chain(), DATE, SPOT * 1.004)                                     # 종가가 호가 시점 수준보다 0.4% 높다
        rows = [{**gex.hist_row(r), "date": f"202606{d:02d}", "net_bn": v} for d, v in zip(range(5, 11), (5.0, -3.0, 2.0, -1.0, -2.0, -4.0))]
        hist = gex.merge_history(pd.DataFrame(columns=gex.HIST_COLS), rows + [gex.hist_row(r)])
        out = gex.summary(r, hist)
        st = out["stats"]
        self.assertEqual((st["days"], st["neg_days"], st["run"], st["run_sign"]), (7, 5, 4, -1))
        self.assertEqual(out["prev"]["date"], "20260610")
        self.assertTrue(any("종가" in f and "다름" in f for f in out["flags"]))
        self.assertIn("음의 감마 4일째", gex.report(out))
        json.dumps(gex.clean(out))                                                   # JSON으로 저장할 수 있어야 한다


class Helpers(unittest.TestCase):
    def test_nice_axis_ends(self):
        self.assertEqual([gex._nice(x) for x in (0, 0.9, 7.9, 10.4, 165, 2400)], [1.0, 1.0, 8.0, 12.0, 200.0, 2400.0])

    def test_signed_numbers_for_charts(self):
        self.assertEqual(gex._sgn(-39.48, 1, "$"), "−$39.5")
        self.assertEqual(gex._sgn(0.004, 1), "0.0")
        self.assertEqual(gex._sgn(None), "–")

    def test_spread_keeps_order_and_minimum_gap(self):
        out = gex._spread([100.0, 104.0, 130.0, 400.0], 32)
        self.assertTrue(all(b - a >= 32 - 1e-9 for a, b in zip(out, out[1:])))
        self.assertAlmostEqual(out[3] - out[2], 400.0 - 130.0 - (out[2] - 130.0) + (out[3] - 400.0))
        self.assertAlmostEqual(float(np.mean(out)), float(np.mean([100.0, 104.0, 130.0, 400.0])))   # 가운데는 제자리
        self.assertEqual(list(gex._spread([300.0, 100.0], 32)), [300.0, 100.0])                    # 이미 떨어져 있으면 그대로

    def test_month_labels_skip_a_short_first_month(self):
        dates = ["20260629", "20260630"] + [f"202607{d:02d}" for d in range(1, 30)] + ["20260803", "20260804", "20260805", "20260806"]
        self.assertEqual(gex._months(dates, gex.GT["ko"]), [(2, "7월"), (31, "8월")])
        self.assertEqual(gex._months(dates, gex.GT["en"])[0], (2, "Jul"))


class Run(unittest.TestCase):
    def _archive(self, tmp, dates):
        for d in dates:
            data = gex.synthetic(d, SPOT).to_csv(index=False).encode()
            with tarfile.open(os.path.join(tmp, f"quotes_{d}.tar.gz"), "w:gz") as tf:
                info = tarfile.TarInfo(f"{d}/SPX/spx_quotes_{d}.csv")
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))

    def test_run_builds_history_once_and_reuses_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            dates = ["20260609", "20260610", "20260611"]
            self._archive(tmp, dates)
            csv, js = os.path.join(tmp, "h.csv"), os.path.join(tmp, "o.json")
            out = io.StringIO()
            stdout, sys.stdout = sys.stdout, out
            try:
                self.assertEqual(gex.run(args(archive_dir=tmp, history_csv=csv, save_history=True, out_json=js)), 0)
                h = gex.read_history(csv)
                self.assertEqual(list(h["date"]), dates)
                h.loc[0, "net_bn"] = 123.456                                         # 표에 있는 날짜는 다시 계산하지 않는다는 표시
                h.to_csv(csv, index=False)
                self.assertEqual(gex.run(args(archive_dir=tmp, history_csv=csv, save_history=True)), 0)
                self.assertEqual(gex.read_history(csv)["net_bn"].iloc[0], 123.456)
                self.assertEqual(gex.run(args(archive_dir=tmp, date="20260101")), 1)   # 없는 날짜
                self.assertEqual(gex.run(args(from_json=js)), 0)                      # 저장해 둔 결과로 보고만 다시
            finally:
                sys.stdout = stdout
            with open(js, encoding="utf-8") as f:
                res = json.load(f)
            self.assertEqual(res["date"], "20260611")
            self.assertEqual(res["history"]["date"], dates)
            self.assertTrue(any("종가를 받지 못해" in f for f in res["flags"]))
            self.assertIn("== SPX 감마 노출(GEX) | 20260611", out.getvalue())


class Closes(unittest.TestCase):
    """지수 종가: 야후 값을 먼저 쓰고, 없으면 수집기가 적어 둔 표를 쓴다."""

    def _run(self, a):
        out = io.StringIO()
        stdout, sys.stdout = sys.stdout, out
        try:
            rc = gex.run(a)
        finally:
            sys.stdout = stdout
        return rc, out.getvalue()

    def test_pick_close(self):
        self.assertEqual(gex.pick_close(7561.669921875, 7561.67), (7561.67, 0.0))      # 야후 값의 끝자리 흔들림은 반올림으로 없앤다
        spot, gap = gex.pick_close(5000.0, 5010.0)
        self.assertEqual(spot, 5000.0)
        self.assertAlmostEqual(gap, 0.2)
        spot, gap = gex.pick_close(None, 5010.0)
        self.assertEqual(spot, 5010.0)
        self.assertTrue(gap != gap)
        self.assertTrue(all(x != x for x in gex.pick_close(float("nan"), None)))
        self.assertTrue(all(x != x for x in gex.pick_close(0.0, -1.0)))

    def test_read_closes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c.csv")
            with open(path, "w", encoding="utf-8") as f:
                f.write("date,spx,vix,source\n20260609,5003.25,15.10,cboe\n20260610,,15.30,cboe\n20260611,abc,,cboe\n")
            self.assertEqual(gex.read_closes(path), {"20260609": 5003.25})
            self.assertEqual(gex.read_closes(os.path.join(tmp, "none.csv")), {})

    def test_saved_close_fills_in_and_strict_mode_skips_days_without_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            dates = ["20260609", "20260610", "20260611"]
            Run._archive(self, tmp, dates)
            hist, cc = os.path.join(tmp, "h.csv"), os.path.join(tmp, "c.csv")
            with open(cc, "w", encoding="utf-8") as f:
                f.write("date,spx,vix,source\n20260609,5003.00,15,cboe\n20260610,5004.00,15,cboe\n")
            rc, out = self._run(args(archive_dir=tmp, history_csv=hist, save_history=True, closes_csv=cc, require_close=True))
            self.assertEqual(rc, 0)
            h = gex.read_history(hist)
            self.assertEqual(list(h["date"]), dates[:2])                             # 종가가 없는 마지막 날은 표에 넣지 않는다
            self.assertEqual(list(h["spot"]), [5003.0, 5004.0])
            self.assertIn("추이 표:", out)
            rc, out = self._run(args(archive_dir=tmp, history_csv=hist, save_history=True, closes_csv=cc, require_close=True))
            self.assertEqual(rc, 0)
            self.assertNotIn("추이 표:", out)                                        # 새로 넣을 줄이 없으면 표를 다시 쓰지 않는다
            rc, _ = self._run(args(archive_dir=tmp, history_csv=hist, save_history=True, closes_csv=cc))
            h = gex.read_history(hist)
            self.assertEqual(list(h["date"]), dates)                                 # 엄격하지 않으면 호가에 담긴 수준으로 넣는다
            self.assertEqual(h["spot"].iloc[-1], h["ref"].iloc[-1])
            self.assertEqual([f for f in os.listdir(tmp) if f.endswith(".tmp")], [])

    def test_gap_between_saved_and_yahoo_close_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            Run._archive(self, tmp, ["20260610", "20260611"])
            cc, js = os.path.join(tmp, "c.csv"), os.path.join(tmp, "o.json")
            with open(cc, "w", encoding="utf-8") as f:
                f.write("date,spx\n20260610,5000.00\n20260611,5010.00\n")
            yahoo = {"20260610": {"spx": 5000.0}, "20260611": {"spx": 5000.0}}
            with mock.patch.object(gex, "index_closes", return_value=yahoo), mock.patch.object(gex, "ohlc", return_value=None):
                rc, _ = self._run(args(archive_dir=tmp, closes_csv=cc, out_json=js, no_yf=False))
            self.assertEqual(rc, 0)
            with open(js, encoding="utf-8") as f:
                res = json.load(f)
            self.assertEqual(res["spot"], 5000.0)                                    # 야후 값을 쓴다
            self.assertTrue(any("수집할 때 적어 둔 지수 종가(5,010.00)" in f and "+0.20%" in f for f in res["flags"]))
            self.assertFalse(any("종가를 받지 못해" in f for f in res["flags"]))

    def test_save_history_leaves_no_partial_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "h.csv")
            with open(path, "w", encoding="utf-8") as f:
                f.write("date,spot\n20260609,1.0\n")

            class Boom:
                def to_csv(self, p, index=False):
                    with open(p, "w") as f:
                        f.write("half")
                    raise OSError("disk full")

            with self.assertRaises(OSError):
                gex.save_history(Boom(), path)
            self.assertEqual(os.listdir(tmp), ["h.csv"])
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), "date,spot\n20260609,1.0\n")             # 원래 표는 그대로


class Charts(unittest.TestCase):
    def test_both_charts_render(self):
        try:
            import matplotlib  # noqa: F401
        except ImportError:
            self.skipTest("matplotlib 없음")
        r = gex.day(chain(), DATE, SPOT)
        rows = [{**gex.hist_row(r), "date": f"202605{d:02d}", "net_bn": v, "pct": v, "flip": 5000.0 + 10 * v, "spot": 5000.0 - 5 * v}
                for d, v in zip(range(11, 30), np.linspace(-9, 9, 19))]
        hist = gex.merge_history(pd.DataFrame(columns=gex.HIST_COLS), rows + [gex.hist_row(r)])
        days = pd.bdate_range(end=pd.Timestamp(DATE), periods=80)
        c = 5000.0 + 40 * np.sin(np.arange(80) / 6)
        c[-1] = SPOT
        bars = {"date": [d.strftime("%Y%m%d") for d in days], "o": (c - 5).tolist(), "h": (c + 20).tolist(), "l": (c - 25).tolist(), "c": c.tolist()}
        res = json.loads(json.dumps(gex.clean(gex.summary(r, hist, bars))))
        with tempfile.TemporaryDirectory() as tmp:
            for fn, name, width in ((gex.chart, "a.png", 2160), (gex.chart_history, "b.png", 1080)):
                path = fn(res, os.path.join(tmp, name))
                with open(path, "rb") as f:
                    head = f.read(24)
                self.assertEqual(head[:8], b"\x89PNG\r\n\x1a\n")
                self.assertEqual(int.from_bytes(head[16:20], "big"), width)          # 가로 픽셀 수
                self.assertGreater(os.path.getsize(path), 30000)
            no_bars = dict(res)
            no_bars.pop("ohlc")
            self.assertTrue(os.path.exists(gex.chart(no_bars, os.path.join(tmp, "c.png"))))   # 일봉이 없으면 추이 표의 종가로 그린다
            no_bars.pop("history")
            self.assertTrue(os.path.exists(gex.chart(no_bars, os.path.join(tmp, "d.png"))))   # 가격 자료가 전혀 없어도 그려진다
        with self.assertRaises(ValueError):
            gex.chart({"error": "x"}, "x.png")


if __name__ == "__main__":
    unittest.main()
