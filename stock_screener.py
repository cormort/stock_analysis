#!/usr/bin/env python3
"""台股存股選股器 v6.0
=====================
v6 相對 v5 的修正（依據 2026-11 的 code review）：
  1. 股利口徑單一化：近 12 個月實際除息 DPS（Yahoo 20+ 年歷史），5 年平均、穩定性倍數、
     累積/連續配息年數都改用同一來源；TWSE 2 年資料僅作 fallback 並標記。
  2. 0 / 缺值分離：抓不到就 null，不再用 0 冒充「沒有配息／沒有獲利」。
  3. 綜合評分改 rank-based 多因子（估值/品質/籌碼/動能），取代 ≈PB 倒數的 CP 值。
  4. 目標殖利率連動 10 年期美債（10Y + 3% 風險溢酬），不再寫死 6.25/5/4。
  5. 新增品質（負債比/ROE/毛利率/營益率/業外依賴/PEG）、籌碼（外資持股/法人/融資）、
     月營收、技術（量比/ATR/RS/二日法則）模組。
  6. MI_INDEX 快取改存 OHLC + 成交金額（原本丟掉 high/low，等於浪費免費資料）。

用法：
  python3 stock_screener.py                 # 完整執行（第一次約 3-5 分鐘）
  python3 stock_screener.py --days 60       # 指定技術面回看交易日
  python3 stock_screener.py --no-yahoo --no-chips   # 只跑 TWSE（快速檢查）
  python3 stock_screener.py --offline       # 只用快取，不發網路請求
"""
from __future__ import annotations

import argparse
import os
import time
from datetime import date, datetime

import numpy as np
import pandas as pd

import stock_metrics as M
import stock_sources as S

SCREENER_VERSION = "6.0"
DATA_DIR = "data"
MANIFEST = "manifest.json"

INDUSTRY = {
    "01": "水泥工業", "02": "食品工業", "03": "塑膠工業", "04": "紡織纖維",
    "05": "電機機械", "06": "電器電纜", "08": "玻璃陶瓷", "09": "造紙工業",
    "10": "鋼鐵工業", "11": "橡膠工業", "12": "汽車工業", "14": "建材營造",
    "15": "航運業", "16": "觀光餐旅", "17": "金融保險", "18": "貿易百貨",
    "20": "其他", "21": "化學工業", "22": "生技醫療", "23": "油電燃氣",
    "24": "半導體業", "25": "電腦及週邊設備業", "26": "光電業",
    "27": "通信網路業", "28": "電子零組件業", "29": "電子通路業",
    "30": "資訊服務業", "31": "其他電子業", "32": "文化創意業",
    "33": "農業科技業", "34": "電子商務", "35": "綠能環保",
    "36": "數位雲端", "37": "運動休閒", "38": "居家生活",
    "80": "管理股票", "91": "存託憑證",
}


def twse_dividend_events(div_df: pd.DataFrame) -> dict[str, list[dict]]:
    """TWSE t187ap45_L → {code: [{date: 年度末, amount: 現金股利總和}]}。

    僅 2 個年度、且是「股利所屬年度」；只當 Yahoo 失敗時的 fallback，
    並在資料狀態標記，不可與 20 年歷史混為一談。
    """
    cols = []
    for key in ("盈餘分配之現金股利", "法定盈餘公積發放之現金", "資本公積發放之現金"):
        c = next((x for x in div_df.columns if key in x and "配股" not in x), None)
        if c:
            cols.append(c)
    if not cols:
        return {}
    ycol = next((c for c in div_df.columns if "股利年度" in c), None)
    if not ycol:
        return {}
    d = div_df.copy()
    d["_year"] = pd.to_numeric(d[ycol], errors="coerce")
    for c in cols:
        d[c] = pd.to_numeric(d[c].replace("", np.nan), errors="coerce").fillna(0)
    d["_cash"] = d[cols].sum(axis=1)
    d = d.dropna(subset=["_year"])
    d = d[d["_year"] <= date.today().year - 1911]
    out = {}
    for (code, roc_year), g in d.groupby(["代號", "_year"]):
        amount = round(float(g["_cash"].sum()), 4)
        if amount > 0:
            out.setdefault(str(code), []).append(
                {"date": f"{int(roc_year) + 1911}-12-31", "amount": amount})
    return out


# ───────────────────────────────────────────
# 主表
# ───────────────────────────────────────────
def build_master(frames, status, us10y, market_hist, taiex, chips,
                 yahoo, source_status, premium=None) -> pd.DataFrame:
    met = frames["metrics"].copy()
    met["PE"] = pd.to_numeric(met["PEratio"].replace("-", np.nan), errors="coerce")
    met["PB"] = pd.to_numeric(met["PBratio"].replace("-", np.nan), errors="coerce")
    met["殖利率_官方"] = pd.to_numeric(met["DividendYield"].replace("-", np.nan), errors="coerce")
    met["資料日期"] = met["Date"].astype(str)

    pri = frames["prices"][["代號", "ClosingPrice"]].copy()
    pri["收盤價"] = pd.to_numeric(pri["ClosingPrice"].replace("-", np.nan), errors="coerce")
    pri = pri.drop_duplicates("代號")[["代號", "收盤價"]]

    pro = frames["profit"].copy()
    net_col = next((c for c in pro.columns if "純益率" in c), None)
    pro["獲利率"] = (pd.to_numeric(pro[net_col], errors="coerce")
                  if net_col else np.nan)
    pro = pro.drop_duplicates("代號")[["代號", "獲利率"]]

    basic = frames["basic"][["代號", "產業別"]].copy()
    basic["產業"] = (basic["產業別"].astype(str).str.strip().str.zfill(2)
                  .map(INDUSTRY).fillna("其他"))
    basic = basic.drop_duplicates("代號")[["代號", "產業"]]

    bal = frames["balance"].copy()
    for c in ("資產總計", "負債總計", "權益總計", "股本", "每股參考淨值"):
        bal[c] = pd.to_numeric(bal[c].replace("", np.nan), errors="coerce")
    bal["負債比%"] = np.where(bal["資產總計"] > 0,
                            (bal["負債總計"] / bal["資產總計"] * 100).round(2), np.nan)
    bal = bal.drop_duplicates("代號")[["代號", "負債比%", "權益總計", "每股參考淨值"]]

    inc = frames["income"].copy()
    for c in ("營業收入", "營業毛利（毛損）淨額", "營業利益（損失）",
              "基本每股盈餘（元）", "季別"):
        inc[c] = pd.to_numeric(inc[c].replace("", np.nan), errors="coerce")
    inc = inc.drop_duplicates("代號")
    q = inc.apply(lambda r: M.quality_metrics(r), axis=1, result_type="expand")
    inc = pd.concat([inc[["代號"]].reset_index(drop=True), q.reset_index(drop=True)], axis=1)

    rev = frames["revenue"].copy()
    rev["月營收YoY%"] = pd.to_numeric(rev["營業收入-去年同月增減(%)"], errors="coerce").round(2)
    rev["月營收MoM%"] = pd.to_numeric(rev["營業收入-上月比較增減(%)"], errors="coerce").round(2)
    rev = rev.rename(columns={"資料年月": "營收年月"})
    rev = rev.drop_duplicates("代號")[["代號", "營收年月", "月營收YoY%", "月營收MoM%"]]

    # 公積配息佔比（同一股利年度可能多列 → 先彙總再取最新年度）
    ds = frames["dividend"].copy()
    cols = {}
    for key, label in [("盈餘分配之現金股利", "配息_盈餘"),
                       ("法定盈餘公積發放之現金", "配息_法積"),
                       ("資本公積發放之現金", "配息_公積")]:
        c = next((x for x in ds.columns if key in x and "配股" not in x), None)
        if c:
            cols[label] = c
    if cols:
        for label, c in cols.items():
            ds[label] = pd.to_numeric(ds[c].replace("", np.nan), errors="coerce")
        ycol = next((c for c in ds.columns if "股利年度" in c), None)
        ds["股利年度"] = pd.to_numeric(ds[ycol], errors="coerce")
        grp = ds.groupby(["代號", "股利年度"], as_index=False).agg(
            {**{k: "sum" for k in cols}, "期別": "count"})
        grp = grp.sort_values("股利年度").groupby("代號", as_index=False).last()
        grp["股利合計_官方"] = grp[list(cols)].sum(axis=1)
        grp["公積配息佔比%"] = np.where(
            grp["股利合計_官方"] > 0,
            ((grp.get("配息_法積", 0) + grp.get("配息_公積", 0))
             / grp["股利合計_官方"] * 100).round(2), np.nan)
        ds = grp[["代號", "公積配息佔比%", "股利合計_官方"]]
    else:
        ds = pd.DataFrame(columns=["代號", "公積配息佔比%", "股利合計_官方"])

    df = met[["代號", "Name", "PE", "PB", "殖利率_官方", "資料日期"]].copy()
    for extra in (pri, pro, basic, bal, inc, rev, ds):
        if extra is not None and len(extra):
            df = df.merge(extra, on="代號", how="left")
    df = df[df["收盤價"].notna()].reset_index(drop=True)

    penalty = set(frames["penalty"]["代號"].astype(str)) if frames.get("penalty") is not None else set()
    tx_closes = list(taiex.values()) if taiex else None
    twse_events = twse_dividend_events(frames["dividend"])

    # ── 逐檔：股利 / EPS / 技術 / 籌碼 ──
    recs = []
    for _, r in df.iterrows():
        code = str(r["代號"])
        y = yahoo.get(code) or {}
        divs = y.get("divs") or []
        dstatus = y.get("div_status", "error")
        if not divs and twse_events.get(code):
            divs, dstatus = twse_events[code], "twse_fallback"
        dstats = M.dividend_stats(divs, date.today())
        estats = M.eps_stats(y.get("eps") or {})
        tech = M.technical_stats(market_hist.get(code, []), tx_closes)
        chip = S.chip_summary(chips, code) if chips else {}
        recs.append({**dstats, **estats, **tech, **chip,
                     "股利資料狀態": dstatus,
                     "獲利資料狀態": y.get("eps_status", "error"),
                     "歷史交易日數": len(market_hist.get(code, []))})

    df = pd.concat([df, pd.DataFrame(recs)], axis=1)
    df = df.rename(columns={
        "cum_years": "累積配息年數", "consec_years": "連續配發現金股利年數",
        "stability": "股利穩定性倍數", "data_years": "股利資料年數",
        "span": "股利歷史跨度", "eps": "年度EPS", "years": "獲利資料年數",
        "streak": "連續獲利年數", "g1": "EPS成長_最新%", "g3": "EPS成長_3年CAGR%",
    })
    # DataFrame 由 dict 組成，缺值會是 object → 統一轉數值（失敗即 NaN）
    text_cols = {"代號", "Name", "產業", "風險標記", "資料狀態", "股價位階",
                 "二日法則_20MA", "二日法則_60MA", "技術資料狀態", "籌碼資料狀態",
                 "股利資料狀態", "獲利資料狀態", "資料日期", "營收年月"}
    for c in df.columns:
        if c not in text_cols:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    # ── 基本衍生 ──
    price = df["收盤價"]
    df["EPS"] = np.where(df["PE"] > 0, (price / df["PE"]).round(2), np.nan)
    df["每股淨值"] = np.where(df["每股參考淨值"] > 0, df["每股參考淨值"],
                           np.where(df["PB"] > 0, price / df["PB"], np.nan))
    df["每股淨值"] = pd.to_numeric(df["每股淨值"], errors="coerce").round(2)
    df["ROE%"] = np.where((df["EPS"] > 0) & (df["每股淨值"] > 0),
                        (df["EPS"] / df["每股淨值"] * 100).round(2), np.nan)
    df["業外依賴度"] = (df["獲利率"] - df["營益率%"]).round(2)

    eps_base = df["年度EPS"].where(df["年度EPS"] > 0, df["EPS"])
    df["盈餘發放率%"] = np.where((df["dps"] > 0) & (eps_base > 0),
                            (df["dps"] / eps_base * 100).round(2), np.nan)
    df["PE_年報"] = np.where(df["年度EPS"] > 0, (price / df["年度EPS"]).round(2), np.nan)
    df["溢價回收年限"] = np.where((df["EPS"] > 0) & (price > 0),
                            ((price - df["每股淨值"]) / df["EPS"]).round(2), np.nan)

    for tag in ("A", "B"):
        g = df[f"EPS成長_{'最新%' if tag == 'A' else '3年CAGR%'}"]
        pe = df["PE_年報"]
        ok = g.between(5, 150) & (pe > 0)
        df[f"PEG_{tag}"] = np.where(ok, (pe / g).round(2), np.nan)

    val = df.apply(lambda r: M.valuation_metrics(
        r["dps"], r["five_avg"], r["收盤價"], us10y, premium), axis=1, result_type="expand")
    df = pd.concat([df, val], axis=1)

    df["殖利率"] = np.where(df["dps"] > 0,
                          (df["dps"] / price * 100).round(2), np.nan)
    df["殖利率差異(pp)"] = (df["殖利率"] - df["殖利率_官方"]).round(2)

    df = M.composite_score(df)
    df["風險標記"] = df.apply(lambda r: M.risk_flags(r, penalty), axis=1)
    df = M.apply_risk_penalty(df)
    df["排除"] = df["風險標記"].str.contains("KY股|違規裁處|流動性不足", na=False)
    df["資料狀態"] = df.apply(lambda r: M.data_status(r, source_status), axis=1)
    return df


# ───────────────────────────────────────────
# 篩選
# ───────────────────────────────────────────
def cap_industry(df: pd.DataFrame, n: int = 5) -> pd.DataFrame:
    if df.empty or "產業" not in df.columns:
        return df
    return df.sort_values("綜合分數", ascending=False).groupby("產業", group_keys=False).head(n)


def apply_filters(df: pd.DataFrame):
    c = M.VALUE_CRITERIA
    base = df[~df["排除"]].copy()
    healthy = ~base["風險標記"].str.contains("股利衰退|🔴 獲利衰退", na=False)
    value = base[
        healthy &
        (base["PE"] > 0) & (base["PB"] > 0) &
        (base["PE"] <= c["PE_上限"]) & (base["PB"] <= c["PB_上限"]) &
        (base["殖利率"] >= c["殖利率_下限"]) &
        (base["安全邊際%"] >= c["安全邊際_下限"]) &
        (base["連續配發現金股利年數"] >= c["連續配息年數_下限"]) &
        (base["盈餘發放率%"].between(c["盈餘發放率_下限"], c["盈餘發放率_上限"]))
    ].sort_values("綜合分數", ascending=False)
    value = cap_industry(value, 5)

    c1 = M.SHEET1_CRITERIA
    sheet1 = base[
        (base["溢價回收年限"] <= c1["溢價回收年限_上限"]) &
        (base["殖利率"] >= c1["殖利率_下限"]) &
        (base["獲利率"] >= c1["獲利率_下限"]) &
        (base["盈餘發放率%"].between(c1["盈餘發放率_下限"], c1["盈餘發放率_上限"]))
    ].sort_values("綜合分數", ascending=False)

    c2 = M.SHEET2_CRITERIA
    sheet2 = base[
        healthy &
        (base["連續配發現金股利年數"] >= c2["連續配息年數_下限"]) &
        (base["盈餘發放率%"].between(c2["盈餘發放率_下限"], c2["盈餘發放率_上限"])) &
        (base["殖利率"] >= c2["殖利率_下限"]) &
        (base["溢價回收年限"] <= c2["溢價回收年限_上限"]) &
        (base["ROE%"].fillna(0) >= c2["ROE_下限"])
    ].sort_values(["綜合分數", "連續配發現金股利年數"], ascending=False)
    return value, sheet1, sheet2


# ───────────────────────────────────────────
# 欄位說明
# ───────────────────────────────────────────
COLUMNS = [
    ("代號", "股票代碼", "—", "—", "識別用"),
    ("Name", "公司名稱", "—", "—", "識別用"),
    ("產業", "證交所產業別", "—", "—", "同產業最多取 5 檔，避免單一循環產業洗版"),
    ("收盤價", "最近交易日收盤價（元）", "—", "—", "估價基準；不是便宜與否的判斷"),
    ("PE", "本益比（TTM）", "市場評價高／成長股", "可能被低估或前景保守", "存股偏好 <15；PE=0 代表無獲利或無資料"),
    ("PB", "股價淨值比", "溢價高", "接近帳面價值", "PB<1.5 較安全；0 或缺值不列入價值篩選"),
    ("殖利率", "近 12 個月實際除息 DPS ÷ 收盤價", "現金回報高", "回報低或成長股", "與官方殖利率差 >2pp 會標記口徑差異"),
    ("殖利率_官方", "TWSE BWIBBU 公告殖利率（基準年度可能不同）", "—", "—", "只用來對帳，不參與估價"),
    ("EPS", "收盤價 ÷ PE（TTM）", "賺錢能力強", "獲利弱或虧損", "年度 EPS 另見「年度EPS」"),
    ("年度EPS", "Yahoo 年度基本每股盈餘（最近年度）", "—", "—", "用於 PEG 與發放率，避免 TTM 與股利年度錯位"),
    ("每股淨值", "TWSE 每股參考淨值（fallback：收盤÷PB）", "資產厚實", "資產單薄", "股價跌破淨值可能是機會或警訊"),
    ("ROE%", "EPS ÷ 每股淨值 ×100", "資本效率高", "效率差", "≥8% 為基本門檻；金融股另計"),
    ("毛利率%", "營業毛利 ÷ 營收（單季累計）", "產品附加價值高", "代工／低毛利", "與營益率一起看趨勢"),
    ("營益率%", "營業利益 ÷ 營收", "本業賺錢", "本業薄利", "淨利率高於營益率＝業外貢獻"),
    ("業外依賴度", "純益率 − 營益率（pp）", "業外佔比高", "本業為主", ">3pp 需查業外來源"),
    ("負債比%", "負債總計 ÷ 資產總計", "槓桿高", "財務保守", ">65% 標記（金融保險除外）"),
    ("月營收YoY%", "當月營收年增率", "需求強", "衰退", "比季報早 1-2 個月反映"),
    ("累積配息年數", "歷史上有配息的年度數（Yahoo 20+ 年）", "配息經驗多", "新上市或中斷", "與連續年數並看，差距大＝中斷過"),
    ("連續配發現金股利年數", "從最近完整年度往回，連續配息年數", "穩定性高", "曾中斷", "≥10 年為定存股黃金標準"),
    ("dps", "近 12 個月實際除息現金股利（元）", "—", "—", "估價與殖利率的唯一分子"),
    ("five_avg", "近 5 個完整年度平均股利（元）", "—", "—", "用於估價，避免單一年度高峰"),
    ("股利穩定性倍數", "DPS ÷ 5 年平均", "≥1.5 高峰股利", "<0.6 股利衰退", "0.6–1.5 才是可用估值基礎"),
    ("連續獲利年數", "年度 EPS > 0 的連續年數（Yahoo 僅 4 年）", "獲利穩定", "曾虧損", "資料年數見「獲利資料年數」，勿把 0 當虧損"),
    ("獲利資料年數", "Yahoo 年度 EPS 覆蓋年數", "—", "—", "小於 10 年時，連續獲利只能視為下限"),
    ("EPS成長_最新%", "最近年度 EPS YoY", "成長", "衰退", "PEG 只在 5–150% 區間有效"),
    ("EPS成長_3年CAGR%", "最近 3 年 EPS 年複合成長", "成長", "衰退", "≤0 代表 PEG 無意義"),
    ("PEG_A", "PE_年報 ÷ 最新年度 EPS 成長率", "—", "—", "<0.5 極便宜、0.5–1 安全、>1.5 貴；空白＝成長率不在 5–150%"),
    ("PEG_B", "PE_年報 ÷ 3 年 EPS CAGR", "—", "—", "與 PEG_A 並看，兩個都便宜才算通過"),
    ("溢價回收年限", "(股價 − 每股淨值) ÷ EPS", "溢價高、回本久", "折價或回本快", "負值代表股價低於淨值，不可用「<=上限」單獨判斷"),
    ("盈餘發放率%", "DPS ÷ 年度 EPS", "配得多（>100% 借錢配息）", "保留盈餘再投資", "理想 40–90%"),
    ("安全邊際%", "(合理價 − 現價) ÷ 合理價", "折價大", "溢價", "以 5 年平均股利估價；>20% 為優質"),
    ("合理殖利率門檻%", "10Y 美債殖利率 + 2% 風險溢酬", "—", "—", "利率上升會讓所有合理價下移；可用 --risk-premium 調整"),
    ("便宜價", "5 年平均股利 ÷ (門檻×1.25)", "—", "—", "現價 ≤ 便宜價＝深度折價"),
    ("合理價", "5 年平均股利 ÷ 門檻", "—", "—", "現價 ≤ 合理價＝可考慮"),
    ("昂貴價", "5 年平均股利 ÷ max(門檻×0.8, 2%)", "—", "—", "現價 > 昂貴價＝暫緩"),
    ("股價位階", "現價對應便宜/合理/昂貴", "—", "—", "用 5 年平均股利，不用單一年度"),
    ("綜合分數", "估值 35%＋品質 35%＋籌碼 15%＋動能 15%（rank 百分位）", "各方面都前段", "後段", "本次主要排序指標；覆蓋率 <50% 不給分"),
    ("估值分", "安全邊際/殖利率/PE/PB 的 rank 平均", "便宜", "貴", "0–100"),
    ("品質分", "ROE/負債比/毛利率/業外/配息穩定 rank 平均", "體質好", "體質差", "0–100"),
    ("籌碼分", "外資持股變化/法人買超/外資持股 rank 平均", "資金流入", "流出", "0–100"),
    ("動能分", "RS20/距高點/量比 rank 平均", "相對強", "相對弱", "0–100"),
    ("外資持股%", "全體外資及陸資持股比率", "—", "—", "客觀持股水位"),
    ("外資持股5日變化(pp)", "與 5 個交易日前相比", "加碼", "減碼", "比單日買賣超穩定"),
    ("三大法人買超5日(張)", "外資＋投信＋自營 5 日累計", "買方", "賣方", "含 ETF 對沖，不等於方向性"),
    ("三大法人買超20日(張)", "20 日累計", "持續買", "持續賣", "搭配股價位置看"),
    ("融資使用率%", "融資餘額 ÷ 融資限額", "散戶槓桿高", "低", ">20% 留意追繳壓力"),
    ("券資比%", "融券餘額 ÷ 融資餘額", "空方多", "空方少", "高券資比可能有軋空也可能真的弱"),
    ("量比", "今日量 ÷ 前 20 日均量", "帶量", "縮量", "<1 縮量、1–1.3 微增、1.3–1.8 明顯帶量、>1.8 大量"),
    ("ATR14", "14 日平均真實區間（元）", "波動大", "波動小", "停損距離＝ATR×1.5～2"),
    ("ATR%", "ATR14 ÷ 收盤價", "波動大", "波動小", "部位上限＝1% ÷ 停損幅度"),
    ("距60日高點%", "現價相對 60 日最高", "接近高點", "深度回檔", "−10% 內＝強勢；<−30% 需確認基本面"),
    ("RS20", "個股 20 日報酬 − 大盤 20 日報酬（pp）", "強於大盤", "弱於大盤", "橫斷面選股比絕對均線訊號有用"),
    ("均線帶寬%", "16/20/25/45/60MA 的最大展開幅度", "發散（趨勢）", "糾結（箱型）", "<1% 視為糾結，未帶量突破不進場"),
    ("二日法則_20MA", "20MA 穿越後次日確認結果", "—", "—", "真突破/假突破/真跌破/假跌破；空白＝近日無穿越"),
    ("風險標記", "負面條件彙總", "—", "—", "KY/裁處/流動性不足會直接排除；建商、景氣循環、業外依賴等會扣分"),
    ("風險扣分", "風險標記對綜合分數的扣分", "—", "—", "股利衰退 -8、獲利衰退 -10、建商 -8、負債偏高 -6…"),
    ("資料狀態", "各模組資料是否成功取得", "—", "—", "⚠️ 代表該模組缺資料，不是 0"),
]


def column_definitions():
    return [{"欄位名稱": n, "定義": d, "數值越大代表": hi, "數值越小代表": lo, "存股參考": ref}
            for n, d, hi, lo, ref in COLUMNS]


# ───────────────────────────────────────────
# 輸出
# ───────────────────────────────────────────
def df_to_records(df: pd.DataFrame) -> list[dict]:
    clean = df.replace({np.nan: None, pd.NaT: None})
    out = []
    for rec in clean.to_dict(orient="records"):
        row = {}
        for k, v in rec.items():
            if isinstance(v, np.integer):
                v = int(v)
            elif isinstance(v, np.floating):
                v = None if pd.isna(v) else float(v)
            elif isinstance(v, (pd.Timestamp, datetime)):
                v = str(v)
            row[k] = v
        out.append(row)
    return out


def export_snapshot(sheets: dict, meta: dict, data_dir=DATA_DIR) -> str:
    os.makedirs(data_dir, exist_ok=True)
    now = datetime.now()
    snap = {
        "generated_at": now.isoformat(timespec="seconds"),
        "generated_at_display": now.strftime("%Y-%m-%d %H:%M:%S"),
        "screener_version": SCREENER_VERSION,
        "twstock_enabled": False,
        "history_enriched": True,
        "meta": meta,
        "column_definitions": column_definitions(),
        "sheets": sheets,
    }
    fname = f"snapshot_{now.strftime('%Y%m%d_%H%M%S')}.json"
    path = os.path.join(data_dir, fname)
    S.save_json(path, snap)
    _update_manifest(data_dir, fname, snap)
    return path


def _update_manifest(data_dir, fname, snap):
    path = os.path.join(data_dir, MANIFEST)
    man = S.load_json(path, {"snapshots": []})
    entry = {"file": fname, "generated_at": snap["generated_at"],
             "generated_at_display": snap["generated_at_display"],
             "counts": {k: len(v["rows"]) for k, v in snap["sheets"].items()},
             "data_date": snap["meta"].get("data_date")}
    man["snapshots"] = [s for s in man["snapshots"] if s.get("file") != fname]
    man["snapshots"].insert(0, entry)
    S.save_json(path, man)


# ───────────────────────────────────────────
# 主程式
# ───────────────────────────────────────────
def run(args):
    t0 = time.perf_counter()
    print(f"🔍 台股存股選股器 v{SCREENER_VERSION}")

    frames, status = S.fetch_all_openapi()
    missing = [k for k, v in frames.items() if v is None]
    if missing:
        print(f"❌ 必要端點失敗：{', '.join(missing)}")
        return 1

    us10y, ten_status = (4.5, "offline") if args.offline else S.fetch_us10y()
    print(f"🏦 10Y 美債 {us10y}%（{ten_status}）→ 合理殖利率門檻 "
          f"{M.target_yield(us10y, args.risk_premium)[0] * 100:.2f}%")

    if args.offline:
        market_hist, taiex, ndays = {}, {}, 0
        print("  ⏭️  offline：跳過日行情")
    else:
        print("📅 抓取全市場日行情（OHLCV）…")
        market_hist, taiex, ndays = S.load_market_history(args.days)

    chips = {}
    if not args.offline and not args.no_chips:
        print("📊 抓取籌碼（T86／融資券／外資持股）…")
        try:
            chips = S.load_chip_history(21)
        except Exception as e:
            print(f"  ⚠️  籌碼模組降級：{e}")

    yahoo = {}
    if args.seed:
        yahoo = S.import_legacy_cache(args.seed)
    if not args.offline and not args.no_yahoo:
        print("🌐 抓取 Yahoo 股利（20+ 年）與年度 EPS…")
        # EPS 只抓成交金額前 500 檔（其餘留 null，不假裝 0）
        by_amount = sorted(
            ((str(c), (rows[-1][5] if rows and len(rows[-1]) >= 6 else 0))
             for c, rows in market_hist.items()),
            key=lambda x: -(x[1] or 0))[:500]
        eps_codes = {c for c, _ in by_amount}
        try:
            yahoo = S.build_yahoo_history([str(c) for c in frames["metrics"]["代號"]],
                                          eps_codes=eps_codes, workers=args.workers)
        except Exception as e:
            print(f"  ⚠️  Yahoo 模組降級：{e}")

    print("⚙️  合併與計算…")
    df = build_master(frames, status, us10y, market_hist, taiex, chips, yahoo, status,
                      args.risk_premium)

    value, sheet1, sheet2 = apply_filters(df)
    print(f"  💎 價值精選 {len(value)}｜📈 廣泛 {len(sheet1)}｜🛡️ 防禦 {len(sheet2)}"
          f"｜🌐 全市場 {len(df)}")

    data_date = str(frames["metrics"]["Date"].max())
    if len(data_date) == 7:
        data_date = f"{int(data_date[:3]) + 1911}-{data_date[3:5]}-{data_date[5:7]}"
    meta = {"data_date": data_date, "us10y": us10y,
            "target_yield_pct": round(M.target_yield(us10y, args.risk_premium)[0] * 100, 2),
            "risk_premium_pct": round(args.risk_premium * 100, 2),
            "history_days": ndays, "offline": args.offline,
            "source_status": status,
            "criteria": {"value": M.VALUE_CRITERIA, "sheet1": M.SHEET1_CRITERIA,
                         "sheet2": M.SHEET2_CRITERIA}}

    titles = {"value": "💎 價值投資精選", "sheet1": "📈 廣泛價值清單",
              "sheet2": "🛡️ 嚴選防禦存股", "all": "🌐 全市場"}
    drop_cols = {"權益總計", "每股參考淨值", "股利合計_官方", "資料日期",
                 "營收年月", "歷史交易日數", "PE_年報", "EPS_年化", "排除"}
    sheets = {}
    for key, sub in (("value", value), ("sheet1", sheet1), ("sheet2", sheet2),
                     ("all", df.sort_values("代號"))):
        sub = sub.drop(columns=[c for c in drop_cols if c in sub.columns])
        sheets[key] = {"title": titles[key],
                       "criteria": meta["criteria"].get(key, {}),
                       "rows": df_to_records(sub)}

    path = export_snapshot(sheets, meta)
    print(f"\n✅ 完成（{time.perf_counter() - t0:.1f}s）→ {path}")
    print(f"   🕒 資料基準日 {data_date}｜合理殖利率 {meta['target_yield_pct']}%")
    print("   🌐 python3 server.py 後開 http://localhost:8000/index.html")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=80, help="技術面回看交易日（預設 80）")
    ap.add_argument("--workers", type=int, default=6, help="Yahoo 抓取執行緒")
    ap.add_argument("--no-yahoo", action="store_true")
    ap.add_argument("--no-chips", action="store_true")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--seed", help="匯入舊版 dividend_cache_v8.json 作為股利種子")
    ap.add_argument("--risk-premium", type=float, default=M.RISK_PREMIUM,
                    help="目標殖利率的風險溢酬（預設 0.02 = 2%%）")
    args = ap.parse_args()
    raise SystemExit(run(args))


if __name__ == "__main__":
    main()
