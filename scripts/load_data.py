"""Binance 공개 아카이브(data.binance.vision)의 BTCUSDT 일봉 zip을 하나의 CSV로 합친다."""
import glob, zipfile, io
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
COLS = ["open_time","open","high","low","close","volume","close_time",
        "quote_volume","trades","taker_base","taker_quote","ignore"]

def build():
    frames = []
    for z in sorted(glob.glob(str(ROOT / "data/raw/*.zip"))):
        with zipfile.ZipFile(z) as zf:
            for n in zf.namelist():
                frames.append(pd.read_csv(io.BytesIO(zf.read(n)), header=None, names=COLS))
    df = pd.concat(frames)
    ts = df["open_time"].astype("int64")
    ts = ts.where(ts < 1e14, ts // 1000)  # 2025년부터 마이크로초 단위
    df["date"] = pd.to_datetime(ts, unit="ms").dt.normalize()
    df = df.drop_duplicates("date").sort_values("date").set_index("date")
    df = df[["open","high","low","close","volume"]].astype(float)
    df.to_csv(ROOT / "data/btcusdt_1d.csv")
    return df

if __name__ == "__main__":
    d = build()
    print(d.index.min(), d.index.max(), len(d))
