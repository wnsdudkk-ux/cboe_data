#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vix_decomp.py - 보관본의 SPX 옵션 호가로 VIX를 직접 계산하고, 하루 변화를 Cboe 'VIX Decomposition' 도구와 같은 여섯 요인으로 나눈다.

쓰임: option_sentiment.py가 SPX를 분석할 때 함께 부른다. 단독 실행도 된다.
예:   python3 vix_decomp.py --archive-dir data/daily --png vix.png --out-json vix.json
      python3 vix_decomp.py --archive-dir data/daily --date 20261113 --png vix.png
필요: pandas, numpy (선택: matplotlib 그림, yfinance 종가, exchange_calendars 조기 폐장일)

계산 기준
- VIX: Cboe 방법론 문서(Volatility Index Methodology: Cboe Volatility Index, Mathematics Methodology)의 규칙대로 계산한다.
  만기 2개(잔존 30일 이하에서 가장 먼 주말 만기와 그다음 주말 만기) / 선도가 F = 콜·풋 가격 차가 가장 작은 행사가에서 계산 /
  K0 = F 이하 최대 행사가 / K0 아래는 풋, 위는 콜 / 매수호가나 매도호가가 0이면 제외, 연달아 두 번 0이면 그 바깥은 버림 /
  분산 = (2/T) x 합(dK/K^2 x e^(RT) x 호가 중간값) - (1/T)(F/K0 - 1)^2 / 두 만기를 30일로 보간.
  공식 값과 다른 점은 금리뿐이다(공식은 미 국채 수익률, 여기는 고정값 R. 1%포인트 차이에 VIX 0.01 안팎).
  보관본의 SPX 호가는 SPX 옵션 정규장 마감(미 동부 16:15) 시점 값이라 직접 계산한 값이 공식 VIX 종가와 거의 같다
  (2026-07-06~11-13의 86일 가운데 자료 시점이 어긋난 2일을 뺀 84일: 차이 평균 +0.003, 표준편차 0.007, 최대 0.03).
- 30일 곡선: 두 만기의 행사가별 IV(외가격 쪽 호가 중간값에서 역산)를 '분산 x 일수'로 보간해 잔존 30일짜리 곡선 하나로 만든다
  (Cboe VIX Decomposition 백서 부록의 방식). 곡선 범위를 Cboe 도구와 같게 하려고 매수호가가 0인 옵션도 넣는다.
- 요인 여섯 개(합 = 직접 계산한 VIX의 하루 변화. 정확히 일치한다)
    곡선 따라 이동(sticky strike) = 어제 곡선에서 [오늘 등가격 행사가의 IV - 어제 등가격 행사가의 IV]
    평행 이동(parallel shift)     = 오늘 등가격 행사가에서 [오늘 곡선의 IV - 어제 곡선의 IV]
    풋 스큐·콜 스큐·하방 볼록성·상방 볼록성 = 'VIX - 등가격 IV'의 변화를 네 구간의 몫으로 나눈 것.
      그날의 델타로 구간을 정한다(스큐 = 델타 10~50, 볼록성 = 델타 10 미만). 구간의 몫 = 그 구간 옵션이 VIX에 기여한 포인트
      - 곡선이 평평했다면(모든 행사가의 IV가 등가격 IV와 같다면) 기여했을 포인트.
  앞의 두 요인은 Cboe 도구와 정의가 같다. 도구가 쓰는 공개 자료(www-api.cboe.com/vix_decomposition/: 요인, 등가격 행사가,
  날짜별 30일 곡선)와 2026-07-07~11-16의 82개 날짜 쌍을 맞대어 보면, 30일 곡선은 등가격 ±10%에서 평균 0.015%포인트 차이로 같고,
  두 요인의 오차(평균제곱근)는 곡선 따라 이동 0.01, 평행 이동 0.02 안팎이다(README).
  뒤의 네 요인은 Cboe가 계산식을 공개하지 않아 같은 틀로 직접 정의한 것이라 도구의 값과 다르다
  (같은 82개 쌍의 오차: 풋 스큐 0.21, 콜 스큐 0.39, 하방 볼록성 0.41, 상방 볼록성 0.24. 백서 설명대로 다시 계산해도 맞지 않는다. README).
- 등가격 행사가: 두 VIX 만기의 선도가를 잔존 30일로 보간한 값(옵션에서 역산한 30일 선도가)을 5단위로 반올림한 행사가.
  Cboe 도구의 등가격 행사가와 87일 가운데 84일 같다. 선도가를 만들지 못하면 S&P 500 종가를 5단위로 반올림한다.
"""
import argparse
import datetime as dt
import json
import math
import os
import sys

import numpy as np
import pandas as pd

from option_sentiment import ET, R, b76, implied_vol, list_dates, load_day, ncdf

M365, M30 = 525600.0, 43200.0          # 365일, 30일의 분 수
# 기초자산별 계산 설정. 같은 VIX 산출 방법론을 SPX 옵션(VIX), VIX 옵션(VVIX), GLD 옵션(GVZ)에 쓴다.
#   roots: 루트별 만기 시각(미 동부). SPX·VIX 옵션은 아침 결제(9:30), SPXW·GLD는 종가 결제(16:00)
#   lag: 호가 시점 = 주식시장 마감 + lag분(SPX·VIX 옵션은 16:15에 끝나고 GLD 옵션은 16:00에 끝난다)
#   rule: 두 만기를 고르는 규칙(pick_terms). 공식 지수와 맞대어 고른 것이다(2026-07-06~11-16, 87일, README)
#     weekly30  주말(금요일, 휴장이면 목요일) 만기 중 잔존 30일 이하에서 가장 먼 것과 그다음(지금의 VIX 방법론). 공식 VIX와 차이 0.01 안팎
#     classic8  월물(VIX 옵션의 표준 만기)만, 잔존 8일 이상인 첫 두 만기(옛 VIX 방법론, VVIX 방식). 공식 VVIX와 차이 중앙값 0.01, 90% 0.04
#     monthly30 월물(셋째 금요일)만, 잔존 8~30일에서 가장 먼 것과 30일 넘는 첫 만기. 30일 이하가 없으면 30일 넘는 첫 두 만기로 바깥 보간
#               (GVZ 방식). 공식 GVZ와 차이 중앙값 0.02, 90% 0.12
#   step: 등가격 행사가 = 30일 선도가를 이 단위로 반올림. SPX는 Cboe 도구와 같은 5
#   tol: 직접 계산한 지수가 공식 값과 이만큼 넘게 다르면 [확인 필요]로 알린다
SPECS = {
    "SPX": {"roots": {"SPX": (9, 30), "SPXW": (16, 0)}, "lag": 15, "rule": "weekly30", "step": 5.0, "tol": 0.05,
            "under": "S&P 500", "index": "VIX", "yahoo": ("^GSPC", "^VIX"), "opt": "SPX 옵션"},
    "VIX": {"roots": {"VIX": (9, 30)}, "lag": 15, "rule": "classic8", "step": 0.5, "tol": 0.3,
            "under": "VIX", "index": "VVIX", "yahoo": ("^VIX", "^VVIX"), "opt": "VIX 옵션"},
    "GLD": {"roots": {"GLD": (16, 0)}, "lag": 0, "rule": "monthly30", "step": 1.0, "tol": 0.3,
            "under": "GLD", "index": "GVZ", "yahoo": ("GLD", "^GVZ"), "opt": "GLD 옵션"},
}
VIX_ROOTS = SPECS["SPX"]["roots"]       # 예전 이름(시험·다른 스크립트가 쓴다)
WING = 0.10                            # 이 델타 미만을 '볼록성(먼 외가격)' 구간으로 본다
NB = 10                                # 델타 5단위 구간 수(한쪽)
ATM_STEP = SPECS["SPX"]["step"]        # 등가격 행사가 = 30일 선도가를 이 단위로 반올림(Cboe 도구와 같은 규칙)
FACTORS = ["sticky", "parallel", "put_skew", "call_skew", "down_conv", "up_conv"]
REGION = {"put_skew": "put_body", "call_skew": "call_body", "down_conv": "put_wing", "up_conv": "call_wing"}
# 풋 쪽(풋 스큐+하방 볼록성)·콜 쪽(콜 스큐+상방 볼록성) 요인이 등가격 IV 변화(곡선 따라 이동+평행 이동)에 딸려 움직이는 비율.
# SPX(이 스크립트의 정의): 2026-07-06~11-13의 85개 날짜 쌍에서 풋 쪽 +0.62배(상관 0.89), 콜 쪽 -0.32배(상관 -0.93)였다
# (등가격 행사가를 30일 선도가 기준으로 바꾼 뒤 84개 쌍으로 다시 재도 +0.64배(0.91), -0.32배(-0.93)).
# VIX·GLD: 같은 정의로 2026-07-07~11-16의 84개 날짜 쌍에서 잰 값. VIX 옵션 풋 쪽 -0.21배(상관 -0.84), 콜 쪽 +0.07배(+0.30),
#   GLD 풋 쪽 +0.11배(+0.30), 콜 쪽 -0.02배(-0.07). SPX_CBOE: Cboe 도구 공식값 95일치에서 잰 값. 풋 쪽 +0.40배(+0.74), 콜 쪽 -0.09배(-0.33).
#   상관이 0.4보다 약한 관계는 0으로 둔다(그날 값을 평소 크기와 그대로 견준다)
SIDE_BETAS = {"SPX": {"put": 0.6, "call": -0.3}, "VIX": {"put": -0.2, "call": 0.0}, "GLD": {"put": 0.0, "call": 0.0},
              "SPX_CBOE": {"put": 0.4, "call": 0.0}}
SIDE_BETA = SIDE_BETAS["SPX"]
CBOE_API = "https://www-api.cboe.com/vix_decomposition/"   # Cboe VIX Decomposition 도구 페이지가 읽는 공개 자료
CBOE_KEYS = {"stickyStrike": "sticky", "parallelShift": "parallel", "putSkew": "put_skew", "callSkew": "call_skew",
             "downsideConvexity": "down_conv", "upsideConvexity": "up_conv"}
NAN = float("nan")


# ----------------------------------------------------------------------------- 시각·만기
def quote_time(date, lag=15):
    """호가 시점(미 동부, 벽시계) = 주식시장 마감 + lag분(SPX·VIX 옵션은 보통 16:15, GLD 옵션은 16:00). 조기 폐장일은 달력 라이브러리가
    있을 때만 반영한다. 보관본의 SPX 호가는 이 시각의 값이다(직접 계산한 VIX가 공식 종가와 0.01~0.03 차이로 맞는 것으로 확인)."""
    try:
        import exchange_calendars as xc
        close = xc.get_calendar("XNYS").session_close(pd.Timestamp(date)).tz_convert(ET)
        return close.to_pydatetime().replace(tzinfo=None) + dt.timedelta(minutes=lag)
    except Exception:
        return dt.datetime.strptime(date, "%Y%m%d").replace(hour=16, minute=0) + dt.timedelta(minutes=lag)


def pick_terms(df, date, spec="SPX"):
    """지수 계산에 쓰는 두 만기 [(만기일, 루트), (만기일, 루트)]. 규칙은 SPECS[spec]["rule"](설명은 SPECS 위 주석).
    weekly30: 주말(금요일, 금요일이 휴장이면 목요일) 만기만 후보. 같은 날 SPX(아침 결제)와 SPXW가 함께 있으면 SPX를 쓴다.
    근월 = 잔존 30일 이하 중 가장 먼 것, 차월 = 그다음."""
    roots, rule = SPECS[spec]["roots"], SPECS[spec]["rule"]
    e = df[df["root"].isin(list(roots))][["exp", "root"]].drop_duplicates()
    if e.empty:
        return None
    wd, day = e["exp"].dt.weekday, e["exp"].dt.day
    if rule == "weekly30":
        week = e["exp"] - pd.to_timedelta(wd, unit="D")
        fri_weeks = set(week[wd == 4])
        e = e[(wd == 4) | ((wd == 3) & ~week.isin(fri_weeks))]
    elif rule == "monthly30":                                        # 셋째 금요일(그날이 휴장이면 전날 목요일)
        e = e[((wd == 4) & (day >= 15) & (day <= 21)) | ((wd == 3) & (day >= 14) & (day <= 20))]
    e = e.sort_values(["exp", "root"]).drop_duplicates("exp")          # "SPX" < "SPXW"
    dte = (e["exp"] - pd.Timestamp(date)).dt.days
    if rule == "classic8":
        two = e[dte >= 8].head(2)
        return [tuple(x) for x in two[["exp", "root"]].itertuples(index=False)] if len(two) == 2 else None
    if rule == "monthly30":
        near, nxt = e[(dte <= 30) & (dte >= 8)].tail(1), e[dte > 30].head(1)
        if near.empty:
            two = e[dte > 30].head(2)
            return [tuple(x) for x in two[["exp", "root"]].itertuples(index=False)] if len(two) == 2 else None
    else:
        near, nxt = e[dte <= 30].tail(1), e[dte > 30].head(1)
    if near.empty or nxt.empty or int(dte[near.index[0]]) < 1:
        return None
    return [(near["exp"].iloc[0], near["root"].iloc[0]), (nxt["exp"].iloc[0], nxt["root"].iloc[0])]


# ----------------------------------------------------------------------------- 만기 하나의 분산
def strip_var(K, Q, F, T, K0):
    """정렬된 행사가 띠 (K, Q)의 분산. Q는 외가격 쪽 가격(K0는 콜·풋 평균). 옵션별 기여(보정항 제외)도 돌려준다."""
    dK = np.empty(len(K))
    dK[1:-1] = (K[2:] - K[:-2]) / 2
    dK[0], dK[-1] = K[1] - K[0], K[-1] - K[-2]
    c = (2 / T) * dK / (K * K) * math.exp(R * T) * Q
    return float(c.sum() - (F / K0 - 1) ** 2 / T), c


def term(df, exp, root, tv, roots=None):
    """만기 하나의 VIX 계산. tv = 호가 시점, roots = 루트별 만기 시각(생략하면 SPX). 계산할 수 없으면 None."""
    roots = roots or VIX_ROOTS
    g = df[(df["exp"] == exp) & (df["root"] == root)]
    side = {}
    for cp in ("C", "P"):
        s = g[g["cp"] == cp].drop_duplicates("K").sort_values("K")
        b, a = s["bid"].values.astype(float), s["ask"].values.astype(float)
        side[cp] = (s["K"].values.astype(float), (b > 0) & (a > 0) & (a >= b), (b + a) / 2)
    (kc, okc, mc), (kp, okp, mp) = side["C"], side["P"]
    kk, ic, ip = np.intersect1d(kc, kp, return_indices=True)
    both = okc[ic] & okp[ip]
    if both.sum() < 3:
        return None
    h, m = roots[root]
    minutes = math.floor((exp.to_pydatetime().replace(hour=h, minute=m) - tv).total_seconds() / 60)
    if minutes <= 0:
        return None
    T = minutes / M365
    diff = np.where(both, np.abs(mc[ic] - mp[ip]), np.inf)
    i = int(np.argmin(diff))                                        # 동률이면 낮은 행사가
    F = float(kk[i] + math.exp(R * T) * (mc[ic][i] - mp[ip][i]))
    below = np.flatnonzero(both & (kk <= F))
    if not len(below):
        return None
    K0 = float(kk[below[-1]])                                       # F 이하에서 호가가 온전한 가장 큰 행사가
    k0_listed = float(kk[kk <= F][-1])

    def walk(k, ok, order):
        keep, zeros = [], 0
        for n in order:
            if ok[n]:
                keep.append(n)
                zeros = 0
            else:
                zeros += 1
                if zeros == 2:
                    break
        return np.array(sorted(keep), dtype=int)

    lo = np.flatnonzero(kp < K0)
    hi = np.flatnonzero(kc > K0)
    pi, ci = walk(kp, okp, lo[::-1]), walk(kc, okc, hi)
    if not len(pi) or not len(ci):
        return None
    q0p, q0c = float(mp[ip][below[-1]]), float(mc[ic][below[-1]])
    K = np.concatenate([kp[pi], [K0], kc[ci]])
    Q = np.concatenate([mp[pi], [(q0p + q0c) / 2], mc[ci]])
    cp = np.concatenate([-np.ones(len(pi)), [0.0], np.ones(len(ci))])
    s2, c = strip_var(K, Q, F, T, K0)
    # 옵션별 IV와 델타(호가에서 역산). K0는 풋·콜 IV의 평균
    iv = implied_vol(F, K, T, Q, np.where(cp < 0, -1.0, 1.0))
    iv0 = np.array([implied_vol(F, np.array([K0]), T, np.array([q]), np.array([s]))[0] for q, s in ((q0p, -1.0), (q0c, 1.0))])
    n0 = len(pi)
    iv[n0] = np.nanmean(iv0) if not np.isnan(iv0).all() else NAN
    ok = ~np.isnan(iv)
    ivf = np.interp(K, K[ok], iv[ok]) if ok.sum() >= 2 else np.full(len(K), NAN)     # 역산이 안 된 곳은 이웃 값으로 메운다
    sd = ivf * math.sqrt(T)
    with np.errstate(divide="ignore", invalid="ignore"):
        nd1 = ncdf((np.log(F / K) + 0.5 * sd * sd) / sd)             # 콜 델타(할인 전). 풋 델타의 크기 = 1 - nd1
    nd1 = np.where(np.isnan(nd1), np.where(K < K0, 1.0, 0.0), nd1)   # IV를 못 구한 옵션은 가장 바깥 구간으로 보낸다
    isp, isc = cp <= 0, cp >= 0                                      # K0는 양쪽 곡선에 모두 넣는다
    return {"exp": exp, "root": root, "minutes": minutes, "T": T, "F": F, "K0": K0, "K": K, "Q": Q, "cp": cp, "c": c,
            "sigma2": s2, "iv": ivf, "corr": (F / K0 - 1) ** 2 / T,
            "dcurve": ((K[isp], 1 - nd1[isp]), (K[isc], nd1[isc])),  # (행사가, 델타 크기): 풋 쪽, 콜 쪽
            "n_put": int(len(pi)), "n_call": int(len(ci)), "k_lo": float(K[0]), "k_hi": float(K[-1]),
            "k0_shift": K0 != k0_listed, "iv_nan": int((~ok).sum())}


def weights(m1, m2):
    """두 만기를 30일로 보간할 때 각 만기의 분산(sigma^2)에 곱하는 값."""
    w1, w2 = (m2 - M30) / (m2 - m1), (M30 - m1) / (m2 - m1)
    return w1 * m1 / M30, w2 * m2 / M30                             # w x T x (365일/30일), T = 분/525600


def blend(s1, s2, m1, m2):
    a1, a2 = weights(m1, m2)
    v = a1 * s1 + a2 * s2
    return 100 * math.sqrt(v) if v > 0 else NAN


# ----------------------------------------------------------------------------- 하루치: 수준과 델타 구간별 기여
def _to_bins(K, cp, v, dcurve, k0_extra=0.0):
    """옵션별 분산 기여 v를 [풋, 콜] x 델타 5단위 구간에 담는다. 각 옵션은 이웃 행사가와의 중간점까지를 맡고 있으므로,
    그 행사가 구간을 델타로 바꿔 5단위 구간과 겹치는 비율대로 나눈다(구간 경계에 걸친 옵션 때문에 생기는 들쭉날쭉함을 없앤다).
    dcurve = 오늘의 (행사가→델타) 대응. K0 옵션(cp == 0)은 보정항 k0_extra와 함께 풋·콜에 반씩 넣는다."""
    edge = np.concatenate([[K[0] - (K[1] - K[0]) / 2], (K[:-1] + K[1:]) / 2, [K[-1] + (K[-1] - K[-2]) / 2]])
    bins = np.zeros((2, NB))
    top = NB - 1e-9
    for s, sgn in ((0, -1.0), (1, 1.0)):
        sel = cp == sgn
        if not sel.any():
            continue
        kx, dx = dcurve[s]
        a, b = np.interp(edge[:-1][sel], kx, dx) * 20, np.interp(edge[1:][sel], kx, dx) * 20     # 구간 번호 단위(5델타 = 1)
        lo, hi = np.clip(np.minimum(a, b), 0, top), np.clip(np.maximum(a, b), 0, top)
        width = hi - lo
        for n in range(NB):
            over = np.clip(np.minimum(hi, n + 1) - np.maximum(lo, n), 0, None)
            share = np.where(width > 1e-9, over / np.maximum(width, 1e-9), (np.floor(lo) == n).astype(float))
            bins[s, n] += float((v[sel] * share).sum())
    k0 = float(v[cp == 0].sum()) + k0_extra
    bins[:, NB - 1] += k0 / 2
    return bins


def skew30(df, terms):
    """잔존 30일로 맞춘 행사가별 IV 곡선 (행사가 배열, IV 배열). 두 만기에 모두 있는 행사가만 쓴다. 만들 수 없으면 None."""
    sm = []
    for t in terms:
        g = df[(df["exp"] == t["exp"]) & (df["root"] == t["root"])]
        ks, vs = [], []
        for cp, sgn in (("P", -1.0), ("C", 1.0)):
            s = g[g["cp"] == cp].drop_duplicates("K")
            s = s[(s["K"] < t["F"]) if cp == "P" else (s["K"] >= t["F"])]
            s = s[(s["ask"] > 0) & (s["ask"] >= s["bid"])]
            k = s["K"].values.astype(float)
            ks.append(k)
            vs.append(implied_vol(t["F"], k, t["T"], ((s["bid"] + s["ask"]) / 2).values.astype(float), np.full(len(k), sgn)) if len(k) else k)
        k, v = np.concatenate(ks), np.concatenate(vs)
        ok = ~np.isnan(v)
        o = np.argsort(k[ok])
        sm.append((k[ok][o], v[ok][o]))
    K = np.intersect1d(sm[0][0], sm[1][0])
    if len(K) < 5:
        return None
    d1, d2 = (t["minutes"] / 1440 for t in terms)
    w1, w2 = (d2 - 30) / (d2 - d1), (30 - d1) / (d2 - d1)
    v1, v2 = np.interp(K, *sm[0]), np.interp(K, *sm[1])
    return K, np.sqrt(np.maximum((w1 * v1 * v1 * d1 + w2 * v2 * v2 * d2) / 30, 0.0))


def implied_spot(df, date, tv, spec="SPX"):
    """옵션에서 역산한 기초자산 수준: 잔존 1일 이상인 가장 가까운 만기의 선도가(보유 비용이 며칠치뿐이라 현물과 거의 같다).
    VIX 옵션은 만기마다 기초자산(VIX 선물)이 달라 가장 가까운 만기의 VIX 선물 수준이 된다."""
    roots = SPECS[spec]["roots"]
    e = df[df["root"].isin(list(roots)) & (df["dte"] >= 1)][["exp", "root"]].drop_duplicates().sort_values(["exp", "root"])
    for exp, root in list(e.itertuples(index=False))[:4]:
        t = term(df, exp, root, tv, roots)
        if t is not None:
            return t["F"]
    return NAN


def level(df, date, spec="SPX"):
    """하루치 옵션 호가로 VIX식 지수(SPX → VIX, VIX 옵션 → VVIX, GLD → GVZ), 델타 구간별 기여, 30일 곡선을 계산한다.
    계산할 수 없으면 {"error": 이유}."""
    sp = SPECS[spec]
    tv = quote_time(date, sp["lag"])
    pk = pick_terms(df, date, spec)
    if not pk:
        return {"error": f"{sp['index']}에 쓸 만기 2개를 찾지 못함"}
    ts = [term(df, e, r, tv, sp["roots"]) for e, r in pk]
    if ts[0] is None or ts[1] is None:
        return {"error": "만기 " + ", ".join(f"{e:%m-%d}" for (e, _), t in zip(pk, ts) if t is None) + "의 호가가 부족함"}
    a = weights(ts[0]["minutes"], ts[1]["minutes"])
    var = a[0] * ts[0]["sigma2"] + a[1] * ts[1]["sigma2"]
    if not var > 0:
        return {"error": "분산이 0 이하로 계산됨"}
    bins = sum(_to_bins(t["K"], t["cp"], w * t["c"], t["dcurve"], -w * t["corr"]) for w, t in zip(a, ts))
    return {"date": date, "tv": tv, "vix": 100 * math.sqrt(var), "var": var, "terms": ts, "a": a, "bins": bins,
            "curve": skew30(df, ts), "spot_implied": implied_spot(df, date, tv, spec), "spec": spec}


def groups(bins):
    """델타 구간을 네 묶음으로: 풋 꼬리(델타 10 미만), 풋 몸통(10~50), 콜 몸통, 콜 꼬리."""
    n = int(round(WING * 100 / 5))
    return {"put_wing": float(bins[0, :n].sum()), "put_body": float(bins[0, n:].sum()),
            "call_body": float(bins[1, n:].sum()), "call_wing": float(bins[1, :n].sum())}


def flat_bins(lv, sigma):
    """곡선이 평평하다면(모든 행사가의 IV가 sigma) 각 델타 구간이 차지했을 분산 기여. 같은 행사가·선도가·잔존 기간으로 계산한다."""
    out = np.zeros((2, NB))
    for w, t in zip(lv["a"], lv["terms"]):
        K, F, T, cp = t["K"], t["F"], t["T"], t["cp"]
        Q = np.asarray(b76(F, K, T, sigma, np.where(cp < 0, -1.0, 1.0)), float)
        i0 = int(np.flatnonzero(cp == 0)[0])
        Q[i0] = 0.5 * float(b76(F, K[i0], T, sigma, 1.0) + b76(F, K[i0], T, sigma, -1.0))
        c = strip_var(K, Q, F, T, t["K0"])[1]
        sd = sigma * math.sqrt(T)
        nd1 = ncdf((np.log(F / K) + 0.5 * sd * sd) / sd)
        out += _to_bins(K, cp, w * c, ((K[cp <= 0], 1 - nd1[cp <= 0]), (K[cp >= 0], nd1[cp >= 0])), -w * t["corr"])
    return out


def premium(lv, atm):
    """VIX가 등가격 IV(atm, 포인트)보다 높은 정도를 네 구간의 몫으로 나눈다. 네 값의 합 = VIX - 등가격 IV(정확히 일치).
    구간의 몫 = 그 구간 옵션의 실제 기여(포인트) - 곡선이 등가격 IV로 평평했다면 그 구간이 기여했을 포인트."""
    act = groups(lv["bins"] / lv["var"] * lv["vix"])
    fb = flat_bins(lv, atm / 100)
    ref = groups(fb / fb.sum() * atm)
    return {k: act[k] - ref[k] for k in act}


def atm_strike(level, step=None):
    """등가격 행사가 = 30일 선도가(없으면 기초자산 수준)를 step 단위(생략하면 SPX의 ATM_STEP)로 반올림한 값."""
    step = step or ATM_STEP
    return step * round(level / step)


def forward30(lv):
    """두 VIX 만기의 선도가를 잔존 30일로 (시간에 대해 직선으로) 보간한 값. Cboe 도구가 등가격 행사가를 고를 때 쓰는 수준과 같다
    (도구 자료의 델타에서 역산한 선도가와 거의 같고, 이 값의 5단위 반올림이 도구의 등가격 행사가와 87일 중 84일 같다). 못 구하면 nan."""
    try:
        t1, t2 = lv["terms"]
        m1, m2 = float(t1["minutes"]), float(t2["minutes"])
        f = float(t1["F"] + (t2["F"] - t1["F"]) * (M30 - m1) / (m2 - m1))
        return f if f > 0 else NAN
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return NAN


def cboe_factors(prev, today, s0, s1):
    """어제(prev)와 오늘(today)의 level() 결과로 요인 여섯 개를 계산한다. 합 = 오늘 VIX - 어제 VIX.
    등가격 행사가는 그날의 30일 선도가로 정하고, 선도가를 못 구한 날만 지수 수준 s0, s1을 쓴다."""
    if "error" in prev or "error" in today:
        return {"error": prev.get("error") or today.get("error")}
    if prev["curve"] is None or today["curve"] is None:
        return {"error": "30일 곡선을 만들지 못함(두 만기에 공통인 행사가 부족)"}
    x0, x1 = forward30(prev), forward30(today)
    x0, x1 = (x0 if x0 == x0 else s0), (x1 if x1 == x1 else s1)
    if not (x0 == x0 and x1 == x1):
        return {"error": "지수 수준을 알 수 없어 등가격 행사가를 정하지 못함"}
    (k0, v0), (k1, v1) = prev["curve"], today["curve"]
    step = SPECS[today.get("spec", "SPX")]["step"]
    a0, a1 = atm_strike(x0, step), atm_strike(x1, step)
    if not (k0[0] <= a0 <= k0[-1] and k0[0] <= a1 <= k0[-1] and k1[0] <= a1 <= k1[-1]):
        return {"error": f"등가격 행사가({a0:g}, {a1:g})가 30일 곡선 범위 밖임"}
    iv00, iv01, iv11 = (100 * float(np.interp(x, k, v)) for x, k, v in ((a0, k0, v0), (a1, k0, v0), (a1, k1, v1)))
    pr0, pr1 = premium(prev, iv00), premium(today, iv11)
    out = {"sticky": iv01 - iv00, "parallel": iv11 - iv01}
    out.update({f: pr1[r] - pr0[r] for f, r in REGION.items()})
    out.update({"atm": {"strike_prev": a0, "strike": a1, "iv_prev": iv00, "iv_prev_at_new": iv01, "iv": iv11},
                "premium_prev": {f: pr0[r] for f, r in REGION.items()}, "premium": {f: pr1[r] for f, r in REGION.items()},
                "resid": float(today["vix"] - prev["vix"] - sum(out.values()))})
    return out


# ----------------------------------------------------------------------------- 여러 날 이어 붙이기·정리
def index_closes(dates, spec="SPX"):
    """야후 파이낸스의 기초자산·공식 지수 종가 {날짜: {"spx": 기초자산, "vix": 공식 지수}}. 키 이름은 예전 그대로다
    (SPX: S&P 500·VIX, VIX: VIX·VVIX, GLD: GLD·GVZ). 받지 못하면 빈 값(분석은 계속한다)."""
    try:
        import yfinance as yf
        u, x = SPECS[spec]["yahoo"]
        a, b = pd.Timestamp(min(dates)), pd.Timestamp(max(dates))
        px = yf.download([u, x], start=a - pd.Timedelta(days=3), end=b + pd.Timedelta(days=1), auto_adjust=False, progress=False)["Close"]
        px.index = pd.to_datetime(px.index).tz_localize(None).normalize()
        out = {}
        for d in dates:
            ts = pd.Timestamp(d)
            if ts in px.index:
                row = {k: float(px.loc[ts, s]) for k, s in (("spx", u), ("vix", x)) if s in px and px.loc[ts, s] == px.loc[ts, s]}
                if row:
                    out[d] = row
        return out
    except Exception:
        return {}


def new_state(closes=None, spec="SPX"):
    return {"rows": [], "prev": None, "last": None, "closes": closes or {}, "spec": spec}


def track(state, date, df):
    """날짜순으로 하루씩 넣는다. 날짜별 요약(state["rows"])과 마지막 이틀의 전체 계산(state["last"])을 쌓는다."""
    lv = level(df, date, state.get("spec", "SPX"))
    cl = state["closes"].get(date, {})
    spot = cl.get("spx", lv.get("spot_implied", NAN))
    row, f, pv = {"date": date}, None, state["prev"]
    if "error" not in lv:
        row["vix"] = lv["vix"]
        row.update({"sh_" + k: v / lv["var"] for k, v in groups(lv["bins"]).items()})
        if pv is not None:
            f = cboe_factors(pv["lv"], lv, pv["spot"], spot)
            if "error" not in f:
                row.update({k: f[k] for k in FACTORS})
    state["rows"].append(row)
    state["last"] = {"date": date, "level": lv, "factors": f, "prev": pv, "spot": spot, "close": cl}
    state["prev"] = {"date": date, "lv": lv, "spot": spot, "close": cl}


def _side(f, past, beta):
    """풋 쪽·콜 쪽 요인이 등가격 IV 변화에 딸려 움직이는 평소 관계(beta)에서 벗어난 몫. past = 과거 요인 dict 목록."""
    atm = f["sticky"] + f["parallel"]
    p = np.array([(r["sticky"] + r["parallel"], r["put_skew"] + r["down_conv"], r["call_skew"] + r["up_conv"]) for r in past if "sticky" in r], float)
    side = {"atm": atm, "n": int(len(p))}
    for name, col, today in (("put", 1, f["put_skew"] + f["down_conv"]), ("call", 2, f["call_skew"] + f["up_conv"])):
        side[name] = {"today": today, "beta": beta[name], "usual": beta[name] * atm, "excess": today - beta[name] * atm}
        if len(p) >= 8:
            side[name]["typ"] = float(np.median(np.abs(p[:, col] - beta[name] * p[:, 0])))
    return side


def _typ(past):
    """요인별 평소 크기(과거 변화의 절댓값 중앙값). 8번 이상 있을 때만."""
    typ = {}
    for k in FACTORS:
        v = np.array([abs(r[k]) for r in past if k in r and r[k] == r[k]], float)
        if len(v) >= 8:
            typ[k] = float(np.median(v))
    return typ


def summary(state):
    """마지막 날의 결과를 보고서·그림·JSON에 쓸 형태로 정리한다."""
    last, rows = state["last"], state["rows"]
    spec = state.get("spec", "SPX")
    sp = SPECS[spec]
    if last is None:
        return {"error": f"{spec} 자료가 없음", "spec": spec}
    lv, f, pv, cl = last["level"], last["factors"], last["prev"], last["close"]
    out = {"date": last["date"], "prev": pv["date"] if pv else None, "flags": [], "spec": spec, "source": "own"}
    if "error" in lv:
        out["error"] = lv["error"]
        return out
    pts = lv["bins"] / lv["var"] * lv["vix"]
    out.update({"vix": lv["vix"], "spx": last["spot"], "spx_is_close": "spx" in cl,
                "share": {k: v / lv["var"] for k, v in groups(lv["bins"]).items()}, "points": groups(pts),
                "terms": [{"exp": f"{t['exp']:%Y-%m-%d}", "root": t["root"], "days": t["minutes"] / 1440, "vol": 100 * math.sqrt(max(t["sigma2"], 0)),
                           "weight": w / sum(lv["a"]), "F": t["F"], "K0": t["K0"], "n_put": t["n_put"], "n_call": t["n_call"],
                           "k_lo": t["k_lo"], "k_hi": t["k_hi"]} for w, t in zip(lv["a"], lv["terms"])]})
    if "vix" in cl:
        out["official"] = cl["vix"]
    for t in lv["terms"]:
        n = t["n_put"] + t["n_call"] + 1
        if t["iv_nan"] > 0.05 * n:
            out["flags"].append(f"만기 {t['exp']:%m-%d}: 옵션 {n}개 중 {t['iv_nan']}개는 IV를 역산하지 못해 델타 구간을 이웃 값으로 정함")
        if t["k0_shift"]:
            out["flags"].append(f"만기 {t['exp']:%m-%d}: 선도가 바로 아래 행사가의 호가가 온전하지 않아 그 아래 행사가를 기준(K0)으로 씀")
    d1, d2 = (t["minutes"] / 1440 for t in lv["terms"])
    if sp["rule"] == "weekly30" and (d1 < 22.5 or d2 > 37.5):         # 월물만 쓰는 VVIX·GVZ 방식은 만기 간격이 원래 넓다
        out["flags"].append(f"만기 선택이 평소 범위(23~37일)를 벗어남: 잔존 {d1:.1f}일, {d2:.1f}일 - 주말 만기가 빠졌을 수 있음")
    sh = np.array([r.get("sh_put_wing", NAN) for r in rows], float)
    sh = sh[~np.isnan(sh)]
    if len(sh) >= 10:
        out["share_rank"] = {"rank": int((sh > out["share"]["put_wing"]).sum()) + 1, "n": int(len(sh))}
    plv = pv["lv"] if pv else None
    if plv is not None and "error" not in plv:
        out.update({"vix_prev": plv["vix"], "chg": lv["vix"] - plv["vix"], "spx_prev": pv["spot"], "spx_prev_is_close": "spx" in pv["close"],
                    "share_prev": {k: v / plv["var"] for k, v in groups(plv["bins"]).items()}})
        if "vix" in pv["close"]:
            out["official_prev"] = pv["close"]["vix"]
    for lab, mine, off in (("오늘", lv["vix"], out.get("official")), ("전일", out.get("vix_prev"), out.get("official_prev"))):
        if mine is not None and off is not None and abs(mine - off) > sp["tol"]:
            out["flags"].append(f"{lab} 직접 계산한 {sp['index']}({mine:.2f})가 공식 종가({off:.2f})와 {mine - off:+.2f} 다름"
                                f" - 그날 {sp['opt']} 호가가 마감 시점 값이 아닐 수 있음")
    if f is None:
        out["flags"].append("비교할 전일 자료가 없어 변화 요인을 계산하지 못함")
    elif "error" in f:
        out["flags"].append("변화 요인을 계산하지 못함: " + f["error"])
    else:
        out["factors"] = {k: f[k] for k in FACTORS}
        out.update({"atm": f["atm"], "premium": f["premium"], "premium_prev": f["premium_prev"]})
        (k0, v0), (k1, v1) = plv["curve"], lv["curve"]
        lo, hi = max(k0[0], k1[0]), min(k0[-1], k1[-1])                # 두 날짜에 모두 있는 범위만(Cboe 도구와 같다)
        m0, m1 = (k0 >= lo) & (k0 <= hi), (k1 >= lo) & (k1 <= hi)
        out["curve"] = {"prev": {"K": k0[m0].tolist(), "iv": (100 * v0[m0]).tolist()}, "today": {"K": k1[m1].tolist(), "iv": (100 * v1[m1]).tolist()}}
        typ = _typ(rows[:-1])
        if typ:
            out["factor_typ"], out["factor_typ_n"] = typ, int(sum(1 for r in rows[:-1] if "sticky" in r))
        # 풋 쪽·콜 쪽 요인은 등가격 IV 변화에 딸려 움직인다(지수가 등가격 IV보다 크게 움직이기 때문). 그 관계에서 벗어난 몫을 따로 낸다
        out["side"] = _side(f, rows[:-1], SIDE_BETAS[spec])
        if abs(f["resid"]) > 1e-6:
            out["flags"].append(f"요인 합계가 {sp['index']} 변화와 {f['resid']:+.4f} 어긋남")
    return out


# ----------------------------------------------------------------------------- Cboe 공식값(SPX만)
def _get_json(url, timeout=30):
    import urllib.request
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def _dash(d):
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


def cboe_official(d0, d1, timeout=30):
    """Cboe VIX Decomposition 도구가 읽는 공개 자료(날짜 YYYYMMDD 두 개). 요인(포인트), 등가격 행사가와 IV, 두 날의 30일 곡선, 종가.
    받지 못하면 {"error": 이유}."""
    q = f"?date1={_dash(d0)}&date2={_dash(d1)}"
    try:
        js = _get_json(CBOE_API + q, timeout)
        f = {CBOE_KEYS[k]: 100 * float(v) for k, v in js["factors"].items() if k in CBOE_KEYS}
        if set(f) != set(FACTORS):
            return {"error": "요인 여섯 개가 다 오지 않음"}
        fr, to = js["atmPairs"]["from"], js["atmPairs"]["to"]
        curves = {}
        for d in (d0, d1):
            (obj,) = js["interpolatedIVs"][_dash(d)].values()
            ks = sorted(obj["ivs"], key=float)
            curves[d] = (np.array([float(k) for k in ks]), np.array([100 * float(obj["ivs"][k]["iv"]) for k in ks]))
    except Exception as e:
        return {"error": f"Cboe 공식값을 받지 못함({type(e).__name__}: {str(e)[:80]})"}
    out = {"factors": f, "curves": curves,
           "atm": {"strike_prev": float(fr["atm"]), "strike": float(to["atm"]), "iv_prev": 100 * float(fr["iv"]), "iv": 100 * float(to["iv"])}}
    out["atm"]["iv_prev_at_new"] = out["atm"]["iv_prev"] + f["sticky"]       # 곡선 따라 이동 = 어제 곡선에서 오늘 등가격 자리의 IV - 어제 등가격 IV
    try:
        cl = _get_json(CBOE_API + "get_closing_data/" + q, timeout)
        c = {(t, d.replace("-", "")): float(v) for t, d, v in cl}
        out["closes"] = {d: {"spx": c.get(("SPX", d)), "vix": c.get(("VIX", d))} for d in (d0, d1)}
    except Exception:
        out["closes"] = {}
    return out


def cboe_history(pairs, timeout=30, pause=0.3):
    """여러 날짜 쌍의 Cboe 공식 요인 [{요인: 포인트}, ...]. 받지 못한 쌍은 건너뛴다(평소 크기를 재는 데만 쓴다)."""
    import time
    out = []
    for d0, d1 in pairs:
        try:
            js = _get_json(CBOE_API + f"?date1={_dash(d0)}&date2={_dash(d1)}", timeout)
            out.append({CBOE_KEYS[k]: 100 * float(v) for k, v in js["factors"].items() if k in CBOE_KEYS})
        except Exception:
            pass
        time.sleep(pause)
    return out


def apply_official(res, off, hist):
    """자체 계산한 SPX 결과(res)에 Cboe 공식 요인·등가격·곡선을 넣는다. 자체 계산 값은 own_factors로 남긴다(대조용).
    hist = 과거 날짜 쌍의 공식 요인 목록(평소 크기·평소 관계를 잰다)."""
    out = dict(res)
    out["flags"] = list(res.get("flags", []))
    if "error" in off:
        out["flags"].append(off["error"] + " - 요인은 자체 계산 값으로 대신함(뒤의 네 요인은 Cboe 값과 계산 방식이 다름)")
        return out
    if "factors" in res:
        out["own_factors"] = res["factors"]
    out["factors"], out["atm"], out["source"] = off["factors"], off["atm"], "cboe"
    (k0, v0), (k1, v1) = off["curves"][res["prev"]], off["curves"][res["date"]]
    lo, hi = max(k0[0], k1[0]), min(k0[-1], k1[-1])
    m0, m1 = (k0 >= lo) & (k0 <= hi), (k1 >= lo) & (k1 <= hi)
    out["curve"] = {"prev": {"K": k0[m0].tolist(), "iv": v0[m0].tolist()}, "today": {"K": k1[m1].tolist(), "iv": v1[m1].tolist()}}
    c0, c1 = off.get("closes", {}).get(res["prev"], {}), off.get("closes", {}).get(res["date"], {})
    if c1.get("vix") and c0.get("vix"):
        out["official"], out["official_prev"] = c1["vix"], c0["vix"]
    if c1.get("spx") and c0.get("spx"):
        out.update({"spx": c1["spx"], "spx_prev": c0["spx"], "spx_is_close": True, "spx_prev_is_close": True})
    if "official" in out and "official_prev" in out:
        out["chg_official"] = out["official"] - out["official_prev"]
    for k in ("premium", "premium_prev"):
        out.pop(k, None)
    typ = _typ(hist)
    if typ:
        out["factor_typ"], out["factor_typ_n"] = typ, len(hist)
    else:
        out.pop("factor_typ", None)
    out["side"] = _side(off["factors"], hist, SIDE_BETAS["SPX_CBOE"])
    tot = sum(off["factors"].values())
    if "chg_official" in out and abs(tot - out["chg_official"]) > 0.015:
        out["flags"].append(f"Cboe 요인 합계({tot:+.2f})가 공식 VIX 변화({out['chg_official']:+.2f})와 다름")
    return out


def prev_session(d):
    """d(YYYYMMDD) 바로 앞 NYSE 거래일. 달력 라이브러리가 없으면 None."""
    try:
        import exchange_calendars as xc
        return xc.get_calendar("XNYS").previous_session(pd.Timestamp(d)).strftime("%Y%m%d")
    except Exception:
        return None


def official_for(res, dates, n_hist=20):
    """자체 계산한 SPX 결과에 Cboe 공식값을 넣는다. dates = 계산에 쓴 수집일(오래된 순). 평소 크기는 그 앞 n_hist개 거래일 쌍의 공식값으로 잰다."""
    past = [d for d in dates if d < res["date"]][-n_hist:]
    pairs = [(prev_session(d) or p, d) for p, d in zip([None] + past[:-1], past) if (prev_session(d) or p)]
    return apply_official(res, cboe_official(res["prev"], res["date"]), cboe_history(pairs))


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


# ----------------------------------------------------------------------------- 글 보고
NAMES = {"sticky": "곡선 따라 이동", "parallel": "평행 이동", "put_skew": "풋 스큐", "call_skew": "콜 스큐", "down_conv": "하방 볼록성", "up_conv": "상방 볼록성"}
GROUPS = {"put_wing": "풋 10델타 미만", "put_body": "풋 10~50델타", "call_body": "콜 10~50델타", "call_wing": "콜 10델타 미만"}


def _ymd(d):
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


def _k(x):
    """행사가 표기: 정수면 소수점 없이, 아니면 한 자리."""
    return f"{x:,.0f}" if abs(x - round(x)) < 1e-9 else f"{x:,.1f}"


HEAD = {"SPX": "VIX 분해", "VIX": "VVIX식 분해(VIX 옵션, 자체 계산)", "GLD": "GVZ식 분해(GLD 옵션, 자체 계산)"}


def report(res):
    spec = res.get("spec", "SPX")
    sp = SPECS[spec]
    idx, off = sp["index"], res.get("source") == "cboe"
    L = [f"\n== {HEAD[spec]}{'(Cboe 공식값)' if off else ''} | {res.get('date', '-')} (비교 대상 {res.get('prev') or '-'})"]
    if "error" in res:
        return "\n".join(L + [f"  [확인 필요] {idx}식 지수를 계산하지 못함: {res['error']}"])
    has_prev = "vix_prev" in res
    s = f"  [직접 계산한 {idx}] {res['vix']:.2f}" + (f" (전일 {res['vix_prev']:.2f}, {res['chg']:+.2f})" if has_prev else "")
    if "official" in res:
        s += f" | 공식 종가 {res['official']:.2f}(차이 {res['vix'] - res['official']:+.2f})"
        if "official_prev" in res and has_prev:
            s += f", 전일 {res['official_prev']:.2f}(차이 {res['vix_prev'] - res['official_prev']:+.2f})"
    else:
        s += " | 공식 종가 대조 못함"
    L.append(s)
    if has_prev:
        lab = "종가" if res["spx_is_close"] and res["spx_prev_is_close"] else "옵션에서 역산한 수준"
        L.append(f"  [{sp['under']} {lab}] {res['spx_prev']:,.2f} → {res['spx']:,.2f} ({res['spx'] - res['spx_prev']:+,.2f}, {(res['spx'] / res['spx_prev'] - 1) * 100:+.2f}%)")
    if "factors" in res:
        f, a = res["factors"], res["atm"]
        chg = res.get("chg_official", res["chg"]) if off else res["chg"]
        L.append(f"  [{idx} 변화 {chg:+.2f}의 요인{'(Cboe 공식값)' if off else ''}] " + " | ".join(f"{NAMES[k]} {f[k]:+.2f}" for k in FACTORS)
                 + f" | 합 {sum(f.values()):+.2f}")
        if "factor_typ" in res:
            src = "Cboe 공식값의 " if off else ""
            L.append(f"    평소 크기(최근 {res['factor_typ_n']}번 {src}변화의 절댓값 중앙값): " + " | ".join(f"{NAMES[k]} {v:.2f}" for k, v in res["factor_typ"].items()))
        if "side" in res:
            sd = res["side"]
            typ = (f", 평소 벗어나는 크기 {sd['put']['typ']:.2f}", f", 평소 벗어나는 크기 {sd['call']['typ']:.2f}") if "typ" in sd["put"] else ("", "")
            L.append(f"  [풋 쪽·콜 쪽 요인의 평소 관계] 등가격 IV 변화(곡선 따라 이동+평행 이동) {sd['atm']:+.2f}에 평소라면 "
                     f"풋 쪽(풋 스큐+하방 볼록성) {sd['put']['usual']:+.2f}, 콜 쪽(콜 스큐+상방 볼록성) {sd['call']['usual']:+.2f}"
                     f" | 오늘 풋 쪽 {sd['put']['today']:+.2f}(평소 관계보다 {sd['put']['excess']:+.2f}{typ[0]}),"
                     f" 콜 쪽 {sd['call']['today']:+.2f}(평소 관계보다 {sd['call']['excess']:+.2f}{typ[1]})")
        L.append(f"  [등가격] 행사가 {_k(a['strike_prev'])} → {_k(a['strike'])} | 30일 곡선의 등가격 IV {a['iv_prev']:.2f}% → {a['iv']:.2f}%"
                 f" (어제 곡선에서 오늘 등가격 행사가의 IV {a['iv_prev_at_new']:.2f}%)")
        if off and "own_factors" in res:
            L.append("  [참고: 자체 계산 요인] " + " | ".join(f"{NAMES[k]} {res['own_factors'][k]:+.2f}" for k in FACTORS))
        for lab, key, v, at in (("오늘", "premium", res["vix"], a["iv"]), ("전일", "premium_prev", res.get("vix_prev"), a["iv_prev"])):
            if key in res and v is not None:
                L.append(f"  [{idx} - 등가격 IV, {lab}] {v - at:+.2f} = " + " | ".join(f"{NAMES[k]} {res[key][k]:+.2f}" for k in FACTORS[2:]))
    L.append("  [계산에 쓴 만기] " + " | ".join(
        f"{t['exp'][5:]} {t['root']}(잔존 {t['days']:.1f}일, 변동성 {t['vol']:.2f}, 비중 {t['weight'] * 100:.0f}%, 풋 {t['n_put']}·콜 {t['n_call']}개,"
        f" 행사가 {_k(t['k_lo'])}~{_k(t['k_hi'])})" for t in res["terms"]))
    shp = res.get("share_prev", {})
    L.append(f"  [{idx} 구성: 델타 구간별 기여(포인트)와 비중] " + " | ".join(
        f"{GROUPS[k]} {res['points'][k]:.2f}({res['share'][k] * 100:.1f}%" + (f", 전일 {shp[k] * 100:.1f}%" if k in shp else "") + ")" for k in GROUPS))
    if "share_rank" in res:
        L.append(f"    풋 10델타 미만 비중은 최근 {res['share_rank']['n']}일 중 {res['share_rank']['rank']}번째로 높음")
    for x in res["flags"]:
        L.append(f"  [확인 필요] {x}")
    return "\n".join(L)


# ----------------------------------------------------------------------------- 그림
INK, INK2, MUTED, GRID, BASE, SURF, FILL = "#f3f2ee", "#bdbbb4", "#8f8d87", "#29292b", "#4a4a4d", "#0c0c0d", "#1f1f21"   # 검은 배경 테마(글자·선은 밝게, 바탕은 어둡게)
C_PREV, C_TODAY = "#4c9bf0", "#2ecf8f"                              # 전일(파랑), 오늘(초록). 검은 배경에 맞춰 밝게 한 값
TXT = {
    "ko": {"title": "VIX 분해", "sub": "SPX 옵션 마감 호가로 직접 계산한 값", "from": "시작일", "to": "종료일", "spx": "S&P 500", "vix": "VIX",
           "abs": "하루 변화(포인트)", "pct": "하루 변화(%)", "fac": "VIX 변화 요인", "total": "요인 합계",
           "f": {"sticky": "곡선 따라 이동", "parallel": "평행 이동", "put_skew": "풋 스큐", "call_skew": "콜 스큐",
                 "down_conv": "하방 볼록성", "up_conv": "상방 볼록성"},
           "fs": {"sticky": "등가격 행사가 {a0} \u2192 {a1}. 어제 곡선에서 그 자리의 IV는 {v0}% \u2192 {v01}%", "sticky0": "등가격 행사가는 {a1} 그대로",
                  "parallel": "행사가 {a1}의 IV가 어제 곡선 {v01}% \u2192 오늘 곡선 {v1}%",
                  "put_skew": "풋(델타 10~50)이 등가격보다 비싼 정도의 변화", "call_skew": "콜(델타 10~50)이 등가격보다 비싼 정도의 변화",
                  "down_conv": "먼 풋(델타 10 미만)이 등가격보다 비싼 정도의 변화", "up_conv": "먼 콜(델타 10 미만)이 등가격보다 비싼 정도의 변화"},
           "note_off": "직접 계산한 VIX {a} → {b} (공식 종가와의 차이 {da}, {db})", "note_own": "VIX는 직접 계산한 값이다(공식 종가를 받지 못함)",
           "note_spot": "S&P 500은 옵션에서 역산한 지수 수준이다(종가를 받지 못함)", "curve": "내재변동성 곡선",
           "curve_s": "잔존 30일로 맞춘 행사가별 IV. 점은 그날의 등가격 행사가", "zoom": "등가격 부근 확대",
           "hollow": "빈 점은 어제 곡선에서 오늘 등가격 행사가의 자리", "y": "내재변동성(%)", "x": "행사가",
           "atm_prev": "전일 등가격 {v}%", "atm": "오늘 등가격 {v}%", "nofac": "변화 요인을 계산하지 못한 날이라 요인 표와 곡선은 그리지 않았다",
           "foot": "자료: Cboe 지연 시세(SPX 옵션 마감 호가) · 방법: Cboe VIX 산출 방법론, VIX Decomposition 백서"},
    "en": {"title": "VIX decomposition", "sub": "Computed from SPX option closing quotes", "from": "From date", "to": "To date", "spx": "S&P 500", "vix": "VIX",
           "abs": "Day change (abs)", "pct": "Day change (pct)", "fac": "VIX decomposition factors", "total": "Total of factors",
           "f": {"sticky": "Sticky strike", "parallel": "Parallel shift", "put_skew": "Put skew", "call_skew": "Call skew",
                 "down_conv": "Downside convexity", "up_conv": "Upside convexity"},
           "fs": {"sticky": "ATM moves {a0} \u2192 {a1} on yesterday's curve: IV {v0}% \u2192 {v01}%", "sticky0": "ATM strike unchanged at {a1}",
                  "parallel": "at strike {a1} the curve moves {v01}% \u2192 {v1}%",
                  "put_skew": "puts (10-50 delta) versus ATM", "call_skew": "calls (10-50 delta) versus ATM",
                  "down_conv": "far puts (under 10 delta) versus ATM", "up_conv": "far calls (under 10 delta) versus ATM"},
           "note_off": "Replicated VIX {a} → {b} (minus official close: {da}, {db})", "note_own": "VIX column is the replicated value (no official close)",
           "note_spot": "S&P 500 is the option-implied level (no close available)", "curve": "Implied volatility curves",
           "curve_s": "30-day IV by strike; dots mark each day's ATM strike", "zoom": "Near the money",
           "hollow": "hollow dot: today's ATM strike on yesterday's curve", "y": "Implied volatility (%)", "x": "Strike",
           "atm_prev": "prev ATM {v}%", "atm": "ATM {v}%", "nofac": "Factors could not be computed for this day; factor table and curves not drawn",
           "foot": "Data: Cboe delayed quotes (SPX closing quotes). Method: Cboe VIX methodology, VIX Decomposition paper"},
}
TAG = {"sticky": "STICKY STRIKE", "parallel": "PARALLEL SHIFT", "put_skew": "PUT SKEW", "call_skew": "CALL SKEW",
       "down_conv": "DOWNSIDE CONVEXITY", "up_conv": "UPSIDE CONVEXITY"}
# 기초자산·자료 출처별로 바꾸는 글자(나머지는 TXT 그대로)
TXT_SPEC = {
    "ko": {
        ("SPX", "cboe"): {"sub": "Cboe VIX Decomposition 도구의 공식값",
                          "fs_put_skew": "풋(델타 약 15~45)의 IV가 평행 이동보다 더 오른 몫", "fs_call_skew": "콜(델타 약 15~45)의 IV가 평행 이동보다 더 오른 몫",
                          "fs_down_conv": "먼 풋(델타 약 1~15)의 IV가 그보다 더 오른 몫", "fs_up_conv": "먼 콜(델타 약 1~15)의 IV가 그보다 더 오른 몫",
                          "foot": "자료: Cboe VIX Decomposition 도구(요인·곡선·종가) · 대조: SPX 옵션 마감 호가로 직접 계산한 VIX"},
        ("VIX", "own"): {"title": "VIX 옵션 변동성 분해", "sub": "VIX 옵션 마감 호가로 직접 계산한 30일 변동성 지수(VVIX와 같은 방식)",
                         "spx": "VIX", "vix": "VVIX", "fac": "VVIX 변화 요인",
                         "note_off": "직접 계산한 값 {a} → {b} (공식 VVIX와의 차이 {da}, {db})", "note_own": "VVIX 열은 직접 계산한 값이다(공식 VVIX를 받지 못함)",
                         "note_spot": "VIX는 옵션에서 역산한 수준이다(종가를 받지 못함)",
                         "foot": "자료: Cboe 지연 시세(VIX 옵션 마감 호가) · 방법: Cboe VIX 산출 방법론을 VIX 옵션에 적용, 요인은 이 스크립트의 정의"},
        ("GLD", "own"): {"title": "GLD 옵션 변동성 분해", "sub": "GLD 옵션 마감 호가로 직접 계산한 30일 변동성 지수(GVZ와 같은 방식)",
                         "spx": "GLD", "vix": "GVZ", "fac": "GVZ 변화 요인",
                         "note_off": "직접 계산한 값 {a} → {b} (공식 GVZ와의 차이 {da}, {db})", "note_own": "GVZ 열은 직접 계산한 값이다(공식 GVZ를 받지 못함)",
                         "note_spot": "GLD는 옵션에서 역산한 수준이다(종가를 받지 못함)",
                         "foot": "자료: Cboe 지연 시세(GLD 옵션 마감 호가) · 방법: Cboe VIX 산출 방법론을 GLD 옵션에 적용, 요인은 이 스크립트의 정의"},
    },
    "en": {
        ("SPX", "cboe"): {"sub": "Official values from the Cboe VIX Decomposition tool",
                          "foot": "Data: Cboe VIX Decomposition tool (factors, curves, closes). Check: VIX replicated from SPX closing quotes"},
        ("VIX", "own"): {"title": "VIX options volatility decomposition", "sub": "30-day volatility index from VIX option closing quotes (VVIX method)",
                         "spx": "VIX", "vix": "VVIX", "fac": "VVIX decomposition factors",
                         "foot": "Data: Cboe delayed quotes (VIX option closing quotes). Method: Cboe VIX methodology on VIX options"},
        ("GLD", "own"): {"title": "GLD options volatility decomposition", "sub": "30-day volatility index from GLD option closing quotes (GVZ method)",
                         "spx": "GLD", "vix": "GVZ", "fac": "GVZ decomposition factors",
                         "foot": "Data: Cboe delayed quotes (GLD option closing quotes). Method: Cboe VIX methodology on GLD options"},
    },
}


def _labels(lang, res):
    """그림에 쓸 글자: TXT[lang]에 기초자산·자료 출처별 바꿀 것을 덮어쓴 사본."""
    L = {k: (dict(v) if isinstance(v, dict) else v) for k, v in TXT[lang].items()}
    for k, v in TXT_SPEC[lang].get((res.get("spec", "SPX"), res.get("source", "own")), {}).items():
        if k.startswith("fs_"):
            L["fs"][k[3:]] = v
        else:
            L[k] = v
    return L


def _font():
    """한글이 나오는 글꼴 이름. 없으면 None(그림의 글자를 영어로 바꾼다)."""
    from matplotlib import font_manager as fm, ft2font
    try:
        import koreanize_matplotlib  # noqa: F401  설치돼 있으면 나눔고딕을 등록해 준다
    except Exception:
        pass
    have = {f.name for f in fm.fontManager.ttflist}
    for n in ("Noto Sans CJK KR", "Noto Sans KR", "NanumGothic", "NanumBarunGothic", "Malgun Gothic", "Apple SD Gothic Neo", "AppleGothic",
              "Noto Sans CJK JP", "Noto Sans CJK SC", "Noto Sans CJK TC", "Noto Sans CJK HK", "UnDotum", "Baekmuk Dotum"):
        if n in have:
            return n
    for f in fm.fontManager.ttflist:
        try:
            if ft2font.FT2Font(f.fname).get_char_index(0xAC00):      # '가' 글리프가 있는 글꼴
                return f.name
        except Exception:
            continue
    return None


def _num(x, n=2, sign=True, comma=False):
    """그림용 숫자. 음수는 유니코드 빼기 기호, 0으로 반올림되는 값은 부호 없이."""
    if abs(x) < 0.5 * 10 ** -n:
        return f"{0:.{n}f}"
    body = f"{abs(x):,.{n}f}" if comma else f"{abs(x):.{n}f}"
    return ("+" if sign and x > 0 else "−" if x < 0 else "") + body


def _ticks(lo, hi, target=4):
    """lo~hi를 덮는 보기 좋은 눈금."""
    raw = max(hi - lo, 1e-9) / target
    mag = 10 ** math.floor(math.log10(raw))
    step = min((s for s in (1, 2, 2.5, 5, 10) if s * mag >= raw), default=10) * mag
    t = np.arange(math.ceil(lo / step - 1e-9), math.floor(hi / step + 1e-9) + 1) * step
    return [float(round(x, 10)) for x in t], step


def chart(res, path):
    """분해 결과를 Cboe 도구와 같은 구성(표 + 내재변동성 곡선)의 그림(PNG, 가로 1080픽셀)으로 저장한다.
    res = summary() 결과, 또는 그것을 JSON으로 저장했다 읽은 것. 화면이 좁은 휴대폰에 맞춰 표를 위에, 곡선을 아래에 둔다."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.patches import Circle, FancyBboxPatch

    if "error" in res:
        raise ValueError(res["error"])
    font = _font()
    L = _labels("ko" if font else "en", res)
    fam = font or "DejaVu Sans"
    has_f = "factors" in res
    W, DPI = 1080, 150
    pt = 72 / DPI                                                     # 픽셀 → 포인트
    # 표에 들어갈 줄을 먼저 정한다(그림 높이가 여기서 정해진다)
    has_off = "official" in res and (not has_f or "official_prev" in res)   # 공식 값과 대조할 수 있는가
    # 표의 지수 열: SPX는 공식 VIX 종가(요인 합계와 같다). VIX·GLD는 직접 계산한 값(요인 합계가 그 변화이고, 공식 값과는 방법 차이만큼 다르다)
    off = has_off and res.get("spec", "SPX") == "SPX"
    v1 = res["official"] if off else res["vix"]
    v0 = (res["official_prev"] if off else res["vix_prev"]) if "vix_prev" in res else None
    s1, s0 = res["spx"], res.get("spx_prev")
    spot_ok = res["spx_is_close"] and res.get("spx_prev_is_close", True)
    rows = [(_ymd(res["prev"]), f"{s0:,.2f}", f"{v0:.2f}")] if v0 is not None else []
    rows.append((_ymd(res["date"]), f"{s1:,.2f}", f"{v1:.2f}"))
    if v0 is not None:
        rows += [(L["abs"], _num(s1 - s0, comma=True), _num(v1 - v0)), (L["pct"], _num((s1 / s0 - 1) * 100) + "%", _num((v1 / v0 - 1) * 100) + "%")]
    notes = [L["note_off"].format(a=f"{res['vix_prev']:.2f}", b=f"{res['vix']:.2f}", da=_num(res["vix_prev"] - res["official_prev"]),
                                  db=_num(res["vix"] - res["official"])) if has_off and has_f else None if has_off else L["note_own"],
             None if spot_ok else L["note_spot"]]
    notes = [n for n in notes if n]
    RH, FH, P1, P2 = 58, 82, 430, 360                                  # 표의 줄 높이, 요인 줄 높이, 곡선 그림 두 개의 높이
    th = 78 + RH * len(rows) + ((76 + FH * len(FACTORS) + 78) if has_f else 0) + 34 * len(notes) + 22
    H = 262 + th + ((28 + 132 + P1 + 140 + P2 + 150) if has_f else 80) + 66
    fig = Figure(figsize=(W / DPI, H / DPI), dpi=DPI, facecolor=SURF)
    FigureCanvasAgg(fig)

    def layer(z):
        ax = fig.add_axes([0, 0, 1, 1], facecolor="none")
        ax.set_xlim(0, W)
        ax.set_ylim(H, 0)
        ax.axis("off")
        ax.set_zorder(z)
        return ax
    bg, fg = layer(0), layer(10)                                       # 바탕 층과, 곡선 위에 점·글자를 올리는 층

    def text(x, y, s, px=24, color=INK, weight="regular", ha="left", va="baseline", ax=bg, **kw):
        if not font and weight == "medium":
            weight = "regular"                                         # 대체 글꼴에는 중간 굵기가 없다
        return ax.text(x, y, s, fontsize=px * pt, color=color, fontweight=weight, ha=ha, va=va, fontfamily=fam, **kw)

    def width(t):
        return t.get_window_extent(fig.canvas.get_renderer()).width

    def card(top, height):
        bg.add_patch(FancyBboxPatch((40, top), W - 80, height, boxstyle="round,pad=0,rounding_size=16", facecolor=SURF, edgecolor="#ffffff2e", lw=1.4 * pt))

    def rule(y, x0=70, x1=W - 70, color=BASE, lw=1.4):
        bg.plot([x0, x1], [y, y], color=color, lw=lw * pt, solid_capstyle="butt")

    # 제목과 날짜
    text(40, 66, L["title"], 40, INK, "bold")
    text(40, 104, L["sub"], 22, INK2)
    for i, (lab, d, fill) in enumerate(((L["from"], res.get("prev"), FILL), (L["to"], res["date"], SURF))):
        x = 40 + i * 330
        text(x, 152, lab, 20, INK2, "medium")
        bg.add_patch(FancyBboxPatch((x, 166), 300, 60, boxstyle="round,pad=0,rounding_size=10", facecolor=fill, edgecolor="#ffffff3a", lw=1.4 * pt))
        text(x + 22, 206, _ymd(d) if d else "–", 25, INK2 if i == 0 else INK, "medium")

    # 표
    xs, xv, top = 790, W - 70, 262
    card(top, th)
    y = top + 50
    text(xs, y, L["spx"] + ("" if spot_ok else "*"), 23, INK, "bold", ha="right")
    text(xv, y, L["vix"], 23, INK, "bold", ha="right")
    rule(y + 20)
    y += 20
    for lab, a, b in rows:
        y += RH
        text(70, y - 18, lab, 24, INK)
        text(xs, y - 18, a, 24, INK, ha="right")
        text(xv, y - 18, b, 24, INK, ha="right")
    if has_f:
        f = res["factors"]
        y += 76
        text(70, y - 22, L["fac"], 24, INK, "bold")
        rule(y)
        at = res["atm"]
        fmt = {"a0": _k(at["strike_prev"]), "a1": _k(at["strike"]), "v0": f"{at['iv_prev']:.2f}", "v01": f"{at['iv_prev_at_new']:.2f}", "v1": f"{at['iv']:.2f}"}
        for k in FACTORS:
            t = text(70, y + 38, L["f"][k], 25, INK, "medium")
            if font:
                text(70 + width(t) + 14, y + 38, TAG[k], 15, MUTED)
            text(70, y + 66, L["fs"]["sticky0" if k == "sticky" and at["strike"] == at["strike_prev"] else k].format(**fmt), 18.5, INK2)
            text(xv, y + 50, _num(f[k]), 26, INK, "medium", ha="right")
            y += FH
        rule(y + 6)
        y += 78
        text(70, y - 24, L["total"], 25, INK, "bold")
        text(xv, y - 24, _num(sum(f.values())), 26, INK, "bold", ha="right")
    for n in notes:
        y += 34
        text(70, y - 8, ("" if n != L["note_spot"] else "* ") + n, 18.5, MUTED)
    y = top + th

    if not has_f:
        text(40, y + 52, L["nofac"], 22, INK2)
        text(40, H - 30, L["foot"], 18, MUTED)
        fig.savefig(path, dpi=DPI, facecolor=SURF)
        return path

    # 내재변동성 곡선: 전체 범위와 등가격 부근 확대
    cv, a = res["curve"], res["atm"]
    kp, vp, kt, vt = (np.array(cv[d][c], float) for d in ("prev", "today") for c in ("K", "iv"))
    top = y + 28
    card(top, H - top - 66)
    text(70, top + 56, L["curve"], 30, INK, "bold")
    text(70, top + 92, L["curve_s"], 20, INK2)
    left, pw = 132, W - 132 - 76

    def plot(ptop, ph, xlo, xhi, ylo, yhi, xt, yt, big):
        """곡선 두 개와 등가격 점. big이면(확대 그림) 어제 곡선에서 오늘 등가격 행사가의 자리(빈 점)와 값도 적는다."""
        ax = fig.add_axes([left / W, 1 - (ptop + ph) / H, pw / W, ph / H], facecolor="none")
        ax.set_xlim(xlo, xhi)
        ax.set_ylim(ylo, yhi)
        ax.axis("off")
        sx, sy = pw / (xhi - xlo), ph / (yhi - ylo)
        for t in yt:
            ax.plot([xlo, xhi], [t, t], color=BASE if t == ylo else GRID, lw=(1.6 if t == ylo else 1.0) * pt)
            text(left - 14, ptop + (yhi - t) * sy + 8, f"{t:g}", 21, MUTED, ha="right")
        for t in xt:
            ax.plot([t, t], [ylo, yhi], color=GRID, lw=1.0 * pt)
            text(left + (t - xlo) * sx, ptop + ph + 32, _k(t), 21, MUTED, ha="center")
        for k, v, col in ((kp, vp, C_PREV), (kt, vt, C_TODAY)):
            # 범위 밖 이웃 점까지 넘겨 선이 확대 틀 끝까지 이어지게 한다(틀 밖은 잘림). 행사가 간격이 넓은 기초자산에서 선이 중간에 끊기지 않게
            i0 = max(int(np.searchsorted(k, xlo)) - 1, 0)
            i1 = min(int(np.searchsorted(k, xhi, side="right")) + 1, len(k))
            ax.plot(k[i0:i1], v[i0:i1], color=col, lw=4.6 * pt, solid_joinstyle="round", solid_capstyle="round")

        def xy(k, v):
            return left + (k - xlo) * sx, ptop + (yhi - v) * sy

        def dot(c, col, hollow=False):
            r = 13 if big else 11
            fg.add_patch(Circle(c, r + 5, facecolor=SURF, edgecolor="none", zorder=5))
            fg.add_patch(Circle(c, r - 2 if hollow else r, facecolor=SURF if hollow else col, edgecolor=col, lw=(4 if hollow else 0) * pt, zorder=6))

        def rises(k, v, at):
            """at 근처에서 곡선이 오른쪽으로 올라가는가. 확대 폭의 15%만큼 양옆 값을 비교해 잔물결은 무시한다."""
            h = 0.15 * (xhi - xlo)
            return float(np.interp(at + h, k, v) - np.interp(at - h, k, v)) > 0

        def tag(c, s, up, rise, px=21):
            # 곡선이 오른쪽으로 내려가면(SPX) 점의 오른쪽 위와 왼쪽 아래가, 올라가면(VIX 옵션) 왼쪽 위와 오른쪽 아래가 비어 있다
            right = (not rise) if up else rise
            text(c[0] + (18 if right else -18), c[1] + (-20 if up else 38), s, px, INK, "medium",
                 ha="left" if right else "right", ax=fg, zorder=7)
        if not (xlo <= a["strike_prev"] <= xhi and xlo <= a["strike"] <= xhi):
            return ptop + ph, False
        P, Hp, Tp = xy(a["strike_prev"], a["iv_prev"]), xy(a["strike"], a["iv_prev_at_new"]), xy(a["strike"], a["iv"])
        dx, dy = abs(Hp[0] - P[0]), abs(Hp[1] - P[1])
        hollow = big and ((dx >= 100 and dy >= 30) or dx >= 230)       # 옮겨 간 거리가 충분히 클 때만 빈 점을 그린다
        above = a["iv"] >= a["iv_prev_at_new"]                         # 오늘 곡선이 어제 곡선보다 위에 있는가
        if hollow:
            fg.plot([P[0], Hp[0]], [P[1], Hp[1]], color=INK2, lw=2.2 * pt, zorder=4)
            fg.plot([Hp[0], Tp[0]], [Hp[1], Tp[1]], color=INK2, lw=2.2 * pt, zorder=4)
            dot(Hp, C_PREV, hollow=True)
            tag(Hp, f"{a['iv_prev_at_new']:.1f}%", not above, rises(kp, vp, a["strike"]))
        dot(P, C_PREV)
        dot(Tp, C_TODAY)
        if big:
            tag(P, L["atm_prev"].format(v=f"{a['iv_prev']:.1f}"), not above, rises(kp, vp, a["strike_prev"]))
            tag(Tp, L["atm"].format(v=f"{a['iv']:.1f}"), above, rises(kt, vt, a["strike"]))
        return ptop + ph, hollow

    xt, step = _ticks(min(kp[0], kt[0]), max(kp[-1], kt[-1]), 6)          # 기초자산마다 행사가 범위가 달라 눈금을 그때그때 정한다
    xlo, xhi = step * math.floor(min(kp[0], kt[0]) / step), step * math.ceil(max(kp[-1], kt[-1]) / step)
    xt = [float(round(xlo + i * step, 10)) for i in range(int(round((xhi - xlo) / step)) + 1)]
    yt, ystep = _ticks(0, max(vp.max(), vt.max(), 1), 5)
    ymax = ystep * math.ceil(max(vp.max(), vt.max(), 1) / ystep)
    yt = [float(round(i * ystep, 10)) for i in range(int(round(ymax / ystep)) + 1)]
    yb, _ = plot(top + 132, P1, xlo, xhi, 0, ymax, xt, yt, False)
    text(left + pw / 2, yb + 68, L["x"], 21, INK2, ha="center")
    text(64, top + 132 + P1 / 2, L["y"], 21, INK2, ha="center", va="center", rotation=90)

    # 확대: 두 등가격 행사가를 가운데 두고, 지수의 2.5%와 이동 폭의 1.2배 가운데 큰 쪽만큼 양옆을 보인다
    lo_a, hi_a = min(a["strike_prev"], a["strike"]), max(a["strike_prev"], a["strike"])
    hw = max(0.025 * hi_a, 1.2 * (hi_a - lo_a))
    zlo, zhi = max(float(max(kp[0], kt[0])), lo_a - hw), min(float(min(kp[-1], kt[-1])), hi_a + hw)
    inz = np.concatenate([vp[(kp >= zlo) & (kp <= zhi)], vt[(kt >= zlo) & (kt <= zhi)], [a["iv_prev"], a["iv"], a["iv_prev_at_new"]]])
    pad = 0.12 * max(float(inz.max() - inz.min()), 1.0)
    zstep = _ticks(float(inz.min()) - pad, float(inz.max()) + pad, 5)[1]
    ylo2, yhi2 = zstep * math.floor((float(inz.min()) - pad) / zstep), zstep * math.ceil((float(inz.max()) + pad) / zstep)
    zt = [float(round(ylo2 + i * zstep, 10)) for i in range(int(round((yhi2 - ylo2) / zstep)) + 1)]
    xt2 = [t for t in _ticks(zlo, zhi, 4)[0] if zlo + 0.04 * (zhi - zlo) <= t <= zhi - 0.04 * (zhi - zlo)]
    ztop = yb + 140
    t = text(70, ztop - 30, L["zoom"], 24, INK, "bold")
    yb2, hollow = plot(ztop, P2, zlo, zhi, ylo2, yhi2, xt2, zt, True)
    if hollow:
        text(70 + width(t) + 18, ztop - 30, L["hollow"], 19, INK2)
    text(left + pw / 2, yb2 + 68, L["x"], 21, INK2, ha="center")
    text(64, ztop + P2 / 2, L["y"], 21, INK2, ha="center", va="center", rotation=90)
    # 범례
    yl = yb2 + 116
    lab = [(C_PREV, _ymd(res["prev"])), (C_TODAY, _ymd(res["date"]))]
    tw = [width(text(0, -100, s, 22)) for _, s in lab]
    x = (W - (sum(tw) + 2 * 58 + 56)) / 2
    for (col, s), w in zip(lab, tw):
        bg.plot([x, x + 44], [yl - 8, yl - 8], color=col, lw=4.6 * pt, solid_capstyle="round")
        text(x + 58, yl, s, 22, INK2)
        x += 58 + w + 56
    text(40, H - 30, L["foot"], 18, MUTED)
    fig.savefig(path, dpi=DPI, facecolor=SURF)
    return path


# ----------------------------------------------------------------------------- 실행
def run(a):
    if a.from_json:
        with open(a.from_json, encoding="utf-8") as f:
            res = json.load(f)
        res = res.get("vix", res)
        print(report(res))
        if a.png and "error" not in res:
            print("그림:", chart(res, a.png))
        return 0 if "error" not in res else 1
    dates = list_dates(a.archive_dir)
    if a.date:
        dates = [d for d in dates if d <= a.date]
        if not dates or dates[-1] != a.date:
            print(f"[오류] {a.date} 보관본이 없음")
            return 1
    if not dates:
        print("[오류] 보관본이 없음")
        return 1
    dates = dates[-(a.lookback + 1):]
    t = a.ticker.upper()
    st = new_state({} if a.no_yf else index_closes(dates, t), t)
    for d in dates:
        try:
            day = load_day(a.archive_dir, d, [t])
        except Exception as e:
            print(f"[확인 필요] {d} 보관본을 읽지 못함: {type(e).__name__}")
            continue
        if t in day and len(day[t]):
            track(st, d, day[t])
    res = summary(st)
    if t == "SPX" and a.cboe and "error" not in res and res.get("prev"):
        res = official_for(res, [r["date"] for r in st["rows"]])
    print(report(res))
    if a.out_json:
        with open(a.out_json, "w", encoding="utf-8") as f:
            json.dump(clean(res), f, ensure_ascii=False, indent=1)
        print(f"\nJSON: {a.out_json}")
    if a.png and "error" not in res:
        print("그림:", chart(clean(res), a.png))
    return 0 if "error" not in res else 1


def self_test():
    """접속 없이 도는 점검: 이론가로 만든 SPX 체인에서 만기 선택, VIX 수준, 30일 곡선, 요인 여섯 개가 알려진 답과 맞는지 본다."""
    from option_sentiment import prep
    F0, EXP = 5000.0, (("SPXW", "2026-12-04"), ("SPXW", "2026-12-11"), ("SPXW", "2026-12-16"), ("SPX", "2026-12-18"), ("SPXW", "2026-12-18"))

    def chain(date, F, ivf):
        """ivf(K) = 행사가별 IV. 12-18의 SPXW(같은 날 SPX가 있어 쓰면 안 되는 루트)에는 엉뚱한 IV를 넣는다."""
        tv, rows = quote_time(date), []
        K = np.arange(3000.0, 7000.1, 5.0)
        for n, (root, e) in enumerate(EXP):
            h, m = VIX_ROOTS[root]
            T = math.floor((dt.datetime.strptime(e, "%Y-%m-%d").replace(hour=h, minute=m) - tv).total_seconds() / 60) / M365
            iv = np.full(len(K), 0.6) if n == 4 else ivf(K)
            for cp, sgn in (("C", 1.0), ("P", -1.0)):
                px = np.asarray(b76(F, K, T, iv, sgn), float)
                bid = np.where(px >= 0.075, px - 0.05, 0.0)             # 값이 작은 옵션은 매수호가가 0 → VIX 계산에서 빠진다
                rows.append(pd.DataFrame({"Symbol": [f"{root}{e[2:4]}{e[5:7]}{e[8:]}{cp}{int(k * 1000):08d}" for k in K], "Expiry": e,
                                          "Type": "Call" if cp == "C" else "Put", "Strike": K, "Bid": bid, "Ask": px + 0.05, "Last": px,
                                          "Volume": 1, "Open Interest": 1, "IV": iv, "Delta": 0.0, "Gamma": 0.0, "Theta": 0.0, "Vega": 0.0, "Rho": 0.0}))
        return prep(pd.concat(rows, ignore_index=True), "SPX", date)

    def flat(v):
        return lambda K: np.full(len(K), v)

    def skew(K):
        return np.clip(0.20 - 0.6 * np.log(K / F0), 0.08, 0.9)          # 행사가가 낮을수록 IV가 높은 곡선

    d0, d1 = "20261113", "20261116"                                    # 금요일 → 월요일
    a = chain(d0, F0, flat(0.20))
    assert [(f"{e:%m-%d}", r) for e, r in pick_terms(a, d0)] == [("12-11", "SPXW"), ("12-18", "SPX")]
    la = level(a, d0)
    assert abs(la["vix"] - 20.0) < 0.03 and abs(la["spot_implied"] - F0) < 1.0, (la["vix"], la["spot_implied"])
    k, v = la["curve"]
    assert k[0] <= 4000 and k[-1] >= 6000 and abs(float(np.interp(F0, k, v)) - 0.20) < 5e-4      # 30일 곡선: 평평한 20%
    pr = premium(la, 20.0)
    assert abs(sum(pr.values()) - (la["vix"] - 20.0)) < 1e-9 and all(abs(x) < 0.03 for x in pr.values()), pr   # 평평하면 스큐 몫이 없다

    def fac(prev_df, df, s0, s1):
        f = cboe_factors(level(prev_df, d0), level(df, d1), s0, s1)
        assert abs(f["resid"]) < 1e-9, f                                # 요인의 합 = VIX 변화
        return f
    f = fac(a, chain(d1, F0, flat(0.22)), F0, F0)                       # 평평한 곡선이 2%포인트 오름: 평행 이동만 +2
    assert abs(f["parallel"] - 2.0) < 0.01 and all(abs(f[x]) < 0.03 for x in FACTORS if x != "parallel"), {x: f[x] for x in FACTORS}
    b = chain(d0, F0, skew)
    lb = level(b, d0)
    pr = premium(lb, 20.0)
    assert pr["put_body"] > 0.3 and pr["put_wing"] > 0.3 and pr["call_body"] < -0.3, pr      # 풋 쪽은 더하고 콜 쪽은 던다
    f = fac(b, chain(d1, 0.98 * F0, skew), F0, 0.98 * F0)               # 지수 -2%, 행사가별 IV는 그대로: 곡선 따라 이동만
    assert abs(f["sticky"] - 60 * math.log(1 / 0.98)) < 0.02 and abs(f["parallel"]) < 0.02, (f["sticky"], f["parallel"])
    assert f["atm"]["strike_prev"] == 5000 and f["atm"]["strike"] == 4900 and atm_strike(7722.01) == 7720 and atm_strike(7364.33) == 7365
    g = fac(b, chain(d1, 0.98 * F0, lambda K: skew(K) + np.where(K < 0.8 * F0, 0.05, 0.0)), F0, 0.98 * F0)   # 거기에 먼 풋만 5%포인트 더 비싸짐
    assert g["down_conv"] - f["down_conv"] > 0.3 and abs(g["sticky"] - f["sticky"]) < 1e-9 and abs(g["up_conv"] - f["up_conv"]) < 0.02
    # 금요일이 휴장인 주(12-25, 01-01)는 목요일 만기를 쓴다
    e = pd.DataFrame({"exp": pd.to_datetime(["2026-12-11", "2026-12-18", "2026-12-18", "2026-12-23", "2026-12-24", "2026-12-31", "2027-01-08"]),
                      "root": ["SPXW", "SPX", "SPXW", "SPXW", "SPXW", "SPXW", "SPXW"]})
    assert [f"{x:%m-%d}" for x, _ in pick_terms(e, "20261125")] == ["12-24", "12-31"]
    st = new_state({d0: {"spx": F0, "vix": lb["vix"]}, d1: {"spx": 0.98 * F0, "vix": 30.0}})
    track(st, d0, b)
    track(st, d1, chain(d1, 0.98 * F0, skew))
    res = clean(summary(st))
    assert json.dumps(res) and "요인" in report(res) and any("공식 종가" in x for x in res["flags"]), res["flags"]
    assert abs(sum(res["factors"].values()) - res["chg"]) < 1e-5 and res["curve"]["prev"]["K"][0] == res["curve"]["today"]["K"][0]
    try:
        import matplotlib  # noqa: F401
    except ImportError:
        print("자체 점검 통과(그림 제외: matplotlib 없음)")
        return 0
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        assert os.path.getsize(chart(res, os.path.join(tmp, "t.png"))) > 30000
    print("자체 점검 통과")
    return 0


def main():
    if "--self-test" in sys.argv:
        return self_test()
    p = argparse.ArgumentParser(description="보관본의 옵션 호가로 VIX식 지수(SPX→VIX, VIX 옵션→VVIX, GLD→GVZ)를 직접 계산하고 하루 변화를 여섯 요인으로 나눈다")
    p.add_argument("--archive-dir", default="data/daily", help="quotes_YYYYMMDD.tar.gz가 있는 폴더")
    p.add_argument("--ticker", default="SPX", choices=sorted(SPECS), help="기초자산(SPX, VIX, GLD)")
    p.add_argument("--cboe", action="store_true", help="SPX의 요인·등가격·곡선을 Cboe VIX Decomposition 도구의 공식값으로 쓴다")
    p.add_argument("--date", help="기준일 YYYYMMDD(생략하면 최신 보관본)")
    p.add_argument("--lookback", type=int, default=20, help="'평소 크기'를 잴 때 쓸 과거 수집일 수")
    p.add_argument("--png", help="그림을 저장할 경로")
    p.add_argument("--out-json")
    p.add_argument("--no-yf", action="store_true", help="야후 파이낸스의 S&P 500·VIX 종가를 받지 않음")
    p.add_argument("--from-json", help="이미 계산해 둔 JSON(이 스크립트나 option_sentiment.py의 출력)으로 보고와 그림만 다시 만든다")
    return run(p.parse_args())


if __name__ == "__main__":
    sys.exit(main())
