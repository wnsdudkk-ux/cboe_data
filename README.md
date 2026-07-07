# cboe_data — CBOE 옵션 일일 스냅샷 저장소

매 거래일 미국 옵션시장 마감 후, S&P500 전 종목(+주요 지수·ETF)의 옵션 체인을
CBOE 지연시세 API에서 내려받아 **하루 = 압축파일 하나**로 이 저장소에 쌓는다.

수집 실행 주체는 **claude.ai 클라우드 루틴**이다 (매일 07:30 KST, 별도 서버 불필요).
이전에 같은 일을 하던 AWS EC2 상시 서버를 대체한다.

## 데이터 구조

```
data/daily/quotes_20260702.tar.gz     # 하루치 전체 (약 10MB)
  └─ quotes_20260702/
       ├─ SPX/spx_quotes_20260702.csv
       ├─ BRK.B/brk.b_quotes_20260702.csv
       └─ ... (티커별 폴더, 약 520여 개)
```

CSV 컬럼(기존 options-project 대시보드와 동일):
`Symbol, Expiry, Type, Strike, Bid, Ask, Last, Volume, Open Interest, IV, Delta, Gamma, Theta, Vega, Rho`

하루치 꺼내 쓰기:
```bash
tar -xzf data/daily/quotes_20260702.tar.gz        # 현재 폴더에 풀림
```

## 수집 대상

- **S&P500 구성종목** (~503개): 매 실행 시 [datasets/s-and-p-500-companies](https://github.com/datasets/s-and-p-500-companies)
  에서 최신 명단을 받아오고, 실패 시 저장소의 `sp500_fallback.txt` 스냅샷 사용.
  클래스주는 CBOE가 요구하는 점 표기(BRK.B, BF.B) 그대로.
- **지수 5** (SPX·VIX·NDX·RUT·DJX) + **ETF 17** (SPY·QQQ 등): 분석의 핵심
  기초자산이라 기본 포함. 빼려면 `--sp500-only`.

## 실행 (로컬 수동 실행도 동일)

```bash
pip install -r requirements.txt
python collect.py                # 이미 오늘치가 있으면 'exists' 출력 후 종료
python collect.py --force        # 재수집
python collect.py --tickers BRK.B NVR --out-dir /tmp/t   # 테스트
python -m unittest discover -s tests                      # 단위테스트 (21개)
```

출력 규약: `OK:`(성공) / `exists:`(할 일 없음) / `ERROR:`(성공률 미달 — 아카이브
미생성, 커밋 금지). 종료코드 0/0/1.

## 설계상 중요한 결정들

- **날짜 라벨 = 마지막으로 종가가 확정된 NYSE 세션.** CBOE 지연시세는 항상 직전
  마감 세션의 스냅샷이므로, 마감(16:00 ET)+15분 이전이면 당일을 세지 않는다.
  덕분에 라벨과 내용이 항상 일치하고, 주말·휴장일 실행은 자동으로 `exists`로 끝난다.
- **멱등성**: 같은 날짜 아카이브가 있으면 아무것도 안 한다. 그래서 부분 실패
  상태를 커밋하면 안 되며(영영 재수집 안 됨), **성공률 90% 미만이면 아카이브를
  만들지 않는다**.
- **403의 두 얼굴**: CBOE는 '없는 심볼'에 404가 아니라 403을 준다(실측: NVR 등)
  → no_data로 분류. 반면 클라우드 샌드박스의 도메인 차단도 403인데 이때는
  `x-deny-reason` 헤더가 붙는다 → 환경설정 오류로 즉시 실패 처리.
- **OSI 옵션심볼은 꼬리 고정폭으로 파싱**: `[뿌리(가변)][YYMMDD][C/P][행사가*1000, 8자리]`.
  뿌리가 BRK.B, AAPL1처럼 가변이라 앞에서 자르면 깨진다.
- **429 레이트리밋**: Retry-After 헤더 존중(상한 120초), 연속 실패 5회면 60초 냉각.
- **원자적 쓰기**: 아카이브는 `.tmp`에 만들고 `os.replace()`로 교체.

## 클라우드 루틴 설정 (claude.ai/code/routines)

- 스케줄: 매일 07:30 KST (= 미 동부 전일 18:30, 장 마감 후)
- 환경: Network access **Custom** + Allowed domains에 `cdn.cboe.com`,
  `raw.githubusercontent.com` (+기본 패키지 목록 포함 체크)
- 환경 Setup script: `pip install -r requirements.txt` (실행 간 캐시됨)
- 레포 권한: Claude GitHub App에 이 레포 접근 허용 + 루틴 Permissions에서
  "Allow unrestricted branch pushes" ON (main에 커밋하므로)
- 지침(Instructions)은 루틴 편집 화면 참고 — `collect.py` 실행 후 `OK:`일 때만
  `data/daily`를 커밋·푸시한다.

## 용량 전망과 관리

하루 ~10MB → 연 ~2.5GB. 몇 년 뒤 저장소가 무거워지면 연 단위로 과거 아카이브를
GitHub Releases로 옮기는 정리를 고려할 것.
