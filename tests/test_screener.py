"""選股器純函式測試：TWSE 股利 fallback 只看完整年度、同業估值位階。"""
import pandas as pd

import stock_screener as S
import stock_metrics as M


def _div_df():
    return pd.DataFrame([
        # 2317：完整年度 → 可用
        {"代號": "2317", "股利年度": "114", "股利所屬期間": "1140101~1141231",
         "股東配發-盈餘分配之現金股利(元/股)": "7.2"},
        # 2330：只有單季 → 不可當年度股利
        {"代號": "2330", "股利年度": "115", "股利所屬期間": "1150701~1150930",
         "股東配發-盈餘分配之現金股利(元/股)": "9.0"},
        # 今年度的完整宣告尚未完成 → 排除
        {"代號": "1101", "股利年度": "115", "股利所屬期間": "1150101~1151231",
         "股東配發-盈餘分配之現金股利(元/股)": "1.0"},
    ])


def test_twse_fallback_only_uses_complete_annual_year():
    ev = S.twse_dividend_events(_div_df())
    assert list(ev) == ["2317"]
    assert ev["2317"][0]["amount"] == 7.2
    assert ev["2317"][0]["date"].endswith("12-31")


def test_industry_valuation_ranks_within_peers():
    rows = []
    for i in range(5):
        rows.append({"代號": f"10{i}", "產業": "食品", "殖利率": 3 + i,
                     "PE": 20 - i * 2, "PB": 2.0 - i * 0.2})
    # 只有 2 檔的產業 → 樣本不足
    rows += [{"代號": "200", "產業": "小業", "殖利率": 9, "PE": 5, "PB": 0.5},
             {"代號": "201", "產業": "小業", "殖利率": 1, "PE": 30, "PB": 3.0}]
    out = M.industry_valuation(pd.DataFrame(rows))
    got = out.set_index("代號")
    assert got.loc["104", "同業估值位階"].startswith("💚")   # 殖利率最高、PE/PB 最低
    assert got.loc["100", "同業估值位階"].startswith("🔴")
    assert pd.isna(got.loc["200", "同業估值分"])              # <5 檔不給分
    assert got.loc["200", "同業估值位階"] == "—"


def test_industry_valuation_no_industry_column_is_noop():
    df = pd.DataFrame({"代號": ["2317"], "PE": [12.0]})
    out = M.industry_valuation(df)
    assert "同業估值分" not in out.columns
