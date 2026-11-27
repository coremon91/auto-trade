"""data.binance.vision 에서 USDT-M 무기한 선물 klines 수집.

python study/fetch.py --interval 1h --start 2026-08-01 --end 2026-11-26 --out data/fut_1h
python study/fetch.py --interval 15m --start 2026-08-01 --end 2026-11-26 --out data/fut_15m --symbols A,B
"""
import argparse, io, os, re, sys, time, zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
import numpy as np, pandas as pd, requests

BASE = "https://data.binance.vision"
S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
sess = requests.Session()

def get(url, tries=4):
    for a in range(tries):
        try:
            r = sess.get(url, timeout=30)
            if r.status_code == 404: return None
            if r.status_code == 200: return r.content
        except requests.RequestException: pass
        time.sleep(1.5*(a+1))
    return None

def list_symbols():
    out, marker = [], ""
    while True:
        t = sess.get(f"{S3}?delimiter=/&prefix=data/futures/um/daily/klines/&marker={marker}", timeout=30).text
        p = re.findall(r"<Prefix>data/futures/um/daily/klines/([^/<]+)/</Prefix>", t)
        out += p
        if "<IsTruncated>true</IsTruncated>" not in t or not p: break
        marker = f"data/futures/um/daily/klines/{p[-1]}/"
    return sorted(s for s in set(out) if s.endswith("USDT") and "_" not in s)

def read_zip(b):
    with zipfile.ZipFile(io.BytesIO(b)) as z: raw = z.read(z.namelist()[0]).decode()
    hdr = not raw.split(",",1)[0].strip().isdigit()
    df = pd.read_csv(io.StringIO(raw), header=0 if hdr else None).iloc[:, [0,1,2,3,4,5,7,9,10]]
    df.columns = ["ts","o","h","l","c","v","qv","tbv","tqv"]
    return df

def months(a, b):
    d = date(a.year, a.month, 1)
    while d <= b:
        yield d; d = date(d.year + (d.month==12), d.month%12+1, 1)

def fetch(sym, iv, start, end, root):
    path = f"{root}/{sym}.pkl"
    if os.path.exists(path): return sym, "cached"
    # 마지막 날 daily 파일이 없으면 현재 상장 종목이 아님 → 스킵
    last = get(f"{BASE}/data/futures/um/daily/klines/{sym}/{iv}/{sym}-{iv}-{end:%Y-%m-%d}.zip")
    if last is None: return sym, "inactive"
    parts = [read_zip(last)]
    cur_m = date(end.year, end.month, 1)
    for m in months(start, cur_m - timedelta(days=1)):
        b = get(f"{BASE}/data/futures/um/monthly/klines/{sym}/{iv}/{sym}-{iv}-{m:%Y-%m}.zip")
        if b: parts.append(read_zip(b))
    d = max(cur_m, start)
    while d < end:
        b = get(f"{BASE}/data/futures/um/daily/klines/{sym}/{iv}/{sym}-{iv}-{d:%Y-%m-%d}.zip")
        if b: parts.append(read_zip(b))
        d += timedelta(days=1)
    df = pd.concat(parts)
    df = df[pd.to_numeric(df.ts, errors="coerce").notna()].astype(float)
    df["ts"] = np.where(df.ts > 1e14, df.ts//1000, df.ts)
    df = df.drop_duplicates("ts").sort_values("ts")
    df.index = pd.to_datetime(df.pop("ts"), unit="ms")
    df.to_pickle(path)
    return sym, f"ok {len(df)}"

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", default="1h"); ap.add_argument("--start", default="2026-08-01")
    ap.add_argument("--end", default="2026-11-25"); ap.add_argument("--out", required=True)
    ap.add_argument("--symbols", default=""); ap.add_argument("--workers", type=int, default=32)
    a = ap.parse_args(); os.makedirs(a.out, exist_ok=True)
    syms = a.symbols.split(",") if a.symbols else list_symbols()
    print(len(syms), "symbols", flush=True)
    s, e = date.fromisoformat(a.start), date.fromisoformat(a.end)
    cnt = {}
    with ThreadPoolExecutor(a.workers) as ex:
        for i, (sym, st) in enumerate(ex.map(lambda x: fetch(x, a.interval, s, e, a.out), syms)):
            k = st.split()[0]; cnt[k] = cnt.get(k, 0) + 1
            if i % 50 == 0: print(i, sym, st, cnt, flush=True)
    print("done", cnt)
