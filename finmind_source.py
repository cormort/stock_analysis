"""
FinMind 多年股利 + 連續獲利來源（取代 Goodinfo 爬蟲）
====================================================
免 token、回傳 JSON、含完整多年歷史，不爬 HTML、不會被鎖。
免費版採逐檔查詢（不帶 data_id 的全市場查詢需付費）。

提供每檔：累積配息年數 / 連續配息年數 / 最新年度現金股利 / 連續獲利年數
手動測試：python3 finmind_source.py
"""
import os
import re
import time
from datetime import date
import requests

API = "https://api.finmindtrade.com/api/v4/data"
DELAY = 0.2  # 逐檔間隔（FinMind 限流寬鬆，實測 12 連發無礙）

_ROC = re.compile(r'(\d+)\s*年')


TOKEN = os.environ.get('FINMIND_TOKEN')  # 選填：免費註冊 token 可提高每小時查詢額度


def _get(dataset: str, code: str, start: str = '2005-01-01'):
    headers = {'Authorization': f'Bearer {TOKEN}'} if TOKEN else None
    r = requests.get(API, params={'dataset': dataset, 'data_id': code, 'start_date': start},
                     headers=headers, timeout=25)
    r.raise_for_status()
    return r.json().get('data') or []


def _roc_year_to_ad(year_str) -> int | None:
    """'114年第4季' / '93年' → 西元年（民國+1911）"""
    m = _ROC.search(str(year_str))
    return int(m.group(1)) + 1911 if m else None


def _consecutive_from_latest(year_to_val: dict) -> int:
    """由最新年度往前，連續 (年度相鄰且值>0) 的年數"""
    years = sorted(year_to_val, reverse=True)
    streak = 0
    for i, y in enumerate(years):
        if year_to_val[y] > 0 and (i == 0 or years[i - 1] - y == 1):
            streak += 1
        else:
            break
    return streak


def _trailing_annual_dividend(payouts: list, today: date | None = None) -> float:
    """近一年現金股利：加總最近一次公告日往前 330 天內的配發（季配4次/半年配2次/年配1次）。
    避免季配股最新年度只有第1季（如 2330 115年僅 7 元）而低估；超過 450 天未配 → 視為停發 0。
    ponytail: 330 天窗口；年配股公告日若提前 >35 天會重複計入，遇到再改用配發頻率判斷。"""
    dated = [(date.fromisoformat(d[:10]), c) for d, _, c in payouts if d[:4].isdigit()]
    if not dated:
        return 0
    latest = max(d for d, _ in dated)
    if ((today or date.today()) - latest).days > 450:
        return 0
    return sum(c for d, c in dated if (latest - d).days < 330)


def fetch_one(code: str) -> dict:
    """回傳單檔的多年股利與連續獲利指標。"""
    result = {'代號': code, 'error': None}
    try:
        # ── 股利（依民國年彙總現金股利＝盈餘配發＋公積配發）──
        div_rows = _get('TaiwanStockDividend', code)
        if not div_rows:
            return {**result, 'error': 'no dividend data'}
        cash_by_year = {}
        payouts = []  # (公告日, 民國年→西元, 現金)
        for r in div_rows:
            y = _roc_year_to_ad(r.get('year'))
            if y is None:
                continue
            cash = (r.get('CashEarningsDistribution') or 0) + (r.get('CashStatutorySurplus') or 0)
            cash_by_year[y] = cash_by_year.get(y, 0) + cash
            payouts.append((str(r.get('date')), y, cash))

        accum = sum(1 for v in cash_by_year.values() if v > 0)
        consec_div = _consecutive_from_latest(cash_by_year)
        latest_div = _trailing_annual_dividend(payouts)

        # ── 連續獲利年數（FinMind 財報 EPS，逐年加總只看正負號）──
        time.sleep(DELAY)
        fin_rows = _get('TaiwanStockFinancialStatements', code, start='2010-01-01')
        eps_by_year = {}
        for r in fin_rows:
            if r.get('type') != 'EPS':
                continue
            y = int(str(r.get('date'))[:4])
            eps_by_year[y] = eps_by_year.get(y, 0) + (r.get('value') or 0)
        profit_streak = _consecutive_from_latest(eps_by_year)

        return {
            '代號': code,
            'fin_累積配息年數': accum,
            'fin_連續配息年數': consec_div,
            'fin_最新股利': round(latest_div, 4),
            'fin_連續獲利年數': profit_streak,
            'error': None,
        }
    except Exception as e:
        return {**result, 'error': str(e)}


def batch_fetch(codes: list):
    import pandas as pd
    results = []
    total = len(codes)
    print(f"🌐 FinMind 批次查詢（{total} 檔，含股利 + EPS）")
    for idx, code in enumerate(codes):
        r = fetch_one(str(code).strip())
        results.append(r)
        if '402' in str(r.get('error')):  # 額度用完，後面必定失敗；已取得者照常寫入快取
            print(f"  ⚠️  FinMind 額度已用完（{idx + 1}/{total} 停止），約 1 小時後重跑會從快取接續")
            break
        if (idx + 1) % 20 == 0 or idx == total - 1:
            ok = sum(1 for x in results if not x.get('error'))
            print(f"  {idx + 1}/{total}（成功 {ok}）")
        if idx < total - 1:
            time.sleep(DELAY)
    ok = sum(1 for r in results if not r.get('error'))
    print(f"✅ 完成: {ok}/{total}")
    return pd.DataFrame(results)


if __name__ == '__main__':
    import json
    # 自我檢查：季配股最新年度不完整、年配、停發
    t = date(2026, 11, 1)
    q = [('2025-09-22', 0, 5), ('2025-12-17', 0, 5), ('2026-03-23', 0, 6), ('2026-06-17', 0, 6), ('2026-09-22', 0, 7)]
    assert _trailing_annual_dividend(q, t) == 24                                              # 季配
    assert _trailing_annual_dividend([('2025-07-27', 0, 1.2), ('2026-07-31', 0, 1.4)], t) == 1.4  # 年配
    assert _trailing_annual_dividend([('2026-06-08', 0, 8), ('2026-07-28', 0, 0)], t) == 8      # 補充 0 元公告
    assert _trailing_annual_dividend([('2024-07-01', 0, 3)], t) == 0                          # 停發
    assert _trailing_annual_dividend([('', 0, 0)], t) == 0
    for c in ['2330', '2603', '1432']:
        print(json.dumps(fetch_one(c), ensure_ascii=False))
