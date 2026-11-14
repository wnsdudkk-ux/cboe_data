#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gex.py - 보관본의 SPX 옵션 호가·미결제약정으로 딜러 감마 노출(GEX)을 계산하고 그림 두 장을 만든다.

그림 1(--png)         그날 마감 기준: 가격 차트, 행사가별 GEX, 가격별 GEX 프로필을 가로로 나란히 놓고 세로축(지수 수준)을 함께 쓴다.
                      콜 벽·감마 플립·종가·풋 벽은 세 그림에 걸쳐 긋는다(가로 2160픽셀)
그림 2(--png-history) 추이: S&P 500 종가와 감마 플립, 순 GEX(콜·풋 합계 대비 %)

예:   python3 gex.py --archive-dir data/daily --history-csv data/daily/gex_history.csv --closes-csv data/daily/index_closes.csv \
                     --png gex.png --png-history gex_history.png
      python3 gex.py --archive-dir data/daily --date 20261113 --png gex.png --out-json gex.json
필요: pandas, numpy (선택: matplotlib 그림, yfinance 종가·일봉, exchange_calendars 조기 폐장일)

추이 표(data/daily/gex_history.csv)는 수집기(collect.py)가 수집을 마칠 때마다 이 스크립트를 띄워 그날 줄을 넣는다
(--save-history --require-close --no-yf). 지수 종가는 야후 파이낸스에서 받고, 받지 못하면 수집기가 Cboe 파일에서 적어 둔
종가 표(--closes-csv, data/daily/index_closes.csv)를 쓴다.

계산 기준
- GEX(계약 하나) = 감마 x 미결제약정 x 100 x 지수^2 x 1%. 지수가 1% 움직일 때 옵션을 받아 준 쪽(딜러)이 위험을 맞추려고
  사고팔아야 하는 금액(달러)이다. 양수면 딜러가 오를 때 팔고 내릴 때 사서 움직임을 줄이고, 음수면 반대로 움직임을 키운다.
- 부호: 딜러가 콜은 사 두고 풋은 팔아 둔 상태라는 관행적 가정을 쓴다(콜 +, 풋 -). 실제 딜러 포지션은 공개되지 않는다.
- 감마: 만기마다 콜·풋 가격 차로 선도가를 구하고, 외가격 쪽 호가 중간값에서 IV를 역산해 Black-76으로 다시 계산한다.
  같은 만기·행사가의 콜과 풋은 감마가 같다. 파일에 든 Cboe 감마로 낸 순 GEX도 함께 적는다(net_cboe).
- 가격별 프로필: 지수가 다른 값이었다면 순 GEX가 얼마였을지. 계약마다 IV는 그대로 두고(행사가 고정) 지수만 바꿔 감마를 다시 계산한다.
- 기준 수준: 옵션 호가는 SPX 옵션 마감(미 동부 16:15) 값이라 그 시점의 지수 수준(ref. 가장 가까운 만기의 선도가에서 보유 비용을
  뺀 값)이 공식 종가(16:00)와 다를 수 있다(86일 중앙값 0.05%, 실적 발표가 장 마감 뒤에 몰린 날은 0.2~0.5%). 프로필의 가로축은
  실제 지수 수준으로 맞추고(가상의 지수 X에서 각 만기의 선도가 = 선도가 x X / ref), 순 GEX·행사가별 GEX·콜 벽·풋 벽은
  공식 종가 수준에서 읽는다. 이렇게 해야 '종가가 감마 플립 아래'와 '순 GEX가 음수'가 같은 뜻이 되고, 수집 시각이 어긋난 날에도 값이 덜 흔들린다.
  호가 시점 그대로의 값(net_quote)도 함께 적는다.
- 감마 플립: 프로필이 0을 지나는 지수 수준. 여러 곳이면 종가에 가장 가까운 것. 종가 ±20% 안에서 찾고, 없으면 비워 둔다.
- 콜 벽·풋 벽: 종가 위쪽에서 콜 GEX가 가장 큰 행사가, 아래쪽에서 풋 GEX가 가장 큰 행사가(전체 만기, 잔존 30일 이내 두 가지).
- 미결제약정은 파일에 든 값(전 영업일 마감 기준)이다. 당일 만기 계약은 뺀다(마감 시점에는 이미 끝났다).
- ilovevol.app의 공개 수치와 대조(2026-11-13, 십억 달러): 종가 수준의 순 GEX -39.5(그 사이트 프로필 그림 약 -39), 감마 플립 7,618(7,617 근처),
  호가 시점의 값 -49.5(추이 화면 -49.3), Cboe 감마로 낸 값 -54.2(일일 리뷰 -54.2), 잔존 30일 이내 콜 벽·풋 벽 7,700·7,500(같음).
"""
import argparse
import json
import math
import os
import sys

import numpy as np
import pandas as pd

from option_sentiment import AM_ROOTS, R, implied_vol, list_dates, load_day
from vix_decomp import index_closes, quote_time

M365 = 525600.0                # 365일의 분 수
SQ2PI = math.sqrt(2 * math.pi)
SPAN = 0.20                    # 프로필과 감마 플립을 계산하는 범위(현재 지수의 ±20%)
STEP = 0.0025                  # 프로필 격자 간격(지수의 0.25%)
BUCKET = 25.0                  # 행사가별 그림에서 한 막대로 묶는 행사가 폭
HIST_COLS = ["date", "spot", "ref", "net_bn", "net7_bn", "net30_bn", "gross_bn", "pct", "flip", "call_wall", "put_wall",
             "call_wall30", "put_wall30", "net_quote_bn", "net_cboe_bn"]
NAN = float("nan")


# ----------------------------------------------------------------------------- 만기별 모형
def expiry_model(g, exp, root, tv):
    """한 만기(루트)의 선도가 F, 잔존 기간 T(년), 행사가별 IV 곡선. tv = 호가 시점. 만들 수 없으면 None."""
    h, m = (9, 30) if root in AM_ROOTS else (16, 0)
    minutes = (exp.to_pydatetime().replace(hour=h, minute=m) - tv).total_seconds() / 60
    if minutes <= 0:
        return None
    T = minutes / M365
    c = g[g["cp"] == "C"].drop_duplicates("K").set_index("K").sort_index()
    p = g[g["cp"] == "P"].drop_duplicates("K").set_index("K").sort_index()
    kk = c.index.intersection(p.index)
    if len(kk) < 3:
        return None
    cm, pm = c.loc[kk, "mid"].values.astype(float), p.loc[kk, "mid"].values.astype(float)   # mid는 양쪽 호가가 있을 때만 값이 있다
    both = ~np.isnan(cm) & ~np.isnan(pm)
    if both.sum() < 2:
        return None
    i = int(np.argmin(np.where(both, np.abs(cm - pm), np.inf)))                           # 콜·풋 가격 차가 가장 작은 행사가
    F = float(kk[i] + math.exp(R * T) * (cm[i] - pm[i]))
    if not F > 0:
        return None

    def side(s, sgn, keep):
        s = s[keep(s.index.values.astype(float))]
        ok = ((s["ask"] > 0) & (s["ask"] >= s["bid"])).values                              # 매수호가가 0이어도 매도호가가 있으면 쓴다
        k = s.index.values.astype(float)[ok]
        px = ((s["bid"] + s["ask"]) / 2).values.astype(float)[ok]
        return k, (implied_vol(F, k, T, px, np.full(len(k), sgn)) if len(k) else k)

    kp, vp = side(p, -1.0, lambda x: x < F)                                                # F 아래는 풋, 위는 콜(외가격 쪽)
    kc, vc = side(c, 1.0, lambda x: x >= F)
    k, v = np.concatenate([kp, kc]), np.concatenate([vp, vc])
    ok = ~np.isnan(v)
    if ok.sum() < 2:
        return None
    return {"F": F, "T": T, "k": k[ok], "v": v[ok]}


def build(df, date):
    """잔존 1일 이상인 계약을 (만기, 루트, 행사가)별 한 줄로 모으고 선도가 F, 잔존 기간 T, IV(sig)를 붙인다.
    돌려주는 것: (표, 모형을 만들지 못해 뺀 계약의 미결제약정 비중)."""
    tv = quote_time(date)
    live = df[df["dte"] >= 1]
    if live.empty:
        return None, NAN
    key = ["exp", "root", "K"]
    c, p = (live[live["cp"] == s].drop_duplicates(key).set_index(key) for s in ("C", "P"))
    t = pd.DataFrame({"oi_c": c["oi"], "oi_p": p["oi"], "cb_c": c["gamma"] * c["oi"], "cb_p": p["gamma"] * p["oi"]}).fillna(0.0).reset_index()
    t["dte"] = (t["exp"] - pd.Timestamp(date)).dt.days
    t["F"], t["T"], t["sig"] = NAN, NAN, NAN
    for (exp, root), idx in t.groupby(["exp", "root"], sort=False).groups.items():
        m = expiry_model(live[(live["exp"] == exp) & (live["root"] == root)], exp, root, tv)
        if m is not None:
            t.loc[idx, "F"], t.loc[idx, "T"] = m["F"], m["T"]
            t.loc[idx, "sig"] = np.interp(t.loc[idx, "K"].values.astype(float), m["k"], m["v"])   # 호가가 없는 행사가는 가장 가까운 값
    oi = t["oi_c"] + t["oi_p"]
    ok = t["sig"].notna()
    miss = float(oi[~ok].sum() / oi.sum()) if oi.sum() > 0 else NAN
    return t[ok].reset_index(drop=True), miss


class Book:
    """build()의 표를 계산용 배열로 바꿔 둔 것. unit(m) = 지수가 m배일 때 행사가 하나의 계약 1개가 내는 GEX(달러, 1% 움직임당)."""

    def __init__(self, t):
        self.t = t
        F, K, T, sig = (t[c].values.astype(float) for c in ("F", "K", "T", "sig"))
        self.sd = np.maximum(sig * np.sqrt(T), 1e-9)
        self.x = np.log(F / K) / self.sd + 0.5 * self.sd                    # m = 1일 때의 d1
        self.a = np.exp(-R * T) * F / (self.sd * SQ2PI)                     # 감마 x 지수^2 = a x m x exp(-d1^2 / 2)
        self.w = (t["oi_c"] - t["oi_p"]).values.astype(float)               # 콜 +, 풋 -
        self.dte = t["dte"].values

    def unit(self, m=1.0):
        d1 = self.x + math.log(m) / self.sd
        return self.a * m * np.exp(-0.5 * d1 * d1)                          # x 100(승수) x 1% = x 1

    def net(self, m=1.0, sel=None):
        u = self.unit(m) * self.w
        return float(u.sum() if sel is None else u[sel].sum())

    def profile(self, ms, sel=None):
        return np.array([self.net(m, sel) for m in ms])

    def flips(self, ms, sel=None):
        """프로필이 0을 지나는 곳(호가 시점 지수 수준의 배수)들과 격자 위의 프로필. 부호가 바뀌는 구간을 찾아 이분법으로 좁힌다."""
        y = self.profile(ms, sel)
        out = []
        for i in np.flatnonzero(np.sign(y[:-1]) * np.sign(y[1:]) < 0):
            lo, hi, flo = ms[i], ms[i + 1], y[i]
            for _ in range(40):
                mid = (lo + hi) / 2
                fm = self.net(mid, sel)
                if flo * fm <= 0:
                    hi = mid
                else:
                    lo, flo = mid, fm
            out.append((lo + hi) / 2)
        return out, y


def ref_level(t):
    """옵션 호가에 담긴 지수 수준: 가장 가까운 만기의 선도가에서 보유 비용(20~70일 만기의 선도가로 잰 연율)을 뺀 값."""
    e = t.groupby(["exp", "root"]).agg(F=("F", "first"), T=("T", "first")).sort_values("T")
    n = e.iloc[0]
    far = e[(e["T"] > 20 / 365) & (e["T"] < 70 / 365)]
    c = float(np.median(np.log(far["F"] / n["F"]) / (far["T"] - n["T"]))) if len(far) else 0.0
    return float(n["F"] * math.exp(-c * n["T"]))


def nearest(fl, x):
    return min(fl, key=lambda v: abs(v - x)) if fl else NAN


def walls(t, u, spot):
    """(콜 벽, 풋 벽) = 현재 지수 위쪽에서 콜 GEX가 가장 큰 행사가, 아래쪽에서 풋 GEX가 가장 큰 행사가.
    내가격 쪽(지수 아래의 콜, 위의 풋)은 뺀다. 같은 행사가에 콜·풋을 함께 쌓는 거래(박스 스프레드 등)의 미결제약정이 벽으로 잡히지 않게 하려는 것이다."""
    out = []
    for col, keep in (("oi_c", t["K"].values >= spot), ("oi_p", t["K"].values <= spot)):
        g = pd.Series((u * t[col].values)[keep]).groupby(t["K"].values[keep]).sum()
        out.append(float(g.idxmax()) if len(g) and g.max() > 0 else NAN)
    return tuple(out)


# ----------------------------------------------------------------------------- 하루치
def day(df, date, spot=NAN):
    """하루치 SPX 호가로 순 GEX, 행사가별 GEX, 가격별 프로필, 감마 플립, 콜 벽·풋 벽을 계산한다. spot = 공식 종가(없으면 호가에 담긴
    지수 수준을 쓴다). 못 하면 {"error": 이유}."""
    t, miss = build(df, date)
    if t is None or not len(t):
        return {"date": date, "error": "잔존 1일 이상인 SPX 옵션의 호가가 없음"}
    b = Book(t)
    ref = ref_level(t)
    if not (spot == spot and spot > 0):
        spot = ref
    mc = spot / ref                                                          # 종가 수준 = 호가 시점 수준의 mc배
    u = b.unit(mc)
    gc, gp = u * t["oi_c"].values, u * t["oi_p"].values
    w7, w30 = b.dte <= 7, b.dte <= 30
    net, gross = float((gc - gp).sum()), float((gc + gp).sum())
    grid = np.arange(1 - SPAN, 1 + SPAN + STEP / 2, STEP)                   # 종가의 배수
    fl, y = b.flips(grid * mc)
    fl30, y30 = b.flips(grid * mc, w30)
    ks = pd.DataFrame({"K": t["K"].values, "c": gc, "p": gp})
    ks = ks[(ks["K"] >= spot * (1 - SPAN)) & (ks["K"] <= spot * (1 + SPAN))].groupby("K").sum()
    lo, hi = int(np.argmin(y)), int(np.argmax(y))
    return {
        "date": date, "spot": float(spot), "ref": ref, "net": net, "net7": float((gc - gp)[w7].sum()), "net30": float((gc - gp)[w30].sum()),
        "call": float(gc.sum()), "put": float(gp.sum()), "gross": gross, "pct": 100 * net / gross if gross > 0 else NAN,
        "net_quote": b.net(1.0), "net_cboe": float(spot * spot * (t["cb_c"] - t["cb_p"]).sum()),
        "flip": nearest([ref * m for m in fl], spot), "flips": [ref * m for m in fl], "flip30": nearest([ref * m for m in fl30], spot),
        "walls": dict(zip(("call", "put"), walls(t, u, spot))), "walls30": dict(zip(("call", "put"), walls(t[w30], u[w30], spot))),
        "walls7": dict(zip(("call", "put"), walls(t[w7], u[w7], spot))),
        "strikes": {"K": ks.index.values.astype(float), "call": ks["c"].values, "put": ks["p"].values},
        "profile": {"price": spot * grid, "net": y, "net30": y30},
        "low": {"price": float(spot * grid[lo]), "net": float(y[lo])}, "high": {"price": float(spot * grid[hi]), "net": float(y[hi])},
        "n": int(len(t)), "n_exp": int(t[["exp", "root"]].drop_duplicates().shape[0]), "miss": miss}


def hist_row(r):
    """day() 결과에서 추이 표 한 줄(금액은 십억 달러)."""
    if "error" in r:
        return None
    return {"date": r["date"], "spot": round(r["spot"], 2), "ref": round(r["ref"], 2), "net_bn": round(r["net"] / 1e9, 3),
            "net7_bn": round(r["net7"] / 1e9, 3), "net30_bn": round(r["net30"] / 1e9, 3), "gross_bn": round(r["gross"] / 1e9, 2),
            "pct": round(r["pct"], 3), "flip": round(r["flip"], 1) if r["flip"] == r["flip"] else NAN,
            "call_wall": r["walls"]["call"], "put_wall": r["walls"]["put"], "call_wall30": r["walls30"]["call"], "put_wall30": r["walls30"]["put"],
            "net_quote_bn": round(r["net_quote"] / 1e9, 3), "net_cboe_bn": round(r["net_cboe"] / 1e9, 3)}


def read_history(path):
    """추이 표(CSV)를 읽는다. 없거나 읽지 못하면 빈 표."""
    try:
        h = pd.read_csv(path, dtype={"date": str})
        return h[[c for c in HIST_COLS if c in h.columns]].drop_duplicates("date").sort_values("date").reset_index(drop=True)
    except Exception:
        return pd.DataFrame(columns=HIST_COLS)


def merge_history(old, rows):
    """새로 계산한 줄을 기존 표에 합친다(같은 날짜는 새 값으로 바꾼다)."""
    new = pd.DataFrame([r for r in rows if r], columns=HIST_COLS)
    if not len(new):
        return old
    keep = old[~old["date"].isin(new["date"])] if len(old) else old
    frames = [f for f in (keep, new) if len(f)]
    return pd.concat(frames, ignore_index=True).sort_values("date").reset_index(drop=True)


def save_history(hist, path):
    """추이 표를 임시 파일에 쓴 뒤 바꿔 넣는다(쓰다 멈춰도 반쪽짜리 표가 남지 않는다)."""
    tmp = path + ".tmp"
    try:
        hist.to_csv(tmp, index=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ----------------------------------------------------------------------------- 지수 종가·일봉
def read_closes(path):
    """수집기가 적어 둔 지수 종가 표(date, spx, ...)에서 {날짜: S&P 500 종가}. 없거나 읽지 못하면 빈 값."""
    try:
        c = pd.read_csv(path, dtype={"date": str})
        v = pd.to_numeric(c["spx"], errors="coerce")
        return {d: float(x) for d, x in zip(c["date"], v) if x == x and x > 0}
    except Exception:
        return {}


def pick_close(yahoo, saved):
    """쓸 종가와 두 출처가 어긋난 정도(%). 야후 값을 먼저 쓰고 없으면 수집기가 적어 둔 값을 쓴다. 둘 다 없으면 (nan, nan)."""
    a = round(float(yahoo), 2) if yahoo is not None and yahoo == yahoo and yahoo > 0 else NAN   # 야후 값은 float32라 끝자리가 흔들린다
    b = round(float(saved), 2) if saved is not None and saved == saved and saved > 0 else NAN
    if a == a:
        return a, (100 * (b / a - 1) if b == b else NAN)
    return b, NAN


def ohlc(end, n=320):
    """야후 파이낸스의 S&P 500 일봉(end까지 최대 n개). 받지 못하면 None(가격 차트만 빠진다)."""
    try:
        import yfinance as yf
        e = pd.Timestamp(end)
        px = yf.download("^GSPC", start=e - pd.Timedelta(days=int(n * 1.6) + 10), end=e + pd.Timedelta(days=1), auto_adjust=False, progress=False)
        if isinstance(px.columns, pd.MultiIndex):
            px.columns = px.columns.get_level_values(0)
        px.index = pd.to_datetime(px.index).tz_localize(None).normalize()
        px = px[["Open", "High", "Low", "Close"]].dropna()
        px = px[px.index <= e].tail(n)
        if len(px) < 30:
            return None
        return {"date": [d.strftime("%Y%m%d") for d in px.index], "o": px["Open"].tolist(), "h": px["High"].tolist(),
                "l": px["Low"].tolist(), "c": px["Close"].tolist()}
    except Exception:
        return None


# ----------------------------------------------------------------------------- 정리: 마지막 날 + 추이
def summary(r, hist, bars=None):
    """마지막 날의 계산(day() 결과)과 추이 표를 보고서·그림·JSON에 쓸 한 덩어리로 묶는다."""
    if "error" in r:
        return {"date": r.get("date"), "error": r["error"]}
    h = hist[hist["date"] <= r["date"]].reset_index(drop=True)
    out = {k: r[k] for k in ("date", "spot", "ref", "net", "net7", "net30", "call", "put", "gross", "pct", "net_quote", "net_cboe", "flip",
                             "flip30", "walls", "walls30", "walls7", "low", "high", "n", "n_exp", "miss")}
    out["strikes"], out["profile"] = r["strikes"], r["profile"]
    out["history"] = {c: h[c].tolist() for c in ("date", "spot", "net_bn", "gross_bn", "pct", "flip") if c in h}
    flags = []
    if len(h) >= 2 and h["date"].iloc[-1] == r["date"]:
        p = h.iloc[-2]
        out["prev"] = {"date": p["date"], "spot": float(p["spot"]), "net": float(p["net_bn"]) * 1e9, "flip": float(p["flip"])}
    if len(h) >= 5:
        net = h["net_bn"].values.astype(float)
        sign = np.sign(net)
        run = 1
        while run < len(sign) and sign[-1 - run] == sign[-1]:
            run += 1
        out["stats"] = {"days": int(len(h)), "neg_days": int((net < 0).sum()), "run": int(run), "run_sign": int(sign[-1]),
                        "rank_low": int((net < net[-1]).sum()) + 1, "median": float(np.median(net)), "first": h["date"].iloc[0]}
    if bars and bars["date"] and bars["date"][-1] == r["date"]:
        out["ohlc"] = bars
        if len(bars["c"]) >= 2:
            out["chg_pct"] = 100 * (bars["c"][-1] / bars["c"][-2] - 1)
    elif "prev" in out:
        out["chg_pct"] = 100 * (r["spot"] / out["prev"]["spot"] - 1)
    if r["miss"] == r["miss"] and r["miss"] > 0.03:
        flags.append(f"호가가 부족해 계산에서 뺀 만기의 미결제약정 비중 {r['miss']:.1%}")
    if abs(r["ref"] / r["spot"] - 1) > 0.003:
        flags.append(f"옵션 호가에 담긴 지수 수준({r['ref']:,.1f})이 종가({r['spot']:,.2f})와 {100 * (r['ref'] / r['spot'] - 1):+.2f}% 다름. "
                     "장 마감 뒤 15분 사이에 시장이 움직였거나 호가 파일이 마감 시점 것이 아닐 수 있음")
    if not r["flip"] == r["flip"]:
        flags.append("종가 ±20% 안에 감마 플립이 없음(프로필이 한쪽 부호뿐임)")
    out["flags"] = flags
    return out


def clean(o):
    """JSON으로 저장할 수 있게 numpy 값과 NaN을 바꾼다."""
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, np.ndarray):
        return clean(o.tolist())
    if isinstance(o, (np.floating, float)):
        return None if o != o else float(o)
    if isinstance(o, np.integer):
        return int(o)
    return o


# ----------------------------------------------------------------------------- 글 보고
def _ymd(d):
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


def _bn(x, n=1):
    return f"{x / 1e9:+,.{n}f}"


def _lv(x):
    return "-" if x is None or x != x else f"{x:,.0f}"


def report(res):
    L = [f"\n== SPX 감마 노출(GEX) | {res.get('date', '-')}"]
    if "error" in res:
        return "\n".join(L + [f"  [확인 필요] {res['error']}"])
    p, st = res.get("prev"), res.get("stats")
    L.append(f"  [순 GEX] {_bn(res['net'])}bn(지수 1% 움직임당, 종가 {res['spot']:,.2f} 수준)" + (f" | 전일 {_bn(p['net'])}bn" if p else "")
             + f" | 콜 {res['call'] / 1e9:,.1f}bn, 풋 {res['put'] / 1e9:,.1f}bn, 합 {res['gross'] / 1e9:,.1f}bn 대비 {res['pct']:+.1f}%")
    L.append(f"    만기별: 잔존 7일 이내 {_bn(res['net7'])} | 8~30일 {_bn(res['net30'] - res['net7'])} | 30일 초과 {_bn(res['net'] - res['net30'])}")
    f = res["flip"]
    if f is not None and f == f:
        d = f - res["spot"]
        L.append(f"  [감마 플립] {f:,.0f} | 종가가 플립보다 {abs(d):,.0f}({abs(d) / res['spot']:.2%}) {'아래 → 음의 감마 구간' if d > 0 else '위 → 양의 감마 구간'}"
                 + (f" | 전일 플립 {p['flip']:,.0f}" if p and p["flip"] == p["flip"] else "") + f" | 잔존 30일 이내만 보면 {_lv(res['flip30'])}")
    else:
        L.append(f"  [감마 플립] 종가 ±20% 안에 없음(순 GEX가 전 구간에서 {'양수' if res['net'] > 0 else '음수'})")
    L.append(f"  [콜 벽·풋 벽] 잔존 30일 이내 {_lv(res['walls30']['call'])} / {_lv(res['walls30']['put'])} | 전체 만기 {_lv(res['walls']['call'])} / {_lv(res['walls']['put'])}"
             f" | 잔존 7일 이내 {_lv(res['walls7']['call'])} / {_lv(res['walls7']['put'])}")
    L.append(f"  [가격별 프로필] 지수가 {res['low']['price']:,.0f}이면 {_bn(res['low']['net'])}bn(가장 낮음), {res['high']['price']:,.0f}이면 {_bn(res['high']['net'])}bn(가장 높음)")
    if st:
        L.append(f"  [추이 {_ymd(st['first'])}~, {st['days']}일] 순 GEX가 음수인 날 {st['neg_days']}일({st['neg_days'] / st['days']:.0%})"
                 f" | 오늘 값은 낮은 쪽에서 {st['rank_low']}번째 | {'음' if st['run_sign'] < 0 else '양'}의 감마 {st['run']}일째 | 중앙값 {st['median']:+.1f}bn")
    L.append(f"  [계산 기준에 따른 차이] 옵션 마감 호가 시점 그대로(지수 환산 {res['ref']:,.1f}) {_bn(res['net_quote'])}bn | 파일의 Cboe 감마로 계산 {_bn(res['net_cboe'])}bn")
    L.append(f"  [가정] 딜러가 콜은 사 두고 풋은 팔아 둔 상태로 본다. 미결제약정은 전 영업일 마감 기준. 계약 {res['n']:,}줄, 만기 {res['n_exp']}개")
    for x in res.get("flags", []):
        L.append(f"  [확인 필요] {x}")
    return "\n".join(L)


# ----------------------------------------------------------------------------- 그림
C_POS, C_NEG = "#2a78d6", "#e34948"                                   # 양의 감마, 음의 감마(색각 이상 구분 검사를 통과한 파랑·빨강 쌍)
C_MA = (("sma60", 60, "#eda100"),)                                    # 가격 차트의 이동평균선(이름, 일수, 색). 파랑·빨강 수준선과의 구분 검사를 통과한 노랑
DPI = 150
GT = {
    "ko": {
        "title": "SPX 감마 노출(GEX)", "sub": "{d} 마감 기준 · 미결제약정은 전 영업일 값", "spx": "S&P 500 종가", "chg": "전일 대비 {v}%",
        "net": "순 GEX (지수 1% 움직임당)", "net_s": "전일 {p} · 콜·풋 합계의 {q}%", "net_s0": "콜·풋 합계의 {q}%",
        "flip": "감마 플립", "below": "종가가 {d}({p}%) 아래 · 음의 감마 구간", "above": "종가가 {d}({p}%) 위 · 양의 감마 구간", "noflip": "종가 ±20% 안에 없음",
        "walls": "콜 벽 · 풋 벽 (잔존 30일 이내)", "walls_s": "전체 만기로는 {c} · {p}",
        "byexp": "만기별 순 GEX(십억 달러)", "e7": "7일 이내", "e30": "8~30일", "e99": "30일 초과",
        "price": "S&P 500 일봉", "price0": "S&P 500 종가", "noprice": "가격 자료를 받지 못함",
        "ma": {"sma60": "60일 이동평균"},
        "pos_s": "양의 감마: 딜러의 매매가 움직임을 줄이는 쪽", "neg_s": "음의 감마: 움직임을 키우는 쪽",
        "cw": "콜 벽", "pw": "풋 벽", "fl": "감마 플립", "fl_s": "플립", "cl": "종가", "at": "종가 수준",
        "strike": "행사가별 GEX", "strike_s": "{b}씩 묶음 · 전체 만기 · 십억 달러", "prof": "가격별 GEX 프로필", "prof_s": "지수가 그 값일 때의 순 GEX · 십억 달러",
        "unit": "십억 달러", "pos": "양의 감마: 딜러의 매매가 움직임을 줄이는 쪽", "neg": "음의 감마: 딜러의 매매가 움직임을 키우는 쪽",
        "foot": ["감마 플립: 순 GEX의 부호가 바뀌는 지수 수준 · 콜 벽·풋 벽: 종가 위·아래에서 콜·풋 GEX가 가장 큰 행사가",
                 "자료: Cboe 지연 시세(SPX 옵션 마감 호가). 미결제약정은 전 영업일 마감 기준.",
                 "가정: 딜러가 콜은 사 두고 풋은 팔아 둔 상태로 본다. 실제 포지션은 공개되지 않는다.",
                 "계산 기준을 바꾸면(십억 달러): 옵션 마감 호가 시점 그대로 {a}, 파일의 Cboe 감마로는 {b}"],
        "h_title": "SPX GEX 추이", "h_sub": "{a} ~ {b} · {n}거래일 · 전체 만기", "h_neg": "순 GEX가 음수인 날", "h_neg_v": "{k}일 ({p}%)",
        "h_neg_s": "지금은 {s}의 감마 {r}일째 · 오늘 값은 낮은 쪽에서 {k}번째", "sign": {1: "양", -1: "음", 0: "0"},
        "h_p1": "S&P 500 종가와 감마 플립", "h_p2": "순 GEX (콜·풋 합계 대비 %)", "h_shade": "순 GEX가 음수인 날",
        "h_foot": ["순 GEX: 지수가 1% 움직일 때 딜러가 위험을 맞추려고 사고팔아야 하는 금액. 음수면 움직임을 키우는 쪽이다.",
                   "감마 플립: 순 GEX의 부호가 바뀌는 지수 수준. 종가가 그 아래면 순 GEX가 음수다.",
                   "자료: Cboe 지연 시세(SPX 옵션 마감 호가). 미결제약정은 전 영업일 마감 기준.",
                   "가정: 딜러가 콜은 사 두고 풋은 팔아 둔 상태로 본다. 실제 포지션은 공개되지 않는다."],
        "month": "{m}월"},
    "en": {
        "title": "SPX gamma exposure (GEX)", "sub": "As of the {d} close · open interest is the prior day's", "spx": "S&P 500 close", "chg": "{v}% vs prior day",
        "net": "Net GEX (per 1% move)", "net_s": "prior {p} · {q}% of calls + puts", "net_s0": "{q}% of calls + puts",
        "flip": "Gamma flip", "below": "close is {d} ({p}%) below · negative gamma", "above": "close is {d} ({p}%) above · positive gamma", "noflip": "none within ±20%",
        "walls": "Call wall · put wall (within 30 days)", "walls_s": "all expiries: {c} · {p}",
        "byexp": "Net GEX by expiry ($bn)", "e7": "≤7 days", "e30": "8–30 days", "e99": ">30 days",
        "price": "S&P 500 daily", "price0": "S&P 500 close", "noprice": "No price data",
        "ma": {"sma60": "SMA 60"},
        "pos_s": "Positive gamma: dealer hedging dampens moves", "neg_s": "Negative gamma: amplifies moves",
        "cw": "Call wall", "pw": "Put wall", "fl": "Gamma flip", "fl_s": "Flip", "cl": "Close", "at": "At close",
        "strike": "GEX by strike", "strike_s": "bucket {b} · all expiries · $bn", "prof": "GEX profile by price", "prof_s": "net GEX if the index were there · $bn",
        "unit": "$bn", "pos": "Positive gamma: dealer hedging dampens moves", "neg": "Negative gamma: dealer hedging amplifies moves",
        "foot": ["Gamma flip: index level where net GEX changes sign · Walls: strikes with the largest call / put GEX above / below the close",
                 "Source: Cboe delayed quotes (SPX option closing quotes). Open interest is as of the prior close.",
                 "Assumption: dealers are long calls and short puts. Actual positioning is not public.",
                 "Other bases ($bn): at the option-close quote state {a}, with the file's Cboe gamma {b}"],
        "h_title": "SPX GEX history", "h_sub": "{a} to {b} · {n} sessions · all expiries", "h_neg": "Days with negative net GEX", "h_neg_v": "{k} ({p}%)",
        "h_neg_s": "{s} gamma for {r} sessions · today ranks {k} from the bottom", "sign": {1: "positive", -1: "negative", 0: "zero"},
        "h_p1": "S&P 500 close and gamma flip", "h_p2": "Net GEX (% of calls + puts)", "h_shade": "Net GEX below zero",
        "h_foot": ["Net GEX: dollars dealers must trade to stay hedged per 1% index move. Negative means hedging amplifies moves.",
                   "Gamma flip: index level where net GEX changes sign. A close below it means negative net GEX.",
                   "Source: Cboe delayed quotes (SPX option closing quotes). Open interest is as of the prior close.",
                   "Assumption: dealers are long calls and short puts. Actual positioning is not public."],
        "month": "{m}"}}
MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


class _Canvas:
    """가로 W픽셀짜리 그림판. 좌표는 왼쪽 위가 (0, 0)인 픽셀이다."""

    def __init__(self, W, H):
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
        from vix_decomp import SURF, _font
        self.W, self.H, self.font = W, H, _font()
        self.L = GT["ko" if self.font else "en"]
        self.fam, self.pt = self.font or "DejaVu Sans", 72 / DPI
        self.fig = Figure(figsize=(W / DPI, H / DPI), dpi=DPI, facecolor=SURF)
        FigureCanvasAgg(self.fig)
        self.bg, self.fg = self._layer(0), self._layer(10)

    def _layer(self, z):
        ax = self.fig.add_axes([0, 0, 1, 1], facecolor="none")
        ax.set_xlim(0, self.W)
        ax.set_ylim(self.H, 0)
        ax.axis("off")
        ax.set_zorder(z)
        return ax

    def text(self, x, y, s, px=24, color=None, weight="regular", ha="left", va="baseline", top=False, halo=False, **kw):
        from matplotlib import patheffects as pe
        from vix_decomp import INK, SURF
        if not self.font and weight == "medium":
            weight = "regular"
        if halo:
            kw["path_effects"] = [pe.withStroke(linewidth=6 * self.pt, foreground=SURF)]
        return (self.fg if top else self.bg).text(x, y, s, fontsize=px * self.pt, color=color or INK, fontweight=weight, ha=ha, va=va,
                                                  fontfamily=self.fam, **kw)

    def width(self, t):
        return t.get_window_extent(self.fig.canvas.get_renderer()).width

    def card(self, top, height):
        from matplotlib.patches import FancyBboxPatch
        from vix_decomp import SURF
        self.bg.add_patch(FancyBboxPatch((40, top), self.W - 80, height, boxstyle="round,pad=0,rounding_size=16", facecolor=SURF,
                                         edgecolor="#0b0b0b24", lw=1.4 * self.pt))

    def line(self, xs, ys, color, lw=1.4, top=False, **kw):
        (self.fg if top else self.bg).plot(xs, ys, color=color, lw=lw * self.pt, **{"solid_capstyle": "butt", **kw})

    def swatch(self, x, y, color, size=18, alpha=1.0):
        from matplotlib.patches import Rectangle
        self.bg.add_patch(Rectangle((x, y - size), size, size, facecolor=color, edgecolor="none", alpha=alpha))

    def dot(self, x, y, color, r=11):
        from matplotlib.patches import Circle
        from vix_decomp import SURF
        self.fg.add_patch(Circle((x, y), r + 5, facecolor=SURF, edgecolor="none", zorder=5))
        self.fg.add_patch(Circle((x, y), r, facecolor=color, edgecolor="none", zorder=6))

    def axes(self, left, top, w, h, xlim, ylim):
        ax = self.fig.add_axes([left / self.W, 1 - (top + h) / self.H, w / self.W, h / self.H], facecolor="none")
        ax.set_xlim(*xlim)
        ax.set_ylim(*ylim)
        ax.axis("off")
        ax.set_zorder(5)
        return ax

    def tile(self, x, y, label, value, sub, sw=None):
        from vix_decomp import INK, INK2
        self.text(x, y + 34, label, 20, INK2, "medium")
        self.text(x, y + 84, value, 42, INK, "bold")
        if sw:
            self.swatch(x, y + 114, sw, 16)
        self.text(x + (26 if sw else 0), y + 114, sub, 19, INK2)

    def save(self, path):
        from vix_decomp import SURF
        self.fig.savefig(path, dpi=DPI, facecolor=SURF)
        return path


def _sgn(x, n=1, unit=""):
    """그림용 부호 있는 숫자(음수는 유니코드 빼기 기호)."""
    if x is None or x != x:
        return "–"
    body = f"{abs(x):,.{n}f}"
    zero = float(body.replace(",", "")) == 0                              # 0으로 반올림되는 값에는 부호를 붙이지 않는다
    return ("" if zero else "−" if x < 0 else "+") + unit + body


def _arr(x):
    return np.array([NAN if v is None else v for v in x], float)


def _nice(x):
    """x 이상인 보기 좋은 축 끝값(반으로 나눠도 깔끔한 수)."""
    if not x > 0:
        return 1.0
    mag = 10 ** math.floor(math.log10(x))
    return float(next(s for s in (1, 1.2, 1.6, 2, 2.4, 3, 4, 5, 6, 8, 10) if s * mag >= x * 0.999) * mag)


def _months(dates, L):
    """날짜 목록에서 달이 바뀌는 자리와 그 달의 이름."""
    out = []
    for i, d in enumerate(dates):
        if i == 0 or d[4:6] != dates[i - 1][4:6]:
            m = int(d[4:6])
            out.append((i, L["month"].format(m=m if "월" in L["month"] else MON[m - 1])))
    if len(out) > 1 and out[1][0] - out[0][0] < max(3, len(dates) // 18):      # 첫 달이 며칠뿐이면 이름이 겹치니 뺀다
        out = out[1:]
    return out


def _spread(ys, gap):
    """세로 위치들을 순서를 지킨 채 최소 간격 gap 이상으로 벌린다. 벌린 만큼 전체를 되돌려 가운데는 제자리에 둔다."""
    order = np.argsort(ys)
    y0 = np.array(ys, float)[order]
    y = y0.copy()
    for i in range(1, len(y)):
        y[i] = max(y[i], y[i - 1] + gap)
    y -= (y - y0).mean()
    out = np.empty(len(y))
    out[order] = y
    return out


def chart(res, path):
    """마지막 날의 그림(가로 2160픽셀). 위에 요약을 두고, 가격 차트·행사가별 GEX·가격별 프로필을 가로로 나란히 놓아
    세로축(지수 수준)을 함께 쓴다. 콜 벽·감마 플립·종가·풋 벽은 세 그림에 걸쳐 긋는다. 일봉을 받지 못했으면 가격 차트에 추이 표의 종가를 쓴다."""
    from vix_decomp import BASE, GRID, INK, INK2, MUTED, SURF, _ticks
    if "error" in res:
        raise ValueError(res["error"])
    spot, flip = res["spot"], res["flip"]
    has_flip = flip is not None and flip == flip
    bars, hs = res.get("ohlc"), res.get("history") or {}
    W, HEAD, GH = 2160, 150, 640
    left, PW, GAP, SW = 110, 1020, 40, 400                                # 왼쪽 여백, 가격 그림 폭, 그림 사이 간격, GEX 그림 하나의 폭
    left2, left3 = left + PW + GAP, left + PW + GAP + SW + GAP
    right = left3 + SW
    H = HEAD + 96 + GH + 104 + 104
    cv = _Canvas(W, H)
    L, text, pt = cv.L, cv.text, cv.pt

    # 제목과 요약(한 줄)
    text(40, 62, L["title"], 38, INK, "bold")
    text(40, 102, L["sub"].format(d=_ymd(res["date"])), 21, INK2)
    p, chg = res.get("prev"), res.get("chg_pct")
    w30, wa = res["walls30"], res["walls"]
    q = _sgn(res["pct"], 1)
    cv.tile(680, 6, L["spx"], f"{spot:,.2f}", L["chg"].format(v=_sgn(chg, 2)) if chg is not None else "")
    cv.tile(930, 6, L["net"], _sgn(res["net"] / 1e9, 1, "$") + "B",
            L["net_s"].format(p=_sgn(p["net"] / 1e9, 1, "$") + "B", q=q) if p else L["net_s0"].format(q=q))
    if has_flip:
        d = flip - spot
        cv.tile(1290, 6, L["flip"], f"{flip:,.0f}", L["below" if d > 0 else "above"].format(d=f"{abs(d):,.0f}", p=f"{100 * abs(d) / spot:.2f}"),
                sw=C_NEG if d > 0 else C_POS)
    else:
        cv.tile(1290, 6, L["flip"], "–", L["noflip"])
    cv.tile(1700, 6, L["walls"], f"{_lv(w30['call'])} · {_lv(w30['put'])}", L["walls_s"].format(c=_lv(wa["call"]), p=_lv(wa["put"])))

    # 가격 자료: 일봉이 있으면 최근 250일, 없으면 추이 표의 종가
    top = HEAD
    cv.card(top, 96 + GH + 104)
    ptop = top + 96
    n, dts, ma = 0, [], {}
    if bars:
        c_all = pd.Series(bars["c"], dtype=float)
        n = min(250, len(c_all))
        o, hi_, lo_, c = (np.array(bars[k][-n:], float) for k in ("o", "h", "l", "c"))
        dts = bars["date"][-n:]
        ma = {k: c_all.rolling(days).mean().values[-n:] for k, days, _ in C_MA}      # 단순 이동평균
        ma = {k: v for k, v in ma.items() if not np.isnan(v).all()}                 # 일봉이 모자라 한 점도 못 내면 뺀다
    elif len(hs.get("date", [])) >= 2:
        dts, c = hs["date"][-250:], _arr(hs["spot"])[-250:]
        n = len(dts)
        hi_ = lo_ = c
    # 세로축(지수 수준): 가격이 움직인 범위와 종가의 -12%~+9%를 모두 담는다
    lo_all = [spot * 0.88] + ([float(np.nanmin(lo_))] if n else []) + [float(np.nanmin(v)) for v in ma.values()]
    hi_all = [spot * 1.09] + ([float(np.nanmax(hi_))] if n else []) + [float(np.nanmax(v)) for v in ma.values()]
    ylo, yhi = 50 * math.floor(min(lo_all) * 0.995 / 50), 50 * math.ceil(max(hi_all) * 1.003 / 50)
    sy = GH / (yhi - ylo)

    def py_(v):
        return ptop + (yhi - v) * sy
    for t in [t for t in _ticks(ylo, yhi, 7)[0] if ylo < t < yhi]:
        for xx, ww in ((left, PW), (left2, SW), (left3, SW)):
            cv.line([xx, xx + ww], [py_(t), py_(t)], GRID, 1.0)
        text(left - 14, py_(t) + 7, f"{t:,.0f}", 20, MUTED, ha="right")

    # 가격 차트
    text(left, top + 46, L["price"] if bars else L["price0"], 26, INK, "bold")
    if n:
        ax = cv.axes(left, ptop, PW, GH, (-0.8, n - 0.2), (ylo, yhi))
        xs = np.arange(n)
        sx = PW / n
        if bars:
            x = left
            for k, _, col in C_MA:                                         # 이동평균선 범례
                if k in ma:
                    cv.line([x, x + 40], [top + 70, top + 70], col, 3.6, solid_capstyle="round")
                    x += 52
                    x += cv.width(text(x, top + 77, L["ma"][k], 19, INK2)) + 30
            up = c >= o
            ax.vlines(xs, lo_, hi_, color=INK2, lw=1.0 * pt)
            bh = np.maximum(np.abs(c - o), 1.5 / sy)                       # 시가와 종가가 같아도 한 줄은 보이게
            ax.bar(xs[up], bh[up], bottom=np.minimum(o, c)[up], width=0.66, color=SURF, edgecolor=INK2, lw=1.0 * pt)
            ax.bar(xs[~up], bh[~up], bottom=np.minimum(o, c)[~up], width=0.66, color=INK2, edgecolor=INK2, lw=1.0 * pt)
            for k, _, col in C_MA:
                if k in ma:
                    ax.plot(xs, ma[k], color=col, lw=3.6 * pt, solid_joinstyle="round", solid_capstyle="round")
        else:
            ax.plot(xs, c, color=INK, lw=4.0 * pt, solid_joinstyle="round", solid_capstyle="round")
        last = -1e9
        for i, lab in _months(dts, L):
            px_ = left + (i + 0.8) * sx
            if px_ - last >= 64:                                           # 달 이름이 겹치지 않게
                text(px_, ptop + GH + 34, lab, 20, MUTED, ha="center")
                last = px_
    else:
        text(left, top + 77, L["noprice"], 19, MUTED)

    # 행사가별 GEX
    for xx, a, b in ((left2, L["strike"], L["strike_s"].format(b=f"{BUCKET:g}")), (left3, L["prof"], L["prof_s"])):
        text(xx, top + 46, a, 26, INK, "bold")
        text(xx, top + 77, b, 19, INK2)
    K, gc, gp = (_arr(res["strikes"][k]) for k in ("K", "call", "put"))
    bk = pd.Series((gc - gp) / 1e9).groupby(np.floor(K / BUCKET + 0.5) * BUCKET).sum()
    bk = bk[(bk.index > ylo) & (bk.index < yhi)]
    xm = _nice(1.06 * float(bk.abs().max()) if len(bk) else 0.0)
    ax = cv.axes(left2, ptop, SW, GH, (-xm, xm), (ylo, yhi))
    ax.barh(bk.index[bk >= 0], bk[bk >= 0], height=BUCKET * 0.62, color=C_POS)
    ax.barh(bk.index[bk < 0], bk[bk < 0], height=BUCKET * 0.62, color=C_NEG)

    # 가격별 프로필
    pr, pn = _arr(res["profile"]["price"]), _arr(res["profile"]["net"]) / 1e9
    m = (pr >= ylo) & (pr <= yhi)
    pr, pn = pr[m], pn[m]
    pm = _nice(1.06 * float(np.abs(pn).max()) if len(pn) else 0.0)
    ax2 = cv.axes(left3, ptop, SW, GH, (-pm, pm), (ylo, yhi))
    ax2.fill_betweenx(pr, 0, np.maximum(pn, 0), color=C_POS, alpha=0.13, lw=0)
    ax2.fill_betweenx(pr, 0, np.minimum(pn, 0), color=C_NEG, alpha=0.13, lw=0)
    if has_flip and pr[0] < flip < pr[-1]:                                 # 부호가 바뀌는 자리에 점을 하나 넣어 두 색의 선이 맞닿게 한다
        i = int(np.searchsorted(pr, flip))
        pr, pn = np.insert(pr, i, flip), np.insert(pn, i, 0.0)
    for col, keep in ((C_POS, pn >= 0), (C_NEG, pn <= 0)):
        ax2.plot(np.where(keep, pn, np.nan), pr, color=col, lw=4.6 * pt, solid_joinstyle="round", solid_capstyle="round")
    for axx, lim, xx in ((ax, xm, left2), (ax2, pm, left3)):
        axx.plot([0, 0], [ylo, yhi], color=BASE, lw=1.6 * pt)
        for t in (-lim, -lim / 2, 0, lim / 2, lim):
            text(xx + (t + lim) / (2 * lim) * SW, ptop + GH + 34, _sgn(t, 0 if lim >= 4 else 1) if t else "0", 20, MUTED,
                 ha="left" if t == -lim else "right" if t == lim else "center")

    # 콜 벽·감마 플립·종가·풋 벽을 세 그림에 걸쳐 긋고 오른쪽 여백에 이름을 적는다
    marks = [(w30["call"], L["cw"], C_POS, (7, 4)), (flip if has_flip else NAN, L["fl_s"], INK2, (3, 3)), (spot, L["cl"], INK, (1, 2.5)),
             (w30["put"], L["pw"], C_NEG, (7, 4))]
    marks = [mk for mk in marks if mk[0] is not None and mk[0] == mk[0] and ylo < mk[0] < yhi]
    ys = _spread([py_(mk[0]) for mk in marks], 32)
    for (v, lab, col, dash), ty in zip(marks, ys):
        cv.line([left, right], [py_(v), py_(v)], col, 2.0, top=True, dashes=dash)
        cv.line([right, right + 12], [py_(v), ty], col, 1.4, top=True)
        text(right + 18, ty + 7, f"{lab} {v:,.0f}", 20, INK, "medium", top=True)
    # 프로필 위의 점: 종가 수준의 값과 플립
    net_bn = res["net"] / 1e9
    cx, cy = left3 + (net_bn + pm) / (2 * pm) * SW, py_(spot)
    cv.dot(cx, cy, C_POS if net_bn >= 0 else C_NEG)
    toright = cx < left3 + SW * 0.55
    lines = sorted(py_(mk[0]) for mk in marks)
    ty = next((t for t in (cy + 40, cy - 22) if not any(t - 24 < ly < t + 8 for ly in lines)), None)   # 점 바로 아래, 안 되면 바로 위
    if ty is None:                                                         # 둘 다 수준선에 걸리면 걸리는 선들 아래로 내린다
        ty = cy + 40
        for ly in lines:
            if ty - 24 < ly < ty + 8:
                ty = ly + 30
    text(cx + (20 if toright else -20), ty, f"{L['at']} {_sgn(net_bn, 1)}", 20, INK, "medium", ha="left" if toright else "right", top=True, halo=True)
    if has_flip and ylo < flip < yhi:
        cv.dot(left3 + SW / 2, py_(flip), INK, r=9)

    # 아래 줄: 만기별 순 GEX(가격 차트 아래), 색 설명(GEX 그림 아래)
    yl = ptop + GH + 80
    x = left + cv.width(text(left, yl, L["byexp"], 19, INK2, "medium")) + 26
    for lab, v in ((L["e7"], res["net7"]), (L["e30"], res["net30"] - res["net7"]), (L["e99"], res["net"] - res["net30"])):
        x += cv.width(text(x, yl, lab, 19, MUTED)) + 10
        x += cv.width(text(x, yl, _sgn(v / 1e9, 1), 20, INK, "medium")) + 28
    x = left2
    for col, lab in ((C_POS, L["pos_s"]), (C_NEG, L["neg_s"])):
        cv.swatch(x, yl, col, 17)
        x += 27
        x += cv.width(text(x, yl, lab, 19, INK2)) + 34
    fy = top + 96 + GH + 104 + 40
    foot = [f.format(a=_sgn(res["net_quote"] / 1e9, 1), b=_sgn(res["net_cboe"] / 1e9, 1)) for f in L["foot"]]
    for i, s_ in enumerate(foot):
        text(40 if i < 2 else left2 - 60, fy + 30 * (i % 2), s_, 18, MUTED)
    return cv.save(path)


def chart_history(res, path):
    """추이 그림: S&P 500 종가와 감마 플립, 순 GEX(콜·풋 합계 대비 %). PNG(가로 1080픽셀)로 저장한다."""
    from vix_decomp import BASE, GRID, INK, INK2, MUTED, _ticks
    if "error" in res:
        raise ValueError(res["error"])
    hs = res["history"]
    dts = hs["date"]
    n = len(dts)
    if n < 2:
        raise ValueError("추이를 그릴 날짜가 부족함")
    px, fl, pct, net = (_arr(hs[k]) for k in ("spot", "flip", "pct", "net_bn"))
    W, KH, P1, P2 = 1080, 290, 420, 300
    FT = 46 + 30 * len(GT["ko"]["h_foot"]) + 24
    H = 132 + KH + 28 + 130 + P1 + 64 + 28 + 96 + P2 + 64 + FT
    cv = _Canvas(W, H)
    L, text, pt = cv.L, cv.text, cv.pt
    text(40, 66, L["h_title"], 40, INK, "bold")
    text(40, 104, L["h_sub"].format(a=_ymd(dts[0]), b=_ymd(dts[-1]), n=n), 22, INK2)

    top = 132
    cv.card(top, KH)
    spot, flip, p, st = res["spot"], res["flip"], res.get("prev"), res.get("stats")
    has_flip = flip is not None and flip == flip
    cv.tile(70, top + 8, L["spx"], f"{spot:,.2f}", _ymd(res["date"]))
    q = _sgn(res["pct"], 1)
    cv.tile(560, top + 8, L["net"], _sgn(res["net"] / 1e9, 1, "$") + "B",
            L["net_s"].format(p=_sgn(p["net"] / 1e9, 1, "$") + "B", q=q) if p else L["net_s0"].format(q=q))
    if has_flip:
        d = flip - spot
        cv.tile(70, top + 132, L["flip"], f"{flip:,.0f}", L["below" if d > 0 else "above"].format(d=f"{abs(d):,.0f}", p=f"{100 * abs(d) / spot:.2f}"),
                sw=C_NEG if d > 0 else C_POS)
    else:
        cv.tile(70, top + 132, L["flip"], "–", L["noflip"])
    if st:
        cv.tile(560, top + 132, L["h_neg"], L["h_neg_v"].format(k=st["neg_days"], p=f"{100 * st['neg_days'] / st['days']:.0f}"),
                L["h_neg_s"].format(s=L["sign"][st["run_sign"]], r=st["run"], k=st["rank_low"]))

    left, pw_ = 110, W - 110 - 122
    sx = pw_ / n
    xl = (-0.7, n - 0.3)

    def months(ptop, ph):
        for i, lab in _months(dts, L):
            text(left + (i + 0.7) * sx, ptop + ph + 34, lab, 20, MUTED, ha="center")

    # 종가와 감마 플립
    top = 132 + KH + 28
    cv.card(top, 130 + P1 + 64)
    text(70, top + 54, L["h_p1"], 28, INK, "bold")
    x = 70
    for kind, lab in (("close", L["cl"]), ("flip", L["fl"]), ("shade", L["h_shade"])):
        if kind == "shade":
            cv.swatch(x, top + 104, C_NEG, 18, alpha=0.22)
            x += 28
        else:
            cv.line([x, x + 40], [top + 96, top + 96], INK if kind == "close" else INK2, 4.2 if kind == "close" else 3.0,
                    **({} if kind == "close" else {"dashes": (3, 2)}))
            x += 52
        x += cv.width(text(x, top + 104, lab, 19, INK2)) + 30
    both = np.concatenate([px, fl[~np.isnan(fl)]])
    yt, _ = _ticks(float(both.min()), float(both.max()), 5)
    pad = 0.05 * (both.max() - both.min())
    ylo, yhi = min(both.min() - pad, yt[0]), max(both.max() + pad, yt[-1])
    ptop = top + 130
    ax = cv.axes(left, ptop, pw_, P1, xl, (ylo, yhi))
    sy = P1 / (yhi - ylo)
    for i in np.flatnonzero(net < 0):
        ax.axvspan(i - 0.5, i + 0.5, color=C_NEG, alpha=0.13, lw=0)
    for t in yt:
        ax.plot(xl, [t, t], color=GRID, lw=1.0 * pt)
        text(left - 14, ptop + (yhi - t) * sy + 7, f"{t:,.0f}", 20, MUTED, ha="right")
    xs = np.arange(n)
    ax.plot(xs, fl, color=INK2, lw=3.0 * pt, dashes=(3, 2))
    ax.plot(xs, px, color=INK, lw=4.2 * pt, solid_joinstyle="round", solid_capstyle="round")
    months(ptop, P1)
    ex = left + (n - 1 + 0.7) * sx
    ends = [(px[-1], L["cl"])] + ([(fl[-1], L["fl_s"])] if fl[-1] == fl[-1] else [])
    ys = [ptop + (yhi - v) * sy for v, _ in ends]
    if len(ys) == 2 and abs(ys[0] - ys[1]) < 56:                          # 두 이름표가 가까우면 위아래로 벌린다
        mid, up = (ys[0] + ys[1]) / 2, ys[0] < ys[1]
        ys = [mid - 28, mid + 28] if up else [mid + 28, mid - 28]
    cv.dot(ex, ptop + (yhi - px[-1]) * sy, INK, r=9)
    for (v, lab), yy in zip(ends, ys):
        text(left + pw_ + 14, yy - 5, lab, 19, INK2, top=True)
        text(left + pw_ + 14, yy + 21, f"{v:,.0f}", 21, INK, "medium", top=True)

    # 순 GEX, 콜·풋 합계 대비 %
    top = top + 130 + P1 + 64 + 28
    cv.card(top, 96 + P2 + 64)
    text(70, top + 54, L["h_p2"], 28, INK, "bold")
    lim = _nice(1.06 * float(np.nanmax(np.abs(pct))))
    ptop = top + 96
    ax = cv.axes(left, ptop, pw_, P2, xl, (-lim, lim))
    sy = P2 / (2 * lim)
    for t in (-lim, -lim / 2, 0, lim / 2, lim):
        ax.plot(xl, [t, t], color=BASE if t == 0 else GRID, lw=(1.6 if t == 0 else 1.0) * pt)
        text(left - 14, ptop + (lim - t) * sy + 7, (_sgn(t, 0) if t else "0") + "%", 20, MUTED, ha="right")
    ax.bar(xs[pct >= 0], pct[pct >= 0], width=0.62, color=C_POS)
    ax.bar(xs[pct < 0], pct[pct < 0], width=0.62, color=C_NEG)
    months(ptop, P2)
    text(left + pw_ + 14, ptop + (lim - pct[-1]) * sy + 8, _sgn(pct[-1], 1) + "%", 21, INK, "medium", top=True)
    fy = H - FT + 58
    for i, s in enumerate(L["h_foot"]):
        text(40, fy + 30 * i, s, 18, MUTED)
    return cv.save(path)


# ----------------------------------------------------------------------------- 실행
def run(a):
    if a.from_json:
        with open(a.from_json, encoding="utf-8") as f:
            res = json.load(f)
        res = res.get("gex", res)
        print(report(res))
    else:
        dates = list_dates(a.archive_dir)
        if a.date:
            dates = [d for d in dates if d <= a.date]
            if not dates or dates[-1] != a.date:
                print(f"[오류] {a.date} 보관본이 없음")
                return 1
        if not dates:
            print("[오류] 보관본이 없음")
            return 1
        hist = read_history(a.history_csv) if a.history_csv else pd.DataFrame(columns=HIST_COLS)
        have = set(hist["date"]) if len(hist) else set()
        need = [d for d in dates if d not in have or d == dates[-1]]            # 표에 없는 날과 마지막 날만 계산한다
        if a.lookback is not None:
            need = need[-(a.lookback + 1):]
        closes = {} if a.no_yf else index_closes(need)
        saved = read_closes(a.closes_csv) if a.closes_csv else {}
        rows, last, gap = [], None, NAN
        for d in need:
            try:
                dd = load_day(a.archive_dir, d, ["SPX"])
            except Exception as e:
                print(f"[확인 필요] {d} 보관본을 읽지 못함: {type(e).__name__}")
                continue
            if "SPX" not in dd or not len(dd["SPX"]):
                print(f"[확인 필요] {d} 보관본에 SPX가 없음")
                continue
            spot, diff = pick_close(closes.get(d, {}).get("spx"), saved.get(d))
            r = day(dd["SPX"], d, spot)
            r["spot_is_close"] = spot == spot
            if r["spot_is_close"] or not a.require_close:                         # 수집기는 공식 종가가 있는 날만 표에 넣는다
                rows.append(hist_row(r))
            if d == dates[-1]:
                last, gap = r, diff
        hist = merge_history(hist, rows)
        if a.history_csv and a.save_history and any(rows):
            save_history(hist, a.history_csv)
            print(f"추이 표: {a.history_csv} ({len(hist)}일)")
        if last is None:
            print(f"[오류] {dates[-1]}의 SPX 자료를 계산하지 못함")
            return 1
        res = summary(last, hist, None if a.no_yf else ohlc(dates[-1]))
        if "error" not in res and not last.get("spot_is_close"):
            res["flags"].append("지수 종가를 받지 못해 옵션 호가에 담긴 지수 수준을 종가 대신 씀")
        if "error" not in res and gap == gap and abs(gap) > 0.02:
            res["flags"].append(f"수집할 때 적어 둔 지수 종가({saved[dates[-1]]:,.2f})가 야후 종가({last['spot']:,.2f})와 {gap:+.2f}% 다름")
        print(report(res))
        res = clean(res)
        if a.out_json:
            with open(a.out_json, "w", encoding="utf-8") as f:
                json.dump(res, f, ensure_ascii=False, indent=1)
            print(f"\nJSON: {a.out_json}")
    if "error" in res:
        return 1
    if a.png:
        print("GEX 그림:", chart(res, a.png))
    if a.png_history:
        try:
            print("GEX 추이 그림:", chart_history(res, a.png_history))
        except ValueError as e:
            print(f"[확인 필요] 추이 그림을 만들지 못함: {e}")
    return 0


def synthetic(date="20260611", spot=5000.0, vol=lambda k: 0.2, extra=()):
    """자체 점검용 가상 SPX 체인(보관본 CSV와 같은 열). 만기 3개, 행사가 4,000~6,000을 25 간격으로, 가격은 Black-76 이론가.
    미결제약정은 4,800 풋과 5,200 콜에 몰아 두었다. extra = [(만기 순번, 행사가, "C"/"P", 미결제약정), …]를 더 얹을 수 있다."""
    from option_sentiment import b76
    tv = quote_time(date)
    rows = []
    for n, days in enumerate((7, 28, 91)):
        exp = pd.Timestamp(date) + pd.Timedelta(days=days)
        T = (exp.to_pydatetime().replace(hour=16, minute=0) - tv).total_seconds() / 60 / M365
        F = spot * math.exp(R * T)
        for K in np.arange(4000.0, 6000.1, 25.0):
            for cp, sgn in (("C", 1.0), ("P", -1.0)):
                px = float(b76(F, K, T, vol(K), sgn))
                oi = {("C", 5200.0): 9000, ("P", 4800.0): 14000}.get((cp, K), 300) + sum(x[3] for x in extra if x[:3] == (n, K, cp))
                rows.append({"Symbol": f"SPXW{exp:%y%m%d}{cp}{int(K * 1000):08d}", "Expiry": f"{exp:%Y-%m-%d}", "Type": "Call" if cp == "C" else "Put",
                             "Strike": K, "Bid": px * 0.999, "Ask": px * 1.001, "Last": px, "Volume": 0, "Open Interest": oi, "IV": vol(K),
                             "Delta": 0.0, "Gamma": 0.0})
    return pd.DataFrame(rows)


def self_test():
    """가상 체인에서 선도가·IV·순 GEX·기준 수준·감마 플립·콜 벽·풋 벽이 알려진 답과 맞는지 확인한다(네트워크 없이 돈다)."""
    from option_sentiment import prep
    date, S = "20260611", 5000.0
    skew = lambda k: 0.2 - 0.3 * math.log(k / S)                            # noqa: E731  행사가가 낮을수록 IV가 높은 곡선
    df = prep(synthetic(date, S, skew), "SPX", date)
    t, miss = build(df, date)
    assert miss == 0 and len(t) == 3 * 81, (miss, len(t))
    assert np.allclose(t["F"], S * np.exp(R * t["T"]), rtol=2e-5), "선도가"
    from option_sentiment import b76
    worth = np.array([float(b76(r["F"], r["K"], r["T"], skew(r["K"]), 1.0 if r["K"] >= r["F"] else -1.0)) for _, r in t.iterrows()]) >= 0.5
    assert np.allclose(t["sig"][worth], [skew(k) for k in t["K"][worth]], atol=3e-4), "IV(값이 0.5 이상인 외가격 옵션)"
    assert abs(ref_level(t) / S - 1) < 2e-5, "기준 수준"
    # 순 GEX: 식을 따로 적어 계산한 값과 비교
    exp_net = 0.0
    for _, r in t.iterrows():
        sd = skew(r["K"]) * math.sqrt(r["T"])
        d1 = (math.log(S * math.exp(R * r["T"]) / r["K"]) + 0.5 * sd * sd) / sd
        gamma = math.exp(-R * r["T"]) * math.exp(-0.5 * d1 * d1) / SQ2PI * math.exp(R * r["T"]) / (S * sd)     # e^(-qT) phi(d1) / (S sd), q = 0
        exp_net += gamma * (r["oi_c"] - r["oi_p"]) * 100 * S * S * 0.01
    res = day(df, date, S)
    assert abs(res["net"] / exp_net - 1) < 2e-3, (res["net"], exp_net)
    assert abs(res["net_quote"] / res["net"] - 1) < 1e-3                    # 종가 = 호가 시점 수준이면 두 값이 같다
    assert res["walls"] == {"call": 5200.0, "put": 4800.0}, res["walls"]
    assert res["net"] < 0 and res["flip"] > S, "풋이 많은 가상 체인: 순 GEX 음수, 플립은 종가 위"
    b = Book(t)
    assert abs(b.net(res["flip"] / res["ref"])) < 1e-6 * res["gross"], "플립에서 순 GEX는 0"
    assert b.net(res["flip"] / res["ref"] * 1.01) > 0 > b.net(res["flip"] / res["ref"] * 0.99)
    # 종가가 호가 시점 수준보다 0.3% 높으면: 프로필을 그 자리에서 읽은 값이 나오고, 플립(지수 수준)은 그대로다
    up = day(df, date, S * 1.003)
    assert abs(up["net"] - b.net(1.003 * S / up["ref"])) < 1e-6 * up["gross"] and abs(up["flip"] - res["flip"]) < 0.05
    assert up["net"] > res["net"] and abs(up["net_quote"] - res["net_quote"]) < 1e-6 * res["gross"]
    # 같은 만기·행사가에 콜과 풋을 같은 수만큼 얹어도(박스 스프레드 따위) 순 GEX는 그대로다
    box = prep(synthetic(date, S, skew, extra=((1, 4500.0, "C", 50000), (1, 4500.0, "P", 50000))), "SPX", date)
    assert abs(day(box, date, S)["net"] - res["net"]) < 1e-6 * res["gross"], "박스"
    # 추이 표
    h = merge_history(pd.DataFrame(columns=HIST_COLS), [hist_row(res)])
    h2 = merge_history(h, [hist_row(up)])
    assert len(h2) == 1 and abs(h2["net_bn"].iloc[0] - round(up["net"] / 1e9, 3)) < 1e-9, "같은 날짜는 새 값으로 바뀐다"
    out = summary(res, h)
    assert out["flags"] == [] and "stats" not in out and "prev" not in out
    assert "error" in day(df[df["dte"] < 0], date, S)
    print("자체 점검 통과")
    return 0


def main():
    ap = argparse.ArgumentParser(description="보관본의 SPX 옵션으로 딜러 감마 노출(GEX)을 계산하고 그림을 만든다")
    ap.add_argument("--archive-dir", default="data/daily")
    ap.add_argument("--date", help="기준일 YYYYMMDD(기본: 가장 최근 보관본)")
    ap.add_argument("--history-csv", help="추이 표(CSV). 있으면 읽어서 빠진 날짜만 새로 계산한다")
    ap.add_argument("--save-history", action="store_true", help="새로 계산한 날짜를 추이 표에 써 넣는다")
    ap.add_argument("--closes-csv", help="수집기가 적어 둔 지수 종가 표(CSV). 야후에서 종가를 받지 못한 날에 쓴다")
    ap.add_argument("--require-close", action="store_true", help="공식 종가가 없는 날은 추이 표에 넣지 않는다(수집기용)")
    ap.add_argument("--lookback", type=int, help="추이 표에 없는 날짜 가운데 최근 며칠만 계산할지(기본: 전부)")
    ap.add_argument("--png", help="GEX 그림(PNG) 저장 경로")
    ap.add_argument("--png-history", help="GEX 추이 그림(PNG) 저장 경로")
    ap.add_argument("--out-json")
    ap.add_argument("--from-json", help="저장해 둔 결과(JSON)로 보고와 그림만 다시 만든다")
    ap.add_argument("--no-yf", action="store_true", help="야후 파이낸스에서 종가·일봉을 받지 않는다")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    return self_test() if a.self_test else run(a)


if __name__ == "__main__":
    sys.exit(main())
