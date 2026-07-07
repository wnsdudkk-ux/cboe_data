#!/usr/bin/env python3
"""
cboe_data — CBOE 옵션 일일 스냅샷 수집기
=========================================
매 거래일 CBOE 지연시세 API에서 S&P500(+주요 지수·ETF) 전 종목의 옵션 체인을
내려받아 data/daily/quotes_<YYYYMMDD>.tar.gz 한 파일로 저장한다.
claude.ai 클라우드 루틴(매일 07:30 KST)이 실행하는 스크립트이며, 로컬에서도
동일하게 동작한다. 외부 의존성은 pandas + exchange_calendars 뿐이다.

핵심 동작 원리
--------------
- 날짜 라벨: '마지막으로 종가가 확정된 NYSE 세션'을 기준으로 정한다.
  (세션 실제 마감시각(조기폐장이면 13:00) + 15분 버퍼 이전이면 당일 제외)
  CBOE 지연시세는 항상 직전 마감 세션의 스냅샷이므로 라벨과 내용이 일치한다.
- 멱등성: 해당 날짜 아카이브가 이미 있으면 아무것도 하지 않는다.
  -> 주말·휴장일에 실행돼도 자동으로 'exists'로 끝난다(별도 휴장 체크 불필요).
- 부분 실패 보호: 성공 비율이 --min-ok-ratio(기본 0.9) 미만이면 아카이브를
  만들지 않고 실패(exit 1)로 끝낸다. 반쪽짜리 데이터가 커밋되면 멱등성 때문에
  그 날짜가 영영 재수집되지 않는 문제를 막는다.
- 403 구분: CBOE는 '없는 심볼'에 403을 준다(실측) -> no_data로 분류.
  단 x-deny-reason 헤더가 있는 403은 샌드박스 이그레스 차단이므로 즉시 오류.
- 원자적 쓰기: 아카이브는 .tmp에 만든 뒤 os.replace()로 교체한다.
- 점 티커: CBOE는 클래스주를 점 표기(BRK.B, BF.B) 그대로 받는다(실측 검증).
  옵션 심볼(OSI)은 뿌리 길이가 가변이므로 반드시 꼬리 고정폭으로 파싱한다.

사용법
------
  python collect.py                     # 정상 수집(이미 있으면 종료)
  python collect.py --force             # 기존 아카이브 무시하고 재수집
  python collect.py --tickers BRK.B NVR # 지정 티커만(테스트용)
  python collect.py --sp500-only        # 지수·ETF 제외, S&P500만
"""
import argparse
import csv
import io
import json
import logging
import os
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import exchange_calendars as xcals

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT_DIR = os.path.join(BASE_DIR, "data", "daily")
FALLBACK_TICKER_FILE = os.path.join(BASE_DIR, "sp500_fallback.txt")
CONSTITUENTS_URL = ("https://raw.githubusercontent.com/datasets/"
                    "s-and-p-500-companies/main/data/constituents.csv")
CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
UA_HEADERS = {"User-Agent": "cboe-data-collector/1.0"}

# 세션 실제 마감시각(조기폐장이면 13:00 등) 이후 스냅샷 반영 여유(분).
SNAPSHOT_BUFFER_MIN = 15

# 연속 실패가 이 횟수에 도달하면 잠시 쉬어 서버 측 차단(레이트리밋)을 피한다.
FAIL_STREAK_LIMIT = 5
FAIL_COOLDOWN_SEC = 60
RETRY_AFTER_CAP_SEC = 120
# 초반에 이만큼 시도했는데 성공이 0이면 계통 장애로 보고 전체를 조기 중단한다.
ABORT_NO_OK_AFTER = 30

CSV_COLUMNS = ["Symbol", "Expiry", "Type", "Strike", "Bid", "Ask", "Last",
               "Volume", "Open Interest", "IV", "Delta", "Gamma", "Theta",
               "Vega", "Rho"]

# S&P500 외에 함께 수집하는 기본 유니버스(대시보드 분석의 핵심 기초자산).
# --sp500-only 로 제외할 수 있다.
EXTRA_INDICES = ["SPX", "VIX", "NDX", "RUT", "DJX"]
EXTRA_ETFS = ["SPY", "QQQ", "IWM", "DIA", "GLD", "SLV", "USO", "CPER", "IEF",
              "TLT", "XLF", "EEM", "FXY", "UUP", "PPLT", "PALL", "IBIT"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("cboe_collect")

_NYSE = xcals.get_calendar("XNYS")


class NoDataError(Exception):
    """해당 티커에 옵션 데이터가 없음(HTTP 404/403). 오류가 아니라 정상 분류."""


class EgressBlockedError(RuntimeError):
    """클라우드 샌드박스가 도메인을 차단(403 + x-deny-reason). 환경설정 문제이므로
    한 번이라도 걸리면 티커별로 계속 시도하지 않고 전체 수집을 즉시 중단한다."""


class SystemicFailureError(RuntimeError):
    """초반 다수 티커가 실패하고 성공이 0인 계통 장애(예: CBOE 전체 장애). 몇 시간씩
    갈아 넣는 것을 막기 위해 전체 수집을 조기 중단한다."""


# ---------------------------------------------------------------------------
# 티커 유니버스
# ---------------------------------------------------------------------------

def normalize_ticker(sym):
    """야후식 하이픈 표기를 CBOE가 요구하는 점 표기로 통일한다 (BRK-B -> BRK.B)."""
    return sym.strip().upper().replace("-", ".")


def get_api_symbol(ticker):
    """CBOE API 경로용 심볼. 지수는 밑줄 접두(_SPX), 나머지는 그대로."""
    t = ticker.upper().strip()
    if t in ("SPX", "VIX", "NDX", "RUT", "DJX", "OEX", "XSP"):
        return f"_{t}"
    return t


def load_sp500():
    """S&P500 구성종목을 (티커목록, 출처) 로 반환.

    1차: datasets/s-and-p-500-companies 의 constituents.csv (라이브)
    2차: 저장소에 커밋된 스냅샷 sp500_fallback.txt
    """
    try:
        req = urllib.request.Request(CONSTITUENTS_URL, headers=UA_HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            rows = list(csv.DictReader(io.TextIOWrapper(resp, encoding="utf-8")))
        syms = sorted({normalize_ticker(r["Symbol"]) for r in rows if r.get("Symbol")})
        if len(syms) < 480:  # 파일 구조가 바뀌어 일부만 읽힌 경우 방어
            raise ValueError(f"too few tickers parsed: {len(syms)}")
        return syms, "remote"
    except Exception as e:
        logger.warning(f"S&P500 라이브 목록 실패({e}) -> 로컬 스냅샷 사용")
        syms = []
        with open(FALLBACK_TICKER_FILE, encoding="utf-8") as f:
            for line in f:  # 한 줄에 티커 하나, '#' 주석·빈 줄은 건너뛴다
                line = line.strip()
                if line and not line.startswith("#"):
                    syms.append(normalize_ticker(line))
        return syms, "fallback"


def build_universe(sp500_only=False):
    sp500, source = load_sp500()
    extras = [] if sp500_only else (EXTRA_INDICES + EXTRA_ETFS)
    return list(dict.fromkeys(extras + sp500)), source


# ---------------------------------------------------------------------------
# 날짜: 마지막으로 종가가 확정된 NYSE 세션
# ---------------------------------------------------------------------------

def completed_session_date(now_et=None):
    """마지막으로 '마감이 끝난' NYSE 세션 날짜를 YYYYMMDD로 반환.

    오늘이 세션이라도 실제 마감시각(조기폐장이면 13:00 등)+버퍼 이전이면 직전
    세션을 쓴다. exchange_calendars의 세션별 마감시각을 쓰므로 조기폐장일에도
    라벨이 어긋나지 않는다.
    """
    if now_et is None:
        now_et = datetime.now(ZoneInfo("US/Eastern"))
    now_ts = pd.Timestamp(now_et)
    today = now_et.date()
    sessions = _NYSE.sessions_in_range(
        pd.Timestamp(today - timedelta(days=14)), pd.Timestamp(today))
    dates = [s.date() for s in sessions]
    if dates and dates[-1] == today:
        close_et = _NYSE.session_close(pd.Timestamp(today)).tz_convert("US/Eastern")
        if now_ts < close_et + pd.Timedelta(minutes=SNAPSHOT_BUFFER_MIN):
            dates = dates[:-1]  # 오늘 마감이 아직 안 났으면 직전 세션
    if not dates:
        raise RuntimeError("최근 2주 내 NYSE 세션을 찾지 못했다")
    return dates[-1].strftime("%Y%m%d")


# ---------------------------------------------------------------------------
# 옵션 심볼(OSI) 파싱 — 꼬리 고정폭 방식
# ---------------------------------------------------------------------------

def parse_option_symbol(symbol):
    """OSI 심볼에서 (만기 'YYYY-MM-DD', 'Call'/'Put', 행사가 float)를 파싱.

    구조: [뿌리(가변: BRK.B, AAPL1 등)] + [YYMMDD 6] + [C/P 1] + [행사가*1000 8].
    뿌리 길이가 가변이므로 앞이 아니라 '끝에서 15자리 고정폭'으로 읽어야
    점 티커·조정계약(AAPL1 등)이 전부 올바르게 처리된다. 실패 시 None 3개.
    """
    if not symbol or len(symbol) < 16:
        return None, None, None
    tail = symbol[-15:]
    ds, cp, strike_s = tail[:6], tail[6], tail[7:]
    if not (ds.isdigit() and cp in "CP" and strike_s.isdigit()):
        return None, None, None
    expiry = f"20{ds[:2]}-{ds[2:4]}-{ds[4:6]}"
    return expiry, ("Call" if cp == "C" else "Put"), int(strike_s) / 1000.0


def rows_from(data, ticker):
    """CBOE JSON -> CSV 행 목록. 파싱 실패 심볼은 세며 경고 로그를 남긴다."""
    rows, bad = [], 0
    for opt in data.get("data", {}).get("options", []):
        symbol = opt.get("option", "")
        expiry, opt_type, strike = parse_option_symbol(symbol)
        if expiry is None:
            bad += 1
            continue
        rows.append({
            "Symbol": symbol, "Expiry": expiry, "Type": opt_type, "Strike": strike,
            "Bid": float(opt.get("bid", 0) or 0),
            "Ask": float(opt.get("ask", 0) or 0),
            "Last": float(opt.get("last_trade_price", 0) or 0),
            "Volume": int(opt.get("volume", 0) or 0),
            "Open Interest": int(opt.get("open_interest", 0) or 0),
            "IV": float(opt.get("iv", 0) or 0),
            "Delta": float(opt.get("delta", 0) or 0),
            "Gamma": float(opt.get("gamma", 0) or 0),
            "Theta": float(opt.get("theta", 0) or 0),
            "Vega": float(opt.get("vega", 0) or 0),
            "Rho": float(opt.get("rho", 0) or 0),
        })
    if bad:
        logger.warning(f"{ticker}: OSI 파싱 실패 {bad}건 (건너뜀)")
    return rows


# ---------------------------------------------------------------------------
# HTTP — 재시도·429 Retry-After 존중·404는 NoData
# ---------------------------------------------------------------------------

def fetch_json(url, max_retries=2, _sleep=time.sleep):
    """URL에서 JSON을 받는다. max_retries=0이어도 최초 1회는 반드시 시도한다.

    - 404: 옵션 미상장으로 보고 즉시 NoDataError (재시도 없음)
    - 429: Retry-After 헤더를 존중(상한 RETRY_AFTER_CAP_SEC), 없으면 지수 백오프
    - 그 외 오류: 지수 백오프 후 재시도, 소진 시 마지막 예외를 올린다
    """
    attempts = max(1, int(max_retries) + 1)
    last_err = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers=UA_HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NoDataError(url)
            if e.code == 403:
                # CBOE는 '없는 심볼'에 403을 준다(실측: NVR, BRK-B 등) -> no_data.
                # 단, 클라우드 샌드박스의 이그레스 차단도 403이므로(x-deny-reason
                # 헤더 존재) 그 경우는 환경설정 오류로 즉시 구분해 올린다.
                if e.headers and e.headers.get("x-deny-reason"):
                    raise EgressBlockedError(
                        f"환경 네트워크 차단(x-deny-reason={e.headers['x-deny-reason']}): "
                        "클라우드 환경의 허용 도메인에 cdn.cboe.com 을 추가해야 한다")
                raise NoDataError(url)
            last_err = e
            if i + 1 >= attempts:
                break
            if e.code == 429:
                ra = e.headers.get("Retry-After") if e.headers else None
                try:
                    wait = max(0, min(int(ra), RETRY_AFTER_CAP_SEC)) if ra else min(2 ** (i + 1), 30)
                except ValueError:
                    wait = min(2 ** (i + 1), 30)
            else:
                wait = min(2 ** (i + 1), 30)
            _sleep(wait)
        except Exception as e:
            last_err = e
            if i + 1 >= attempts:
                break
            _sleep(min(2 ** (i + 1), 30))
    raise last_err


# ---------------------------------------------------------------------------
# 수집·아카이브
# ---------------------------------------------------------------------------

def collect(tickers, date, root_dir, sleep=0.3, max_retries=2):
    """티커별 CSV를 root_dir/quotes_<date>/<TICKER>/ 아래에 쓴다.

    반환: (성공 목록, 미상장 목록, 실패 [(티커, 오류)] 목록)
    """
    ok, no_data, failed = [], [], []
    streak = 0
    total = len(tickers)
    for i, t in enumerate(tickers):
        url = CBOE_URL.format(sym=urllib.parse.quote(get_api_symbol(t)))
        try:
            rows = rows_from(fetch_json(url, max_retries=max_retries), t)
            if not rows:
                no_data.append(t)
            else:
                _write_ticker_csv(root_dir, date, t, rows)
                ok.append(t)
            streak = 0
        except NoDataError:
            no_data.append(t)  # 옵션 미상장(404/403)은 정상 분류
            streak = 0
        except EgressBlockedError:
            raise  # 환경 차단은 티커별로 계속하지 않고 전체 즉시 중단
        except Exception as e:
            failed.append((t, str(e)[:120]))
            streak += 1
            logger.warning(f"실패 {t}: {e}")
            if streak >= FAIL_STREAK_LIMIT:
                logger.warning(f"연속 실패 {streak}회 -> {FAIL_COOLDOWN_SEC}초 대기")
                time.sleep(FAIL_COOLDOWN_SEC)
                streak = 0
        # 계통 장애 조기 중단: 초반부터 성공이 하나도 없이 실패/빈응답만 쌓이면 멈춘다.
        if not ok and (len(failed) + len(no_data)) >= ABORT_NO_OK_AFTER:
            raise SystemicFailureError(
                f"{len(failed) + len(no_data)}개 시도 동안 성공 0 -> 중단 "
                f"(fail {len(failed)}, no_data {len(no_data)})")
        if sleep:
            time.sleep(sleep)
        if (i + 1) % 100 == 0:
            print(f"  ... {i + 1}/{total} (ok {len(ok)})", flush=True)
    return ok, no_data, failed


def _write_ticker_csv(root_dir, date, ticker, rows):
    """티커 CSV를 원자적으로 쓴다(.tmp -> replace). 쓰다 실패하면 잘린 파일을 남기지
    않으므로 아카이브에 반쪽 CSV가 섞이지 않는다."""
    tdir = os.path.join(root_dir, f"quotes_{date}", ticker)
    os.makedirs(tdir, exist_ok=True)
    fpath = os.path.join(tdir, f"{ticker.lower()}_quotes_{date}.csv")
    tmp = fpath + ".tmp"
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            w.writeheader()
            w.writerows(rows)
        os.replace(tmp, fpath)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def make_archive(src_root, dest):
    """src_root 폴더를 dest(.tar.gz)로 원자적으로 압축한다(.tmp -> replace)."""
    tmp = dest + ".tmp"
    try:
        with tarfile.open(tmp, "w:gz") as tar:
            tar.add(src_root, arcname=os.path.basename(src_root))
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def main(argv=None):
    # Windows 콘솔(cp949)에서 한글 출력이 깨지지 않도록 UTF-8로 재설정
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="CBOE 옵션 일일 스냅샷 수집기")
    ap.add_argument("--force", action="store_true",
                    help="이미 아카이브가 있어도 다시 수집")
    ap.add_argument("--tickers", nargs="+", default=None,
                    help="지정 티커만 수집(테스트용)")
    ap.add_argument("--sp500-only", action="store_true",
                    help="지수·ETF를 빼고 S&P500만 수집")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    ap.add_argument("--sleep", type=float, default=0.3,
                    help="요청 간 간격(초), CBOE 예의용")
    ap.add_argument("--max-retries", type=int, default=2,
                    help="요청당 재시도 횟수(0이어도 최초 1회는 시도)")
    ap.add_argument("--min-ok-ratio", type=float, default=0.9,
                    help="성공/전체 가 이 값 미만이면 아카이브 생성 안 함")
    args = ap.parse_args(argv)

    date = completed_session_date()
    os.makedirs(args.out_dir, exist_ok=True)
    archive = os.path.join(args.out_dir, f"quotes_{date}.tar.gz")
    if os.path.exists(archive) and not args.force:
        print(f"exists: {archive} - nothing to do")
        return 0

    if args.tickers:
        tickers, source = [normalize_ticker(t) for t in args.tickers], "manual"
    else:
        tickers, source = build_universe(sp500_only=args.sp500_only)
    print(f"수집 시작: {len(tickers)}개 티커 (S&P500 출처: {source}), 날짜 {date}")

    tmp_root = tempfile.mkdtemp(prefix="cboe_")
    try:
        try:
            ok, no_data, failed = collect(tickers, date, tmp_root,
                                          sleep=args.sleep, max_retries=args.max_retries)
        except (EgressBlockedError, SystemicFailureError) as e:
            print(f"ERROR: {e}")
            return 1
        # 순수 성공 비율만 센다. no_data(미상장)는 소수여야 정상이며, 만약
        # 차단·장애로 대량 발생하면 이 비율이 무너져 아카이브가 만들어지지 않는다.
        ok_ratio = len(ok) / len(tickers) if tickers else 0
        if ok_ratio < args.min_ok_ratio or not ok:
            print(f"ERROR: 성공률 미달({ok_ratio:.0%} < {args.min_ok_ratio:.0%}) - "
                  f"아카이브를 만들지 않음. ok {len(ok)}, no_data {len(no_data)}, "
                  f"fail {len(failed)}")
            for t, err in failed[:10]:
                print(f"  fail {t}: {err}")
            return 1
        make_archive(os.path.join(tmp_root, f"quotes_{date}"), archive)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    size_mb = os.path.getsize(archive) / 1e6
    print(f"OK: {archive} ({size_mb:.1f} MB) - ok {len(ok)}, "
          f"no_data {len(no_data)}, fail {len(failed)}, date {date}")
    if no_data:
        print(f"  옵션 미상장/빈 응답: {', '.join(no_data)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
