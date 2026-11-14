"""collect.py 의 '곁들이는 표'(지수 종가, GEX 추이) 단위 테스트 — 네트워크 없이 돈다.

지키려는 것은 하나다: 이 단계가 어떻게 되든 수집 결과('OK:' 줄, 종료 코드, 아카이브)는 그대로여야 한다.
  1. 종가는 마감 스냅샷으로 확인된 지수 파일에서만 적는다            -> TestCloseCapture
  2. 종가 표는 날짜별 한 줄, 받은 값만 바꾸고, 쓰다 멈춰도 원래 표가 남는다 -> TestClosesTable
  3. GEX 추이는 별도 프로세스로 돌리고, 실패·지연·예외가 밖으로 나오지 않는다 -> TestUpdateDerived
  4. main(): 'OK:' 뒤에 안내 줄이 붙고, 끌 수 있고, 지정 티커 모드에서는 하지 않는다 -> TestMain
  5. 실제 gex.py 를 띄워 표에 줄이 들어가는지                         -> TestEndToEnd
"""
import io
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import collect  # noqa: E402

DATE = "20261113"                     # 금요일. 정규장 마감 16:00 ET = 21:00 UTC
OK_TS = "2026-11-13 22:30:00"         # 17:30 ET: 마감 뒤
INTRADAY_TS = "2026-11-13 20:49:00"   # 15:49 ET: 마감 전


def payload(sym, ts=OK_TS, px=None, last="2026-11-13T15:59:59"):
    obj = {"data": {"options": [{"option": f"{sym}261218C00150000", "bid": 1, "ask": 2}]}}
    if ts:
        obj["timestamp"] = ts
    if last:
        obj["data"]["last_trade_time"] = last
    if px is not None:
        obj["data"]["current_price"] = px
    return obj


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestCloseCapture(unittest.TestCase):
    def test_underlying_price(self):
        up = collect.underlying_price
        self.assertEqual(up({"data": {"current_price": 7561.6699}}), 7561.6699)
        self.assertEqual(up({"data": {"current_price": "19.3"}}), 19.3)
        for bad in ({}, {"data": None}, {"data": {}}, {"data": {"current_price": None}}, {"data": {"current_price": "x"}},
                    {"data": {"current_price": 0}}, {"data": {"current_price": -1}}, {"data": {"current_price": float("nan")}},
                    {"data": {"current_price": float("inf")}}, {"data": []}):
            self.assertIsNone(up(bad), bad)

    def _collect(self, files):
        rep = {}
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(collect, "fetch_json", side_effect=lambda url, **k: files[url.rsplit("/", 1)[1][:-5]]), \
                 mock.patch.object(collect, "request_refresh", return_value=False):
                ok, _, _ = collect.collect([t.lstrip("_") for t in files], DATE, td, sleep=0, report=rep)
        return ok, rep

    def test_only_index_closing_snapshots_are_recorded(self):
        ok, rep = self._collect({"_SPX": payload("SPX", px=7561.6699), "_VIX": payload("VIX", px=19.3),
                                 "_NDX": payload("NDX", px=27000.0), "AAPL": payload("AAPL", px=354.2)})
        self.assertEqual(sorted(ok), ["AAPL", "NDX", "SPX", "VIX"])
        self.assertEqual(rep["closes"], {"SPX": 7561.6699, "VIX": 19.3})          # 다른 지수·주식은 적지 않는다

    def test_intraday_or_undated_file_is_not_a_close(self):
        ok, rep = self._collect({"_SPX": payload("SPX", ts=INTRADAY_TS, px=7500.0), "_VIX": payload("VIX", ts=None, last=None, px=20.0)})
        self.assertEqual(sorted(ok), ["SPX", "VIX"])                              # 저장은 예전대로 한다
        self.assertEqual(rep["closes"], {})

    def test_missing_price_is_skipped(self):
        _, rep = self._collect({"_SPX": payload("SPX"), "_VIX": payload("VIX", px="n/a")})
        self.assertEqual(rep["closes"], {})


class TestClosesTable(unittest.TestCase):
    def test_upsert_keeps_other_days_and_untouched_values(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, collect.CLOSES_FILE)
            with open(path, "w", encoding="utf-8") as f:
                f.write("date,spx,vix,source\n20261112,7592.40,18.70,yahoo\n20261110,7600.00,17.00,yahoo\n")
            self.assertEqual(collect.save_index_closes(td, DATE, {"SPX": 7561.6699, "VIX": 19.3}), 3)
            self.assertEqual(read(path), "date,spx,vix,source\n20261110,7600.00,17.00,yahoo\n20261112,7592.40,18.70,yahoo\n"
                                         "20261113,7561.67,19.30,cboe\n")
            self.assertEqual(collect.save_index_closes(td, DATE, {"VIX": 19.5}), 3)   # 다시 받은 값만 바꾼다
            self.assertTrue(read(path).endswith("20261113,7561.67,19.50,cboe\n"))
            self.assertEqual(os.listdir(td), [collect.CLOSES_FILE])

    def test_creates_table_and_tolerates_unknown_columns(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(collect.save_index_closes(td, DATE, {"SPX": 7561.67}), 1)
            self.assertEqual(read(os.path.join(td, collect.CLOSES_FILE)), "date,spx,vix,source\n20261113,7561.67,,cboe\n")
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, collect.CLOSES_FILE)
            with open(path, "w", encoding="utf-8") as f:
                f.write("date,spx,extra\n20261112,7592.40,zzz\n,1,2\n")
            self.assertEqual(collect.save_index_closes(td, DATE, {"SPX": 7561.67}), 2)
            self.assertEqual(read(path), "date,spx,vix,source\n20261112,7592.40,,\n20261113,7561.67,,cboe\n")

    def test_failed_write_leaves_the_old_table(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, collect.CLOSES_FILE)
            with open(path, "w", encoding="utf-8") as f:
                f.write("date,spx,vix,source\n20261112,7592.40,18.70,yahoo\n")
            with mock.patch.object(collect.os, "replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    collect.save_index_closes(td, DATE, {"SPX": 1.0})
            self.assertEqual(read(path), "date,spx,vix,source\n20261112,7592.40,18.70,yahoo\n")
            self.assertEqual(os.listdir(td), [collect.CLOSES_FILE])


class TestUpdateDerived(unittest.TestCase):
    CLOSES = {"SPX": 7561.6699, "VIX": 19.3}

    def test_nothing_to_do_without_closes(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(collect.subprocess, "run") as run:
            self.assertEqual(collect.update_derived(td, DATE, {}), [])
            self.assertEqual(os.listdir(td), [])
        run.assert_not_called()

    def test_no_gex_row_without_spx_close(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(collect.subprocess, "run") as run:
            lines = collect.update_derived(td, DATE, {"VIX": 19.3})
            self.assertEqual(os.listdir(td), [collect.CLOSES_FILE])
        run.assert_not_called()
        self.assertEqual(len(lines), 1)
        self.assertIn("지수 종가 표: index_closes.csv (1일) - VIX 19.30", lines[0])

    def test_runs_gex_in_a_separate_process(self):
        done = subprocess.CompletedProcess([], 0, stdout="추이 표: x/gex_history.csv (87일)\n\n== SPX 감마 노출(GEX) | 20261113\n", stderr="")
        with tempfile.TemporaryDirectory() as td, mock.patch.object(collect.subprocess, "run", return_value=done) as run:
            lines = collect.update_derived(td, DATE, self.CLOSES)
        cmd, kw = run.call_args[0][0], run.call_args[1]
        self.assertEqual(cmd[1], os.path.join(collect.BASE_DIR, "gex.py"))
        self.assertEqual(cmd[cmd.index("--archive-dir") + 1], td)
        self.assertEqual(cmd[cmd.index("--history-csv") + 1], os.path.join(td, "gex_history.csv"))
        self.assertEqual(cmd[cmd.index("--closes-csv") + 1], os.path.join(td, "index_closes.csv"))
        for flag in ("--save-history", "--require-close", "--no-yf"):                 # 종가 없는 줄은 넣지 않고, 네트워크를 쓰지 않는다
            self.assertIn(flag, cmd)
        self.assertEqual(kw["timeout"], collect.DERIVED_TIMEOUT_SEC)
        self.assertLessEqual(collect.DERIVED_TIMEOUT_SEC, 120)                        # 루틴의 명령 실행 시간을 넘기지 않게 짧게 둔다
        self.assertEqual(lines[0], "  지수 종가 표: index_closes.csv (1일) - SPX 7,561.67, VIX 19.30")
        self.assertEqual(lines[1], "  GEX 추이 표: x/gex_history.csv (87일)")

    def test_failures_become_a_note_never_an_exception(self):
        cases = [
            (dict(return_value=subprocess.CompletedProcess([], 1, stdout="[오류] 보관본이 없음\n", stderr="")), "종료 코드 1: [오류] 보관본이 없음"),
            (dict(return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="Traceback\nModuleNotFoundError: numpy\n")), "ModuleNotFoundError: numpy"),
            (dict(return_value=subprocess.CompletedProcess([], 0, stdout="== SPX 감마 노출\n", stderr="")), "종료 코드 0"),   # 표를 쓰지 않고 끝남
            (dict(return_value=subprocess.CompletedProcess([], 2, stdout=None, stderr=None)), "출력 없음"),
            (dict(side_effect=subprocess.TimeoutExpired("gex.py", 90)), "초 안에 끝나지 않음"),
            (dict(side_effect=OSError("exec format error")), "OSError"),
        ]
        for kw, expect in cases:
            with tempfile.TemporaryDirectory() as td, mock.patch.object(collect.subprocess, "run", **kw):
                lines = collect.update_derived(td, DATE, self.CLOSES)
            self.assertEqual(len(lines), 2, expect)
            self.assertTrue(lines[1].startswith("  참고: GEX 추이 표를 갱신하지 못함("), lines[1])
            self.assertIn(expect, lines[1])
            for ln in lines:                                                          # 루틴이 읽는 머리말로 시작하는 줄을 만들지 않는다
                self.assertFalse(ln.lstrip().startswith(("ERROR", "OK:", "exists", "skip", "WARN")), ln)

    def test_closes_table_failure_stops_quietly(self):
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(collect, "save_index_closes", side_effect=PermissionError("read-only")), \
             mock.patch.object(collect.subprocess, "run") as run:
            lines = collect.update_derived(td, DATE, self.CLOSES)
        run.assert_not_called()
        self.assertEqual(len(lines), 1)
        self.assertIn("참고: 지수 종가 표를 갱신하지 못함(PermissionError", lines[0])

    def test_skips_gex_when_the_script_is_absent(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as empty, \
             mock.patch.object(collect, "BASE_DIR", empty), mock.patch.object(collect.subprocess, "run") as run:
            lines = collect.update_derived(td, DATE, self.CLOSES)
        run.assert_not_called()
        self.assertEqual(len(lines), 1)


class TestMain(unittest.TestCase):
    def _run(self, argv_extra=(), derived=None, tickers=None):
        date = collect.completed_session_date()
        close = collect._NYSE.session_close(pd.Timestamp(date))
        ts = (close + pd.Timedelta(minutes=90)).strftime("%Y-%m-%d %H:%M:%S")
        last = close.tz_convert("US/Eastern").strftime("%Y-%m-%dT15:59:59")
        prices = {"_SPX": 7561.6699, "_VIX": 19.3}

        def fake(url, **k):
            sym = url.rsplit("/", 1)[1][:-5]
            return payload(sym.lstrip("_"), ts=ts, px=prices.get(sym, 100.0), last=last)

        done = subprocess.CompletedProcess([], 0, stdout="추이 표: gex_history.csv (87일)\n", stderr="")
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            patches = [mock.patch.object(collect, "fetch_json", side_effect=fake),
                       mock.patch.object(collect, "request_refresh", return_value=False),
                       mock.patch.object(collect, "build_universe", return_value=(["SPX", "VIX"] + [f"T{i}" for i in range(10)], "test")),
                       mock.patch.object(collect, "snapshot_window", return_value=(False, False)),
                       mock.patch.object(collect.subprocess, "run", return_value=done),
                       mock.patch("sys.stdout", buf)]
            if derived is not None:
                patches.append(mock.patch.object(collect, "update_derived", **derived))
            for p in patches:
                p.start()
            try:
                rc = collect.main(["--out-dir", td, "--sleep", "0", *argv_extra])
                run_mock = collect.subprocess.run
                derived_mock = collect.update_derived if derived is not None else None
            finally:
                for p in reversed(patches):
                    p.stop()
            files = sorted(os.listdir(td))
            closes = read(os.path.join(td, collect.CLOSES_FILE)) if collect.CLOSES_FILE in files else None
        return rc, buf.getvalue(), files, closes, date, run_mock, derived_mock

    def test_notes_follow_the_ok_line(self):
        rc, out, files, closes, date, run, _ = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(files, [collect.CLOSES_FILE, f"quotes_{date}.tar.gz"])
        self.assertEqual(closes, f"date,spx,vix,source\n{date},7561.67,19.30,cboe\n")
        lines = out.splitlines()
        i = next(k for k, ln in enumerate(lines) if ln.startswith("OK:"))
        self.assertIn("  지수 종가 표: index_closes.csv (1일) - SPX 7,561.67, VIX 19.30", lines[i + 1:])
        self.assertIn("  GEX 추이 표: gex_history.csv (87일)", lines[i + 1:])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(sum(ln.startswith(("OK:", "ERROR", "exists")) for ln in lines), 1)   # 루틴이 읽는 줄은 그대로 하나

    def test_collection_result_survives_any_failure_here(self):
        rc, out, files, _, date, _, _ = self._run(derived=dict(side_effect=RuntimeError("boom")))
        self.assertEqual(rc, 0)
        self.assertIn(f"quotes_{date}.tar.gz", files)
        self.assertIn("OK:", out)
        self.assertIn("참고: 곁들이는 표를 갱신하지 못함(RuntimeError)", out)
        self.assertNotIn("Traceback", out)

    def test_can_be_switched_off(self):
        rc, out, files, _, date, run, derived = self._run(argv_extra=["--no-derived"], derived=dict(return_value=["x"]))
        self.assertEqual(rc, 0)
        derived.assert_not_called()
        self.assertEqual(files, [f"quotes_{date}.tar.gz"])

    def test_ticker_mode_does_not_touch_the_tables(self):
        rc, _, files, _, _, _, derived = self._run(argv_extra=["--tickers", "SPX"], derived=dict(return_value=["x"]))
        self.assertEqual(rc, 0)
        derived.assert_not_called()
        self.assertNotIn(collect.CLOSES_FILE, files)

    def test_no_archive_no_tables(self):
        # 성공률 미달로 아카이브를 만들지 않은 날에는 표도 건드리지 않는다
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(collect, "fetch_json", side_effect=collect.NoDataError("x")), \
                 mock.patch.object(collect, "build_universe", return_value=([f"T{i}" for i in range(12)], "test")), \
                 mock.patch.object(collect, "snapshot_window", return_value=(False, False)), \
                 mock.patch.object(collect, "update_derived") as derived, \
                 mock.patch("sys.stdout", io.StringIO()):
                rc = collect.main(["--out-dir", td, "--sleep", "0"])
            self.assertEqual(rc, 1)
            self.assertEqual(os.listdir(td), [])
        derived.assert_not_called()


class TestEndToEnd(unittest.TestCase):
    """실제 gex.py 를 별도 프로세스로 띄운다(가상 SPX 체인 두 날짜)."""

    def test_real_script_adds_the_days_row(self):
        try:
            import gex
        except ImportError as e:                                                      # numpy 등이 없는 환경
            self.skipTest(f"gex.py 를 불러오지 못함: {e}")
        dates = ["20260610", "20260611"]
        with tempfile.TemporaryDirectory() as td:
            for d in dates:
                data = gex.synthetic(d, 5000.0).to_csv(index=False).encode()
                with tarfile.open(os.path.join(td, f"quotes_{d}.tar.gz"), "w:gz") as tf:
                    info = tarfile.TarInfo(f"quotes_{d}/SPX/spx_quotes_{d}.csv")
                    info.size = len(data)
                    tf.addfile(info, io.BytesIO(data))
            with open(os.path.join(td, collect.CLOSES_FILE), "w", encoding="utf-8") as f:
                f.write("date,spx,vix,source\n20260610,4990.00,15.00,yahoo\n")
            lines = collect.update_derived(td, "20260611", {"SPX": 5001.2345, "VIX": 15.5})
            self.assertEqual(lines[0], "  지수 종가 표: index_closes.csv (2일) - SPX 5,001.23, VIX 15.50")
            self.assertEqual(lines[1], f"  GEX 추이 표: {os.path.join(td, 'gex_history.csv')} (2일)", lines)
            h = pd.read_csv(os.path.join(td, collect.GEX_FILE), dtype={"date": str})
            self.assertEqual(list(h["date"]), dates)
            self.assertEqual(list(h["spot"]), [4990.0, 5001.23])                      # 표에 적힌 종가 수준에서 계산한다
            self.assertTrue((h["gross_bn"] > 0).all())
            self.assertEqual(sorted(f for f in os.listdir(td) if not f.endswith(".tar.gz")), [collect.GEX_FILE, collect.CLOSES_FILE])
            before = read(os.path.join(td, collect.GEX_FILE))
            lines = collect.update_derived(td, "20260611", {"SPX": 5001.2345, "VIX": 15.5})   # 다시 돌려도 같은 표
            self.assertEqual(read(os.path.join(td, collect.GEX_FILE)), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
