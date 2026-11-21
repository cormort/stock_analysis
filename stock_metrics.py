"""純計算層：股利/獲利 streak、估值、品質、技術、籌碼與綜合評分。

所有函式都不發網路請求，方便用 pytest 驗證。
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

# 篩選與評分參數（集中在這裡，UI 會把 criteria 寫進快照）
RISK_PREMIUM = 0.02          # 目標殖利率 = 10Y + 2%（可用 --risk-premium 覆寫）
MIN_AVG_VOLUME = 500         # 日均量（張）
DEBT_RATIO_MAX = 65          # 負債比上限（金融保險除外）
ROE_MIN = 8.0
MAX_PE, MAX_PB = 15.0, 2.0
MIN_YIELD = 4.0
PAYOUT_MIN, PAYOUT_MAX = 30.0, 110.0

VALUE_CRITERIA = {
    "PE_上限": MAX_PE, "PB_上限": MAX_PB, "殖利率_下限": MIN_YIELD,
    "盈餘發放率_下限": PAYOUT_MIN, "盈餘發放率_上限": 100.0,
    "連續配息年數_下限": 5, "安全邊際_下限": 0,
}
SHEET1_CRITERIA = {
    "溢價回收年限_上限": 15, "殖利率_下限": 3.0, "獲利率_下限": 3.0,
    "盈餘發放率_下限": PAYOUT_MIN, "盈餘發放率_上限": PAYOUT_MAX,
}
SHEET2_CRITERIA = {
    "連續配息年數_下限": 10, "盈餘發放率_下限": 40, "盈餘發放率_上限": 90,
    "殖利率_下限": 4.0, "溢價回收年限_上限": 15, "ROE_下限": 6.0,
}


# ───────────────────────────────────────────
# 股利
# ───────────────────────────────────────────
def streak_years(years) -> int:
    ys = sorted({int(y) for y in years}, reverse=True)
    if not ys:
        return 0
    n = 1
    for i in range(1, len(ys)):
        if ys[i - 1] - ys[i] == 1:
            n += 1
        else:
            break
    return n


def dividend_stats(events: list[dict], today: date | None = None) -> dict:
    """events: [{'date': 'YYYY-MM-DD', 'amount': float}]（除息日）。

    口徑：
      - 有效 DPS = 近 365 天除息總和；若為 0 但最近一次在 500 天內（年配股的時序漂移），取該次。
      - 5 年平均 = 最近 5 個「完整年度」加總平均；不足 3 年覆蓋 → None（不假裝穩定）。
      - streak 只用完整年度，避免今年尚未除息的季節性斷點。
    """
    today = today or date.today()
    ev = []
    for e in events or []:
        try:
            d = datetime.strptime(str(e["date"])[:10], "%Y-%m-%d").date()
            ev.append((d, float(e.get("amount") or 0)))
        except Exception:
            continue
    ev.sort()
    if not ev:
        return {"dps": None, "five_avg": None, "stability": None,
                "cum_years": None, "consec_years": None, "data_years": None, "span": None}

    ttm = sum(a for d, a in ev if 0 <= (today - d).days <= 365)
    if ttm == 0:
        d, a = ev[-1]
        if (today - d).days <= 500 and a > 0:
            ttm = a
    dps = round(ttm, 4) if ttm > 0 else 0.0

    by_year: dict[int, float] = {}
    for d, a in ev:
        by_year[d.year] = by_year.get(d.year, 0.0) + a
    complete = {y: v for y, v in by_year.items() if y < today.year}
    if not complete:
        return {"dps": dps, "five_avg": None, "stability": None,
                "cum_years": 0, "consec_years": 0, "data_years": 0, "span": None}

    years_sorted = sorted(complete)
    span = years_sorted[-1] - years_sorted[0] + 1
    window = [complete.get(y, 0.0) for y in range(today.year - 5, today.year)]
    five_avg = round(sum(window) / 5, 4) if span >= 3 else None
    stability = round(dps / five_avg, 2) if five_avg and five_avg > 0 else None

    positive_complete = [y for y, v in complete.items() if v > 0]
    return {
        "dps": dps,
        "five_avg": five_avg,
        "stability": stability,
        "cum_years": len(positive_complete),
        # 最近有配息的完整年度早於去年 → 已停發，連續年數歸 0（同 eps_stats 的錨定）
        "consec_years": (streak_years(positive_complete)
                         if positive_complete and max(positive_complete) >= today.year - 1 else 0),
        "data_years": len(complete),
        "span": span,
    }


# ───────────────────────────────────────────
# 年度 EPS
# ───────────────────────────────────────────
def eps_stats(eps: dict) -> dict:
    vals = {int(y): float(v) for y, v in (eps or {}).items() if v is not None}
    if not vals:
        return {"eps": None, "years": 0, "streak": None, "g1": None, "g3": None}
    years = sorted(vals)
    latest = vals[years[-1]]
    g1 = g3 = None
    if len(years) >= 2 and vals[years[-2]] > 0 and latest > 0:
        g1 = round((latest / vals[years[-2]] - 1) * 100, 1)
    if len(years) >= 4 and vals[years[-4]] > 0 and latest > 0:
        g3 = round(((latest / vals[years[-4]]) ** (1 / 3) - 1) * 100, 1)
    positive = [y for y in years if vals[y] > 0]
    # 只從「最新年度」往回數；若最新年度虧損就是 0
    streak = streak_years(positive) if positive and positive[-1] == years[-1] else 0
    return {"eps": round(latest, 2), "years": len(vals), "streak": streak, "g1": g1, "g3": g3}


# ───────────────────────────────────────────
# 技術面（資料列： [vol, open, high, low, close, amount]）
# ───────────────────────────────────────────
def _row6(r):
    """兼容舊快取的 3 欄格式 [vol, open, close]。"""
    if len(r) >= 6:
        return r[:6]
    return [r[0], r[1], r[1], r[1], r[2], 0]


def sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def atr(rows, n=14):
    r6 = [_row6(r) for r in rows]
    if len(r6) < n + 1:
        return None
    trs = []
    for i in range(len(r6) - n, len(r6)):
        h, l, pc = r6[i][2], r6[i][3], r6[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return round(sum(trs) / n, 2)


def volume_ratio(rows, n=20):
    """今日量 / 前 n 日均量（不含當日）——skill 的鐵則。"""
    if len(rows) < n + 1:
        return None
    vols = [r[0] for r in rows]
    base = sum(vols[-n - 1:-1]) / n
    return round(vols[-1] / base, 2) if base > 0 else None


def two_day_rule(rows, n=20):
    """回傳『真突破/假突破/真跌破/假跌破』或 None（以收盤判穿越、次日高低判確認）。"""
    r6 = [_row6(r) for r in rows]
    if len(r6) < max(n + 3, 4):
        return None
    closes = [r[4] for r in r6]
    if any(c is None for c in closes[-n - 3:]):
        return None
    ma = [sum(closes[i - n + 1:i + 1]) / n for i in range(n - 1, len(closes))]
    if len(ma) < 3:
        return None
    c_prev, c_break, c_now = closes[-3], closes[-2], closes[-1]
    m_prev, m_break = ma[-3], ma[-2]
    if c_prev <= m_prev and c_break > m_break:
        return "真突破" if r6[-1][2] > r6[-2][2] else "假突破"
    if c_prev >= m_prev and c_break < m_break:
        return "真跌破" if r6[-1][3] < r6[-2][3] else "假跌破"
    return None


def pct_return(vals, n):
    if len(vals) < n + 1 or vals[-n - 1] <= 0:
        return None
    return (vals[-1] / vals[-n - 1] - 1) * 100


def technical_stats(rows, taiex_closes=None) -> dict:
    r6 = [_row6(r) for r in rows]
    out = {"5日均價": None, "20日均價": None, "60日均價": None,
           "量比": None, "ATR14": None, "ATR%": None, "距60日高點%": None,
           "RS20": None, "RS60": None, "均線帶寬%": None,
           "二日法則_20MA": None, "二日法則_60MA": None, "技術資料狀態": "—"}
    if len(r6) < 6:
        out["技術資料狀態"] = "資料不足"
        return out
    closes = [r[4] for r in r6]
    for n in (5, 20, 60):
        v = sma(closes, n)
        out[f"{n}日均價"] = round(v, 2) if v else None
    out["量比"] = volume_ratio(r6)
    a = atr(r6)
    if a:
        out["ATR14"] = a
        out["ATR%"] = round(a / closes[-1] * 100, 2) if closes[-1] else None

    highs = [r[2] for r in r6[-60:]]
    if highs and closes[-1]:
        out["距60日高點%"] = round((closes[-1] / max(highs) - 1) * 100, 2)

    mas = {n: sma(closes, n) for n in (16, 20, 25, 45, 60)}
    if all(mas.values()):
        lo, hi = min(mas.values()), max(mas.values())
        out["均線帶寬%"] = round((hi - lo) / lo * 100, 2)
    out["二日法則_20MA"] = two_day_rule(r6, 20)
    out["二日法則_60MA"] = two_day_rule(r6, 60)

    if taiex_closes and len(taiex_closes) >= 21:
        for n in (20, 60):
            s = pct_return(closes, n)
            t = pct_return(list(taiex_closes), n)
            if s is not None and t is not None:
                out[f"RS{n}"] = round(s - t, 2)
    if len(r6) >= 60:
        out["日均量(張)"] = int(sum(r[0] for r in r6[-20:]) / 20 / 1000)
    out["技術資料狀態"] = "✅"
    return out


# ───────────────────────────────────────────
# 品質 / 估值
# ───────────────────────────────────────────
def quality_metrics(row: pd.Series) -> dict:
    """由 TWSE 單季財報 + 年度 EPS 算品質指標。"""
    revenue = _f(row.get("營業收入"))
    gross = _f(row.get("營業毛利（毛損）淨額"))
    op = _f(row.get("營業利益（損失）"))
    eps_q = _f(row.get("基本每股盈餘（元）"))
    q = _f(row.get("季別")) or 4
    eps_annual = None
    if eps_q is not None and q:
        eps_annual = eps_q * 4 / q
    return {
        "毛利率%": round(gross / revenue * 100, 2) if revenue and gross is not None else None,
        "營益率%": round(op / revenue * 100, 2) if revenue and op is not None else None,
        "EPS_年化": round(eps_annual, 2) if eps_annual is not None else None,
    }


def _f(v):
    try:
        x = float(v)
        return None if math.isnan(x) else x
    except (TypeError, ValueError):
        return None


def target_yield(us10y: float, premium: float | None = None) -> tuple[float, float, float]:
    """回傳 (合理, 便宜, 昂貴) 殖利率門檻（小數）。"""
    fair = max(us10y / 100 + (RISK_PREMIUM if premium is None else premium), 0.02)
    cheap = fair * 1.25
    expensive = max(fair * 0.8, 0.02)
    return round(fair, 4), round(cheap, 4), round(expensive, 4)


def valuation_metrics(dps, five_avg, price, us10y, premium: float | None = None):
    """安全邊際用保守股利：min(現行 TTM DPS, 5 年均)。

    只取 5 年均會在股利被砍 30% 時仍用舊高檔估價，製造假的安全邊際；
    只取現行 DPS 則會被一次性高配誤導。兩者取小是保守做法。
    """
    fair_y, cheap_y, exp_y = target_yield(us10y, premium)
    avg = five_avg if five_avg and five_avg > 0 else None
    # dps=0 代表「有除息紀錄但近一年沒配」＝停發，不可退回 5 年均假裝還有股利
    cur = max(dps, 0) if dps is not None else None
    div = min(avg, cur) if avg is not None and cur is not None else (avg or cur)
    if not div or not price:
        return {"便宜價": None, "合理價": None, "昂貴價": None, "安全邊際%": None,
                "股價位階": "—", "合理殖利率門檻%": round(fair_y * 100, 2),
                "估值股利": None}
    cheap, fair, exp = div / cheap_y, div / fair_y, div / exp_y
    mos = (fair - price) / fair * 100
    if price <= cheap:
        level = "💚 便宜"
    elif price <= fair:
        level = "💛 合理"
    elif price <= exp:
        level = "🟠 偏貴"
    else:
        level = "🔴 昂貴"
    return {"便宜價": round(cheap, 2), "合理價": round(fair, 2), "昂貴價": round(exp, 2),
            "安全邊際%": round(mos, 2), "股價位階": level,
            "合理殖利率門檻%": round(fair_y * 100, 2), "估值股利": round(div, 4)}


def payout_ratio(dps, eps_ttm, eps_annual):
    """發放率優先用 TTM EPS；TTM 缺值才退回年度 EPS（避免用去年高峰 EPS 低估配息壓力）。"""
    base = eps_ttm if eps_ttm and eps_ttm > 0 else eps_annual
    if not dps or not base or base <= 0:
        return None
    return round(dps / base * 100, 2)


# ───────────────────────────────────────────
# 同業相對估值（單一快照就能算，不需歷史）
# ───────────────────────────────────────────
def industry_valuation(df: pd.DataFrame, min_peers: int = 5) -> pd.DataFrame:
    """在同產業內比較 PE / PB / 殖利率，避免跨產業比 PER 的謬誤。

    位階 0–100：100 = 同業最便宜（殖利率最高、PE/PB 最低）。
    同業家數 < min_peers → 不給位階（樣本不足）。
    """
    if "產業" not in df.columns or df.empty:
        return df
    n = df.groupby("產業")["代號"].transform("count")
    parts = {}
    for col in ("殖利率", "PE", "PB"):
        s = pd.to_numeric(df[col], errors="coerce")
        higher_is_better = col == "殖利率"
        parts[f"{col}_同業中位數"] = df.assign(_s=s).groupby("產業")["_s"].transform("median").round(2)
        parts[f"{col}_同業位階"] = df.assign(_s=s).groupby("產業")["_s"].rank(
            pct=True, ascending=higher_is_better).mul(100).round(0)
    out = pd.DataFrame(parts, index=df.index)
    out["同業估值分"] = out[["殖利率_同業位階", "PE_同業位階", "PB_同業位階"]].mean(axis=1).round(1)
    out.loc[n < min_peers, "同業估值分"] = np.nan
    cond = [out["同業估值分"] >= 75, out["同業估值分"] >= 50,
            out["同業估值分"] >= 25, out["同業估值分"] < 25]
    out["同業估值位階"] = np.select(cond, ["💚 同業便宜", "💛 同業合理",
                                       "🟠 同業偏貴", "🔴 同業昂貴"], default="—")
    out.loc[n < min_peers, "同業估值位階"] = "—"
    return pd.concat([df, out], axis=1)


# ───────────────────────────────────────────
# 綜合評分（rank-based，解決量綱與 PB 代理問題）
# ───────────────────────────────────────────
WEIGHTS = {
    "安全邊際%": (0.10, True), "殖利率": (0.09, True), "PE": (0.06, False), "PB": (0.04, False),
    "同業估值分": (0.06, True),
    "ROE%": (0.10, True), "負債比%": (0.07, False), "毛利率%": (0.06, True),
    "業外依賴度": (0.05, False), "連續配發現金股利年數": (0.04, True),
    "股利穩定距離": (0.03, False),
    "外資持股5日變化(pp)": (0.06, True), "法人買超20日/日均量": (0.05, True),
    "外資持股%": (0.04, True),
    "RS20": (0.07, True), "距60日高點%": (0.05, True), "量比": (0.03, True),
}


def _rank_pct(s: pd.Series, higher_is_better: bool) -> pd.Series:
    """回傳 0–100；NaN 保持 NaN。ascending=True 讓最大值拿 100。"""
    return s.rank(pct=True, ascending=higher_is_better) * 100


def composite_score(df: pd.DataFrame) -> pd.DataFrame:
    comp = pd.DataFrame(index=df.index)
    for col, (w, higher) in WEIGHTS.items():
        if col == "股利穩定距離":
            if "股利穩定性倍數" not in df.columns:
                comp[col] = np.nan
                continue
            s = (pd.to_numeric(df["股利穩定性倍數"], errors="coerce") - 1).abs()
        elif col == "法人買超20日/日均量":
            if "三大法人買超20日(張)" not in df.columns or "日均量(張)" not in df.columns:
                comp[col] = np.nan
                continue
            vol = pd.to_numeric(df["日均量(張)"], errors="coerce").replace(0, np.nan)
            s = pd.to_numeric(df["三大法人買超20日(張)"], errors="coerce") / vol
        elif col not in df.columns:
            comp[col] = np.nan
            continue
        else:
            s = pd.to_numeric(df[col], errors="coerce")
        comp[col] = _rank_pct(s, higher)

    wmat = pd.DataFrame({c: np.where(comp[c].notna(), WEIGHTS[c][0], 0.0) for c in comp},
                        index=df.index)
    wsum = wmat.sum(axis=1)
    total = sum(w for w, _ in WEIGHTS.values())
    df["綜合分數"] = ((comp.fillna(0) * wmat).sum(axis=1) / wsum.replace(0, np.nan)).round(1)
    df["評分覆蓋%"] = (wsum / total * 100).round(0)
    df.loc[df["評分覆蓋%"] < 50, "綜合分數"] = np.nan

    df["估值分"] = comp[["安全邊際%", "殖利率", "PE", "PB"]].mean(axis=1).round(1)
    df["品質分"] = comp[["ROE%", "負債比%", "毛利率%", "業外依賴度",
                        "連續配發現金股利年數", "股利穩定距離"]].mean(axis=1).round(1)
    df["籌碼分"] = comp[["外資持股5日變化(pp)", "法人買超20日/日均量", "外資持股%"]].mean(axis=1).round(1)
    df["動能分"] = comp[["RS20", "距60日高點%", "量比"]].mean(axis=1).round(1)
    return df


def apply_risk_penalty(df: pd.DataFrame) -> pd.DataFrame:
    """依風險標記扣分；在 risk_flags 之後呼叫（composite_score 本身不認識標記）。"""
    if "風險標記" not in df.columns or "綜合分數" not in df.columns:
        return df
    flags = df["風險標記"].fillna("")
    penalty = (flags.str.contains("股利衰退") * 8 +
               flags.str.contains("🔴 獲利衰退") * 10 +
               flags.str.contains("🟡 獲利轉弱") * 4 +
               flags.str.contains("負債偏高") * 6 +
               flags.str.contains("業外依賴") * 5 +
               flags.str.contains("高峰股利") * 5 +
               flags.str.contains("建商") * 8 +
               flags.str.contains("景氣循環") * 5 +
               flags.str.contains("ROE偏低") * 4)
    df["風險扣分"] = penalty
    df["綜合分數"] = (df["綜合分數"] - penalty).clip(lower=0).round(1)
    return df


def risk_flags(row: pd.Series, penalty_codes: set[str]) -> str:
    f = []
    code, name = str(row.get("代號", "")), str(row.get("Name", ""))
    if code.endswith("KY") or name.endswith("-KY"):
        f.append("KY股")
    if code in penalty_codes:
        f.append("違規裁處")
    vol = _f(row.get("日均量(張)"))
    if vol is not None and vol < MIN_AVG_VOLUME:
        f.append("流動性不足")
    debt = _f(row.get("負債比%"))
    if debt is not None and debt > DEBT_RATIO_MAX and row.get("產業") != "金融保險":
        f.append("負債偏高")
    payout = _f(row.get("盈餘發放率%"))
    if payout is not None and payout > 100:
        f.append("超額配息")
    surplus = _f(row.get("公積配息佔比%"))
    if surplus is not None and surplus > 50:
        f.append("公積配息為主")
    stab = _f(row.get("股利穩定性倍數"))
    if stab is not None:
        if stab >= 1.5:
            f.append("高峰股利")
        elif stab < 0.8:
            f.append("股利衰退")
    if (_f(row.get("股利含配股筆數")) or 0) > 0:
        f.append("股利含配股(金額偏高)")   # TWT49U 權息合計無法拆現金
    extra = _f(row.get("業外依賴度"))
    if extra is not None and extra > 3:
        f.append("業外依賴")
    g1, g3 = _f(row.get("EPS成長_最新%")), _f(row.get("EPS成長_3年CAGR%"))
    if g1 is not None and g1 > 150:
        f.append("極端成長(基期效應)")
    if g1 is not None and g3 is not None:
        if g1 < 0 and g3 < 0:
            f.append("🔴 獲利衰退")
        elif g1 < 0 or g3 < 0:
            f.append("🟡 獲利轉弱")
    roe = _f(row.get("ROE%"))
    if roe is not None and roe < ROE_MIN:
        f.append("ROE偏低")
    industry = str(row.get("產業") or "")
    if industry == "建材營造":
        f.append("建商(完工入帳)")
    elif industry in {"航運業", "鋼鐵工業", "塑膠工業", "造紙工業", "汽車工業", "觀光餐旅"}:
        f.append("景氣循環")
    if _f(row.get("股利資料年數")) is not None and _f(row.get("股利資料年數")) < 5:
        f.append("股利資料<5年")
    return "、".join(f) if f else "✅ 無"


def data_status(row: pd.Series, source_status: dict) -> str:
    div = row.get("股利資料狀態")
    div_txt = {"ok": "股利✅", "twse_fallback": "股利⚠️近2年"}.get(div, "股利❌")
    q = "品質✅" if source_status.get("income") == "ok" else "品質⚠️"
    ch = "籌碼✅" if row.get("籌碼資料狀態") == "✅" else "籌碼—"
    tech = "技術✅" if row.get("技術資料狀態") == "✅" else "技術—"
    return "｜".join([div_txt, q, ch, tech])
