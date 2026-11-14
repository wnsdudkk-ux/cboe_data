#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""option_sentiment.py - cboe_data 일일 보관본에서 옵션 체인을 읽어 전 거래일 대비 변화를 계산한다.

입력: data/daily/quotes_YYYYMMDD.tar.gz (압축을 풀지 않고 필요한 종목만 바로 읽는다)
출력: 종목별 요약(터미널) + 전체 수치(JSON, --out-json)
예:   python3 option_sentiment.py --archive-dir data/daily --tickers SPX VIX GLD --out-json out.json
      python3 option_sentiment.py --archive-dir data/daily --tickers QQQ --date 20261113   # 날짜 지정(수동)
종료 코드: 0 분석함 / 3 오늘치 수집본 없음 / 4 휴장(새 자료 없음) / 1 오류
필요: pandas, numpy (선택: exchange_calendars 휴장일 판정, yfinance 종가 대조)

계산 기준
- IV는 파일의 IV 열을 그대로 쓰지 않고 마감 호가 중간값에서 직접 역산한다(Black-76, 달력 일수). 파일의 IV·델타는
  Cboe가 수집 시각의 기초자산 추정치로 다시 계산한 값이라 마감 호가와 어긋나는 날이 있다. 파일 IV는 대조용으로만 쓴다.
- 기초자산 수준은 풋-콜 패리티로 구한 선도가(F = K + (콜 - 풋) x e^(rT))를 쓴다. 보관본에는 기초자산 가격이 없다.
- 미결제약정은 전 영업일 마감 기준이라, 두 파일 사이의 증감은 '전 거래일' 거래의 결과다. 거래량은 당일 것이다.
"""
import argparse
import datetime as dt
import json
import math
import os
import re
import sys
import tarfile
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

R = 0.04       # 할인율(연). 만기가 짧으면 결과에 거의 영향이 없다
MULT = 100     # 계약 승수
INDEX = {"SPX", "VIX", "NDX", "RUT", "DJX", "XSP", "OEX", "XEO", "MRUT"}   # 루트가 여러 개인 지수(SPX·SPXW 등)
AM_ROOTS = {"SPX", "SPXO", "VIX", "VIXW"}                                    # 만기일 아침에 결제되는 루트
YF = {"SPX": "^GSPC", "VIX": "^VIX", "NDX": "^NDX", "RUT": "^RUT"}
TENORS = [7, 30, 90, 180]
NAN = float("nan")
ET = ZoneInfo("America/New_York")
# 최근 추이와 견줄 지표(오늘 변화가 평소 하루 변동폭의 몇 배인지 계산한다)
CTX_KEYS = ["spot", "atm7", "atm30", "atm90", "ts_7_30", "ts_30_90", "rr30", "rr90", "bf30", "ptail30", "ctail30",
            "fwd30", "fwd90", "pcr_vol", "pcr_prem", "pcr_oi", "gex_net", "spread", "atm30_cboe"]


# ----------------------------------------------------------------------------- 읽기
def list_dates(adir):
    return sorted(m.group(1) for f in os.listdir(adir) if (m := re.fullmatch(r"quotes_(\d{8})\.tar\.gz", f)))


def prep(raw, ticker, date):
    sym = raw["Symbol"].astype(str).str.strip()

    def num(c):
        return pd.to_numeric(raw[c], errors="coerce")
    df = pd.DataFrame({
        "opt": sym, "root": sym.str[:-15], "exp": pd.to_datetime(raw["Expiry"], errors="coerce"),
        "cp": raw["Type"].astype(str).str[:1].str.upper(), "K": num("Strike"),
        "bid": num("Bid").fillna(0), "ask": num("Ask").fillna(0), "last": num("Last").fillna(0),
        "vol": num("Volume").fillna(0), "oi": num("Open Interest").fillna(0), "iv": num("IV").fillna(0),
        "delta": num("Delta").fillna(0), "gamma": num("Gamma").fillna(0)})
    base = ticker.replace(".", "")
    keep = df["root"].str.startswith(base) if ticker in INDEX else df["root"].isin([base, ticker])
    df = df[keep & df["exp"].notna() & df["K"].notna() & df["cp"].isin(["C", "P"])].drop_duplicates("opt").copy()
    df["dte"] = (df["exp"] - pd.Timestamp(date)).dt.days
    df["mid"] = ((df["bid"] + df["ask"]) / 2).where((df["bid"] > 0) & (df["ask"] >= df["bid"]))   # 양쪽 호가가 있을 때만
    return df[df["dte"] >= 0].reset_index(drop=True)


def load_day(adir, date, tickers):
    """하루치 압축파일에서 필요한 종목의 CSV만 바로 읽는다(디스크에 풀지 않는다)."""
    want = {f"/{t}/{t.lower()}_quotes_{date}.csv": t for t in tickers}
    out = {}
    with tarfile.open(os.path.join(adir, f"quotes_{date}.tar.gz"), "r:gz") as tf:
        for m in tf:
            t = next((t for suf, t in want.items() if m.name.endswith(suf)), None)
            if t and m.isfile():
                out[t] = prep(pd.read_csv(tf.extractfile(m)), t, date)
                out[t].attrs["fetched"] = m.mtime            # 수집기가 이 CSV를 쓴 시각 = Cboe에서 받은 시각
                if len(out) == len(want):
                    break
    return out


# ----------------------------------------------------------------------------- 옵션 가격식(Black-76)
def ncdf(x):
    """표준정규 누적분포(오차 1e-7 이내)."""
    x = np.asarray(x, float)
    t = 1.0 / (1.0 + 0.2316419 * np.abs(x))
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
    y = 1.0 - np.exp(-0.5 * x * x) / math.sqrt(2 * math.pi) * poly
    return np.where(x >= 0, y, 1.0 - y)


def b76(F, K, T, v, cp):
    """cp = +1 콜, -1 풋. K, v, cp는 배열 가능."""
    sd = v * math.sqrt(T)
    d1 = (np.log(F / K) + 0.5 * sd * sd) / sd
    return math.exp(-R * T) * cp * (F * ncdf(cp * d1) - K * ncdf(cp * (d1 - sd)))


def implied_vol(F, K, T, px, cp):
    """가격에서 IV 역산(이분법). 0.5%~500% 범위를 벗어나면 nan."""
    lo, hi = np.full(len(K), 0.005), np.full(len(K), 5.0)
    for _ in range(50):
        mid = (lo + hi) / 2
        up = b76(F, K, T, mid, cp) < px
        lo, hi = np.where(up, mid, lo), np.where(up, hi, mid)
    v = (lo + hi) / 2
    return np.where((v > 0.0051) & (v < 4.99), v, np.nan)


def greeks(F, K, T, v):
    """한 행사가의 그릭스(기초자산 1단위 기준). 베가는 IV 1%포인트당, 세타는 달력 하루당."""
    sd = v * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sd * sd) / sd
    dfac, phi = math.exp(-R * T), math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
    nd1 = float(ncdf(d1))
    decay = -dfac * F * phi * v / (2 * math.sqrt(T))
    return {"delta_c": dfac * nd1, "delta_p": -dfac * (1 - nd1), "gamma": dfac * phi / (F * sd), "vega": dfac * F * phi * math.sqrt(T) / 100,
            "theta_c": (decay + R * float(b76(F, K, T, v, 1.0))) / 365, "theta_p": (decay + R * float(b76(F, K, T, v, -1.0))) / 365}


def years(dte, root):
    """잔존 기간(년). 수집 시각은 마감 직후이고, 아침 결제 루트는 만기일 개장 때 끝난다."""
    return max(dte - (0.31 if root in AM_ROOTS else 0.04), 0.25) / 365.0


# ----------------------------------------------------------------------------- 만기별 곡면
def interp(x, xs, ys):
    """선형 보간. xs의 범위를 벗어나면 nan."""
    xs, ys = np.asarray(xs, float), np.asarray(ys, float)
    ok = ~(np.isnan(xs) | np.isnan(ys))
    xs, ys = xs[ok], ys[ok]
    if len(xs) < 2 or not (xs[0] <= x <= xs[-1]):
        return NAN
    return float(np.interp(x, xs, ys))


def per_strike(g):
    """행사가별로 콜과 풋을 나란히 놓는다."""
    c, p = g[g["cp"] == "C"].set_index("K"), g[g["cp"] == "P"].set_index("K")
    j = c[["mid", "iv"]].join(p[["mid", "iv"]], how="outer", lsuffix="_c", rsuffix="_p").sort_index()
    return j[~j.index.duplicated()]


def fwd(j, T):
    """풋-콜 패리티로 구한 선도가. 콜과 풋 가격이 가장 비슷한 행사가 근처 5개의 중앙값."""
    y = (j["mid_c"] - j["mid_p"]).dropna()
    if len(y) < 5:
        return NAN
    k, v = y.index.values.astype(float), y.values.astype(float)
    near = np.argsort(np.abs(k - k[np.argmin(np.abs(v))]))[:5]
    return float(np.median(k[near] + v[near] * math.exp(R * T)))


def surface(df):
    """만기마다 선도가 F, 등가격(ATM) IV, 25·10델타 풋/콜 IV를 호가에서 계산한다. 외가격 쪽(F 아래는 풋, 위는 콜) 호가만 쓴다.
    같은 날짜에 루트가 둘이면 미결제약정이 큰 쪽을 쓰고, 호가가 부실한 만기(등가격 양옆 IV가 15% 넘게 어긋남 등)는 뺀다.
    돌려주는 smiles[만기] = (행사가 배열, IV 배열, 잔존 기간(년), F)."""
    live = df[df["dte"] >= 1]
    pick = (live.groupby(["exp", "root"])["oi"].sum().reset_index()
            .sort_values(["exp", "oi"], ascending=[True, False]).drop_duplicates("exp"))
    rows, smiles, bad = [], {}, 0
    for exp, root in pick[["exp", "root"]].itertuples(index=False):
        g = live[(live["exp"] == exp) & (live["root"] == root)]
        dte = int(g["dte"].iloc[0])
        T = years(dte, root)
        j = per_strike(g)
        F = fwd(j, T)
        if F != F:
            bad += 1
            continue
        k = j.index.values.astype(float)
        cp = np.where(k < F, -1.0, 1.0)
        px = np.where(k < F, j["mid_p"].values, j["mid_c"].values).astype(float)
        ok = ~np.isnan(px)
        k, cp, px = k[ok], cp[ok], px[ok]
        v = implied_vol(F, k, T, px, cp) if len(k) else np.array([])
        ok = ~np.isnan(v)
        k, cp, v = k[ok], cp[ok], v[ok]
        if (cp < 0).sum() < 3 or (cp > 0).sum() < 3:
            bad += 1
            continue
        lo, hi = v[cp < 0][-1], v[cp > 0][0]                 # 등가격 바로 아래 풋, 바로 위 콜
        if abs(lo - hi) > 0.15 * (lo + hi) / 2:
            bad += 1
            continue
        d1 = (np.log(F / k) + 0.5 * v * v * T) / (v * math.sqrt(T))
        dl = math.exp(-R * T) * np.where(cp > 0, ncdf(d1), -ncdf(-d1))

        def at(target):                                       # 델타가 target인 지점의 IV(풋은 음수)
            m = (cp < 0) if target < 0 else (cp > 0)
            o = np.argsort(dl[m])
            return interp(target, dl[m][o], v[m][o])
        cb = j["iv_p"].where(j.index < F, j["iv_c"])
        cb = cb[cb > 0]
        smiles[exp] = (k, v, T, F)
        rows.append({"exp": exp, "root": root, "dte": dte, "F": F, "atm": interp(F, k, v), "p25": at(-0.25), "c25": at(0.25),
                     "p10": at(-0.10), "c10": at(0.10), "atm_cboe": interp(F, cb.index.values, cb.values)})
    sf = pd.DataFrame(rows, columns=["exp", "root", "dte", "F", "atm", "p25", "c25", "p10", "c10", "atm_cboe"])
    sf.attrs["bad"] = bad
    return sf, smiles


def cm(sf, col, days, var=True):
    """잔존일수 days에 맞춘 값(날짜가 지나며 만기가 가까워지는 효과 제거). IV는 분산x시간을 선형 보간, 그 밖은 선형 보간.
    범위 밖은 가장 가까운 만기 값. 잔존 1일 만기는 쓰지 않는다."""
    s = sf[sf["dte"] >= 2].dropna(subset=[col]).sort_values("dte")
    if s.empty:
        return NAN
    t, v = s["dte"].values.astype(float), s[col].values.astype(float)
    if days <= t[0]:
        return float(v[0])
    if days >= t[-1]:
        return float(v[-1])
    i = int(np.searchsorted(t, days))
    t0, t1, v0, v1 = t[i - 1], t[i], v[i - 1], v[i]
    if not var:
        return float(v0 + (v1 - v0) * (days - t0) / (t1 - t0))
    w = (v0 * v0 * t0 * (t1 - days) + v1 * v1 * t1 * (days - t0)) / (t1 - t0)
    return math.sqrt(max(w, 0.0) / days)


def ratio(a, b):
    return float(a / b) if b else NAN


def headline(df):
    """하루치 요약 지표."""
    sf, smiles = surface(df)
    h = {"n_exp": int(len(sf)), "n_exp_bad": int(sf.attrs.get("bad", 0)), "n_contracts": int(len(df))}
    if not sf.empty:
        h["spot"] = float(sf.sort_values("dte")["F"].iloc[0])   # 가장 가까운 만기의 선도가 = 마감 호가에 반영된 기초자산 수준
        for d in TENORS:
            a, p, c = cm(sf, "atm", d), cm(sf, "p25", d), cm(sf, "c25", d)
            h[f"atm{d}"], h[f"rr{d}"], h[f"bf{d}"] = a, p - c, (p + c) / 2 - a
            h[f"ptail{d}"], h[f"ctail{d}"] = cm(sf, "p10", d) - a, cm(sf, "c10", d) - a
            h[f"fwd{d}"] = cm(sf, "F", d, var=False)
        h["ts_7_30"], h["ts_30_90"] = h["atm7"] - h["atm30"], h["atm30"] - h["atm90"]
        h["atm30_cboe"] = cm(sf, "atm_cboe", 30)
    isc = df["cp"] == "C"
    h["vol_c"], h["vol_p"] = float(df.loc[isc, "vol"].sum()), float(df.loc[~isc, "vol"].sum())
    h["pcr_vol"] = ratio(h["vol_p"], h["vol_c"])
    h["vol0_share"] = ratio(float(df.loc[df["dte"] == 0, "vol"].sum()), h["vol_c"] + h["vol_p"])
    live = df[df["dte"] >= 1]
    lc = live["cp"] == "C"
    otm = live["delta"].abs() <= 0.55                     # 등가격·외가격만(깊은 내가격 거래는 방향성 수요로 보기 어렵다)
    prem = (live["vol"] * live["mid"].fillna(live["last"]) * MULT).where(otm, 0.0)
    h["prem_c"], h["prem_p"] = float(prem[lc].sum()), float(prem[~lc].sum())
    h["pcr_prem"] = ratio(h["prem_p"], h["prem_c"])
    h["oi_c"], h["oi_p"] = float(live.loc[lc, "oi"].sum()), float(live.loc[~lc, "oi"].sum())
    h["pcr_oi"] = ratio(h["oi_p"], h["oi_c"])
    S = h.get("spot", NAN)
    if S == S:
        gex = live["gamma"] * live["oi"] * MULT * S * S * 0.01    # 기초자산 1% 변동 때 델타 변화액(달러). 감마는 파일 값
        h["gex_c"], h["gex_p"] = float(gex[lc].sum()), float(gex[~lc].sum())
        h["gex_net"] = h["gex_c"] - h["gex_p"]               # 관행적 가정: 딜러가 콜은 매수, 풋은 매도 포지션
        for cp, key in (("C", "wall_c"), ("P", "wall_p")):       # 잔존 35일 이내, 외가격 쪽에서 감마x미결제약정이 가장 큰 행사가
            n = live[(live["dte"] <= 35) & (live["cp"] == cp) & ((live["K"] >= S) if cp == "C" else (live["K"] <= S))]
            gk = (n["gamma"] * n["oi"]).groupby(n["K"]).sum()
            h[key] = float(gk.idxmax()) if len(gk) and gk.max() > 0 else NAN
    q = live[live["delta"].abs().between(0.2, 0.5) & live["dte"].between(7, 45) & live["mid"].notna()]
    h["spread"] = float(((q["ask"] - q["bid"]) / q["mid"]).median()) if len(q) else NAN
    return h, sf, smiles


# ----------------------------------------------------------------------------- 두 날짜 비교
def decomp(label, K, mid0, mid1, g, sides, dF, dv, days):
    """옵션 가격 변화를 전일 그릭스로 나눈다: 기초자산(델타+감마) / IV(베가) / 시간(세타) / 나머지. sides = "C", "P" 또는 "CP"(스트래들)."""
    n = len(sides)
    under = sum(g["delta_" + s.lower()] for s in sides) * dF + 0.5 * n * g["gamma"] * dF * dF
    vol = n * g["vega"] * dv * 100
    time = sum(g["theta_" + s.lower()] for s in sides) * days
    return {"label": label, "K": float(K), "mid0": float(mid0), "mid1": float(mid1), "chg": float(mid1 - mid0), "under": float(under),
            "vol": float(vol), "time": float(time), "resid": float(mid1 - mid0 - under - vol - time)}


def pair(d0, d1, s0, s1, days):
    """d0=전 거래일, d1=오늘. s0, s1 = surface() 결과."""
    (sf0, sm0), (sf1, sm1) = s0, s1
    out = {}
    m = d1.merge(d0[["opt", "oi", "mid"]], on="opt", suffixes=("", "_0"))
    live = m[m["dte"] >= 1].copy()
    live["doi"] = live["oi"] - live["oi_0"]
    multi = live["root"].nunique() > 1
    for cp in ("C", "P"):
        s = live[live["cp"] == cp]
        out["doi_" + cp.lower()] = float(s["doi"].sum())
        for name, t in (("oi_up_", s.nlargest(3, "doi")), ("oi_dn_", s.nsmallest(2, "doi"))):
            out[name + cp.lower()] = [{"exp": f"{r.exp:%Y-%m-%d}", "K": float(r.K), "root": r.root if multi else "",
                                       "doi": int(r.doi), "oi": int(r.oi)} for r in t.itertuples() if r.doi != 0]
    both = sf1.merge(sf0[["exp", "F", "atm"]], on="exp", suffixes=("", "_0")).dropna(subset=["F", "F_0", "atm", "atm_0"])
    # 행사가를 고정하고 본 IV 변화(같은 만기·같은 행사가끼리). 기초자산이 움직여 생긴 '곡선 위 이동'을 뺀 실제 재평가
    fp, fc = [], []
    for a in both[both["dte"].between(7, 60)].itertuples():
        (k0, v0, T0, F0), (k1, v1, _, _) = sm0[a.exp], sm1[a.exp]
        common, i0, i1 = np.intersect1d(k0, k1, return_indices=True)
        d1_ = (np.log(F0 / common) + 0.5 * v0[i0] ** 2 * T0) / (v0[i0] * math.sqrt(T0))
        nd = ncdf(d1_)                                        # 콜 델타(할인 전). 풋 델타 = nd - 1
        dv = v1[i1] - v0[i0]
        fp += list(dv[(common < F0) & (nd - 1 <= -0.15)])
        fc += list(dv[(common >= F0) & (nd >= 0.15)])
    out["fs_put"], out["fs_call"] = (float(np.median(fp)) if fp else NAN), (float(np.median(fc)) if fc else NAN)
    for target in (30, 90):
        if both.empty:
            break
        a = both.loc[(both["dte"] - target).abs().idxmin()]
        (k0, v0, T0, F0), (k1, v1, _, F1) = sm0[a["exp"]], sm1[a["exp"]]
        iv0_at_f1 = interp(F1, k0, v0)                        # 어제 곡선에서 오늘 등가격 행사가의 IV
        fs = {"exp": f"{a['exp']:%Y-%m-%d}", "dte": int(a["dte"]), "F0": F0, "F1": F1, "atm0": float(a["atm_0"]), "atm1": float(a["atm"]),
              "slide": iv0_at_f1 - a["atm_0"], "fixed": a["atm"] - iv0_at_f1}
        out[f"fs{target}"] = fs
        if target != 30:
            continue
        g = live[(live["exp"] == a["exp"]) & (live["root"] == a["root"]) & live["mid"].notna() & live["mid_0"].notna()]
        common, i0, i1 = np.intersect1d(k0, k1, return_indices=True)
        if not len(common):
            continue
        nd = ncdf((np.log(F0 / common) + 0.5 * v0[i0] ** 2 * T0) / (v0[i0] * math.sqrt(T0)))
        ex, dF = [], F1 - F0
        for cp, sel, tgt, label in (("P", common < F0, nd - 1 + 0.25, "25델타 풋"), ("C", common >= F0, nd - 0.25, "25델타 콜")):
            r = g[(g["cp"] == cp) & g["K"].isin(common[sel])]
            if len(r):
                i = int(np.argmin(np.where(sel & np.isin(common, r["K"].values), np.abs(tgt), np.inf)))
                x = r[r["K"] == common[i]].iloc[0]
                ex.append(decomp(label, common[i], x["mid_0"], x["mid"], greeks(F0, common[i], T0, v0[i0][i]), cp, dF, v1[i1][i] - v0[i0][i], days))
        gc, gp = g[g["cp"] == "C"].set_index("K"), g[g["cp"] == "P"].set_index("K")
        ks = [x for x in common if x in gc.index and x in gp.index]
        if ks:
            kk = min(ks, key=lambda x: abs(x - F0))
            i = int(np.where(common == kk)[0][0])
            ex.append(decomp("등가격 스트래들(콜+풋)", kk, gc.loc[kk, "mid_0"] + gp.loc[kk, "mid_0"], gc.loc[kk, "mid"] + gp.loc[kk, "mid"],
                             greeks(F0, kk, T0, v0[i0][i]), "CP", dF, v1[i1][i] - v0[i0][i], days))
        out["decomp"] = ex
    return out


def context(hist, key):
    """hist: 날짜순 요약 지표 목록(마지막이 오늘). chg=직전 수집일 대비 변화, x=그 변화가 평소 하루 변동폭(과거 변화 절댓값의
    중앙값)의 몇 배인지, rank=오늘 수준이 기간 안에서 높은 쪽부터 몇 번째인지."""
    v = np.array([h.get(key, NAN) for h in hist], float)
    if len(v) < 2 or v[-1] != v[-1] or v[-2] != v[-2]:
        return {}
    d = np.diff(v)
    past = np.abs(d[:-1])
    past = past[~np.isnan(past)]
    out = {"now": float(v[-1]), "prev": float(v[-2]), "chg": float(d[-1])}
    if len(past) >= 8 and np.median(past) > 0:
        out["x"] = float(abs(d[-1]) / np.median(past))
    lv = v[~np.isnan(v)]
    if len(lv) >= 10:
        out["rank"], out["n"] = int((lv > v[-1]).sum()) + 1, int(len(lv))
    return out


# ----------------------------------------------------------------------------- 날짜·대조
def expected_session(now=None):
    """지금 시점에 마감이 끝난 가장 최근 미국 거래일과 그 마감 시각. exchange_calendars가 있으면 휴장일·조기폐장까지 반영한다."""
    now = now or dt.datetime.now(ET)
    try:
        import exchange_calendars as xc
        cal = xc.get_calendar("XNYS")
        ds = list(cal.sessions_in_range(pd.Timestamp(now.date() - dt.timedelta(days=14)), pd.Timestamp(now.date())))
        close = cal.session_close(ds[-1]).tz_convert(ET)
        if pd.Timestamp(now) < close + pd.Timedelta(minutes=15):
            ds = ds[:-1]
            close = cal.session_close(ds[-1]).tz_convert(ET)
        return ds[-1].strftime("%Y%m%d"), True, close.to_pydatetime()
    except Exception:
        d = now.date() if now.time() >= dt.time(16, 15) else now.date() - dt.timedelta(days=1)
        while d.weekday() >= 5:
            d -= dt.timedelta(days=1)
        return d.strftime("%Y%m%d"), False, dt.datetime.combine(d, dt.time(16, 0), tzinfo=ET)


def fetch_check(t, date, ts):
    """수집 시각이 '마감 직후'인지 본다. 지수 옵션은 미 동부 20:15에 야간 거래가 다시 열려 호가가 바뀐다(시세는 15분 지연)."""
    if not ts:
        return "-", None
    f = dt.datetime.fromtimestamp(ts, ET)
    day = dt.datetime.strptime(date, "%Y%m%d").replace(tzinfo=ET)
    lo = day.replace(hour=16, minute=20)
    hi = day.replace(hour=20, minute=30) if t in INDEX else day + dt.timedelta(days=1, hours=9, minutes=25)
    bad = None if lo <= f <= hi else f"{date} 자료의 수집 시각이 {f:%m-%d %H:%M} ET - 마감 직후 자료가 아닐 수 있음"
    return f"{f:%m-%d %H:%M}", bad


def closes(tickers, d0, d1):
    """야후 파이낸스 종가(대조용). 실패하면 빈 값 - 분석은 계속한다."""
    try:
        import yfinance as yf
        syms = {t: YF.get(t, t.replace(".", "-")) for t in tickers}
        a, b = pd.Timestamp(d0), pd.Timestamp(d1)
        px = yf.download(sorted(set(syms.values())), start=a - pd.Timedelta(days=3), end=b + pd.Timedelta(days=1),
                         auto_adjust=False, progress=False)["Close"]
        px.index = pd.to_datetime(px.index).tz_localize(None).normalize()
        out = {}
        for t, s in syms.items():
            if s in px and a in px.index and b in px.index and px.loc[a, s] == px.loc[a, s] and px.loc[b, s] == px.loc[b, s]:
                out[t] = (float(px.loc[a, s]), float(px.loc[b, s]))
        return out
    except Exception:
        return {}


# ----------------------------------------------------------------------------- 출력
def P(x, n=1):          # IV 수준(%)
    return "-" if x != x else f"{x * 100:.{n}f}%"


def D(x, n=2):          # IV 변화(%포인트)
    return "-" if x != x else f"{x * 100:+.{n}f}%p"


def N(x, n=2):
    return "-" if x != x else f"{x:,.{n}f}"


def ctx_txt(c, pp=True, n=2):
    if not c:
        return "-"
    s = (D(c["chg"], n) if pp else f"{c['chg']:+,.{n}f}")
    if "x" in c:
        s += f", 평소의 {c['x']:.1f}배"
    if "rank" in c:
        s += f", 최근 {c['n']}일 중 {c['rank']}번째로 높음"
    return s


def oi_txt(rows):
    return ", ".join(f"{r['exp'][5:]} {r['root'] + ' ' if r['root'] else ''}{r['K']:g} {r['doi']:+,}(잔고 {r['oi']:,})" for r in rows) or "-"


def report(t, res, d0, d1):
    h, c, pr, cl, p0 = res["today"], res["ctx"], res["pair"], res.get("close"), res["prev"]
    fe = res.get("fetched_et", {})
    L = [f"\n== {t} | {d1} (비교 대상 {d0})  계약 {h['n_contracts']:,}건, 만기 {h['n_exp']}개(호가 부실로 뺀 만기 {h['n_exp_bad']}개)"
         f" | 수집 시각(미 동부) 오늘 {fe.get('today', '-')}, 직전 {fe.get('prev', '-')}"]
    if "spot" not in h or "spot" not in p0:
        return "\n".join(L + ["  [확인 필요] 만기별 곡면을 만들지 못함(호가 부족)"])
    L.append("  [기초자산] " + (f"종가 {N(cl[1])} ({(cl[1] / cl[0] - 1) * 100:+.2f}%)" if cl else "종가 대조 못함")
             + f" | 마감 호가에 반영된 수준(최근월 선도가) {N(h['spot'])} ({(h['spot'] / p0['spot'] - 1) * 100:+.2f}%)"
             + (f" | 종가 대비 {(h['spot'] / cl[1] - 1) * 100:+.2f}%(전일 {(p0['spot'] / cl[0] - 1) * 100:+.2f}%)" if cl else ""))
    L.append(f"  [선도가 곡선] 30일 {N(h['fwd30'])} ({ctx_txt(c.get('fwd30'), False)}) | 90일 {N(h['fwd90'])} ({ctx_txt(c.get('fwd90'), False)})"
             f" | 90일-30일 {h['fwd90'] - h['fwd30']:+,.2f} (전일 {p0['fwd90'] - p0['fwd30']:+,.2f})")
    L.append("  [등가격 IV] " + " | ".join(f"{d}일 {P(h[f'atm{d}'])} ({ctx_txt(c.get(f'atm{d}'))})" for d in (7, 30, 90)) + f" | 180일 {P(h['atm180'])}")
    L.append(f"  [대조: 파일의 Cboe IV 열 기준 30일 등가격] {P(h['atm30_cboe'], 2)} ({ctx_txt(c.get('atm30_cboe'))}) | 호가 기준과의 차이 {D(h['atm30_cboe'] - h['atm30'])} (전일 {D(p0['atm30_cboe'] - p0['atm30'])})")
    L.append(f"  [기간구조] 7일-30일 {D(h['ts_7_30'])} ({ctx_txt(c.get('ts_7_30'))}) | 30일-90일 {D(h['ts_30_90'])} ({ctx_txt(c.get('ts_30_90'))})")
    L.append(f"  [스큐: 25델타 풋 IV - 콜 IV] 30일 {D(h['rr30'])} ({ctx_txt(c.get('rr30'))}) | 90일 {D(h['rr90'])} ({ctx_txt(c.get('rr90'))})")
    L.append(f"  [꼬리: 10델타 IV - 등가격 IV, 30일] 풋 쪽 {D(h['ptail30'])} ({ctx_txt(c.get('ptail30'))}) | 콜 쪽 {D(h['ctail30'])} ({ctx_txt(c.get('ctail30'))})"
             f" | 양 날개 평균-등가격(25델타) {D(h['bf30'])} ({ctx_txt(c.get('bf30'))})")
    for tg in (30, 90):
        f = pr.get(f"fs{tg}")
        if f:
            L.append(f"  [행사가 고정 {tg}일] 만기 {f['exp']}(잔존 {f['dte']}일) 선도가 {N(f['F0'])}→{N(f['F1'])}, 등가격 IV {P(f['atm0'], 2)}→{P(f['atm1'], 2)}"
                     f" = 곡선 위 이동 {D(f['slide'])} + 행사가 고정 변화 {D(f['fixed'])}")
    L.append(f"  [같은 행사가 IV 변화 중앙값, 잔존 7~60일 외가격(델타 0.15~0.5)] 풋 {D(pr['fs_put'])} | 콜 {D(pr['fs_call'])}")
    for e in pr.get("decomp", []):
        L.append(f"  [가격 분해] {e['label']} {e['K']:g}: {N(e['mid0'])}→{N(e['mid1'])} ({e['chg']:+,.2f}) = 기초자산 {e['under']:+,.2f}"
                 f" + IV {e['vol']:+,.2f} + 시간 {e['time']:+,.2f} + 나머지 {e['resid']:+,.2f}")
    L.append(f"  [거래량] 콜 {h['vol_c']:,.0f} / 풋 {h['vol_p']:,.0f} | 풋÷콜 {N(h['pcr_vol'])} ({ctx_txt(c.get('pcr_vol'), False)})"
             f" | 만기 당일 계약 비중 {h['vol0_share'] * 100:.0f}%")
    L.append(f"  [거래대금(등가격·외가격, 만기 당일 제외)] 콜 ${h['prem_c'] / 1e6:,.0f}M / 풋 ${h['prem_p'] / 1e6:,.0f}M | 풋÷콜 {N(h['pcr_prem'])} ({ctx_txt(c.get('pcr_prem'), False)})")
    L.append(f"  [미결제약정] 콜 {h['oi_c']:,.0f} / 풋 {h['oi_p']:,.0f} | 풋÷콜 {N(h['pcr_oi'])} ({ctx_txt(c.get('pcr_oi'), False)})"
             f" | 같은 계약 증감 합계: 콜 {pr['doi_c']:+,.0f} / 풋 {pr['doi_p']:+,.0f}")
    L.append(f"    증가 상위 콜: {oi_txt(pr['oi_up_c'])}")
    L.append(f"    증가 상위 풋: {oi_txt(pr['oi_up_p'])}")
    L.append(f"    감소 상위 콜: {oi_txt(pr['oi_dn_c'])} / 풋: {oi_txt(pr['oi_dn_p'])}")
    if "gex_net" in h:
        g = c.get("gex_net", {})
        L.append(f"  [감마(가정 기반 추정)] 순 감마 ${h['gex_net'] / 1e9:+,.2f}bn/1%"
                 + (f" (전일 ${g['prev'] / 1e9:+,.2f}bn)" if g else "") + f" | 콜 ${h['gex_c'] / 1e9:,.2f}bn, 풋 ${h['gex_p'] / 1e9:,.2f}bn"
                 f" | 잔존 35일 이내 감마 최대 행사가: 위쪽 콜 {h['wall_c']:g}, 아래쪽 풋 {h['wall_p']:g}")
    L.append(f"  [호가 스프레드 중앙값(잔존 7~45일, 델타 0.2~0.5)] {h['spread'] * 100:.2f}% ({ctx_txt(c.get('spread'))})")
    for f in res.get("flags", []):
        L.append(f"  [확인 필요] {f}")
    return "\n".join(L)


def clean(o):
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, (float, np.floating)):
        return None if o != o else round(float(o), 6)
    if isinstance(o, np.integer):
        return int(o)
    return o


# ----------------------------------------------------------------------------- 실행
def run(a):
    dates = list_dates(a.archive_dir)
    if len(dates) < 2:
        print("[오류] 보관본이 2개 미만이라 비교할 수 없음")
        return 1
    if a.date:
        if a.date not in dates:
            print(f"[오류] {a.date} 보관본이 없음")
            return 1
        dates = [d for d in dates if d <= a.date]
        if len(dates) < 2:
            print(f"[오류] {a.date} 이전 보관본이 없음")
            return 1
    else:
        now = dt.datetime.now(ET)
        exp, cal, close = expected_session(now)
        hrs = (now - close).total_seconds() / 3600
        print(f"[세션] 최신 보관본 {dates[-1]} / 마감이 끝난 최근 미국 거래일 {exp}(마감 후 {hrs:.1f}시간) / 휴장일 반영 {'예' if cal else '아니오(달력 라이브러리 없음)'}")
        if hrs > 24 and not a.force:
            print(f"[휴장] 최근 24시간 안에 마감한 미국 거래일이 없음 - 새 자료 없음(마지막 거래일 {exp})")
            return 4
        if dates[-1] < exp and not a.force:
            print(f"[수집본 없음] {exp} 보관본이 저장소에 없음(수집 실패, 또는 달력 라이브러리가 없으면 휴장일 수 있음). 가장 최근 보관본은 {dates[-1]}")
            return 3
    d1, d0 = dates[-1], dates[-2]
    hist_dates = dates[-(a.lookback + 1):]
    gap = int(np.busday_count(dt.datetime.strptime(d0, "%Y%m%d").date(), dt.datetime.strptime(d1, "%Y%m%d").date()))
    days = (dt.datetime.strptime(d1, "%Y%m%d") - dt.datetime.strptime(d0, "%Y%m%d")).days
    print(f"[비교] 오늘 {d1} vs 직전 수집본 {d0} (평일 기준 {gap}일 전, 달력 {days}일) / 추이 비교 {len(hist_dates)}일치({hist_dates[0]}~)")
    tickers = [t.upper() for t in a.tickers]
    hist, keep = {t: [] for t in tickers}, {}
    for d in hist_dates:
        try:
            day = load_day(a.archive_dir, d, tickers)
        except Exception as e:
            print(f"[확인 필요] {d} 보관본을 읽지 못함: {type(e).__name__}")
            day = {}
        for t in tickers:
            if t in day and len(day[t]):
                h, sf, sm = headline(day[t])
                hist[t].append(dict(h, date=d))
                if d in (d0, d1):
                    keep[(t, d)] = (day[t], (sf, sm), day[t].attrs.get("fetched"))
    cl = {} if a.no_yf else closes(tickers, d0, d1)
    result = {"date": d1, "prev": d0, "gap_weekdays": gap, "lookback_days": len(hist_dates), "tickers": {}}
    for t in tickers:
        if (t, d0) not in keep or (t, d1) not in keep:
            print(f"\n== {t}: [확인 필요] {d0} 또는 {d1} 보관본에 이 종목이 없음")
            continue
        (df0, s0, ts0), (df1, s1, ts1) = keep[(t, d0)], keep[(t, d1)]
        (f0, bad0), (f1, bad1) = fetch_check(t, d0, ts0), fetch_check(t, d1, ts1)
        res = {"today": hist[t][-1], "prev": hist[t][-2], "ctx": {k: context(hist[t], k) for k in CTX_KEYS},
               "pair": pair(df0, df1, s0, s1, days), "close": cl.get(t), "fetched_et": {"today": f1, "prev": f0},
               "flags": [b for b in (bad1, bad0) if b]}
        h, past = res["today"], hist[t][:-1]
        if gap > 1:
            res["flags"].append(f"직전 수집본이 {gap}거래일 전 자료라 하루치 변화가 아님")
        if len(df1) == len(df0) and (df1["oi"].values == df0["oi"].values).all() and (df1["bid"].values == df0["bid"].values).all():
            res["flags"].append("두 날짜의 호가·미결제약정이 완전히 같음 - 자료가 갱신되지 않았을 수 있음")
        if cl.get(t) and t != "VIX" and "spot" in h and abs(h["spot"] / cl[t][1] - 1) > 0.006:
            res["flags"].append(f"마감 호가에 반영된 기초자산 수준이 종가와 {(h['spot'] / cl[t][1] - 1) * 100:+.2f}% 다름 - 마감 뒤 변동이 반영됐거나 자료 시점이 마감이 아닐 수 있음")
        if h["vol_c"] + h["vol_p"] == 0:
            res["flags"].append("거래량이 전부 0")
        gaps = np.array([abs(x.get("atm30_cboe", NAN) - x.get("atm30", NAN)) for x in past], float)
        gaps = gaps[~np.isnan(gaps)]
        gap30 = h.get("atm30_cboe", NAN) - h.get("atm30", NAN)
        if gap30 == gap30 and abs(gap30) > 0.015 * h["atm30"] and (len(gaps) < 8 or abs(gap30) > 3 * np.median(gaps)):
            res["flags"].append(f"파일의 Cboe IV 열이 호가에서 계산한 IV와 {D(gap30)} 어긋남(30일 등가격, 평소 차이 {D(float(np.median(gaps))) if len(gaps) else '-'})"
                                " - 이 날은 파일의 IV·델타 값을 그대로 비교하면 변화가 왜곡된다")
        sp = np.array([x.get("spread", NAN) for x in past], float)
        sp = sp[~np.isnan(sp)]
        if len(sp) >= 8:
            wide = [f"{lab} {x['spread'] * 100:.1f}%" for lab, x in (("오늘", h), ("직전", res["prev"]))
                    if x.get("spread", NAN) == x.get("spread", NAN) and x["spread"] > max(2 * np.median(sp), 0.03)]
            if wide:
                res["flags"].append(f"호가 간격이 평소({np.median(sp) * 100:.1f}%)보다 넓음: {', '.join(wide)} - 이 종목의 IV 변화는 오차가 클 수 있음")
        result["tickers"][t] = res
        print(report(t, res, d0, d1))
    if a.out_json:
        with open(a.out_json, "w", encoding="utf-8") as f:
            json.dump(clean(result), f, ensure_ascii=False, indent=1)
        print(f"\nJSON: {a.out_json}")
    return 0 if result["tickers"] else 1


def self_test():
    """접속 없이 도는 점검: 이론가로 만든 체인에서 선도가·IV·스큐·행사가 고정 변화·가격 분해가 맞게 나오는지 본다."""
    from statistics import NormalDist
    assert abs(float(ncdf(0.3)) - NormalDist().cdf(0.3)) < 1e-6 and abs(float(ncdf(-1.7)) - NormalDist().cdf(-1.7)) < 1e-6
    T = years(28, "ZZ")
    c0, p0 = float(b76(100.0, 104.0, T, 0.2, 1.0)), float(b76(100.0, 104.0, T, 0.2, -1.0))
    assert abs((c0 - p0) - math.exp(-R * T) * (100.0 - 104.0)) < 1e-6                      # 풋-콜 패리티
    assert abs(float(implied_vol(100.0, np.array([104.0]), T, np.array([c0]), np.array([1.0]))[0]) - 0.2) < 1e-6
    g = greeks(100.0, 104.0, T, 0.2)
    bump = (float(b76(100.5, 104.0, T, 0.2, 1.0)) - float(b76(99.5, 104.0, T, 0.2, 1.0)))
    vbump = float(b76(100.0, 104.0, T, 0.205, 1.0)) - float(b76(100.0, 104.0, T, 0.195, 1.0))
    assert abs(g["delta_c"] - bump) < 1e-3 and abs(g["vega"] - vbump) < 1e-3, (g, bump, vbump)

    def chain(date, F, base):
        rows = []
        for ymd, dte in (("261211", 28), ("270115", 63)):
            for K in range(60, 141, 2):
                v = base + 0.002 * (100 - K)          # 행사가가 낮을수록 IV가 높은 곡선
                for cp, sgn in (("C", 1.0), ("P", -1.0)):
                    px = float(b76(F, float(K), years(dte, "ZZ"), v, sgn))
                    rows.append({"Symbol": f"ZZ{ymd}{cp}{K * 1000:08d}", "Expiry": f"20{ymd[:2]}-{ymd[2:4]}-{ymd[4:]}",
                                 "Type": "Call" if cp == "C" else "Put", "Strike": K, "Bid": px * 0.99, "Ask": px * 1.01, "Last": px,
                                 "Volume": 10 if cp == "C" else 20, "Open Interest": 100, "IV": v + 0.01, "Delta": 0.3 * sgn,
                                 "Gamma": 0.01, "Theta": -0.02, "Vega": 0.1, "Rho": 0})
        return prep(pd.DataFrame(rows), "ZZ", date)

    d0, d1 = chain("20261113", 100.0, 0.20), chain("20261113", 98.0, 0.22)
    h0, sf0, sm0 = headline(d0)
    assert abs(sf0["F"].iloc[0] - 100.0) < 0.01 and abs(h0["atm30"] - 0.20) < 0.0005, (sf0["F"].iloc[0], h0["atm30"])
    assert abs(h0["atm30_cboe"] - 0.21) < 0.0005 and 0.01 < h0["rr30"] < 0.03 and abs(h0["pcr_vol"] - 2.0) < 1e-9
    assert abs(cm(pd.DataFrame({"dte": [20, 40], "x": [0.2, 0.2]}), "x", 30) - 0.2) < 1e-12
    h1, sf1, sm1 = headline(d1)
    pr = pair(d0, d1, (sf0, sm0), (sf1, sm1), 1)
    f = pr["fs30"]
    assert abs(f["slide"] - 0.004) < 0.0005 and abs(f["fixed"] - 0.02) < 0.0005, f       # 2포인트 하락 x 0.002 = +0.4%p, 곡선 전체 +2%p
    assert abs(pr["fs_put"] - 0.02) < 1e-4 and abs(pr["fs_call"] - 0.02) < 1e-4 and len(pr["decomp"]) == 3, pr
    for e in pr["decomp"]:
        assert abs(e["chg"] - (e["under"] + e["vol"] + e["time"] + e["resid"])) < 1e-9 and abs(e["resid"]) < 0.25 * abs(e["chg"]) + 0.05, e
    c = context([{"x": float(i % 3)} for i in range(12)], "x")
    assert abs(c["x"] - 1.0) < 1e-9 and c["rank"] == 1 and expected_session(dt.datetime(2026, 11, 14, 9, 0, tzinfo=ET))[0] == "20261113"
    print("자체 점검 통과")
    return 0


def main():
    if "--self-test" in sys.argv:
        return self_test()
    p = argparse.ArgumentParser(description="cboe_data 보관본으로 옵션 지표의 전일 대비 변화 계산")
    p.add_argument("--archive-dir", default="data/daily", help="quotes_YYYYMMDD.tar.gz가 있는 폴더")
    p.add_argument("--tickers", nargs="+", default=["SPX", "VIX", "GLD"])
    p.add_argument("--date", help="분석 기준일 YYYYMMDD(생략하면 최신 보관본. 생략 시 휴장·수집 누락을 점검한다)")
    p.add_argument("--lookback", type=int, default=20, help="추이 비교에 쓸 과거 수집일 수(폴더에 있는 만큼만 쓴다)")
    p.add_argument("--out-json")
    p.add_argument("--no-yf", action="store_true", help="야후 종가 대조를 하지 않음")
    p.add_argument("--force", action="store_true", help="휴장·수집 누락이어도 최신 보관본으로 분석")
    return run(p.parse_args())


if __name__ == "__main__":
    sys.exit(main())
