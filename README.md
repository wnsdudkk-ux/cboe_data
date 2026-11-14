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
- **지수 5** (SPX·VIX·NDX·RUT·DJX) + **ETF 18** (SPY·QQQ·SOXX 등): 분석의 핵심
  기초자산이라 기본 포함. 빼려면 `--sp500-only`.

## 실행 (로컬 수동 실행도 동일)

```bash
pip install -r requirements.txt
python collect.py                # 이미 오늘치가 있으면 'exists' 출력 후 종료
python collect.py --force        # 재수집
python collect.py --tickers BRK.B NVR --out-dir /tmp/t   # 테스트
python -m unittest discover -s tests                      # 단위테스트 (64개)
```

출력 규약: `OK:`(성공) / `exists:`(할 일 없음) / `ERROR:`(성공률 미달·수집 가능 시간
경과 등 — 아카이브 미생성, 커밋 금지). 종료코드 0/0/1. `OK:` 뒤에는 저장하지 않은 종목과
`WARN:`(핵심 종목 누락 등) 줄이 붙을 수 있다. 이때도 아카이브는 만들어졌으므로 커밋한다.

## 전일 대비 분석 — option_sentiment.py

쌓인 보관본 두 날짜(최신과 그 직전 수집일)를 비교해 옵션 시장이 하루 사이 어떻게
달라졌는지 수치로 뽑는다. 압축을 풀지 않고 필요한 종목의 CSV만 바로 읽는다.
claude.ai 예약 작업(화~토 아침 KST)이 이 스크립트를 받아 실행한다.

```bash
# 저장소 전체(1.5GB 이상)를 받지 않고 스크립트와 최근 21일치 보관본만 받는다 (약 380MB, 20초)
git clone --depth 1 --filter=blob:none --no-checkout https://github.com/wnsdudkk-ux/cboe_data.git && cd cboe_data
git checkout HEAD -- option_sentiment.py $(git ls-tree -r --name-only HEAD data/daily | grep -E 'quotes_[0-9]{8}\.tar\.gz$' | sort | tail -21)
pip install pandas numpy exchange_calendars yfinance      # 뒤의 둘은 선택(휴장일 판정, 종가 대조)
python option_sentiment.py --archive-dir data/daily --tickers SPX VIX GLD --out-json out.json
python option_sentiment.py --archive-dir data/daily --tickers QQQ --date 20261113   # 날짜 지정
python option_sentiment.py --self-test                     # 접속 없이 계산 로직 점검
```

`git ls-tree -l`처럼 파일 크기를 묻는 명령은 모든 보관본을 내려받게 만드니 쓰지 않는다.
종료 코드: `0` 분석함 / `3` 오늘치 보관본 없음 / `4` 휴장(최근 24시간 안에 마감한 거래일
없음) / `1` 오류. `--date`를 주면 휴장·누락 점검을 건너뛴다.

**계산하는 것** (종목별, 직전 수집일 대비 변화와 최근 20수집일 안에서의 위치)

- 기초자산 수준(풋-콜 패리티로 구한 선도가)과 선도가 곡선(VIX에서는 선물 곡선에 해당)
- 등가격 IV(잔존 7·30·90·180일로 환산), 기간구조(7일−30일, 30일−90일)
- 스큐(25델타 풋 IV − 콜 IV), 꼬리(10델타 IV − 등가격 IV)
- 행사가 고정 IV 변화: 등가격 IV 변화를 "기초자산이 움직여 곡선 위에서 옮겨 간 몫"과
  "같은 행사가의 IV가 실제로 바뀐 몫"으로 나눈다
- 옵션 가격 변화 분해: 25델타 풋·콜과 등가격 스트래들의 가격 변화 =
  기초자산(델타·감마) + IV(베가) + 시간(세타) + 나머지
- 거래량·거래대금·미결제약정의 풋÷콜 비율, 미결제약정이 크게 늘거나 준 계약
- 감마 노출 추정(딜러가 콜은 매수, 풋은 매도 포지션이라는 관행적 가정), 호가 스프레드

**계산 기준에서 알아 둘 것**

- IV는 파일의 `IV` 열을 쓰지 않고 마감 호가 중간값에서 직접 역산한다(Black-76, 달력
  일수). 파일의 `IV`·`Delta`는 Cboe가 수집 시각의 기초자산 추정치로 다시 계산한 값이어서
  마감 호가와 어긋나는 날이 있다. 2026-10-23~11-13(16거래일)의 SPX 30일 등가격 IV로
  대조하니 두 값의 차이는 중앙값 0.05%p였지만 11-13에는 0.34%p였고, 하루 변화를 VIX
  종가 변화에 맞춰 보면 호가 기준이 더 잘 맞았다(잔차 표준편차 0.19 대 0.32 VIX포인트,
  표본 15개). VIX 옵션은 파일 `IV` 열이 크게 틀리는 날이 많다. 파일 값은 대조용으로만
  출력하고, 평소보다 크게 어긋난 날은 `[확인 필요]`로 알린다.
- 보관본에는 기초자산 가격과 수집 시각 열이 없다. 기초자산 수준은 패리티 선도가로,
  수집 시각은 압축파일 안 CSV의 수정 시각으로 읽는다. SPX·VIX 옵션은 정규장 뒤
  17:00(미 동부)까지 연장 거래가 있어 선도가에 마감 뒤 움직임이 섞인다. 야후 종가를
  받을 수 있으면 함께 출력한다.
- `Open Interest`는 전 영업일 마감 기준이다. 두 파일 사이의 증감은 **전 거래일** 거래의
  결과이고 `Volume`은 당일 것이다(11-12→11-13 SPX에서 증감과 11-12 거래량의 상관 0.81,
  11-13 거래량과는 0.31).
- "평소의 N배"는 그 지표의 과거 하루 변화 절댓값의 중앙값 대비 오늘 변화의 크기다.

## 설계상 중요한 결정들

- **날짜 라벨 = 마지막으로 종가가 확정된 NYSE 세션.** 마감(16:00 ET, 조기폐장이면
  13:00)+15분 이전이면 당일을 세지 않는다. 주말·휴장일 실행은 자동으로 `exists`로 끝난다.
- **라벨과 내용이 같은 날인지 확인한다(2026-11 추가).** 라벨은 달력으로 정하지만 내용은
  CBOE가 준 파일이라, 둘이 어긋나는 경우를 따로 막는다.
  - *수집 가능 시간*: 다음 거래일 정규장이 열린 뒤에는 `ERROR:`로 끝낸다(그때 받으면 다음
    날 장중 자료에 전날 라벨이 붙는다). 야간 거래가 있는 지수 옵션(SPX·VIX·XSP)은 일~목
    20:15 ET에 다음 세션 호가가 시작되므로, 그 뒤에 돌면 이 종목만 빼고 `WARN:`을 낸다.
    지정 티커 모드(`--tickers`)는 검사하지 않고, `--allow-late`로 끌 수 있다.
  - *종목별 자료 시점*: CBOE의 JSON 파일은 종목마다 만들어진 시각이 다르다. 2026-11-13
    마감 뒤 예비 명단 기준 526개의 파일 생성 시각을 확인하니 6개가 마감 전에 만들어진
    파일이었다(거래가 끊긴 KVUE 11-06·WBD 10-19·PSKY 10-12, 당일 장중 HONA 10:10·Q 14:00·
    UUP 15:49 ET). 실제 보관본에서는 KVUE가 거래 종료 뒤 11-02~11-04 사흘간 호가가 전부
    0인 같은 자료로 저장됐고, 11-13 보관본의 HONA·Q·UUP는 장중 파일이다. 그래서 파일의
    생성 시각(`timestamp`)과 기초자산 마지막 체결일을 보고, **그 세션이 열리기 전에
    만들어진 파일과 기초자산이 그날 거래되지 않은 종목은 저장하지 않는다.** 그날 장중에
    만들어진 파일은 저장하되 출력에 적는다.
  - *갱신 요청*: 낡은 파일을 만나면 CBOE 시세 페이지가 하는 것과 같은 갱신 요청
    (`www.cboe.com/delayed_quote/api/options/<종목>`)을 보내고 새 파일을 기다린다. 한 번
    실행에 30건까지. 허용 도메인에 `www.cboe.com`이 없으면 건너뛰고 그 사실을 출력한다.
- **2차 시도**: 일시 오류로 실패한 종목과 응답이 비어 온 핵심 종목(지수 5·ETF 18)은
  전체를 한 바퀴 돈 뒤 한 번 더 받는다. 끝까지 빠진 핵심 종목은 `WARN:`으로 알린다.
- **멱등성**: 같은 날짜 아카이브가 있으면 아무것도 안 한다. 그래서 부분 실패
  상태를 커밋하면 안 되며(영영 재수집 안 됨), **성공률 90% 미만이면 아카이브를
  만들지 않는다**.
- **403의 두 얼굴**: CBOE는 '없는 심볼'에 404가 아니라 403을 준다(실측: NVR 등)
  → no_data로 분류. 반면 클라우드 샌드박스의 도메인 차단도 403이다: `x-deny-reason`
  헤더가 붙은 403이거나, 프록시가 CONNECT 자체를 거부(`Tunnel connection failed: 403`)
  한다 → 막힌 호스트 이름을 담아 `ERROR:`로 즉시 실패(수 초). 단 이미 성공한 티커가
  있으면 호스트는 허용된 것이므로 일시 장애로 보고 계속한다.
- **CBOE 호스트 이전(2026-09)**: `cdn.cboe.com/api/...`가 전부 `cdn-api.cboe.com`으로
  307 리다이렉트되기 시작했다. 허용 도메인에 `cdn.cboe.com`만 있던 루틴이 이 때문에
  09-24·09-25분 수집에 실패했고, 이후 `CBOE_URL`을 `cdn-api.cboe.com`으로 바꿨다.
- **OSI 옵션심볼은 꼬리 고정폭으로 파싱**: `[뿌리(가변)][YYMMDD][C/P][행사가*1000, 8자리]`.
  뿌리가 BRK.B, AAPL1처럼 가변이라 앞에서 자르면 깨진다.
- **429 레이트리밋**: Retry-After 헤더 존중(상한 120초), 연속 실패 5회면 60초 냉각.
- **원자적 쓰기**: 아카이브는 `.tmp`에 만들고 `os.replace()`로 교체.

## 클라우드 루틴 설정 (claude.ai/code/routines)

- 스케줄: 매일 07:30 KST (= 미 동부 전날 17:30, 서머타임 기간에는 18:30).
  SPX·VIX 옵션은 정규장 뒤 17:00 ET까지 연장 거래가 있고 시세가 15분 지연되므로,
  17:15 ET 이후에 받아야 마감 호가가 확정된 상태다. 07:00 KST(겨울 17:00 ET)는 이르다.
  야간 거래가 시작되는 20:15 ET(겨울 10:15 KST, 여름 09:15 KST) 전에는 끝나야 한다.
- 환경: Network access **Custom** + Allowed domains에 `cdn-api.cboe.com`,
  `cdn.cboe.com`, `raw.githubusercontent.com` (+기본 패키지 목록 포함 체크).
  `cdn-api.cboe.com`이 빠지면 `ERROR: 환경 네트워크 차단(...)`으로 끝난다.
  `www.cboe.com`은 선택이다. 넣으면 낡은 파일의 갱신 요청이 동작한다.
- 환경 Setup script: `pip install -r requirements.txt` (실행 간 캐시됨)
- 레포 권한: Claude GitHub App에 이 레포 접근 허용 + 루틴 Permissions에서
  "Allow unrestricted branch pushes" ON (main에 커밋하므로)
- 지침(Instructions)은 루틴 편집 화면 참고 — `collect.py` 실행 후 `OK:`일 때만
  `data/daily`를 커밋·푸시한다.

## 용량 전망과 관리

하루 ~10MB → 연 ~2.5GB. 몇 년 뒤 저장소가 무거워지면 연 단위로 과거 아카이브를
GitHub Releases로 옮기는 정리를 고려할 것.
