# auto-trade

바이낸스 USDT-M 무기한 선물 알트코인 롱 전략 연구/백테스트.

## 급등 패턴 스캐너 (analyzer/pattern_scan.py)

바이낸스 공개 API 로 지금 시장을 스캔합니다 (API 키 불필요). Windows: `run_scan.bat`

**방식 1 — 급등 패턴 유사도** (`--mode pattern`)
1. 24h 상승률 상위 5개 종목 선정 (주식/ETF/원자재/법정화폐·스테이블 선물 제외, 24h 거래대금 $2M 이상)
2. 각 종목의 마지막 상승 구간 시작점(launch)을 찾고, 그 **직전까지** 4h 42봉 · 1h 48봉 · 15m 48봉 차트와 특징값
   (7일/24h 수익률, 30일 고점 대비, RSI, ATR%, BB폭 순위, 거래량 배수, 펀딩, OI 24h 변화)을 기록
   → `analyzer/patterns/library.jsonl` 누적 + 차트 PNG
3. 나머지 코인의 지금 차트와 비교해 유사도 순위 → `analyzer/out/scan_*.md / .csv / PNG`
   (`--library` 를 주면 그동안 쌓인 패턴 전체와 비교)

**방식 2 — 횡보 후 거래량 증가** (`--mode range`)
1. 최근 4h 를 뺀 이전 24h 가 박스권인 코인: 고저폭 3% 이내 / 5% 이내 두 단계
2. 그중 최근 4h 평균 거래량이 박스 구간 평균의 2배 이상인 코인 → `analyzer/out/range_*.md / .csv`
   (박스 상단 돌파 / 박스 안 / 하단 이탈 표시)

기본값은 두 방식 모두 실행. 주요 옵션: `--top 5 --pick 20 --lookback 72 --range-hours 24 --recent 4 --vol-mult 2`

```
python analyzer/pattern_scan.py
python analyzer/pattern_scan.py --mode range --range-hours 48 --vol-mult 1.5
```

### 6가지 방식 비교 (analyzer/tracker.py)

| 방식 | 조건 |
|---|---|
| M0 무작위 | 기준선. 조건 없이 무작위 10종목 |
| M1 급등 차트 유사 | 24h 상승 상위 5개의 상승 직전 패턴과 닮은 상위 5종목 |
| M2 횡보 + 거래량 | 직전 24h 박스폭 3%/5% 이내 + 최근 4h 거래량 2배↑ |
| M3 이평선 골든크로스 | 1h EMA20 이 EMA50 상향 돌파(4h 이내) + 종가 > EMA200 |
| M4 RSI 과매도 반등 | 1h RSI 30 아래 → 위 회복 (강세 다이버전스 동반 시 div) |
| M5 펀딩+OI 숏스퀴즈 | 7일 -15%↓, 24h -12%~+3%, 펀딩 ≤ 0, OI 24h +10%↑ |
| M6 BTC 대비 상대강도 | BTC 눌림 후 회복 중, 코인/BTC 비율 7일 고점 근처 + 7일 수익률 BTC 대비 +10%p↑ |

- 스캔할 때마다 신호를 `analyzer/data/tracker.db` 에 쌓고, 4h·24h·72h 뒤 수익률, 최대 상승/하락,
  가상 거래(손절 -8% / 익절 +15% / 72h, 수수료 0.2%) 결과를 자동 계산합니다. 모든 신호에 BTC 20일선 필터 여부도 기록.
- `python analyzer/tracker.py scan` : 지금 스캔 + 기록 + 지난 신호 성과 갱신 (대시보드의 스캔 실행 버튼과 같음)
- `python analyzer/tracker.py backfill --days 60 --every 4` : 과거 60일을 4시간마다 스캔한 것처럼 채워 바로 비교 가능
  (공개 아카이브 사용, 처음엔 15m 데이터 다운로드로 오래 걸림)
- `python analyzer/tracker.py report` : 비교표 `analyzer/out/compare_*.md`
- 대시보드 **6가지 비교** 탭: 과거 채움 / 실시간 기록, 전체 / BTC 필터 전환

### 웹 대시보드 (localhost)

> 실행 전 Python 3.9 이상 필요 (https://www.python.org/downloads/ , 설치 시 "Add python.exe to PATH" 체크).
> `run_*.bat` 이 "Python" 만 찍고 끝나면 Microsoft Store 가짜 python 이 잡힌 것 → 파이썬 설치 후
> 설정 > 앱 > 고급 앱 설정 > 앱 실행 별칭 에서 python.exe / python3.exe 별칭을 끄세요.

Windows: `run_web.bat` 더블클릭 → 브라우저에서 **http://localhost:8000** 이 열립니다. (직접 실행: `python analyzer/web.py`)

- 화면에서 방식·상위 종목 수·횡보 구간·거래량 배수 등을 정하고 **스캔 실행** (실행 로그 실시간 표시, 15/30/60분 자동 반복 가능)
- 탭: 급등 패턴 유사(급등 종목 차트 + 유사 후보 순위, 행 클릭 시 차트 확대) / 횡보 + 거래량(3%·5% 박스) / 패턴 기록(누적) / 백테스트 결과
- 지난 실행 결과는 상단 "실행 기록"에서 선택. 표 머리글을 누르면 정렬
- 추가 설치 없음(파이썬 표준 라이브러리 서버). 127.0.0.1 에만 열려 같은 PC 에서만 접속됩니다. 포트 변경: `run_web.bat --port 8080`

## 국면별 백테스트 (프로그램)

BTC 시장 국면 5구간(`scripts/periods.py`)마다 같은 전략을 돌려 비교합니다.

| 구간 | 기간 |
|---|---|
| 1 하락장 | 2025-10-06 ~ 2026-11-26 |
| 2 상승장 | 2024-11-05 ~ 2025-07-14 |
| 3 반감기 후 횡보→상승 | 2024-04-20 ~ 2024-12-17 |
| 4-1 횡보장 | 2023-03-20 ~ 2023-10-15 |
| 4-2 횡보장 | 2026-02-06 ~ 2026-05-31 |

### 실행 (Windows)

`run_backtest.bat` 더블클릭, 또는:

```
pip install -r requirements.txt
python backtest\regime_bt.py all
```

- 처음엔 전 종목 1h 봉/펀딩 수집에 시간이 걸립니다(`data/bt/` 에 캐시, 이후 재사용).
- 결과: `backtest/results_regime/summary.md`, `summary.json`, `trades.csv`
- 옵션: `--fee 0.002`(왕복 수수료), `--periods 1_bear,4_2_range`, `--limit 50`(빠른 테스트)

전략 규칙은 `backtest/regime_bt.py` 상단 설명에 있습니다.
데이터는 `data.binance.vision` 공개 아카이브만 사용합니다(API 키 불필요, 상장폐지 종목 포함).

## 급등 차트 특징 연구

`study/` — 최근 60일 +30% 급등 코인의 15m/1h/4h 차트 특징. 결과: `study/out/pump_study.md`
