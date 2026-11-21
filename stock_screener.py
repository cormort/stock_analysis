"""
台股終極存股選股器 v5.0 — 混合模式
===================================
架構：
  Phase 1: TWSE OpenAPI 批量篩選全市場 (PE/PB/殖利率/股利/獲利率)
  Phase 2: twstock 個股補充分析 (即時報價 + 四大買賣點 + 均線)

優化項目：
  1. requests.Session 連線池 + 自動重試
  2. 連續配息 / 連續獲利年數演算法
  3. twstock 混合模式（自動導入 + 優雅降級）
  4. Excel 欄位說明頁 + 表頭樣式 + 資料格式化
"""

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import pandas as pd
import numpy as np
import concurrent.futures
import warnings
import time
import os
import json
from datetime import datetime, timedelta
from functools import reduce

warnings.filterwarnings('ignore', category=DeprecationWarning)
warnings.filterwarnings('ignore', category=FutureWarning)

# ── twstock 導入（優雅降級：未安裝則跳過 Phase 2） ──
try:
    import twstock
    TWSTOCK_AVAILABLE = True
except ImportError:
    twstock = None
    TWSTOCK_AVAILABLE = False
    print("⚠️  未偵測到 twstock，將跳過 Phase 2 個股分析。如需啟用請執行：pip install twstock")

# ── 多年歷史補強（FinMind；手動開關：FINMIND=1）──
# TWSE 股利端點僅近 2 年且為季配部分加總；FinMind 提供多年完整股利與 EPS 歷史。
# 啟用：FINMIND=1 python3 stock_screener.py（GOODINFO=1 亦相容）
ENABLE_HISTORY = os.environ.get('FINMIND') == '1' or os.environ.get('GOODINFO') == '1'
HISTORY_USED = False  # 實際是否成功補強（寫進快照供 UI 標示）

# ───────────────────────────────────────────
# 常數設定 — 所有篩選閾值集中管理
# ───────────────────────────────────────────
SHEET1_CRITERIA = {
    '溢價回收年限_上限': 15,
    '殖利率_下限': 3.0,
    '獲利率_下限': 3.0,
    '盈餘發放率_下限': 30,
    '盈餘發放率_上限': 110,
}
SHEET2_CRITERIA = {
    '累積配息年數_下限': 2,   # ⚠️ TWSE API 僅提供近2年股利資料，待 Goodinfo 補強
    '盈餘發放率_下限': 50,
    '盈餘發放率_上限': 85,
    '殖利率_下限': 4.0,
    '溢價回收年限_上限': 15,
}

# 價值投資估價法參數（殖利率法）
VALUE_YIELD = {
    '便宜': 0.0625,   # 殖利率 6.25% → 便宜價
    '合理': 0.05,     # 殖利率 5%    → 合理價
    '昂貴': 0.04,     # 殖利率 4%    → 昂貴價
}

# 價值投資精選篩選條件
VALUE_CRITERIA = {
    'PE_上限': 15,                  # 本益比 ≤ 15
    'PB_上限': 2.0,                 # 股價淨值比 ≤ 2.0
    '安全邊際_下限': 0,              # 安全邊際 > 0%（現價低於合理價）
    '連續獲利年數_下限': 0,          # 暫設0 — PE≤15已隱含獲利條件，待Goodinfo補強歷史EPS
    '殖利率_下限': 4.0,             # 殖利率 ≥ 4%
    '盈餘發放率_下限': 30,
    '盈餘發放率_上限': 110,
}

# API 端點定義
ENDPOINTS = {
    'metrics':  "/exchangeReport/BWIBBU_ALL",       # PE, PB, Yield
    'profit':   "/opendata/t187ap17_L",              # 獲利率
    'prices':   "/exchangeReport/STOCK_DAY_AVG_ALL", # 價格
    'div_hist': "/opendata/t187ap45_L",              # 歷史股利
}

API_BASE = "https://openapi.twse.com.tw/v1"
SCREENER_VERSION = "5.0"
DATA_DIR = "data"                       # 快照與 manifest 存放目錄
MANIFEST_FILENAME = "manifest.json"     # Web UI 用來列出歷史快照
HISTORY_CACHE = "data/finmind_cache.json"  # FinMind 結果磁碟快取（跨次累積，免重複查詢）
HISTORY_TTL_DAYS = 30                     # 快取有效天數（股利/EPS 為年度資料，變動慢）

# 三分頁的顯示標題與篩選條件（一併寫進快照，供 UI 顯示）
SHEET_META = {
    'value':  {'title': '💎 價值投資精選', 'criteria': VALUE_CRITERIA},
    'sheet1': {'title': '📈 廣泛價值清單', 'criteria': SHEET1_CRITERIA},
    'sheet2': {'title': '🛡️ 嚴選防禦存股', 'criteria': SHEET2_CRITERIA},
    'all':    {'title': '🌐 全市場', 'criteria': {}},  # 未篩選，供個股追蹤與自訂排序
}


# ───────────────────────────────────────────
# 1. HTTP 層 — 連線池 + 自動重試
# ───────────────────────────────────────────
def _build_session() -> requests.Session:
    """建立帶有連線池與重試邏輯的 Session（同 host 僅需一次 TCP 握手）"""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,          # 0.5s → 1s → 2s
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=1,          # 同一 host 只需 1 個連線池
        pool_maxsize=4,              # 最多 4 條並行連線
    )
    session.mount("https://", adapter)
    session.headers.update({
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
    })
    return session


def fetch_endpoint(session: requests.Session, key: str, path: str):
    """抓取單一 API 端點，回傳 (key, DataFrame | None)"""
    url = f"{API_BASE}{path}"
    try:
        r = session.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        if not data:
            print(f"  ⚠️  {key}: API 回傳空資料")
            return key, None
        return key, pd.DataFrame(data)
    except requests.exceptions.RequestException as e:
        print(f"  ❌ {key}: {e}")
        return key, None


# ───────────────────────────────────────────
# 2. 資料清理
# ───────────────────────────────────────────
_CODE_ALIASES = {'Code', '公司代號', '證券代號'}

def normalize_code_column(df: pd.DataFrame) -> pd.DataFrame:
    """將各種代號欄位統一命名為『代號』並去空白"""
    matched = _CODE_ALIASES & set(df.columns)
    if matched:
        df = df.rename(columns={col: '代號' for col in matched})
    if '代號' in df.columns:
        df['代號'] = df['代號'].astype(str).str.strip()
    return df


def safe_numeric(series: pd.Series, fill: float = 0) -> pd.Series:
    """統一的安全數值轉換：dash → NaN → fill"""
    return pd.to_numeric(
        series.replace(['-', '--', ''], np.nan),
        errors='coerce'
    ).fillna(fill)


# ───────────────────────────────────────────
# 3. 股利邏輯 — 連續配發現金股利 + 連續獲利
# ───────────────────────────────────────────
def _calc_consecutive_years(df: pd.DataFrame, year_col: str, val_col: str, label: str = '') -> pd.DataFrame:
    """
    通用函數：計算每檔股票從最新年度往前，連續 val_col > 0 的年數。
    回傳 DataFrame: [代號, placeholder]
    """
    streak_dict = {}
    for code, group in df.groupby('代號'):
        # 取出該股有正值的年度，由新到舊排列
        positive_mask = group[val_col] > 0
        positive_years = sorted(group.loc[positive_mask, year_col].unique(), reverse=True)

        # 最新年度本身未配/虧損 → 連續中斷，為 0
        if len(positive_years) == 0 or positive_years[0] != group[year_col].max():
            streak_dict[code] = 0
            continue

        # 從最新年度往回數連續年
        streak = 1
        for i in range(1, len(positive_years)):
            if positive_years[i - 1] - positive_years[i] == 1:
                streak += 1
            else:
                break
        streak_dict[code] = streak

    result = pd.DataFrame(list(streak_dict.items()), columns=['代號', 'placeholder'])

    # Debug: 顯示 top 5 驗證
    if label:
        top5 = result.nlargest(5, 'placeholder')
        samples = ', '.join(f"{r['代號']}={r['placeholder']}年" for _, r in top5.iterrows())
        print(f"    [{label}] Top5: {samples}")

    return result


def process_dividends(df_div_hist: pd.DataFrame):
    """
    回傳:
      - df_streak: 代號 / 累積配息年數 / 連續配發現金股利年數 / 連續獲利年數
      - df_latest_div: 代號 / 最新年度現金股利
    """
    # Debug: 顯示原始欄位名稱
    print(f"    股利 API 原始欄位: {list(df_div_hist.columns)}")

    # 動態偵測欄位名稱（容錯）
    div_col = next((c for c in df_div_hist.columns if '現金股利' in c and '盈餘' in c), None)
    if not div_col:
        div_col = next((c for c in df_div_hist.columns if '現金股利' in c), None)
    if not div_col:
        print("  ❌ 找不到現金股利欄位！")
        return pd.DataFrame(columns=['代號', '累積配息年數', '連續配發現金股利年數', '連續獲利年數']), \
               pd.DataFrame(columns=['代號', '最新年度現金股利'])
    print(f"    現金股利欄位: '{div_col}'")

    year_col = next((c for c in df_div_hist.columns if '年度' in c), '股利年度')
    print(f"    年度欄位: '{year_col}'")

    df_div_hist[div_col] = safe_numeric(df_div_hist[div_col])
    df_div_hist[year_col] = safe_numeric(df_div_hist[year_col]).astype(int)

    # 動態偵測 EPS 欄位（常見名稱：每股盈餘(元)、每股盈餘）
    eps_col = next((c for c in df_div_hist.columns if '每股盈餘' in c), None)
    if eps_col:
        df_div_hist[eps_col] = safe_numeric(df_div_hist[eps_col])
        print(f"    EPS 欄位: '{eps_col}'")
    else:
        print("  ⚠️  歷史股利資料中未找到 EPS 欄位，連續獲利年數將設為 0")

    # 季配息 → 年度加總
    agg_dict = {div_col: 'sum'}
    if eps_col:
        agg_dict[eps_col] = 'first'  # EPS 同年度取第一筆（非加總）

    df_yearly = (
        df_div_hist
        .groupby(['代號', year_col], as_index=False)
        .agg(agg_dict)
    )

    # ── 累積配息年數 ──
    df_total = (
        df_yearly[df_yearly[div_col] > 0]
        .groupby('代號')[year_col]
        .nunique()
        .reset_index(name='累積配息年數')
    )

    # Debug: 顯示年度範圍和資料筆數
    yr_min, yr_max = df_yearly[year_col].min(), df_yearly[year_col].max()
    n_stocks = df_yearly['代號'].nunique()
    print(f"    股利資料: {n_stocks} 檔, 年度 {yr_min}~{yr_max} (共 {yr_max - yr_min + 1} 年)")

    # 抽樣一檔確認: 取累積配息最多的一檔看看
    sample_code = df_total.nlargest(1, '累積配息年數')['代號'].iloc[0] if not df_total.empty else None
    if sample_code:
        sample = df_yearly[df_yearly['代號'] == sample_code][[year_col, div_col]].sort_values(year_col, ascending=False).head(8)
        print(f"    驗證 ({sample_code}) 最近股利:")
        for _, row in sample.iterrows():
            print(f"      年度 {int(row[year_col])}: 股利 {row[div_col]}")

    # ── 連續配發現金股利年數（從最新年度往前算） ──
    df_consec_div = _calc_consecutive_years(df_yearly, year_col, div_col, label='連續配息')
    df_consec_div.columns = ['代號', '連續配發現金股利年數']

    # ── 連續獲利年數（從最新年度往前算，EPS > 0） ──
    if eps_col:
        df_consec_eps = _calc_consecutive_years(df_yearly, year_col, eps_col, label='連續獲利')
        df_consec_eps.columns = ['代號', '連續獲利年數']
    else:
        df_consec_eps = pd.DataFrame({'代號': df_yearly['代號'].unique(), '連續獲利年數': 0})

    # 合併所有 streak 指標
    df_streak = df_total
    for df_s in [df_consec_div, df_consec_eps]:
        df_streak = pd.merge(df_streak, df_s, on='代號', how='outer')
    df_streak = df_streak.fillna(0)

    # ── 最新年度現金股利 ──
    df_latest = (
        df_yearly
        .sort_values(year_col, ascending=False)
        .groupby('代號', as_index=False)
        .first()
        [['代號', div_col]]
        .rename(columns={div_col: '最新年度現金股利'})
    )

    return df_streak, df_latest


# ───────────────────────────────────────────
# 4. 核心計算（全向量化）
# ───────────────────────────────────────────
def build_master_table(
    df_metrics: pd.DataFrame,
    df_profit: pd.DataFrame,
    df_prices: pd.DataFrame,
    df_streak: pd.DataFrame,
    df_latest_div: pd.DataFrame,
) -> pd.DataFrame:
    """合併所有資料並計算衍生指標"""

    # 轉換數值欄位
    df_metrics['PE']   = safe_numeric(df_metrics['PEratio'])
    df_metrics['PB']   = safe_numeric(df_metrics['PBratio'])
    df_metrics['殖利率'] = safe_numeric(df_metrics['DividendYield'])
    df_prices['收盤價']  = safe_numeric(df_prices['ClosingPrice'])

    # 動態偵測純益率欄位
    net_col = next((c for c in df_profit.columns if '純益率' in c), None)
    assert net_col, "找不到純益率欄位，API 結構可能已變動"
    df_profit['獲利率'] = safe_numeric(df_profit[net_col])

    # 多表合併（用 reduce 取代串接 merge）
    merge_specs = [
        df_metrics[['代號', 'Name', 'PE', 'PB', '殖利率']],
        df_prices[['代號', '收盤價']],
        df_profit[['代號', '獲利率']],
    ]
    df_all = reduce(lambda l, r: pd.merge(l, r, on='代號', how='inner'), merge_specs)
    df_all = pd.merge(df_all, df_streak, on='代號', how='left')
    df_all = pd.merge(df_all, df_latest_div, on='代號', how='left')

    # 填補 left join 產生的 NaN
    fill_cols = {'累積配息年數': 0, '連續配發現金股利年數': 0, '連續獲利年數': 0, '最新年度現金股利': 0}
    df_all = df_all.fillna(fill_cols)

    return _derive_metrics(df_all)


def _derive_metrics(df_all: pd.DataFrame) -> pd.DataFrame:
    """由原始欄位（價格 / PE / PB / 股利 / 配息年數）計算所有衍生指標。
    覆蓋股利資料（如 Goodinfo 補強）後可再呼叫一次以重算估價。"""
    # 衍生指標（全部向量化）
    pe_pos = df_all['PE'] > 0
    pb_pos = df_all['PB'] > 0

    df_all['EPS']     = np.where(pe_pos, (df_all['收盤價'] / df_all['PE']).round(2), 0)
    df_all['每股淨值'] = np.where(pb_pos, (df_all['收盤價'] / df_all['PB']).round(2), 0)

    eps_pos = df_all['EPS'] > 0
    df_all['溢價回收年限'] = np.where(
        eps_pos,
        ((df_all['收盤價'] - df_all['每股淨值']) / df_all['EPS']).round(2),
        99
    )
    df_all['盈餘發放率%'] = np.where(
        eps_pos,
        ((df_all['最新年度現金股利'] / df_all['EPS']) * 100).round(2),
        0
    )

    # 投資 CP 值
    adj_premium = np.maximum(df_all['溢價回收年限'], 0) + 1
    df_all['投資CP值'] = (
        (df_all['殖利率'] + df_all['累積配息年數'] / 2) / adj_premium
    ).round(2)

    # ── 價值投資估價（殖利率法） ──
    div = df_all['最新年度現金股利']
    div_pos = div > 0
    df_all['便宜價'] = np.where(div_pos, (div / VALUE_YIELD['便宜']).round(2), 0)
    df_all['合理價'] = np.where(div_pos, (div / VALUE_YIELD['合理']).round(2), 0)
    df_all['昂貴價'] = np.where(div_pos, (div / VALUE_YIELD['昂貴']).round(2), 0)

    # 安全邊際 = (合理價 - 現價) / 合理價 × 100%
    fair = df_all['合理價']
    df_all['安全邊際%'] = np.where(
        fair > 0,
        ((fair - df_all['收盤價']) / fair * 100).round(2),
        -999
    )

    # 股價位階（無股利 → 無估價基準，標示為「—」而非誤判昂貴）
    conditions = [
        ~div_pos,
        df_all['收盤價'] <= df_all['便宜價'],
        df_all['收盤價'] <= df_all['合理價'],
        df_all['收盤價'] <= df_all['昂貴價'],
        df_all['收盤價'] > df_all['昂貴價'],
    ]
    choices = ['—', '💚 便宜', '💛 合理', '🟠 偏貴', '🔴 昂貴']
    df_all['股價位階'] = np.select(conditions, choices, default='—')

    return df_all


def enrich_with_finmind(df_all: pd.DataFrame) -> pd.DataFrame:
    """用 FinMind 多年歷史覆蓋候選股的 累積/連續配息年數、最新年度現金股利、連續獲利年數，再重算估價。

    候選以 TWSE 穩定指標（殖利率/PE/PB）粗篩以限制查詢量；非候選沿用 TWSE 資料。
    結果存磁碟快取，重跑只補未快取者。
    """
    global HISTORY_USED
    try:
        import finmind_source
    except ImportError as e:
        print(f"  ⚠️  無法載入 finmind_source（{e}），跳過歷史補強")
        return df_all

    cand = df_all[(df_all['殖利率'] >= 4) & (df_all['PE'] > 0) &
                  (df_all['PE'] <= 18) & (df_all['PB'] <= 2.5)]['代號'].tolist()
    if not cand:
        print("  ⚠️  無符合粗篩的候選股，跳過歷史補強")
        return df_all

    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(HISTORY_CACHE, encoding='utf-8') as f:
            cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}
    now = time.time()
    # v2：最新股利改為近一年（季配加總最近4次）；舊版快取視為過期
    is_fresh = lambda v: v.get('v') == 2 and (now - v.get('ts', 0)) < HISTORY_TTL_DAYS * 86400

    to_fetch = [c for c in cand if not (c in cache and is_fresh(cache[c]))]
    print(f"  📦 候選 {len(cand)} 檔；已快取 {len(cand) - len(to_fetch)} 檔，待查 {len(to_fetch)} 檔")

    if to_fetch:
        fdf = finmind_source.batch_fetch(to_fetch)
        if 'fin_最新股利' in fdf.columns:
            for _, row in fdf.dropna(subset=['fin_最新股利']).iterrows():
                cache[str(row['代號'])] = {
                    '累積': int(row['fin_累積配息年數']),
                    '連續': int(row['fin_連續配息年數']),
                    '最新': float(row['fin_最新股利']),
                    '獲利': int(row['fin_連續獲利年數']),
                    'ts': now,
                    'v': 2,
                }
        with open(HISTORY_CACHE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)

    applied = {c: cache[c] for c in cand if c in cache and is_fresh(cache[c])}
    if not applied:
        print("  ⚠️  尚無可用的 FinMind 快取，沿用 TWSE 股利")
        return df_all

    m = df_all['代號'].isin(applied)
    df_all.loc[m, '累積配息年數'] = df_all.loc[m, '代號'].map(lambda c: applied[c]['累積'])
    df_all.loc[m, '連續配發現金股利年數'] = df_all.loc[m, '代號'].map(lambda c: applied[c]['連續'])
    df_all.loc[m, '最新年度現金股利'] = df_all.loc[m, '代號'].map(lambda c: applied[c]['最新'])
    df_all.loc[m, '連續獲利年數'] = df_all.loc[m, '代號'].map(lambda c: applied[c].get('獲利', 0))

    HISTORY_USED = True
    print(f"  ✅ 套用 FinMind {int(m.sum())} 檔（快取共 {len(cache)} 檔），重算估價中...")
    return _derive_metrics(df_all)


# ───────────────────────────────────────────
# 5. 篩選
# ───────────────────────────────────────────
def apply_filters(df_all: pd.DataFrame):
    """三軌篩選，回傳 (value_df, sheet1_df, sheet2_df)"""

    # ── 價值投資精選（以安全邊際排序） ──
    cv = VALUE_CRITERIA
    value_sheet = df_all[
        (df_all['PE'] > 0) &
        (df_all['PE']              <= cv['PE_上限']) &
        (df_all['PB']              <= cv['PB_上限']) &
        (df_all['安全邊際%']       >= cv['安全邊際_下限']) &
        (df_all['連續獲利年數']    >= cv['連續獲利年數_下限']) &
        (df_all['殖利率']          >= cv['殖利率_下限']) &
        (df_all['盈餘發放率%']     >= cv['盈餘發放率_下限']) &
        (df_all['盈餘發放率%']     <= cv['盈餘發放率_上限'])
    ].sort_values('安全邊際%', ascending=False).copy()

    # ── 廣泛價值清單 ──
    c1 = SHEET1_CRITERIA
    sheet1 = df_all[
        (df_all['溢價回收年限'] <= c1['溢價回收年限_上限']) &
        (df_all['殖利率']       >= c1['殖利率_下限']) &
        (df_all['獲利率']       >= c1['獲利率_下限']) &
        (df_all['盈餘發放率%']  >= c1['盈餘發放率_下限']) &
        (df_all['盈餘發放率%']  <= c1['盈餘發放率_上限'])
    ].sort_values('投資CP值', ascending=False).copy()

    # ── 嚴選防禦存股 ──
    c2 = SHEET2_CRITERIA
    sheet2 = df_all[
        (df_all['累積配息年數']  >= c2['累積配息年數_下限']) &
        (df_all['盈餘發放率%']  >= c2['盈餘發放率_下限']) &
        (df_all['盈餘發放率%']  <= c2['盈餘發放率_上限']) &
        (df_all['殖利率']       >= c2['殖利率_下限']) &
        (df_all['溢價回收年限'] <= c2['溢價回收年限_上限'])
    ].sort_values('投資CP值', ascending=False).copy()

    return value_sheet, sheet1, sheet2


# ───────────────────────────────────────────
# 5b. twstock 個股補充分析
# ───────────────────────────────────────────
def _fetch_realtime_batch(codes: list) -> dict:
    """批量抓取即時報價，回傳 {code: {price, change_pct, volume}} 或空 dict"""
    result = {}
    try:
        # twstock.realtime.get 支援 list 批量查詢
        for i in range(0, len(codes), 30):
            batch = codes[i:i+30]
            try:
                data = twstock.realtime.get(batch) if len(batch) > 1 else {batch[0]: twstock.realtime.get(batch[0])}
            except Exception as e:  # 單批有一檔缺欄位就整批失敗 → 只略過這批
                print(f"\n  ⚠️  即時報價第 {i // 30 + 1} 批失敗: {e}", end='')
                continue
            for code in batch:
                if code not in data or not isinstance(data[code], dict):
                    continue
                entry = data[code]
                if not entry.get('success'):
                    continue
                rt = entry.get('realtime', {})
                info = entry.get('info', {})
                try:
                    price = float(rt.get('latest_trade_price', '0').replace(',', ''))
                    yesterday = float(rt.get('open', '0').replace(',', ''))  # 近似用開盤價
                    volume = int(rt.get('accumulate_trade_volume', '0').replace(',', ''))
                    change_pct = round((price / yesterday - 1) * 100, 2) if yesterday > 0 else 0
                    result[code] = {
                        '即時成交價': price,
                        '漲跌幅%': change_pct,
                        '成交量(張)': volume,
                    }
                except (ValueError, TypeError):
                    pass
            if i + 30 < len(codes):
                time.sleep(2)
    except Exception as e:
        print(f"  ⚠️  即時報價抓取失敗: {e}")
    return result


DAILY_CACHE = "data/daily_prices.json"   # MI_INDEX 每日全市場行情快取（過去日期不會變）
MA_DAYS = 60                             # 需要的交易日數（60日均價）
MI_INDEX_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"


def load_daily_history(n_days: int = MA_DAYS) -> dict:
    """抓最近 n_days 個交易日的全市場日行情（TWSE MI_INDEX，一天一個請求含全部個股）。

    回傳 {代號: [(成交股數, 開盤, 收盤), ...]}（舊→新）。過去日期存磁碟快取，
    之後每次只需補當天；休市日也記錄（空 dict）避免重查。
    """
    try:
        with open(DAILY_CACHE, encoding='utf-8') as f:
            cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}

    today = datetime.now().date()
    days, fetched, d = [], 0, today
    while len(days) < n_days and (today - d).days < n_days * 2:
        key = d.strftime('%Y%m%d')
        if d.weekday() < 5:
            day = {}
            if key not in cache:
                day = _fetch_mi_index(key)
                fetched += 1
                print(f"\r    下載日行情 {key}（第 {fetched} 天）", end='')
                if day or (day is not None and d < today):  # 網路失敗(None)或今天尚未收盤 → 不快取，下次再試
                    cache[key] = day
                time.sleep(1.5)                # TWSE 限流：過快會被暫時封鎖 IP
            if cache.get(key):
                days.append(key)
            elif day is None:              # 網路失敗：中斷，避免整段空查
                break
        d -= timedelta(days=1)
    if fetched:
        print()
        keep = {k: v for k, v in cache.items() if k >= d.strftime('%Y%m%d')}  # 只留窗口內，檔案不無限長大
        with open(DAILY_CACHE, 'w', encoding='utf-8') as f:
            json.dump(keep, f, separators=(',', ':'))

    hist = {}
    for key in sorted(days):
        for code, row in cache[key].items():
            hist.setdefault(code, []).append(row)
    print(f"  📅 日行情 {len(days)} 個交易日（新下載 {fetched} 天），{len(hist)} 檔")
    return hist


def _fetch_mi_index(date_key: str) -> dict:
    """單日全市場收盤行情 → {代號: [成交股數, 開盤, 收盤]}；休市/無資料回傳 {}，網路失敗回傳 None"""
    try:
        r = requests.get(MI_INDEX_URL, params={'date': date_key, 'type': 'ALLBUT0999', 'response': 'json'},
                         timeout=20)
        j = r.json()
    except Exception as e:
        print(f"\n  ⚠️  {date_key} 日行情抓取失敗: {e}")
        return None
    table = next((t for t in j.get('tables', []) if '每日收盤行情' in t.get('title', '')), None)
    if j.get('stat') != 'OK' or not table:
        return {}
    f = table['fields']
    ic, iv, io, ix = f.index('證券代號'), f.index('成交股數'), f.index('開盤價'), f.index('收盤價')
    num = lambda v: float(str(v).replace(',', ''))
    out = {}
    for row in table['data']:
        try:
            out[row[ic].strip()] = [num(row[iv]), num(row[io]), num(row[ix])]
        except ValueError:                     # '--' = 當日無成交，略過該日
            pass
    return out


def _analyze_history(rows: list) -> dict:
    """由日行情計算均線 + twstock 四大買賣點（沿用 twstock 判斷邏輯，只換資料來源）"""
    result = {'四大買賣點': '—', '技術面理由': '—', '5日均價': 0, '20日均價': 0, '60日均價': 0}
    if len(rows) < 6:                          # 四大買賣點需 6 日均線
        result['四大買賣點'] = '資料不足'
        return result
    price = [r[2] for r in rows]
    for n in (5, 20, 60):
        if len(price) >= n:
            result[f'{n}日均價'] = round(sum(price[-n:]) / n, 2)

    class _Hist(twstock.analytics.Analytics):  # BestFourPoint 只用 price / open / capacity
        pass
    h = _Hist()
    h.price, h.open, h.capacity = price, [r[1] for r in rows], [r[0] for r in rows]
    try:
        signal = twstock.BestFourPoint(h).best_four_point()
    except Exception:
        signal = None
    if signal:
        is_buy, reason = signal
        result['四大買賣點'] = '🟢 買進' if is_buy else '🔴 賣出'
        result['技術面理由'] = reason
    else:
        result['四大買賣點'] = '⚪ 觀望'
        result['技術面理由'] = '無明確訊號'
    return result


def build_twstock_cache(codes: list, analysis_codes: list) -> tuple:
    """回傳 (realtime_map, analysis_map)：即時報價走 twstock（codes）；
    技術分析用 MI_INDEX 日行情本機計算，成本低，故涵蓋 analysis_codes（全市場）。"""
    print(f"  📡 抓取 {len(codes)} 檔即時報價...", end=' ')
    realtime_map = _fetch_realtime_batch(codes)
    print(f"✅ 取得 {len(realtime_map)} 檔")

    print("  📊 四大買賣點 + 均線分析（TWSE 日行情）")
    hist = load_daily_history()
    analysis_map = {c: _analyze_history(hist.get(c, [])) for c in analysis_codes}
    return realtime_map, analysis_map


def enrich_with_twstock(df: pd.DataFrame, realtime_map: dict, analysis_map: dict) -> pd.DataFrame:
    """用預先抓好的快取補充即時報價 + 四大買賣點 + 均線分析（純對應，不發網路請求）"""
    if not TWSTOCK_AVAILABLE or df.empty:
        return df

    # 合併回 DataFrame
    df['即時成交價'] = df['代號'].map(lambda c: realtime_map.get(c, {}).get('即時成交價', 0))
    df['漲跌幅%']    = df['代號'].map(lambda c: realtime_map.get(c, {}).get('漲跌幅%', 0))
    df['成交量(張)']  = df['代號'].map(lambda c: realtime_map.get(c, {}).get('成交量(張)', 0))
    df['四大買賣點']  = df['代號'].map(lambda c: analysis_map.get(c, {}).get('四大買賣點', '—'))
    df['技術面理由']  = df['代號'].map(lambda c: analysis_map.get(c, {}).get('技術面理由', '—'))
    df['5日均價']    = df['代號'].map(lambda c: analysis_map.get(c, {}).get('5日均價', 0))
    df['20日均價']   = df['代號'].map(lambda c: analysis_map.get(c, {}).get('20日均價', 0))
    df['60日均價']   = df['代號'].map(lambda c: analysis_map.get(c, {}).get('60日均價', 0))
    return df


# ───────────────────────────────────────────
# 6. 欄位說明資料
# ───────────────────────────────────────────
COLUMN_DEFINITIONS = [
    {'欄位名稱': '代號', '定義': '台灣證券交易所的股票代碼', '數值越大代表': '—', '數值越小代表': '—', '存股參考': '純識別用途，無方向性意義'},
    {'欄位名稱': 'Name', '定義': '公司名稱', '數值越大代表': '—', '數值越小代表': '—', '存股參考': '純識別用途'},
    {'欄位名稱': 'PE (本益比)', '定義': '股價 ÷ 每股盈餘 (EPS)。代表投資人願意為每 1 元獲利付出多少錢', '數值越大代表': '市場給予較高評價，或股價偏貴（成長股常見 PE 20~40+）', '數值越小代表': '可能被低估，或市場對前景保守（價值股常見 PE 8~15）', '存股參考': '⭐ 存股偏好低 PE（<15），代表用較少的錢買到較多的獲利能力'},
    {'欄位名稱': 'PB (股價淨值比)', '定義': '股價 ÷ 每股淨值。衡量股價相對於公司帳面資產的溢價程度', '數值越大代表': '股價遠超過帳面價值，溢價高（品牌/技術溢價或泡沫）', '數值越小代表': '股價接近甚至低於帳面價值，可能是撿便宜機會', '存股參考': '⭐ PB < 1.5 較安全；PB < 1 代表「打折買資產」，但需排除衰退股'},
    {'欄位名稱': '殖利率 (%)', '定義': '每股現金股利 ÷ 股價 × 100%。衡量持有一年的現金回報率', '數值越大代表': '💰 每投入 1 元能拿回更多股利，現金流回報高', '數值越小代表': '股利回報低，可能是成長股（不配息）或股價過高', '存股參考': '⭐⭐ 存股核心指標！一般 ≥ 4% 為佳，≥ 5% 為優，≥ 7% 需留意是否為一次性'},
    {'欄位名稱': '收盤價', '定義': '該股票最近一個交易日的收盤價格（元）', '數值越大代表': '每股價格高（不代表貴，需搭配 PE/PB 判斷）', '數值越小代表': '每股價格低（不代表便宜，低價股可能是體質差）', '存股參考': '單看股價無意義，需搭配其他估值指標綜合判斷'},
    {'欄位名稱': '獲利率 (純益率 %)', '定義': '稅後淨利 ÷ 營收 × 100%。公司每賺 100 元營收，實際淨賺多少', '數值越大代表': '💪 公司賺錢效率高，護城河深，產品附加價值高', '數值越小代表': '利潤微薄，競爭激烈或成本控制差', '存股參考': '⭐ 獲利率 ≥ 10% 為優秀，≥ 5% 為合格，< 3% 需警戒'},
    {'欄位名稱': '累積配息年數', '定義': '歷史資料中，該公司總共有幾個年度發放過現金股利', '數值越大代表': '📈 配息經驗豐富，公司有長期回饋股東的習慣', '數值越小代表': '配息歷史短，可能是新上市或過去較少配息', '存股參考': '⭐ ≥ 10 年代表穩定配息體質，是防禦型存股的基本門檻'},
    {'欄位名稱': '連續配發現金股利年數', '定義': '從最近一年往前推算，連續不中斷發放現金股利的年數', '數值越大代表': '🏆 穩定性極高，即使景氣波動也持續配發現金', '數值越小代表': '近年曾中斷配息，現金流穩定性存疑', '存股參考': '⭐⭐ 連續 ≥ 10 年是「定存股」黃金標準，比累積更嚴格'},
    {'欄位名稱': '連續獲利年數', '定義': '從最近一年往前推算，連續每股盈餘 (EPS) > 0 的年數', '數值越大代表': '💪 公司獲利穩定，即使遇到景氣低谷也不虧損', '數值越小代表': '近年曾出現虧損，獲利體質不夠穩健', '存股參考': '⭐⭐ 連續獲利是配息的根基！無獲利則配息不可持續，≥ 10 年為優'},
    {'欄位名稱': '最新年度現金股利', '定義': '最近一個完整年度發放的現金股利合計（含季配加總）', '數值越大代表': '每股發放的現金多', '數值越小代表': '每股發放的現金少', '存股參考': '需搭配 EPS 看發放率，單獨看意義有限'},
    {'欄位名稱': 'EPS (每股盈餘)', '定義': '收盤價 ÷ PE。代表每一股在過去一年賺了多少錢', '數值越大代表': '💰 公司賺錢能力強，每股含金量高', '數值越小代表': '獲利能力弱或虧損', '存股參考': '⭐ EPS 穩定成長的公司最適合存股，代表獲利引擎健康'},
    {'欄位名稱': '每股淨值', '定義': '收盤價 ÷ PB。公司每一股對應的帳面資產價值', '數值越大代表': '每股背後的資產厚實', '數值越小代表': '資產單薄，抗風險能力可能較差', '存股參考': '淨值是股價的「地板價」參考，股價跌破淨值可能是機會或警訊'},
    {'欄位名稱': '溢價回收年限', '定義': '(股價 - 每股淨值) ÷ EPS。買入時多付的溢價，需要幾年的獲利才能賺回來', '數值越大代表': '⚠️ 溢價太高，回本時間長，風險較高', '數值越小代表': '✅ 溢價低甚至折價，回本快，安全邊際大', '存股參考': '⭐⭐ 核心安全指標！≤ 5 年極佳，≤ 10 年良好，> 15 年偏貴'},
    {'欄位名稱': '盈餘發放率 (%)', '定義': '現金股利 ÷ EPS × 100%。公司把賺到的錢拿出多少比例分給股東', '數值越大代表': '股東分到的比例高（但 >100% 代表借錢配息，危險⚠️）', '數值越小代表': '公司保留較多盈餘再投資（成長股常見）', '存股參考': '⭐ 存股理想區間 50%~85%，兼顧配息與公司成長動能'},
    {'欄位名稱': '投資CP值', '定義': '(殖利率 + 累積配息年數/2) ÷ (溢價回收年限+1)。綜合評分指標', '數值越大代表': '🌟 殖利率高 + 配息穩 + 溢價低 → 高性價比', '數值越小代表': '性價比低，可能殖利率差或溢價過高', '存股參考': '⭐⭐⭐ 本報告的核心排序指標，越高越值得優先研究'},
    {'欄位名稱': '即時成交價', '定義': '由 twstock 抓取的即時成交價格（盤中為即時價，盤後為收盤價）', '數值越大代表': '當前股價高', '數值越小代表': '當前股價低', '存股參考': '搭配收盤價比較，看是否在合理買入區間'},
    {'欄位名稱': '漲跌幅%', '定義': '今日即時價相對於開盤價的漲跌百分比', '數值越大代表': '🟢 今日上漲，市場看好', '數值越小代表': '🔴 今日下跌，可能是進場機會或警訊', '存股參考': '存股不追漲，下跌時分批買進反而有利'},
    {'欄位名稱': '四大買賣點', '定義': 'twstock 內建技術分析：量價關係 + 3日/6日均線交叉判斷買賣訊號', '數值越大代表': '—', '數值越小代表': '—', '存股參考': '⭐ 🟢買進 = 可考慮分批進場；🔴賣出 = 暫緩買入；⚪觀望 = 等待訊號'},
    {'欄位名稱': '技術面理由', '定義': '四大買賣點的具體觸發理由', '數值越大代表': '—', '數值越小代表': '—', '存股參考': '搭配四大買賣點一起看'},
    {'欄位名稱': '5日/20日/60日均價', '定義': '最近 N 日收盤價的簡單移動平均 (SMA)', '數值越大代表': '近期均價較高，可能處於上升趨勢', '數值越小代表': '近期均價較低', '存股參考': '⭐ 現價 < 20日均價 可能是進場機會；現價 > 60日均價很多則偏貴'},
    {'欄位名稱': '便宜價', '定義': '最新年度現金股利 ÷ 6.25%。殖利率達 6.25% 時的股價', '數值越大代表': '該股股利高，對應的「便宜」門檻也高', '數值越小代表': '股利低', '存股參考': '⭐⭐ 現價 ≤ 便宜價 → 極度低估，是分批進場的好時機'},
    {'欄位名稱': '合理價', '定義': '最新年度現金股利 ÷ 5%。殖利率達 5% 時的股價', '數值越大代表': '合理估值較高', '數值越小代表': '合理估值較低', '存股參考': '⭐⭐ 現價 ≤ 合理價 → 估值合理，可考慮買入'},
    {'欄位名稱': '昂貴價', '定義': '最新年度現金股利 ÷ 4%。殖利率只有 4% 時的股價', '數值越大代表': '昂貴門檻高', '數值越小代表': '昂貴門檻低', '存股參考': '現價 > 昂貴價 → 股價偏貴，存股族應暫緩買入'},
    {'欄位名稱': '安全邊際%', '定義': '(合理價 − 現價) ÷ 合理價 × 100%。正值代表現價低於合理價', '數值越大代表': '🟢 折價幅度大，安全邊際高，買入風險低', '數值越小代表': '🔴 現價超過合理價，溢價越多風險越高', '存股參考': '⭐⭐⭐ 價值投資核心指標！> 20% 為優質安全邊際'},
    {'欄位名稱': '股價位階', '定義': '將現價對應到便宜/合理/昂貴三個價位的分類', '數值越大代表': '—', '數值越小代表': '—', '存股參考': '⭐ 💚便宜 = 積極買入；💛合理 = 可買；🟠偏貴 = 觀望；🔴昂貴 = 不買'},
]


# ───────────────────────────────────────────
# 7. JSON 快照輸出（供 Web UI 載入；xlsx 改由前端匯出）
# ───────────────────────────────────────────
def _df_to_records(df: pd.DataFrame) -> list:
    """DataFrame → list[dict]，正確處理 numpy 型別與 NaN（NaN → null）"""
    return json.loads(df.to_json(orient='records', force_ascii=False))


def export_to_json(value_sheet: pd.DataFrame, sheet1: pd.DataFrame, sheet2: pd.DataFrame, df_all: pd.DataFrame,
                   data_dir: str = DATA_DIR) -> tuple:
    """寫出一份帶時間戳的快照 JSON，並更新 manifest。回傳 (快照路徑, generated_at_iso)。

    快照結構（前端與『個股追蹤』皆依賴此格式，欄位請保持穩定）：
      {
        generated_at:    ISO8601 時間戳（排序、追蹤用）
        generated_at_display: 人類可讀時間
        screener_version, twstock_enabled,
        column_definitions: [...],
        sheets: { value|sheet1|sheet2|all: { title, criteria, rows: [...] } }  # all = 全市場未篩選
      }
    """
    os.makedirs(data_dir, exist_ok=True)
    now = datetime.now()
    generated_at = now.isoformat(timespec='seconds')

    sheets_df = {'value': value_sheet, 'sheet1': sheet1, 'sheet2': sheet2,
                 'all': df_all.sort_values('代號')}
    snapshot = {
        'generated_at': generated_at,
        'generated_at_display': now.strftime('%Y-%m-%d %H:%M:%S'),
        'screener_version': SCREENER_VERSION,
        'twstock_enabled': TWSTOCK_AVAILABLE,
        'history_enriched': HISTORY_USED,
        'column_definitions': COLUMN_DEFINITIONS,
        'sheets': {
            key: {
                'title': SHEET_META[key]['title'],
                'criteria': SHEET_META[key]['criteria'],
                'rows': _df_to_records(df),
            }
            for key, df in sheets_df.items()
        },
    }

    fname = f"snapshot_{now.strftime('%Y%m%d_%H%M%S')}.json"
    fpath = os.path.join(data_dir, fname)
    with open(fpath, 'w', encoding='utf-8') as f:
        json.dump(snapshot, f, ensure_ascii=False, separators=(',', ':'))  # 精簡：含全市場後檔案變大

    _update_manifest(data_dir, fname, snapshot)
    return fpath, generated_at


def _update_manifest(data_dir: str, fname: str, snapshot: dict):
    """維護 manifest.json：最新在前，供 Web UI 列出歷史快照可供切換/比較。"""
    manifest_path = os.path.join(data_dir, MANIFEST_FILENAME)
    try:
        with open(manifest_path, encoding='utf-8') as f:
            manifest = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        manifest = {'snapshots': []}

    entry = {
        'file': fname,
        'generated_at': snapshot['generated_at'],
        'generated_at_display': snapshot['generated_at_display'],
        'counts': {k: len(v['rows']) for k, v in snapshot['sheets'].items()},
    }
    # 去除同檔重複後插到最前
    manifest['snapshots'] = [s for s in manifest['snapshots'] if s.get('file') != fname]
    manifest['snapshots'].insert(0, entry)

    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


# ───────────────────────────────────────────
# 主程式
# ───────────────────────────────────────────
def run_ultimate_dividend_screener():
    t0 = time.perf_counter()

    print("🔍 [1/7] 啟動終極選股器：建立連線池 + 多執行緒抓取中...")
    # 1) 多執行緒抓取
    session = _build_session()
    data_frames = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(fetch_endpoint, session, key, path): key
            for key, path in ENDPOINTS.items()
        }
        for future in concurrent.futures.as_completed(futures):
            key, df = future.result()
            data_frames[key] = df

    # 驗證
    failed = [k for k, v in data_frames.items() if v is None]
    if failed:
        print(f"❌ 以下端點抓取失敗：{', '.join(failed)}，請檢查網路或稍後再試。")
        return

    # 2) 統一代號欄位
    print("🧹 [2/7] 資料清理與股息邏輯重構 (季配息加總 + 連續配息計算)...")
    for key in data_frames:
        data_frames[key] = normalize_code_column(data_frames[key])

    # 3) 股利處理
    df_streak, df_latest_div = process_dividends(data_frames['div_hist'])

    # 4) 合併 + 計算
    print("⚙️ [3/7] 向量化高速運算 (EPS, 估價, 安全邊際, 投資CP值)...")
    df_all = build_master_table(
        data_frames['metrics'],
        data_frames['profit'],
        data_frames['prices'],
        df_streak,
        df_latest_div,
    )

    # 4b) FinMind 多年歷史補強（手動啟用：FINMIND=1）
    if ENABLE_HISTORY:
        print("🌐 [4b] FinMind 多年歷史補強（覆蓋累積/連續配息年數、最新股利、連續獲利年數）...")
        df_all = enrich_with_finmind(df_all)
    else:
        print("ℹ️  歷史補強未啟用（如需多年配息/獲利資料：FINMIND=1 python3 stock_screener.py）")

    # 5) 三軌篩選
    print("📊 [4/7] 三軌條件篩選 (價值投資 + 廣泛清單 + 防禦存股)...")
    value_sheet, sheet1, sheet2 = apply_filters(df_all)

    # 6) twstock 個股分析
    if TWSTOCK_AVAILABLE:
        print(f"🔍 [5/7] twstock 混合分析（即時報價 + 四大買賣點 + 均線）...")
        # 三分頁高度重疊 → 對 union 去重後只抓一次，再對應回各分頁
        union_codes = pd.concat(
            [value_sheet['代號'], sheet1['代號'], sheet2['代號']]
        ).drop_duplicates().tolist()
        realtime_map, analysis_map = build_twstock_cache(union_codes, df_all['代號'].tolist())
        value_sheet = enrich_with_twstock(value_sheet, realtime_map, analysis_map)
        sheet1 = enrich_with_twstock(sheet1, realtime_map, analysis_map)
        sheet2 = enrich_with_twstock(sheet2, realtime_map, analysis_map)
        df_all = enrich_with_twstock(df_all, realtime_map, analysis_map)
    else:
        print("⚠️ [5/7] twstock 不可用，跳過個股分析")

    # 7) 輸出 JSON 快照（xlsx 改由 Web UI 匯出）
    print("💾 [6/7] 輸出 JSON 快照...")
    fpath, generated_at = export_to_json(value_sheet, sheet1, sheet2, df_all)

    elapsed = time.perf_counter() - t0
    twstock_status = '✅ 已整合' if TWSTOCK_AVAILABLE else '❌ 未整合'
    print(f"\n✅ [7/7] 大功告成！耗時 {elapsed:.1f} 秒 → {fpath}")
    print(f"   🕒 產生時間 {generated_at} | twstock {twstock_status}")
    print(f"   💎 價值投資精選: {len(value_sheet)} 家 ← 安全邊際排序")
    print(f"   📈 廣泛價值標的: {len(sheet1)} 家")
    print(f"   🛡️ 嚴選防禦標的: {len(sheet2)} 家")
    print(f"   🌐 全市場: {len(df_all)} 家")
    print(f"   🌐 用瀏覽器開 index.html 檢視（建議：python3 -m http.server 後開 http://localhost:8000）")


# ───────────────────────────────────────────
if __name__ == '__main__':
    run_ultimate_dividend_screener()