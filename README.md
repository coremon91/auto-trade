# auto-trade: 급등 종목 스크리너

바이낸스 USDT-M 무기한 선물 전 종목의 공개 데이터를 모아서, 과거 급등(기본값: 72시간 안에 +20%)
직전의 특징을 학습하고, 현재 종목을 점수화해 **관찰 대상 워치리스트**를 만듭니다.
주문 실행이나 API 키는 사용하지 않습니다. 진입은 이 목록을 받아 15분봉 거래량 돌파 등 별도 규칙으로 판단하는 구조입니다.

## 설치

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## 사용 순서

```bash
# 1) 과거 데이터: data.binance.vision 아카이브 (상장폐지 종목 포함, OI/롱숏 5분 데이터 포함)
python -m screener backfill --start 2025-06-01            # 전 종목, 시간이 오래 걸림
python -m screener backfill --start 2025-06-01 --no-metrics  # OI/롱숏 생략, 빠름

# 2) 최근 데이터 + 이어받기: REST (현재 상장 종목)
python -m screener collect

# 3) 학습 + 검증 리포트 (models/report.json, models/test_predictions.csv.gz)
python -m screener train
python -m screener train --pump 0.3 --horizon 72        # +30% 기준으로 다시

# 4) 현재 종목 점수화 → output/watchlist_latest.md, output/watchlist_YYYYMMDD_HHMM.csv
python -m screener score --top 30
python -m screener score --sort p_pump_first             # 급락 전에 급등할 확률 순

# 과거 급등 이벤트와 직전 특징 (분석용)
python -m screener events --out output/pump_events.csv
```

**collect는 주기적으로 돌려야 합니다.** 바이낸스 REST는 OI·롱숏비율을 약 30일치만 주기 때문에,
계속 쌓아야 학습 데이터가 길어집니다. 예시 (4시간마다):

```cron
5 */4 * * * cd /path/to/auto-trade && .venv/bin/python -m screener collect >> data/collect.log 2>&1 && .venv/bin/python -m screener score
```

## 구성

| 파일 | 역할 |
|---|---|
| `screener/binance.py` | REST(fapi) + 아카이브(data.binance.vision) 클라이언트, 레이트리밋 처리 |
| `screener/storage.py` | SQLite 저장 (`data/market.sqlite`), 중복 없이 upsert, 상장폐지 종목도 삭제하지 않음 |
| `screener/collector.py` | `collect`(이어받기) / `backfill`(아카이브) |
| `screener/features.py` | 1시간 격자 위 특징 45개 + 라벨 |
| `screener/model.py` | 시간 분할 학습/검증, 모델 3개 저장 |
| `screener/scorer.py` | 최신 봉 점수화 → 워치리스트 |
| `screener/events.py` | 과거 급등 이벤트 추출 |

## 라벨 (모델 3개)

- `p_pump`: 다음 72시간 고가가 현재 종가 대비 +20% 이상
- `p_dump`: 다음 72시간 저가가 -15% 이하 (리스크)
- `p_pump_first`: +20%를 -15%보다 먼저 도달 (같은 봉에서 둘 다면 급락으로 처리)

롱 진입 관점에서는 `p_pump`만 보지 말고 `p_dump`, `p_pump_first`를 같이 봐야 합니다.
이전 분석에서 급등 신호가 강한 종목은 급락 확률도 함께 높았습니다(방향이 아니라 큰 움직임을 예측).

## 특징

- 가격/변동성: 1h~30일 수익률, 30일 고점 대비 낙폭, BB 폭(4시간봉 20개 상당)과 그 백분위, ATR
- 거래량: 8h·24h 거래량 배수, 테이커 매수 비중, 체결 수 배수, 거래대금
- OI: 4h/24h/72h 변화율, OI/거래대금
- 펀딩/포지션: 최근 펀딩, 72h 최저·평균, 글로벌 롱숏, 탑트레이더 포지션 롱숏, 테이커 롱숏
- 이력/시장: 최근 60일 급등 횟수(반복 급등주), 30일 최대 상승폭, 상장 경과일, BTC 24h/72h 수익률
- 같은 시점 전체 종목 대비 순위: 7일 수익률, BB 폭, 거래량 배수, OI 변화, 펀딩
- 이전 분석의 규칙 신호: 가격횡보+OI 10%↑, MANTRA형(7일 -15% 이하 + OI 급증), 음수펀딩(≤-0.03%),
  조용한 거래량 급증(3배↑, 가격 ±5% 이내), 급락+OI 급증(고위험), 고변동+7일 상승 상위 20%

## 편향 방지

- **미래 정보 차단**: 1h 봉은 마감 시각(`open_time + 1h`)에만 알려진 것으로 보고, OI·펀딩·롱숏은
  그 시각 이전 값만 as-of 조인합니다. 테스트(`test_features_have_no_lookahead`)가 이를 확인합니다.
- **생존 편향**: 아카이브 백필은 상장폐지 종목까지 포함하고, DB는 종목을 지우지 않습니다.
  음성 샘플은 "조용한 시기"를 골라내지 않고 모든 종목·모든 시점(4시간 간격)에서 뽑습니다.
- **시간 분할**: 마지막 25% 기간이 검증, 그 직전 72시간은 엠바고(라벨 창이 겹치지 않게).
  리포트의 수치는 검증 기간 기준이고, 저장되는 최종 모델은 전체 기간으로 다시 학습합니다.

## 한계

- 이전 분석 기준 급등의 약 73%는 차트상 선행 신호가 없었습니다. 리프트는 기대할 수 있지만 대부분의 급등을 미리 잡지는 못합니다.
- 하락장 데이터만으로 학습하면 상승장에서 다르게 동작할 수 있으니, 백필 기간에 상승장을 포함하는 것이 좋습니다.
- BTC 도미넌스는 아직 특징에 없습니다(바이낸스 BTCDOMUSDT 선물을 대용으로 추가 가능).

## 테스트

```bash
pytest -q
```

합성 데이터(OI 선행 후 급등을 심은 랜덤워크)로 라벨, 미래정보 차단, 엠바고, 저장소, 수집 이어받기,
학습→점수화 전체 흐름을 검사합니다. 네트워크는 쓰지 않습니다.
