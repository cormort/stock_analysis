"""資料源層：TWSE OpenAPI / TWSE rwd / Yahoo Finance。

設計原則（見 tw-stock-data-sources skill）：
  1. 抓取失敗不可靜默轉 0：每個來源回傳 status（ok / no_data / error / quota）。
  2. 只用「當時真的能取得」的官方資料；衍生指標另存來源欄位。
  3. 逐日資料一律落地快取、可續跑；不重複打同一日期。
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import time
from datetime import date, datetime, timedelta

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_BASE = "https://openapi.twse.com.tw/v1"
RWD_BASE = "https://www.twse.com.tw/rwd/zh"
DATA_DIR = "data"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")

HEADERS = {"User-Agent": UA, "Referer": "https://www.twse.com.tw/",
           "Accept": "application/json, text/plain, */*"}


def build_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=0.6, status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET"])
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8))
    s.headers.update(HEADERS)
    return s


SESSION = build_session()


# ───────────────────────────────────────────
# 快取工具
# ───────────────────────────────────────────
def load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: str, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def _num(v):
    """TWSE 數字字串 → float；'--' / '' → None（不可當 0）。"""
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def normalize_code(df: pd.DataFrame) -> pd.DataFrame:
    aliases = {"Code", "公司代號", "證券代號", "股票代號"}
    hit = aliases & set(df.columns)
    if hit:
        df = df.rename(columns={c: "代號" for c in hit})
    if "代號" in df.columns:
        df["代號"] = df["代號"].astype(str).str.strip()
    return df


# ───────────────────────────────────────────
# TWSE OpenAPI（單期快照）
# ───────────────────────────────────────────
OPENAPI = {
    "metrics": "/exchangeReport/BWIBBU_ALL",       # PE / PB / 殖利率
    "prices": "/exchangeReport/STOCK_DAY_AVG_ALL",  # 收盤價
    "profit": "/opendata/t187ap17_L",               # 純益率
    "dividend": "/opendata/t187ap45_L",             # 股利分派（僅近 2 年）
    "balance": "/opendata/t187ap07_L_ci",           # 資產負債表（單季）
    "income": "/opendata/t187ap06_L_ci",            # 綜合損益表（單季累計）
    "basic": "/opendata/t187ap03_L",                # 產業別
    "revenue": "/opendata/t187ap05_L",              # 月營收
    "penalty": "/opendata/t187ap22_L",              # 違規裁處
}


def fetch_openapi(key: str) -> tuple[str, pd.DataFrame | None, str]:
    try:
        r = SESSION.get(API_BASE + OPENAPI[key], timeout=30)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list) or not data:
            return key, None, "no_data"
        return key, normalize_code(pd.DataFrame(data)), "ok"
    except Exception as e:
        print(f"  ❌ {key}: {e}")
        return key, None, "error"


def fetch_all_openapi() -> tuple[dict, dict]:
    frames, status = {}, {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for key, df, st in pool.map(fetch_openapi, OPENAPI):
            frames[key], status[key] = df, st
    return frames, status


# ───────────────────────────────────────────
# 10 年期美債殖利率（目標殖利率的基準）
# ───────────────────────────────────────────
def fetch_us10y(cache_path=f"{DATA_DIR}/us10y.json") -> tuple[float, str]:
    cached = load_json(cache_path, {})
    if cached.get("ts") and time.time() - cached["ts"] < 86400 and cached.get("value"):
        return float(cached["value"]), "cache"
    try:
        r = SESSION.get("https://query1.finance.yahoo.com/v8/finance/chart/%5ETNX"
                        "?range=5d&interval=1d", timeout=20)
        closes = [c for c in r.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"] if c]
        if closes:
            val = round(float(closes[-1]), 3)
            save_json(cache_path, {"value": val, "ts": time.time()})
            return val, "ok"
    except Exception as e:
        print(f"  ⚠️  10Y 殖利率抓取失敗（{e}），沿用 4.5% 備援")
    return float(cached.get("value") or 4.5), "fallback"


# ───────────────────────────────────────────
# Yahoo：股利（20+ 年）+ 年度 EPS（近 4 年）
# ───────────────────────────────────────────
def _yahoo_chart(code: str):
    for suf in ("TW", "TWO"):
        try:
            r = SESSION.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.{suf}"
                            "?range=max&interval=1mo&events=div", timeout=25)
            if r.status_code != 200:
                continue
            res = r.json().get("chart", {}).get("result")
            if res:
                return res[0]
        except Exception:
            continue
    return None


def _yahoo_get(path: str):
    """query1/query2 輪替；429 回 (None, 'rate_limited')。"""
    for host in ("query1", "query2"):
        try:
            r = SESSION.get(f"https://{host}.finance.yahoo.com{path}", timeout=25)
            if r.status_code == 429:
                continue
            if r.status_code != 200:
                continue
            return r, "ok"
        except Exception:
            continue
    return None, "rate_limited"


def fetch_dividends(code: str) -> tuple[list[dict], str]:
    """回傳 [{'date': 'YYYY-MM-DD', 'amount': float}, ...]（除息日）。"""
    res = None
    status = "error"
    for suf in ("TW", "TWO"):
        r, st = _yahoo_get(f"/v8/finance/chart/{code}.{suf}?range=max&interval=1mo&events=div")
        status = st
        if r is None:
            continue
        result = r.json().get("chart", {}).get("result")
        if result:
            res = result[0]
            break
    if res is None:
        return [], status
    ev = (res.get("events") or {}).get("dividends") or {}
    out = []
    for v in ev.values():
        try:
            out.append({"date": datetime.utcfromtimestamp(int(v["date"])).strftime("%Y-%m-%d"),
                        "amount": round(float(v.get("amount") or 0), 4)})
        except Exception:
            continue
    out.sort(key=lambda x: x["date"])
    return out, ("ok" if out else "no_data")


def fetch_annual_eps(code: str) -> tuple[dict, str]:
    status = "error"
    for suf in ("TW", "TWO"):
        url = ("/ws/fundamentals-timeseries/v1/finance/"
               f"timeseries/{code}.{suf}?symbol={code}.{suf}&type=annualBasicEPS"
               "&period1=1262304000&period2=1790000000&merge=false")
        r, status = _yahoo_get(url)
        if r is None:
            continue
        try:
            res = r.json().get("timeseries", {}).get("result", [])
            if not res:
                continue
            out = {}
            for x in res[0].get("annualBasicEPS") or []:
                if isinstance(x, dict) and x.get("reportedValue"):
                    out[int(x["asOfDate"][:4])] = float(x["reportedValue"]["raw"])
            if out:
                return out, "ok"
        except Exception:
            continue
    return {}, status


def import_legacy_cache(path: str, cache_path=f"{DATA_DIR}/yahoo_hist.json") -> dict:
    """把 v8/v9 的 dividend_cache（{code: {dividends: {year: amt}, eps: {...}}}）
    轉成 v6 格式，作為 Yahoo 429 時的種子。年份以 7/1 當除息日估計。
    """
    legacy = load_json(path, {})
    cache = load_json(cache_path, {})
    now = time.time()
    n = 0
    for code, v in legacy.items():
        if code in cache and cache[code].get("divs"):
            continue
        divs = [{"date": f"{int(y)}-07-01", "amount": round(float(a), 4)}
                for y, a in (v.get("dividends") or {}).items() if float(a or 0) > 0]
        divs.sort(key=lambda x: x["date"])
        eps = {int(y): float(a) for y, a in (v.get("eps") or {}).items() if a is not None}
        cache[str(code)] = {"divs": divs, "div_status": "ok" if divs else "no_data",
                            "eps": eps, "eps_status": "ok" if eps else "no_data",
                            "ts": now, "seed": True}
        n += 1
    save_json(cache_path, cache)
    print(f"  🌱 匯入舊快取 {n} 檔（{path}）")
    return cache


def build_yahoo_history(codes: list[str], cache_path=f"{DATA_DIR}/yahoo_hist.json",
                        workers: int = 6, eps_limit: int = 500,
                        eps_codes: set[str] | None = None, ttl_days: int = 7) -> dict:
    """逐檔抓股利 + 年度 EPS，落地快取。EPS 只抓流動性前 N 檔（Yahoo 較慢）。"""
    cache = load_json(cache_path, {})
    now = time.time()
    err_ttl = 3600

    def fresh(v):
        st = v.get("div_status")
        ttl = err_ttl if st in ("error", "rate_limited") else ttl_days * 86400
        return bool(v.get("ts")) and now - v["ts"] < ttl

    todo = [c for c in codes if not fresh(cache.get(c, {}))]
    if eps_codes is None:
        eps_codes = set(codes[:eps_limit])
    print(f"  Yahoo 歷史：快取 {len(codes) - len(todo)} 檔，待抓 {len(todo)} 檔"
          f"（EPS 上限 {len(eps_codes)} 檔）")

    def work(code):
        divs, dstatus = fetch_dividends(code)
        eps, estatus = ({}, "skip")
        if code in eps_codes:
            eps, estatus = fetch_annual_eps(code)
        return code, divs, dstatus, eps, estatus

    done = 0
    # 先探測一檔：Yahoo 已對本 IP 429 時直接跳過，不要浪費幾百次重試
    if todo:
        _, probe_status = fetch_dividends(todo[0])
        if probe_status == "rate_limited":
            print("  ⚠️  Yahoo 目前 429（IP 限流），本輪跳過；使用快取／TWSE fallback")
            return cache
    # 分塊：Yahoo 429 時整批中止，已抓到的保留、失敗的下次重試
    for i in range(0, len(todo), 150):
        chunk = todo[i:i + 150]
        limited = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for code, divs, ds, eps, es in pool.map(work, chunk):
                cache[code] = {"divs": divs, "div_status": ds, "eps": eps,
                               "eps_status": es, "ts": now}
                limited += ds == "rate_limited"
                done += 1
                if done % 50 == 0 or done == len(todo):
                    print(f"\r    進度 {done}/{len(todo)}", end="")
                    save_json(cache_path, cache)
        print()
        if chunk and limited / len(chunk) > 0.4:
            print(f"  ⚠️  Yahoo 429 比例過高（{limited}/{len(chunk)}），中止本輪；"
                  "已抓到的會保留，其餘下次重試")
            break
        time.sleep(2)
    save_json(cache_path, cache)
    return cache


# ───────────────────────────────────────────
# TWSE 除權除息計算結果表（TWT49U）：全市場 20 年除息事件，一年一請求
# ───────────────────────────────────────────
TWT49U_CACHE = f"{DATA_DIR}/twt49u.json"
TWT49U_DETAIL_CACHE = f"{DATA_DIR}/twt49u_detail.json"   # {"code,date": 現金股利}


def parse_twt49u(j: dict) -> list[list]:
    """→ [[code, 'YYYY-MM-DD', 現金股利, mixed]]。

    早年有獨立「息值」欄（精確）；近年只有「權值+息值」：
      權/息 = '息' → 即現金股利；'權息' → 配股+配息合計，無法拆 → mixed=True（金額偏高）；
      '權' → 純配股，略過。
    """
    f = j.get("fields") or []
    if "股票代號" not in f or "資料日期" not in f:
        return []
    i_code, i_date = f.index("股票代號"), f.index("資料日期")
    i_cash = f.index("息值") if "息值" in f else None
    i_sum = f.index("權值+息值") if "權值+息值" in f else None
    i_kind = f.index("權/息") if "權/息" in f else None
    out = []
    for r in j.get("data") or []:
        try:
            y, m, d = (int(x) for x in str(r[i_date]).replace("年", "-").replace("月", "-")
                       .replace("日", "").split("-"))
            day = f"{y + 1911:04d}-{m:02d}-{d:02d}"
            kind = str(r[i_kind]) if i_kind is not None else ""
            if i_cash is not None:
                cash, mixed = _num(r[i_cash]), False
            elif kind == "息":
                cash, mixed = _num(r[i_sum]), False
            elif kind == "權息":
                cash, mixed = _num(r[i_sum]), True
            else:
                continue
            if cash and cash > 0:
                out.append([str(r[i_code]).strip(), day, round(cash, 4), mixed])
        except Exception:
            continue
    return out


def load_twse_dividend_history(start_year: int = 2003, offline: bool = False) -> dict:
    """{code: [{'date', 'amount', 'mixed'}]}；過去年度永久快取，今年每次重抓。"""
    cache = load_json(TWT49U_CACHE, {})
    this_year = date.today().year
    fetched = 0
    for y in range(start_year, this_year + 1):
        if offline or (str(y) in cache and y < this_year):
            continue
        start = f"{y}0505" if y == 2003 else f"{y}0101"   # TWT49U 最早 92/5/5
        j = _rwd_json("exRight/TWT49U", {"startDate": start, "endDate": f"{y}1231",
                                         "response": "json"})
        if not j:                                     # 網路錯誤或「系統忙碌」：不寫快取，下次重試
            print(f"    ⚠️  TWT49U {y} 抓取失敗")
            continue
        cache[str(y)] = parse_twt49u(j)
        fetched += 1
        print(f"\r    除息 {y}（{len(cache[str(y)])} 筆）", end="")
        time.sleep(1.5)
    if fetched:
        print()
        save_json(TWT49U_CACHE, cache)
    detail = load_json(TWT49U_DETAIL_CACHE, {})
    out: dict[str, list[dict]] = {}
    busy, n_new = offline, 0
    for rows in cache.values():
        for code, d, amt, mixed in rows:
            # 權息合計只對近 6 年（dps / 5 年均會用到）查明細拆出現金；更早的只影響年數，保留合計
            if mixed and int(d[:4]) >= this_year - 5:
                key = f"{code},{d}"
                if key not in detail and not busy:
                    # TWSE 約 40 次連發後回「忙碌」：每筆間隔 2s，忙碌時退避 20/40/60s，連 3 次失敗本輪停查
                    for wait in (20, 40, 60, None):
                        cash = fetch_twt49u_cash(code, d)
                        if cash is not None or wait is None:
                            break
                        time.sleep(wait)
                    if cash is None:
                        busy = True
                    else:
                        detail[key] = cash
                        n_new += 1
                        if n_new % 25 == 0:
                            save_json(TWT49U_DETAIL_CACHE, detail)
                            print(f"\r    權息明細 {n_new} 筆", end="")
                        time.sleep(2.0)
                if isinstance(detail.get(key), (int, float)):   # 'missing' → 保留合計與標記
                    amt, mixed = detail[key], False
                    if amt <= 0:
                        continue
            out.setdefault(code, []).append({"date": d, "amount": amt, "mixed": mixed})
    if n_new:
        save_json(TWT49U_DETAIL_CACHE, detail)
    for v in out.values():
        v.sort(key=lambda e: e["date"])
    left = sum(1 for v in out.values() for e in v if e["mixed"] and int(e["date"][:4]) >= this_year - 5)
    print(f"  💰 TWT49U 除息歷史 {len(cache)} 年（新抓 {fetched} 年），{len(out)} 檔；"
          f"權息明細新查 {n_new} 筆，未拆 {left} 筆")
    return out


def fetch_twt49u_cash(code: str, day: str):
    """TWT49UDetail → 每股現金股利（float）；'missing' = 查無明細；None = 忙碌/網路錯誤（可重試）。"""
    try:
        r = SESSION.get(f"{RWD_BASE}/exRight/TWT49UDetail", timeout=30,
                        params={"STK_NO": code, "T1": day.replace("-", ""), "response": "json"})
        j = r.json()
    except Exception:
        return None
    stat = str(j.get("stat", ""))
    if stat.lower() != "ok":
        return "missing" if "無相關資料" in stat else None
    f, rows = j.get("fields") or [], j.get("data") or []
    i = next((k for k, x in enumerate(f) if "現金股利" in x), None)
    if i is None or not rows:
        return "missing"
    return _num(str(rows[0][i]).split("元")[0]) or 0.0


# ───────────────────────────────────────────
# 全市場日行情（MI_INDEX）：OHLCV + 成交金額 + 大盤指數
# ───────────────────────────────────────────
MI_INDEX_URL = f"{RWD_BASE}/afterTrading/MI_INDEX"
DAILY_CACHE = f"{DATA_DIR}/daily_prices.json"
TAIEX_CACHE = f"{DATA_DIR}/taiex.json"


def fetch_mi_index(date_key: str) -> tuple[dict | None, float | None]:
    """單日全市場 → ({code: [vol, open, high, low, close, amount]}, taiex)；失敗 None。"""
    try:
        r = SESSION.get(MI_INDEX_URL, params={"date": date_key, "type": "ALLBUT0999",
                                              "response": "json"}, timeout=30)
        text = r.text.lstrip()
        if not text.startswith("{"):          # TWSE 反 DDoS 的 HTML 防護頁
            return None, None
        j = r.json()
    except Exception:
        return None, None
    if j.get("stat") != "OK":
        return {}, None
    tables = j.get("tables", [])
    daily = next((t for t in tables if "每日收盤行情" in t.get("title", "")), None)
    if not daily or not daily.get("data"):
        return {}, None

    f = daily["fields"]
    idx = {name: f.index(name) for name in
           ("證券代號", "成交股數", "開盤價", "最高價", "最低價", "收盤價", "成交金額")}
    out = {}
    for row in daily["data"]:
        vals = [_num(row[idx[k]]) for k in
                ("成交股數", "開盤價", "最高價", "最低價", "收盤價", "成交金額")]
        if any(v is None for v in vals):
            continue
        out[str(row[idx["證券代號"]]).strip()] = vals

    taiex = None
    for t in tables:
        if "價格指數(臺灣證券交易所)" not in str(t.get("title", "")):
            continue
        cols = t.get("fields", [])
        if "收盤指數" not in cols:
            continue
        i = cols.index("收盤指數")
        for row in t.get("data", []):
            if row and str(row[0]).strip() == "發行量加權股價指數":
                taiex = _num(row[i])
                break
    return out, taiex


def fetch_taiex_history(range_: str = "6m") -> dict:
    """大盤日收盤 → {yyyymmdd: close}。

    優先 TWSE FMTQIK（每月一個請求，官方）；失敗才走 Yahoo ^TWII。
    """
    out = {}
    today = date.today()
    months = []
    y, m = today.year, today.month
    for _ in range(4):
        months.append(f"{y}{m:02d}01")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    for ym in months:
        try:
            r = SESSION.get(f"{RWD_BASE}/afterTrading/FMTQIK",
                            params={"date": ym, "response": "json"}, timeout=30)
            if not r.text.lstrip().startswith("{"):
                continue
            j = r.json()
            if j.get("stat") != "OK":
                continue
            fields = j.get("fields", [])
            di = fields.index("日期")
            ii = fields.index("發行量加權股價指數")
            for row in j.get("data", []):
                d = str(row[di]).strip().replace("/", "")
                if len(d) == 7:
                    d = f"{int(d[:3]) + 1911}{d[3:5]}{d[5:7]}"
                v = _num(row[ii])
                if v:
                    out[d] = v
            time.sleep(1.0)
        except Exception:
            continue
    if out:
        return out
    try:
        r = SESSION.get(f"https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII"
                        f"?range=2y&interval=1d", timeout=25)
        res = r.json()["chart"]["result"][0]
        for t, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]):
            if c:
                out[datetime.utcfromtimestamp(t).strftime("%Y%m%d")] = round(float(c), 2)
    except Exception as e:
        print(f"  ⚠️  TAIEX 歷史抓取失敗：{e}")
    return out


def load_market_history(n_days: int = 60) -> tuple[dict, dict, int]:
    """回傳 ({code: [row...]}, {date: taiex}, 實際交易日數)。逐日快取、可續跑。"""
    cache = load_json(DAILY_CACHE, {})
    taiex = load_json(TAIEX_CACHE, {})
    today = date.today()
    days, fetched, d = [], 0, today
    while len(days) < n_days and (today - d).days < n_days * 2 + 30:
        key = d.strftime("%Y%m%d")
        if d.weekday() < 5:
            if key not in cache:
                day, tx = fetch_mi_index(key)
                fetched += 1
                print(f"\r    日行情 {key}（{fetched}）", end="")
                if day is None:               # 網路失敗：不寫入、下次重試
                    break
                # 收盤前的當日不寫入（避免把盤中半成品永久快取）
                if day or d < today:
                    cache[key] = day
                if tx:
                    taiex[key] = tx
                time.sleep(1.3)
            if cache.get(key):
                days.append(key)
        d -= timedelta(days=1)
    if fetched:
        print()
        # 只保留窗口內，避免檔案無限長大
        floor = d.strftime("%Y%m%d")
        cache = {k: v for k, v in cache.items() if k >= floor}
        taiex = {k: v for k, v in taiex.items() if k >= floor}
        save_json(DAILY_CACHE, cache)
        save_json(TAIEX_CACHE, taiex)

    hist = {}
    for key in sorted(days):
        for code, row in cache[key].items():
            hist.setdefault(code, []).append(row)
    # 指數表可能因 TWSE 回應格式變動而抓不到 → 用 Yahoo ^TWII 補一次
    if len(taiex) < len(days) * 0.8:
        print("  ℹ️  指數序列不足，改用 Yahoo ^TWII 補齊")
        taiex = fetch_taiex_history()
        save_json(TAIEX_CACHE, taiex)
    tx_series = {k: taiex[k] for k in sorted(days) if k in taiex}
    print(f"  📅 日行情 {len(days)} 個交易日（新抓 {fetched} 天），{len(hist)} 檔")
    return hist, tx_series, len(days)


# ───────────────────────────────────────────
# 籌碼：三大法人 T86 / 融資券 MI_MARGN / 外資持股 MI_QFIIS
# ───────────────────────────────────────────
CHIP_CACHE = f"{DATA_DIR}/chips.json"


def _rwd_json(path: str, params: dict):
    try:
        r = SESSION.get(f"{RWD_BASE}/{path}", params=params, timeout=30)
        if not r.text.lstrip().startswith("{"):
            return None
        j = r.json()
        return j if j.get("stat") == "OK" else {}
    except Exception:
        return None


def fetch_t86(date_key: str) -> dict | None:
    """{code: {'foreign': 張, 'trust': 張, 'total': 張}}"""
    j = _rwd_json("fund/T86", {"date": date_key, "selectType": "ALL", "response": "json"})
    if j is None:
        return None
    rows = j.get("data") or []
    if not rows:
        return {}
    fields = j["fields"]
    def col(key):
        return next((i for i, f in enumerate(fields) if key in f), None)
    ci, cf, ct, ca = col("證券代號"), col("外陸資買賣超股數"), col("投信買賣超股數"), col("三大法人買賣超股數")
    out = {}
    for r in rows:
        code = str(r[ci]).strip()
        vals = [_num(r[i]) for i in (cf, ct, ca) if i is not None]
        if len(vals) == 3 and all(v is not None for v in vals):
            out[code] = {"foreign": round(vals[0] / 1000, 1),
                         "trust": round(vals[1] / 1000, 1),
                         "total": round(vals[2] / 1000, 1)}
    return out


def fetch_margin(date_key: str | None = None) -> dict | None:
    """{code: {'fin': 張, 'short': 張, 'limit': 張}}

    MI_MARGN 只在 OpenAPI 提供「最新交易日」全市場快照（rwd 端點不吃 selectType）。
    """
    try:
        r = SESSION.get(f"{API_BASE}/exchangeReport/MI_MARGN", timeout=30)
        if not r.text.lstrip().startswith("["):
            return None
        rows = r.json()
    except Exception:
        return None
    if not rows:
        return {}
    def val(row, key):
        return row.get(key) if key in row else None
    out = {}
    for row in rows:
        code = str(val(row, "股票代號") or "").strip()
        if not code:
            continue
        out[code] = {"fin": _num(val(row, "融資今日餘額")),
                     "short": _num(val(row, "融券今日餘額")),
                     "limit": _num(val(row, "融資限額"))}
    return out


def fetch_qfiis(date_key: str) -> dict | None:
    """{code: 外資持股比率%}"""
    j = _rwd_json("fund/MI_QFIIS", {"date": date_key, "selectType": "ALLBUT0999",
                                    "response": "json"})
    if j is None:
        return None
    rows = j.get("data") or []
    if not rows:
        return {}
    fields = j["fields"]
    ci = fields.index("證券代號")
    pi = next((i for i, f in enumerate(fields) if "持股比率" in f), None)
    if pi is None:
        return {}
    out = {}
    for r in rows:
        v = _num(r[pi])
        if v is not None:
            out[str(r[ci]).strip()] = v
    return out


def load_chip_history(days: int = 21) -> dict:
    """逐日抓 T86 / MI_MARGN / MI_QFIIS 並快取。

    QFIIS 與 MARGN 只需近 6 日（5 日變化、融資現況）；
    T86 需 21 日（20 日累計買超）。
    """
    cache = load_json(CHIP_CACHE, {"t86": {}, "margin": {}, "qfiis": {}})
    for k in ("t86", "margin", "qfiis"):
        cache.setdefault(k, {})
    today = date.today()
    d, trading, fetched = today, [], 0
    while len(trading) < days and (today - d).days < days * 2 + 20:
        key = d.strftime("%Y%m%d")
        if d.weekday() < 5:
            need_t86 = key not in cache["t86"]
            need_q = len(trading) < 6 and key not in cache["qfiis"]
            if need_t86 or need_q or "latest" not in cache["margin"]:
                got = False
                if need_t86:
                    t86 = fetch_t86(key)
                    if t86 is None:
                        break
                    if t86:
                        cache["t86"][key] = t86
                        got = True
                if need_q:
                    q = fetch_qfiis(key)
                    if q:
                        cache["qfiis"][key] = q
                        got = True
                if "latest" not in cache["margin"]:
                    m = fetch_margin()
                    if m:
                        cache["margin"]["latest"] = m
                        got = True
                if got:
                    fetched += 1
                    print(f"\r    籌碼 {key}（{fetched}）", end="")
                time.sleep(1.2)
            if key in cache["t86"]:
                trading.append(key)
        d -= timedelta(days=1)
    if fetched:
        print()
    # 只保留近 40 個交易日，避免檔案無限長大
    for k, keep in (("t86", 40), ("qfiis", 40)):
        dates = sorted(cache[k])
        cache[k] = {dt: cache[k][dt] for dt in dates[-keep:]}
    save_json(CHIP_CACHE, cache)
    floors = {k: v for k, v in cache.items()}
    print(f"  📊 籌碼 {len(trading)} 個交易日（新抓 {fetched} 天）")
    return floors


# ───────────────────────────────────────────
# 衍生：從逐日籌碼算 5/20 日累計
# ───────────────────────────────────────────
def chip_summary(cache: dict, code: str) -> dict:
    t86_dates = sorted(cache.get("t86", {}))
    q_dates = sorted(cache.get("qfiis", {}))

    def sum_t86(n):
        vals = [cache["t86"][d].get(code) for d in t86_dates[-n:]]
        vals = [v for v in vals if v]
        if not vals:
            return None
        return {"foreign": round(sum(v["foreign"] for v in vals), 1),
                "trust": round(sum(v["trust"] for v in vals), 1),
                "total": round(sum(v["total"] for v in vals), 1)}

    q_now = cache.get("qfiis", {}).get(q_dates[-1], {}).get(code) if q_dates else None
    q_prev = cache.get("qfiis", {}).get(q_dates[-6], {}).get(code) if len(q_dates) >= 6 else None
    m_now = (cache.get("margin", {}).get("latest") or {}).get(code)
    s5, s20 = sum_t86(5), sum_t86(20)
    fin, short, limit = (m_now or {}).get("fin"), (m_now or {}).get("short"), (m_now or {}).get("limit")
    return {
        "外資持股%": q_now,
        "外資持股5日變化(pp)": round(q_now - q_prev, 2) if q_now is not None and q_prev is not None else None,
        "外資買超5日(張)": s5["foreign"] if s5 else None,
        "投信買超5日(張)": s5["trust"] if s5 else None,
        "三大法人買超5日(張)": s5["total"] if s5 else None,
        "三大法人買超20日(張)": s20["total"] if s20 else None,
        "融資餘額(張)": fin,
        "融資使用率%": round(fin / limit * 100, 2) if fin and limit else None,
        "券資比%": round(short / fin * 100, 2) if short and fin else None,
        "籌碼資料狀態": "✅" if (s5 or q_now) else "—",
    }
