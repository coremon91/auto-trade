"""스캐너 웹 대시보드 — http://localhost:8000

  python analyzer/web.py              # 서버 시작 + 브라우저 열기
  python analyzer/web.py --port 8080 --no-browser

표준 라이브러리만 사용 (추가 설치 없음). 127.0.0.1 에만 열림 (같은 PC 에서만 접속).
화면에서 스캔 실행 → analyzer/pattern_scan.py 를 백그라운드로 실행하고 결과(analyzer/out/run_*.json)를 표시.
"""
from __future__ import annotations

import argparse, json, os, re, subprocess, sys, threading, time, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT, PAT, STATIC = os.path.join(HERE, "out"), os.path.join(HERE, "patterns"), os.path.join(HERE, "web")
BT = os.path.join(ROOT, "backtest", "results_regime", "summary.json")
LOG = os.path.join(OUT, "web_scan.log")
DEFAULT_SOURCE = "api"

_job = {"proc": None, "started": None, "args": None}
_lock = threading.Lock()


def runs():
    if not os.path.isdir(OUT):
        return []
    out = []
    for f in sorted(os.listdir(OUT), reverse=True):
        m = re.fullmatch(r"run_(\d{8}_\d{4}(?:_\d+)?)\.json", f)
        if m:
            try:
                r = json.load(open(os.path.join(OUT, f), encoding="utf-8"))
                out.append(dict(stamp=m.group(1), time=r.get("time"), mode=r.get("mode"), source=r.get("source"),
                                n_cand=len(r.get("candidates") or []),
                                n_range=sum(1 for x in ((r.get("range") or {}).get("rows") or []) if x.get("selected"))))
            except Exception:
                pass
    return out


def library():
    p = os.path.join(PAT, "library.jsonl")
    if not os.path.exists(p):
        return []
    rows = []
    for line in open(p, encoding="utf-8"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        d.pop("shape", None)
        rows.append(d)
    return rows[::-1]


ALLOWED = {"mode": {"both", "pattern", "range"}, "source": {"api", "archive"}}
NUM = {"top": int, "pick": int, "lookback": int, "range_hours": int, "recent": int, "vol_mult": float,
       "min_qv": float, "max_chg": float, "charts": int}


def start_scan(params):
    with _lock:
        if _job["proc"] and _job["proc"].poll() is None:
            return False, "이미 스캔이 실행 중입니다"
        args = [sys.executable, os.path.join(HERE, "pattern_scan.py")]
        src = params.get("source") or DEFAULT_SOURCE
        if src not in ALLOWED["source"]:
            return False, "source 값 오류"
        args += ["--source", src]
        mode = params.get("mode") or "both"
        if mode not in ALLOWED["mode"]:
            return False, "mode 값 오류"
        args += ["--mode", mode]
        for k, typ in NUM.items():
            v = params.get(k)
            if v in (None, ""):
                continue
            try:
                v = typ(v)
            except (TypeError, ValueError):
                return False, f"{k} 값 오류"
            args += ["--" + k.replace("_", "-"), str(v)]
        if params.get("library"):
            args.append("--library")
        os.makedirs(OUT, exist_ok=True)
        log = open(LOG, "w", encoding="utf-8")
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        _job.update(proc=subprocess.Popen(args, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, env=env),
                    started=time.time(), args=args[2:])
        return True, "시작"


def status():
    p = _job["proc"]
    running = bool(p and p.poll() is None)
    tail = ""
    if os.path.exists(LOG):
        with open(LOG, encoding="utf-8", errors="replace") as f:
            lines = [l for l in f.read().splitlines() if not l.startswith("  skip")]
        tail = "\n".join(lines[-25:])
    return dict(running=running, code=(p.returncode if p and not running else None), default_source=DEFAULT_SOURCE,
                started=_job["started"], args=_job["args"], log=tail)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False, default=str).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype):
        with open(path, "rb") as f:
            self._send(200, f.read(), ctype)

    def do_GET(self):
        u = urlparse(self.path)
        p = unquote(u.path)
        if p in ("/", "/index.html"):
            return self._file(os.path.join(STATIC, "index.html"), "text/html; charset=utf-8")
        if p == "/api/runs":
            return self._send(200, runs())
        m = re.fullmatch(r"/api/run/(\d{8}_\d{4}(?:_\d+)?)", p)
        if m:
            f = os.path.join(OUT, f"run_{m.group(1)}.json")
            return self._file(f, "application/json; charset=utf-8") if os.path.exists(f) else self._send(404, {"error": "없음"})
        if p == "/api/library":
            return self._send(200, library())
        if p == "/api/backtest":
            return self._file(BT, "application/json; charset=utf-8") if os.path.exists(BT) else self._send(200, {})
        if p == "/api/status":
            return self._send(200, status())
        m = re.fullmatch(r"/img/(?:analyzer/)?(out|patterns)/([^/\\]+\.png)", p)   # 이전 실행 기록(역슬래시 경로)도 허용
        if m:
            f = os.path.join(OUT if m.group(1) == "out" else PAT, m.group(2))
            return self._file(f, "image/png") if os.path.exists(f) else self._send(404, {"error": "없음"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if urlparse(self.path).path != "/api/scan":
            return self._send(404, {"error": "not found"})
        if self.headers.get("Origin") not in (None, f"http://localhost:{self.server.server_port}",
                                              f"http://127.0.0.1:{self.server.server_port}"):
            return self._send(403, {"error": "forbidden"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            params = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": "JSON 오류"})
        ok, msg = start_scan(params)
        self._send(200 if ok else 409, {"ok": ok, "msg": msg})


def main():
    global DEFAULT_SOURCE
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--source", choices=["api", "archive"], default="api", help="화면에서 실행할 스캔의 기본 데이터 소스")
    a = ap.parse_args()
    DEFAULT_SOURCE = a.source
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    url = f"http://localhost:{a.port}"
    print(f"대시보드: {url}   (종료: Ctrl+C)", flush=True)
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
