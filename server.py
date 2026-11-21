"""本機檢視伺服器：靜態檔 + 唯讀 JSON API + 即時報價代理。

API（全部只讀，綁 127.0.0.1）：
  GET /api/health
  GET /api/analysis?codes=2317,2330[&fields=收盤價,殖利率,綜合分數]
  GET /api/screen?sheet=value&limit=20&sort=綜合分數[&asc=1]
  GET /api/quote?codes=2317

用法：python3 server.py [port]
"""
from __future__ import annotations

import json
import os
import re
import sys
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
MIS = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp?json=1&delay=0&ex_ch="
CODE = re.compile(r"^\d{4,6}[A-Z]?$")

_snap_cache = {"file": None, "mtime": 0.0, "snap": None}


# ───────────────────────────────────────────
# 快照
# ───────────────────────────────────────────
def latest_snapshot(data_dir: str | None = None) -> dict | None:
    """讀 manifest 最新一份；以 mtime 快取，避免每個請求重讀 2MB JSON。"""
    data_dir = data_dir or DATA_DIR
    man_path = os.path.join(data_dir, "manifest.json")
    try:
        with open(man_path, encoding="utf-8") as f:
            man = json.load(f)
        fname = man["snapshots"][0]["file"]
        path = os.path.join(data_dir, fname)
        mtime = os.path.getmtime(path)
    except (FileNotFoundError, KeyError, IndexError, json.JSONDecodeError):
        return None
    if _snap_cache["file"] == path and _snap_cache["mtime"] == mtime:
        return _snap_cache["snap"]
    with open(path, encoding="utf-8") as f:
        snap = json.load(f)
    _snap_cache.update(file=path, mtime=mtime, snap=snap)
    return snap


def _row_index(snap: dict) -> dict:
    """全市場列優先，找不到才用其他 sheet（兼顧舊快照）。"""
    idx = {}
    for sheet in ("all", "value", "sheet1", "sheet2"):
        for r in snap.get("sheets", {}).get(sheet, {}).get("rows", []):
            code = str(r.get("代號") or "")
            if code and code not in idx:
                idx[code] = r
    return idx


# ───────────────────────────────────────────
# API payload
# ───────────────────────────────────────────
def analysis_payload(snap: dict | None, codes: list[str], fields: list[str] | None = None) -> dict:
    if not snap:
        return {"error": "no snapshot"}
    idx = _row_index(snap)
    stocks = {}
    for code in codes:
        row = idx.get(code)
        if row is None:
            stocks[code] = {"error": "not_found"}
            continue
        stocks[code] = ({k: row.get(k) for k in fields} if fields else row)
    meta = snap.get("meta", {})
    return {
        "generated_at": snap.get("generated_at"),
        "data_date": meta.get("data_date"),
        "screener_version": snap.get("screener_version"),
        "stocks": stocks,
    }


def _sort_key(row: dict, col: str):
    v = row.get(col)
    if isinstance(v, bool):
        return (0, int(v))
    if isinstance(v, (int, float)):
        return (0, v)
    if v is None:
        return (2, 0)
    return (1, str(v))


def screen_payload(snap: dict | None, sheet: str = "value", limit: int = 20,
                   sort: str = "綜合分數", ascending: bool = False) -> dict:
    if not snap:
        return {"error": "no snapshot"}
    sh = snap.get("sheets", {}).get(sheet)
    if sh is None:
        return {"error": "unknown sheet", "available": list(snap.get("sheets", {}))}
    rows = list(sh.get("rows", []))
    if sort:
        rows.sort(key=lambda r: _sort_key(r, sort), reverse=not ascending)
    if limit > 0:
        rows = rows[:limit]
    return {
        "generated_at": snap.get("generated_at"),
        "data_date": snap.get("meta", {}).get("data_date"),
        "sheet": sheet,
        "title": sh.get("title"),
        "criteria": sh.get("criteria", {}),
        "total": len(sh.get("rows", [])),
        "rows": rows,
    }


# ───────────────────────────────────────────
# 即時報價（TWSE MIS 不允許瀏覽器跨域，由本機代理）
# ───────────────────────────────────────────
def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None                                   # '-' = 本盤尚無成交


def quote(codes: list[str]) -> dict:
    """{代號: {price, prev_close, change_pct, volume, time}}（僅上市股票）"""
    url = MIS + "|".join(f"tse_{c}.tw" for c in codes)
    with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=10) as r:
        data = json.load(r)
    out = {}
    for m in data.get("msgArray", []):
        price = _num(m.get("z")) or _num(m.get("pz"))  # z=最新成交；無則用前一筆成交
        prev = _num(m.get("y"))                        # 昨收（漲跌幅基準）
        out[m.get("c")] = {
            "name": m.get("n"),
            "price": price,
            "prev_close": prev,
            "change_pct": round((price / prev - 1) * 100, 2) if price and prev else None,
            "volume": int(_num(m.get("v")) or 0),      # 累積成交量（張）
            "time": f"{m.get('d', '')} {m.get('t', '')}".strip(),
        }
    return out


# ───────────────────────────────────────────
# HTTP
# ───────────────────────────────────────────
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _codes(self, qs) -> list[str]:
        raw = (qs.get("codes", [""])[0] or "").split(",")
        return [c.strip() for c in raw if CODE.match(c.strip())][:50]

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)

        if u.path == "/api/health":
            snap = latest_snapshot()
            return self._json({"ok": True, "snapshot": bool(snap),
                               "generated_at": snap.get("generated_at") if snap else None,
                               "data_date": snap.get("meta", {}).get("data_date") if snap else None})

        if u.path == "/api/analysis":
            codes = self._codes(qs)
            if not codes:
                return self._json({"error": "codes required（例：?codes=2317,2330）"}, 400)
            fields = None
            if qs.get("fields"):
                fields = [f for f in qs["fields"][0].split(",") if f]
            return self._json(analysis_payload(latest_snapshot(), codes, fields))

        if u.path == "/api/screen":
            sheet = (qs.get("sheet", ["value"])[0] or "value")
            try:
                limit = max(0, min(int(qs.get("limit", ["20"])[0]), 2000))
            except ValueError:
                limit = 20
            sort = qs.get("sort", ["綜合分數"])[0]
            asc = qs.get("asc", ["0"])[0] in ("1", "true", "True")
            return self._json(screen_payload(latest_snapshot(), sheet, limit, sort, asc))

        if u.path == "/api/quote":
            codes = self._codes(qs)
            try:
                return self._json(quote(codes) if codes else {}, 200)
            except Exception as e:
                return self._json({"error": str(e)}, 502)

        return super().do_GET()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    print(f"🌐 http://localhost:{port}/index.html（Ctrl-C 停止）")
    print("   API: /api/health  /api/analysis?codes=2317  /api/screen?sheet=value")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
