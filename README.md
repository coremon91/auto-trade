# auto-trade

바이낸스 USDT-M 무기한 선물 알트코인 롱 전략 연구/백테스트.

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
