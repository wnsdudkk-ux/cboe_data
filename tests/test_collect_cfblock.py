"""Cloudflare IP 차단(403 'Sorry, you have been blocked') 구분 테스트 — 네트워크 없이 돈다.

실측 2026-11-19: 클라우드 환경에서 cdn-api.cboe.com 의 모든 종목이 Cloudflare 차단 페이지를
받았는데, '없는 심볼' 403(no_data)으로 분류돼 30개를 센 뒤 'ERROR: 성공 0' 으로만 끝났다.
"""
import io
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import collect  # noqa: E402

CF_PAGE = b"""<!DOCTYPE html><html><head><title>Attention Required! | Cloudflare</title></head>
<body><div id="cf-error-details"><h1>Sorry, you have been blocked</h1>
<h2>You are unable to access cboe.com</h2>
<span>Cloudflare Ray ID: <strong class="font-semibold">a5d2b07aeface13f</strong></span>
<span id="cf-footer-ip" class="hidden">160.79.106.145</span></div></body></html>"""


def http_error(code, body=b"", headers=None):
    return urllib.error.HTTPError("https://cdn-api.cboe.com/x.json", code, "err",
                                  headers or {}, io.BytesIO(body))


class TestCloudflareBlock(unittest.TestCase):
    def test_block_page_is_site_blocked_with_ip_and_ray(self):
        calls = []

        def boom(*a, **k):
            calls.append(1)
            raise http_error(403, CF_PAGE, {"server": "cloudflare"})
        with mock.patch.object(collect.urllib.request, "urlopen", side_effect=boom):
            with self.assertRaises(collect.SiteBlockedError) as cm:
                collect.fetch_json("https://cdn-api.cboe.com/x.json", max_retries=3,
                                   _sleep=lambda s: None)
        msg = str(cm.exception)
        self.assertIn("160.79.106.145", msg)
        self.assertIn("a5d2b07aeface13f", msg)
        self.assertIn("cdn-api.cboe.com", msg)
        self.assertEqual(len(calls), 1)  # 다시 받아도 풀리지 않으므로 재시도하지 않는다

    def test_cf_mitigated_challenge_is_site_blocked(self):
        with mock.patch.object(collect.urllib.request, "urlopen",
                               side_effect=http_error(403, b"<html></html>",
                                                      {"cf-mitigated": "challenge", "cf-ray": "abc"})):
            with self.assertRaises(collect.SiteBlockedError):
                collect.fetch_json("http://x", max_retries=0, _sleep=lambda s: None)

    def test_plain_403_still_nodata(self):
        # CBOE '없는 심볼' 403(차단 문구 없음)은 그대로 no_data
        body = b'<?xml version="1.0"?><Error><Code>AccessDenied</Code></Error>'
        with mock.patch.object(collect.urllib.request, "urlopen",
                               side_effect=http_error(403, body, {"server": "cloudflare"})):
            with self.assertRaises(collect.NoDataError):
                collect.fetch_json("http://x", max_retries=0, _sleep=lambda s: None)

    def test_collect_aborts_on_first_ticker(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(collect, "fetch_json",
                                   side_effect=collect.SiteBlockedError("blocked")) as m:
                with self.assertRaises(collect.SiteBlockedError):
                    collect.collect(["SPX", "AAPL", "MSFT"], "20260702", td, sleep=0)
            self.assertEqual(m.call_count, 1)

    def test_collect_aborts_even_after_success(self):
        opt = {"data": {"options": [{"option": "AAPL261218C00150000"}]}}
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(collect, "fetch_json",
                                   side_effect=[opt, collect.SiteBlockedError("blocked")]):
                with self.assertRaises(collect.SiteBlockedError):
                    collect.collect(["AAPL", "MSFT", "GOOG"], "20260702", td, sleep=0)

    def test_refresh_block_turns_refresh_off(self):
        with mock.patch.object(collect.urllib.request, "urlopen",
                               side_effect=http_error(403, CF_PAGE)):
            with self.assertRaises(collect.SiteBlockedError):
                collect.request_refresh("AAPL")

    def test_main_prints_error_and_exits_1(self):
        with tempfile.TemporaryDirectory() as td:
            buf = io.StringIO()
            err = collect.SiteBlockedError("cdn-api.cboe.com 의 Cloudflare 보안이 ... 차단했다")
            with mock.patch.object(collect, "fetch_json", side_effect=err), \
                 mock.patch("sys.stdout", buf):
                rc = collect.main(["--tickers", "SPX", "AAPL", "--out-dir", td, "--sleep", "0"])
            self.assertEqual(rc, 1)
            self.assertIn("ERROR:", buf.getvalue())
            self.assertIn("Cloudflare", buf.getvalue())
            self.assertFalse([f for f in os.listdir(td) if f.endswith(".tar.gz")])


if __name__ == "__main__":
    unittest.main()
