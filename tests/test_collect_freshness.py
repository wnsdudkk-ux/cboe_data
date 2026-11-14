"""collect.py 2026-11 추가분 단위 테스트 — 네트워크 없이 돈다 (mock 사용).

라벨(세션 날짜)과 내용이 어긋나는 경우를 막는 장치들을 고정한다:
  1. 자료 시점 판정(마감 스냅샷 / 장중 / 다른 날)          -> TestClassify
  2. 다른 날 자료는 저장하지 않고, 장중 자료는 저장하되 알림 -> TestCollectFreshness
  3. 갱신 요청: 접수되면 새 파일로 대체, 환경이 막으면 건너뜀 -> TestRefresh
  4. 2차 시도: 일시 실패 복구, 핵심 종목의 빈 응답 재시도     -> TestSecondPass
  5. 수집 가능 시간(다음 날 장 개시, 야간 거래 개시)          -> TestWindow / TestMainGuard
"""
import io
import os
import sys
import tarfile
import tempfile
import unittest
import urllib.error
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import collect  # noqa: E402

ET = ZoneInfo("US/Eastern")
DATE = "20261113"   # 금요일. 정규장 09:30~16:00 ET = 14:30~21:00 UTC
OK_TS = "2026-11-13 22:30:00"         # 17:30 ET: 마감 뒤
INTRADAY_TS = "2026-11-13 20:49:00"   # 15:49 ET: 마감 전
STALE_TS = "2026-11-06 21:23:00"      # 일주일 전


def payload(sym="AAPL", ts=OK_TS, last="2026-11-13T15:59:59"):
    obj = {"data": {"options": [{"option": f"{sym}261218C00150000", "bid": 1, "ask": 2}]}}
    if ts:
        obj["timestamp"] = ts
    if last:
        obj["data"]["last_trade_time"] = last
    return obj


def http_error(code, headers=None):
    return urllib.error.HTTPError("http://x", code, "err", headers or {}, io.BytesIO(b""))


def csv_exists(root, ticker, date=DATE):
    return os.path.exists(os.path.join(root, f"quotes_{date}", ticker,
                                       f"{ticker.lower()}_quotes_{date}.csv"))


class TestClassify(unittest.TestCase):
    def test_after_close_is_ok(self):
        self.assertEqual(collect.classify_snapshot(payload(), DATE)[0], "ok")

    def test_generated_before_close_is_intraday(self):
        self.assertEqual(collect.classify_snapshot(payload(ts=INTRADAY_TS), DATE)[0], "intraday")

    def test_within_buffer_after_close_is_intraday(self):
        # 16:10 ET에 만들어진 파일은 15분 지연 때문에 15:55 호가다 -> 마감 스냅샷이 아니다
        self.assertEqual(collect.classify_snapshot(payload(ts="2026-11-13 21:10:00"), DATE)[0],
                         "intraday")

    def test_generated_before_session_open_is_stale(self):
        st, gen = collect.classify_snapshot(payload(ts=STALE_TS), DATE)
        self.assertEqual(st, "stale")
        self.assertEqual(collect._et_label(gen), "11-06 16:23 ET")

    def test_underlying_not_traded_that_day_is_stale(self):
        # 실측 KVUE: 파일은 새로 만들어져도 기초자산 마지막 체결이 10-30 -> 거래가 끊긴 종목
        self.assertEqual(collect.classify_snapshot(payload(last="2026-10-30T15:59:59"), DATE)[0],
                         "stale")

    def test_unknown_timestamp_is_ok(self):
        # 생성 시각이 없으면 판정하지 않는다(예전 동작 유지)
        self.assertEqual(collect.classify_snapshot(payload(ts=None, last=None), DATE), ("ok", None))
        self.assertEqual(collect.classify_snapshot({"timestamp": "garbage", "data": {}}, DATE),
                         ("ok", None))

    def test_early_close_uses_actual_close(self):
        # 2026-11-27 13:00 ET 조기폐장. 14:00 ET(19:00 UTC)에 만들어진 파일은 마감 스냅샷
        ok = payload(ts="2026-11-27 19:00:00", last="2026-11-27T12:59:59")
        self.assertEqual(collect.classify_snapshot(ok, "20261127")[0], "ok")
        before = payload(ts="2026-11-27 17:30:00", last="2026-11-27T12:29:59")
        self.assertEqual(collect.classify_snapshot(before, "20261127")[0], "intraday")

    def test_non_session_date_is_not_judged_by_timestamp(self):
        # 세션이 아닌 날짜(토요일)는 개장·마감 시각이 없으므로 생성 시각으로는 판정하지 않는다
        self.assertEqual(collect.classify_snapshot(payload(last=None), "20261114")[0], "ok")


class TestCollectFreshness(unittest.TestCase):
    def test_stale_excluded_intraday_kept(self):
        seq = [payload("AAPL"), payload("KVUE", ts=STALE_TS), payload("Q", ts="2026-11-13 19:00:00")]
        rep = {}
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(collect, "fetch_json", side_effect=seq), \
                 mock.patch.object(collect, "request_refresh", return_value=False):
                ok, no_data, failed = collect.collect(["AAPL", "KVUE", "Q"], DATE, td, sleep=0,
                                                      report=rep)
            self.assertEqual((ok, no_data, failed), (["AAPL", "Q"], ["KVUE"], []))
            self.assertTrue(csv_exists(td, "AAPL"))
            self.assertTrue(csv_exists(td, "Q"))         # 그날 자료는 저장
            self.assertFalse(csv_exists(td, "KVUE"))     # 다른 날 자료는 저장하지 않는다
        self.assertEqual(rep["stale"], [("KVUE", "11-06 16:23 ET")])
        self.assertEqual(rep["intraday"], [("Q", "11-13 14:00 ET")])

    def test_report_is_optional(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(collect, "fetch_json", side_effect=[payload("AAPL")]):
                self.assertEqual(collect.collect(["AAPL"], DATE, td, sleep=0), (["AAPL"], [], []))

    def test_main_reports_stale_and_omits_it_from_archive(self):
        tickers = [f"T{i}" for i in range(11)] + ["DEAD"]
        date = collect.completed_session_date()

        def fake(url, **k):
            sym = url.rsplit("/", 1)[1][:-5]
            if sym == "DEAD":
                return payload(sym, ts=None, last="2020-01-02T15:59:59")
            return payload(sym, ts=None, last=None)

        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(collect, "fetch_json", side_effect=fake), \
                 mock.patch.object(collect, "request_refresh", return_value=False), \
                 mock.patch.object(collect, "build_universe", return_value=(tickers, "test")), \
                 mock.patch.object(collect, "snapshot_window", return_value=(False, False)), \
                 mock.patch("sys.stdout", buf):
                rc = collect.main(["--out-dir", td, "--sleep", "0"])
            self.assertEqual(rc, 0)
            with tarfile.open(os.path.join(td, f"quotes_{date}.tar.gz")) as tar:
                names = tar.getnames()
        out = buf.getvalue()
        self.assertIn("OK:", out)
        self.assertIn("다른 날 자료라 저장하지 않음", out)
        self.assertIn("DEAD(", out)
        self.assertFalse(any("/DEAD/" in n for n in names))
        self.assertTrue(any("/T0/" in n for n in names))


class TestRefresh(unittest.TestCase):
    def test_accepted_refresh_replaces_old_file(self):
        seq = [payload("UUP", ts=INTRADAY_TS), payload("UUP", ts=OK_TS)]
        rep = {}
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(collect, "fetch_json", side_effect=seq), \
                 mock.patch.object(collect, "request_refresh", return_value=True) as mref:
                ok, no_data, failed = collect.collect(["UUP"], DATE, td, sleep=0, report=rep)
            self.assertTrue(csv_exists(td, "UUP"))
        self.assertEqual((ok, no_data, failed), (["UUP"], [], []))
        self.assertEqual((rep["refreshed"], rep["intraday"]), (["UUP"], []))
        mref.assert_called_once_with("UUP")

    def test_refresh_not_reflected_keeps_original(self):
        # 갱신이 접수됐지만 새 파일이 끝내 올라오지 않으면 처음 받은 것을 쓴다(실측: Q)
        old = payload("Q", ts=INTRADAY_TS)
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(collect, "fetch_json", side_effect=[old, old, old, old]), \
                 mock.patch.object(collect, "request_refresh", return_value=True):
                rep = {}
                ok, _, _ = collect.collect(["Q"], DATE, td, sleep=0, report=rep)
        self.assertEqual(ok, ["Q"])
        self.assertEqual((rep["refreshed"], [t for t, _ in rep["intraday"]]), ([], ["Q"]))

    def test_blocked_host_disables_refresh_for_the_run(self):
        seq = [payload("Q", ts=INTRADAY_TS), payload("HONA", ts=INTRADAY_TS)]
        rep = {}
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(collect, "fetch_json", side_effect=seq), \
                 mock.patch.object(collect, "request_refresh",
                                   side_effect=collect.EgressBlockedError("blocked")) as mref:
                ok, no_data, failed = collect.collect(["Q", "HONA"], DATE, td, sleep=0, report=rep)
        self.assertEqual((ok, no_data, failed), (["Q", "HONA"], [], []))  # 수집은 계속된다
        self.assertTrue(rep["refresh_blocked"])
        self.assertEqual(mref.call_count, 1)  # 막힌 걸 안 뒤로는 다시 보내지 않는다

    def test_proprietary_index_is_never_refreshed(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(collect, "fetch_json",
                                   side_effect=[payload("SPX", ts=INTRADAY_TS)]), \
                 mock.patch.object(collect, "request_refresh") as mref:
                collect.collect(["SPX"], DATE, td, sleep=0)
        mref.assert_not_called()

    def test_request_refresh_result_mapping(self):
        ok = mock.MagicMock(__enter__=lambda s: mock.MagicMock(read=lambda: b"{}"),
                            __exit__=lambda *x: False)
        with mock.patch.object(collect.urllib.request, "urlopen", return_value=ok):
            self.assertTrue(collect.request_refresh("UUP"))
        with mock.patch.object(collect.urllib.request, "urlopen", side_effect=http_error(400)):
            self.assertFalse(collect.request_refresh("KVUE"))  # 거래가 끊긴 종목은 400
        with mock.patch.object(collect.urllib.request, "urlopen",
                               side_effect=http_error(403, {"x-deny-reason": "host_not_allowed"})):
            with self.assertRaisesRegex(collect.EgressBlockedError, r"www\.cboe\.com"):
                collect.request_refresh("UUP")
        tunnel = urllib.error.URLError(OSError("Tunnel connection failed: 403 Forbidden"))
        with mock.patch.object(collect.urllib.request, "urlopen", side_effect=tunnel):
            with self.assertRaisesRegex(collect.EgressBlockedError, r"www\.cboe\.com"):
                collect.request_refresh("UUP")
        with mock.patch.object(collect.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("timed out")):
            self.assertIsNone(collect.request_refresh("UUP"))  # 보내지 못함(접속 문제)

    def test_unreachable_twice_disables_refresh(self):
        tickers = ["Q", "HONA", "KO"]
        seq = [payload(t, ts=INTRADAY_TS) for t in tickers]
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(collect, "fetch_json", side_effect=seq), \
                 mock.patch.object(collect, "request_refresh", return_value=None) as mref:
                ok, _, _ = collect.collect(tickers, DATE, td, sleep=0)
        self.assertEqual(ok, tickers)
        self.assertEqual(mref.call_count, 2)  # 두 번 연달아 못 보내면 그만둔다


class TestSecondPass(unittest.TestCase):
    def test_transient_failure_recovered(self):
        seq = [payload("AAPL"), urllib.error.URLError("down"), payload("MSFT")]
        rep = {}
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(collect, "fetch_json", side_effect=seq):
                ok, no_data, failed = collect.collect(["AAPL", "MSFT"], DATE, td, sleep=0,
                                                      max_retries=0, report=rep)
            self.assertTrue(csv_exists(td, "MSFT"))
        self.assertEqual((ok, no_data, failed), (["AAPL", "MSFT"], [], []))
        self.assertEqual(rep["retried"], ["MSFT"])

    def test_no_second_pass_when_nothing_succeeded(self):
        # 성공이 0이면 접속 자체가 안 되는 상황 -> 다시 돌지 않는다
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(collect, "fetch_json",
                                   side_effect=urllib.error.URLError("down")) as mfetch:
                ok, no_data, failed = collect.collect(["AAPL", "MSFT"], DATE, td, sleep=0,
                                                      max_retries=0)
        self.assertEqual((ok, no_data, [t for t, _ in failed]), ([], [], ["AAPL", "MSFT"]))
        self.assertEqual(mfetch.call_count, 2)

    def test_core_ticker_no_data_is_retried(self):
        seq = [payload("AAPL"), collect.NoDataError("x"), payload("SPX")]
        rep = {}
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(collect, "fetch_json", side_effect=seq):
                ok, no_data, failed = collect.collect(["AAPL", "SPX"], DATE, td, sleep=0, report=rep)
        self.assertEqual((ok, no_data, failed), (["AAPL", "SPX"], [], []))
        self.assertEqual((rep["retried"], rep["core_missing"]), (["SPX"], []))

    def test_core_ticker_still_missing_is_reported(self):
        seq = [payload("AAPL"), collect.NoDataError("x"), collect.NoDataError("x")]
        rep = {}
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(collect, "fetch_json", side_effect=seq):
                ok, no_data, failed = collect.collect(["AAPL", "SPX"], DATE, td, sleep=0, report=rep)
        self.assertEqual((ok, no_data, failed), (["AAPL"], ["SPX"], []))
        self.assertEqual(rep["core_missing"], ["SPX"])


class TestWindow(unittest.TestCase):
    def w(self, date, *ymdhm):
        return collect.snapshot_window(date, datetime(*ymdhm, tzinfo=ET))

    def test_friday_session(self):
        self.assertEqual(self.w("20261113", 2026, 11, 13, 17, 30), (False, False))  # 정상 수집 시각
        self.assertEqual(self.w("20261113", 2026, 11, 14, 12, 0), (False, False))   # 토요일
        self.assertEqual(self.w("20261113", 2026, 11, 15, 20, 20), (False, False))  # 일 20:15 개시 직후(지연 15분 전)
        self.assertEqual(self.w("20261113", 2026, 11, 15, 20, 45), (False, True))   # 야간 거래 호가 시작
        self.assertEqual(self.w("20261113", 2026, 11, 16, 9, 29), (False, True))
        self.assertEqual(self.w("20261113", 2026, 11, 16, 9, 30), (True, True))     # 월요일 정규장 개시

    def test_weekday_session(self):
        self.assertEqual(self.w("20261112", 2026, 11, 12, 18, 30), (False, False))
        self.assertEqual(self.w("20261112", 2026, 11, 12, 20, 45), (False, True))   # 같은 날 저녁
        self.assertEqual(self.w("20261112", 2026, 11, 13, 11, 0), (True, True))

    def test_holiday_gap(self):
        # 11-25(수) 세션, 11-26(목) 추수감사절 휴장, 다음 세션은 11-27(금)
        self.assertEqual(self.w("20261125", 2026, 11, 26, 12, 0), (False, True))
        self.assertEqual(self.w("20261125", 2026, 11, 27, 9, 45), (True, True))


class TestMainGuard(unittest.TestCase):
    def test_late_full_run_refuses_without_fetching(self):
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(collect, "snapshot_window", return_value=(True, True)), \
                 mock.patch.object(collect, "build_universe") as muni, \
                 mock.patch.object(collect, "fetch_json") as mfetch, \
                 mock.patch("sys.stdout", buf):
                rc = collect.main(["--out-dir", td, "--sleep", "0"])
            self.assertEqual(rc, 1)
            self.assertEqual([f for f in os.listdir(td) if f.endswith(".tar.gz")], [])
        self.assertIn("ERROR: 수집 가능 시간이 지났다", buf.getvalue())
        muni.assert_not_called()
        mfetch.assert_not_called()

    def test_allow_late_overrides(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(collect, "snapshot_window", return_value=(True, True)), \
                 mock.patch.object(collect, "build_universe", return_value=(["SPX", "AAPL"], "t")), \
                 mock.patch.object(collect, "fetch_json",
                                   side_effect=lambda url, **k: payload("X", ts=None, last=None)), \
                 mock.patch("sys.stdout", io.StringIO()):
                rc = collect.main(["--out-dir", td, "--sleep", "0", "--allow-late"])
            self.assertEqual(rc, 0)

    def test_overnight_session_started_skips_gth_indices_only(self):
        tickers = ["SPX", "VIX", "NDX"] + [f"T{i}" for i in range(10)]
        fetched = []

        def fake(url, **k):
            fetched.append(url.rsplit("/", 1)[1][:-5])
            return payload("X", ts=None, last=None)

        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(collect, "snapshot_window", return_value=(False, True)), \
                 mock.patch.object(collect, "build_universe", return_value=(tickers, "t")), \
                 mock.patch.object(collect, "fetch_json", side_effect=fake), \
                 mock.patch("sys.stdout", buf):
                rc = collect.main(["--out-dir", td, "--sleep", "0"])
            self.assertEqual(rc, 0)
        self.assertNotIn("_SPX", fetched)
        self.assertNotIn("_VIX", fetched)
        self.assertIn("_NDX", fetched)   # 야간 거래가 없는 지수는 그대로 받는다
        self.assertIn("WARN: 야간 거래가 이미 시작돼", buf.getvalue())

    def test_ticker_mode_skips_time_check(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(collect, "snapshot_window") as mwin, \
                 mock.patch.object(collect, "fetch_json",
                                   side_effect=lambda url, **k: payload("AAPL", ts=None, last=None)), \
                 mock.patch("sys.stdout", io.StringIO()):
                rc = collect.main(["--tickers", "AAPL", "--out-dir", td, "--sleep", "0"])
            self.assertEqual(rc, 0)
        mwin.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
