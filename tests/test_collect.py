"""collect.py 단위 테스트 — 네트워크 없이 돈다 (mock 사용).

이전 클라우드 세션 리뷰가 잡은 6건의 수정이 실제로 구현됐는지를 고정한다:
  1. 점 티커 OSI 파싱 (BRK.B...)          -> test_parse_dot_ticker
  2. 조정계약 뿌리 (AAPL1...)             -> test_parse_adjusted_root
  3. max_retries=0 이어도 1회는 시도       -> test_zero_retries_still_tries_once
  4. 429 Retry-After 존중(상한 120초)      -> test_429_respects_retry_after_capped
  5. 404는 오류가 아니라 no_data           -> test_404_is_nodata / test_collect_404_counts_no_data
  6. 아카이브 원자적 쓰기(.tmp -> replace) -> test_archive_atomic
추가: 날짜 라벨(마감 확정 세션), 성공률 미달 시 아카이브 미생성.
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


def http_error(code, headers=None):
    return urllib.error.HTTPError("http://x", code, "err", headers or {}, io.BytesIO(b""))


class TestSymbolParsing(unittest.TestCase):
    def test_parse_normal(self):
        self.assertEqual(collect.parse_option_symbol("AAPL261218C00150000"),
                         ("2026-12-18", "Call", 150.0))

    def test_parse_dot_ticker(self):
        # 점 티커: 뿌리 'BRK.B' — 꼬리 고정폭 파싱이어야 정상 처리된다
        self.assertEqual(collect.parse_option_symbol("BRK.B261218C00150000"),
                         ("2026-12-18", "Call", 150.0))

    def test_parse_adjusted_root(self):
        # 조정계약: 뿌리 'AAPL1'
        self.assertEqual(collect.parse_option_symbol("AAPL1260116P00090500"),
                         ("2026-01-16", "Put", 90.5))

    def test_parse_rejects_garbage(self):
        for bad in ("", "AAPL", "AAPL2612X8C00150000", "AAPL261218X00150000",
                    "AAPL261218C0015000Z"):
            self.assertEqual(collect.parse_option_symbol(bad), (None, None, None))

    def test_normalize_hyphen_to_dot(self):
        self.assertEqual(collect.normalize_ticker(" brk-b "), "BRK.B")

    def test_api_symbol_indices_underscored(self):
        self.assertEqual(collect.get_api_symbol("SPX"), "_SPX")
        self.assertEqual(collect.get_api_symbol("BRK.B"), "BRK.B")


class TestFetch(unittest.TestCase):
    def test_zero_retries_still_tries_once(self):
        calls = []
        with mock.patch.object(collect.urllib.request, "urlopen",
                               side_effect=lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(
                                   urllib.error.URLError("down"))):
            with self.assertRaises(urllib.error.URLError):
                collect.fetch_json("http://x", max_retries=0, _sleep=lambda s: None)
        self.assertEqual(len(calls), 1)  # 0이어도 최초 1회는 시도

    def test_retries_count(self):
        calls = []
        with mock.patch.object(collect.urllib.request, "urlopen",
                               side_effect=lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(
                                   urllib.error.URLError("down"))):
            with self.assertRaises(urllib.error.URLError):
                collect.fetch_json("http://x", max_retries=2, _sleep=lambda s: None)
        self.assertEqual(len(calls), 3)  # 1 + 재시도 2

    def test_404_is_nodata(self):
        calls = []
        with mock.patch.object(collect.urllib.request, "urlopen",
                               side_effect=lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(
                                   http_error(404))):
            with self.assertRaises(collect.NoDataError):
                collect.fetch_json("http://x", max_retries=3, _sleep=lambda s: None)
        self.assertEqual(len(calls), 1)  # 404는 재시도하지 않는다

    def test_plain_403_is_nodata(self):
        # CBOE 실측: 없는 심볼(NVR, BRK-B 형식 오류 등)은 403 -> no_data
        with mock.patch.object(collect.urllib.request, "urlopen",
                               side_effect=http_error(403)):
            with self.assertRaises(collect.NoDataError):
                collect.fetch_json("http://x", max_retries=3, _sleep=lambda s: None)

    def test_403_with_deny_reason_is_hard_error(self):
        # 샌드박스 이그레스 차단(403 + x-deny-reason)은 환경설정 오류로 즉시 실패
        with mock.patch.object(collect.urllib.request, "urlopen",
                               side_effect=http_error(403, {"x-deny-reason": "host_not_allowed"})):
            with self.assertRaisesRegex(RuntimeError, "cdn.cboe.com"):
                collect.fetch_json("http://x", max_retries=3, _sleep=lambda s: None)

    def test_429_respects_retry_after_capped(self):
        sleeps = []
        seq = [http_error(429, {"Retry-After": "999"}), http_error(429, {"Retry-After": "7"})]

        def fake_urlopen(*a, **k):
            if seq:
                raise seq.pop(0)
            import json as _j
            return mock.MagicMock(__enter__=lambda s: mock.MagicMock(
                read=lambda: _j.dumps({"data": {"options": []}}).encode()),
                __exit__=lambda *x: False)

        with mock.patch.object(collect.urllib.request, "urlopen", side_effect=fake_urlopen):
            out = collect.fetch_json("http://x", max_retries=3, _sleep=sleeps.append)
        self.assertEqual(out, {"data": {"options": []}})
        self.assertEqual(sleeps[0], 120)  # 999 -> 상한 120으로 캡
        self.assertEqual(sleeps[1], 7)    # 헤더값 존중


class TestSessionDate(unittest.TestCase):
    # 2026-07-03(금) = 독립기념일 대체휴장, 07-04(토), 07-05(일)
    def test_before_close_uses_previous_session(self):
        now = datetime(2026, 7, 6, 4, 20, tzinfo=ET)  # 월요일 새벽(장 마감 전)
        self.assertEqual(collect.completed_session_date(now), "20260702")

    def test_after_close_uses_today(self):
        now = datetime(2026, 7, 6, 18, 30, tzinfo=ET)  # 월요일 장 마감 후
        self.assertEqual(collect.completed_session_date(now), "20260706")

    def test_weekend_uses_last_session(self):
        now = datetime(2026, 7, 5, 12, 0, tzinfo=ET)  # 일요일
        self.assertEqual(collect.completed_session_date(now), "20260702")

    def test_holiday_uses_last_session(self):
        now = datetime(2026, 7, 3, 12, 0, tzinfo=ET)  # 금요일이지만 휴장
        self.assertEqual(collect.completed_session_date(now), "20260702")


class TestArchiveAndThreshold(unittest.TestCase):
    def test_archive_atomic(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "quotes_20260702")
            os.makedirs(os.path.join(src, "AAPL"))
            with open(os.path.join(src, "AAPL", "a.csv"), "w") as f:
                f.write("x\n")
            dest = os.path.join(td, "quotes_20260702.tar.gz")
            collect.make_archive(src, dest)
            self.assertTrue(os.path.exists(dest))
            self.assertFalse(os.path.exists(dest + ".tmp"))  # 임시파일 잔존 금지
            with tarfile.open(dest) as tar:
                self.assertIn("quotes_20260702/AAPL/a.csv", tar.getnames())

    def test_collect_404_counts_no_data(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(collect, "fetch_json",
                                   side_effect=collect.NoDataError("x")):
                ok, no_data, failed = collect.collect(["NVR"], "20260702", td, sleep=0)
        self.assertEqual((ok, no_data, failed), ([], ["NVR"], []))

    def test_mass_no_data_blocks_archive(self):
        # 대량 no_data(예: 차단 오인)여도 순수 성공률이 기준 미달이면 아카이브 금지
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(collect, "fetch_json",
                                   side_effect=collect.NoDataError("x")):
                rc = collect.main(["--tickers", "AAPL", "MSFT", "NVDA", "--out-dir", td,
                                   "--sleep", "0"])
            self.assertEqual(rc, 1)
            self.assertEqual([f for f in os.listdir(td) if f.endswith(".tar.gz")], [])

    def test_min_ok_ratio_blocks_partial_archive(self):
        # 전부 실패하면 아카이브를 만들지 않고 exit 1
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(collect, "fetch_json",
                                   side_effect=urllib.error.URLError("down")), \
                 mock.patch.object(collect.time, "sleep", lambda s: None):
                rc = collect.main(["--tickers", "AAPL", "MSFT", "--out-dir", td,
                                   "--sleep", "0", "--max-retries", "0"])
            self.assertEqual(rc, 1)
            self.assertEqual([f for f in os.listdir(td) if f.endswith(".tar.gz")], [])

    def test_exists_short_circuits(self):
        with tempfile.TemporaryDirectory() as td:
            date = collect.completed_session_date()
            open(os.path.join(td, f"quotes_{date}.tar.gz"), "w").close()
            with mock.patch.object(collect, "fetch_json") as mfetch:
                rc = collect.main(["--out-dir", td, "--tickers", "AAPL", "--sleep", "0"])
            self.assertEqual(rc, 0)
            mfetch.assert_not_called()  # 멱등성: 이미 있으면 네트워크 요청 없음


class TestReviewFixes(unittest.TestCase):
    """검수에서 제기돼 확정·수정한 결함들의 회귀 방지."""

    def test_fallback_skips_comment_lines(self):
        # 폴백 파일의 '#' 주석·빈 줄을 티커로 오파싱하지 않는다(예전엔 FOR 등 유입)
        content = "# header comment\n# source: x refreshed 2026\nAAPL\nBRK.B\nBF.B\n\n"
        with tempfile.TemporaryDirectory() as td:
            fp = os.path.join(td, "fb.txt")
            with open(fp, "w", encoding="utf-8") as f:
                f.write(content)
            with mock.patch.object(collect, "FALLBACK_TICKER_FILE", fp), \
                 mock.patch.object(collect.urllib.request, "urlopen",
                                   side_effect=urllib.error.URLError("no net")):
                syms, source = collect.load_sp500()
        self.assertEqual(source, "fallback")
        self.assertEqual(syms, ["AAPL", "BRK.B", "BF.B"])

    def test_early_close_after_close_uses_today(self):
        # 2026-11-27 추수감사절 다음날 = 13:00 ET 조기폐장. 14:00엔 당일이 확정됨
        now = datetime(2026, 11, 27, 14, 0, tzinfo=ET)
        self.assertEqual(collect.completed_session_date(now), "20261127")

    def test_early_close_before_close_uses_previous(self):
        now = datetime(2026, 11, 27, 12, 0, tzinfo=ET)  # 조기폐장(13:00) 전
        self.assertEqual(collect.completed_session_date(now), "20261125")

    def test_egress_block_aborts_immediately(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(collect, "fetch_json",
                                   side_effect=collect.EgressBlockedError("blocked")):
                with self.assertRaises(collect.EgressBlockedError):
                    collect.collect(["AAPL", "MSFT"], "20260702", td, sleep=0)

    def test_systemic_failure_aborts_early(self):
        # 40개 전부 실패해도 30개째에서 계통 장애로 중단(전부 갈아넣지 않음)
        many = [f"T{i}" for i in range(40)]
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(collect, "fetch_json",
                                   side_effect=urllib.error.URLError("down")) as mfetch, \
                 mock.patch.object(collect.time, "sleep", lambda s: None):
                with self.assertRaises(collect.SystemicFailureError):
                    collect.collect(many, "20260702", td, sleep=0, max_retries=0)
            self.assertEqual(mfetch.call_count, collect.ABORT_NO_OK_AFTER)

    def test_negative_retry_after_no_crash(self):
        sleeps, seq = [], [http_error(429, {"Retry-After": "-5"})]

        def fake(*a, **k):
            if seq:
                raise seq.pop(0)
            import json as _j
            return mock.MagicMock(__enter__=lambda s: mock.MagicMock(
                read=lambda: _j.dumps({"data": {"options": []}}).encode()),
                __exit__=lambda *x: False)

        with mock.patch.object(collect.urllib.request, "urlopen", side_effect=fake):
            out = collect.fetch_json("http://x", max_retries=2, _sleep=sleeps.append)
        self.assertEqual(out, {"data": {"options": []}})
        self.assertEqual(sleeps, [0])  # -5 -> 0, 음수 sleep 크래시 없음

    def test_write_ticker_csv_atomic_success(self):
        with tempfile.TemporaryDirectory() as td:
            rows = [{c: 0 for c in collect.CSV_COLUMNS}]
            collect._write_ticker_csv(td, "20260702", "BRK.B", rows)
            f = os.path.join(td, "quotes_20260702", "BRK.B", "brk.b_quotes_20260702.csv")
            self.assertTrue(os.path.exists(f))
            self.assertFalse(os.path.exists(f + ".tmp"))

    def test_write_ticker_csv_no_partial_on_failure(self):
        with tempfile.TemporaryDirectory() as td:
            rows = [{c: 0 for c in collect.CSV_COLUMNS}]
            with mock.patch.object(collect.os, "replace", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    collect._write_ticker_csv(td, "20260702", "AAPL", rows)
            d = os.path.join(td, "quotes_20260702", "AAPL")
            leftovers = sorted(os.listdir(d)) if os.path.exists(d) else []
            self.assertEqual(leftovers, [])  # 최종본도 .tmp도 남기지 않음


if __name__ == "__main__":
    unittest.main(verbosity=2)
