"""純計算層測試：用固定資料驗證口徑，不打網路。"""
from datetime import date

import numpy as np
import pandas as pd

import stock_metrics as M


def test_dividend_stats_annual_fallback_within_500_days():
    ev = [{"date": "2023-07-01", "amount": 5},
          {"date": "2024-07-01", "amount": 5},
          {"date": "2025-07-01", "amount": 6}]
    s = M.dividend_stats(ev, date(2026, 3, 1))     # 243 天前 → 用最近一次
    assert s["dps"] == 6
    assert s["five_avg"] == round((5 + 5 + 6) / 5, 4)   # 5 年平均含 0 年度
    assert s["consec_years"] == 3
    assert s["cum_years"] == 3


def test_dividend_stats_quarterly_ttm():
    ev = [{"date": "2025-09-22", "amount": 5}, {"date": "2025-12-17", "amount": 5},
          {"date": "2026-03-23", "amount": 6}, {"date": "2026-06-17", "amount": 6},
          {"date": "2026-09-22", "amount": 7}]
    s = M.dividend_stats(ev, date(2026, 11, 21))
    assert s["dps"] == 24          # 近 365 天：5+6+6+7
    assert s["consec_years"] == 1  # 只有 2025 是完整年度


def test_dividend_stats_stopped_payer_streak_is_zero():
    ev = [{"date": f"{y}-07-01", "amount": 1} for y in range(2010, 2024)]  # 2024、2025 未配
    s = M.dividend_stats(ev, date(2026, 11, 21))
    assert s["consec_years"] == 0
    assert s["cum_years"] == 14


def test_dividend_stats_suspended_is_zero_not_missing():
    s = M.dividend_stats([{"date": "2023-07-01", "amount": 3}], date(2026, 11, 21))
    assert s["dps"] == 0
    assert s["stability"] is None


def test_streak_breaks_on_gap():
    assert M.streak_years([2025, 2024, 2023, 2020]) == 3
    assert M.streak_years([]) == 0


def test_eps_stats_growth_and_streak():
    s = M.eps_stats({2022: 10.21, 2023: 10.25, 2024: 11.01, 2025: 13.61})
    assert s["streak"] == 4
    assert s["g1"] == 23.6
    assert s["g3"] == 10.1


def test_eps_stats_negative_latest_breaks_streak():
    s = M.eps_stats({2023: 2.0, 2024: 1.0, 2025: -0.5})
    assert s["streak"] == 0
    assert s["g1"] is None


def test_target_yield_follows_10y():
    fair, cheap, exp = M.target_yield(4.5)
    assert fair == 0.065
    assert cheap == round(0.065 * 1.25, 4)
    assert exp == 0.052


def test_valuation_uses_5y_average_and_flags_level():
    fair, cheap_y, _ = M.target_yield(4.5)
    v = M.valuation_metrics(dps=7.2, five_avg=5.5, price=50, us10y=4.5)
    assert v["便宜價"] == round(5.5 / cheap_y, 2)
    assert v["股價位階"].startswith("💚")
    assert v["安全邊際%"] > 0


def test_valuation_missing_dividend_is_dash_not_expensive():
    v = M.valuation_metrics(None, None, 100, 4.5)
    assert v["股價位階"] == "—"
    assert v["安全邊際%"] is None


def test_rank_score_higher_is_better():
    df = pd.DataFrame({
        "安全邊際%": [30, 10, -10], "殖利率": [8, 6, 4], "PE": [8, 12, 20],
        "PB": [0.8, 1.2, 2.0], "ROE%": [20, 12, 4], "負債比%": [20, 40, 70],
        "毛利率%": [40, 20, 5], "業外依賴度": [0, 2, 8],
        "連續配發現金股利年數": [20, 10, 1], "股利穩定性倍數": [1.0, 1.1, 0.5],
        "外資持股5日變化(pp)": [1.0, 0, -1.0], "三大法人買超20日(張)": [5000, 0, -5000],
        "日均量(張)": [1000, 1000, 1000], "外資持股%": [40, 20, 5],
        "RS20": [10, 0, -10], "距60日高點%": [-2, -10, -30], "量比": [1.5, 1.0, 0.6],
        "同業估值分": [90, 50, 10],
        })
    out = M.composite_score(df.copy())
    assert out.loc[0, "綜合分數"] > out.loc[1, "綜合分數"] > out.loc[2, "綜合分數"]
    assert out.loc[0, "評分覆蓋%"] == 100


def test_composite_score_handles_missing_modules():
    df = pd.DataFrame({"安全邊際%": [10, 20], "殖利率": [5, 6], "PE": [10, 12],
                       "PB": [1.0, 1.2]})
    out = M.composite_score(df.copy())
    assert out["綜合分數"].isna().all()          # 覆蓋率不足 → 不給分


def test_two_day_rule_true_and_false_breakout():
    base = [[1000, 100, 101, 99, 100, 1e6] for _ in range(30)]
    rows = base + [
        [1000, 100, 100.5, 99.0, 99.5, 1e6],    # 收盤跌破 20MA（約 99.98）
        [1000, 99.5, 102.5, 99.4, 102, 1e6],    # 突破日，高 102.5
        [1000, 102, 103.5, 101.8, 103, 1e6],    # 次日再過高 → 真突破
    ]
    assert M.two_day_rule(rows, 20) == "真突破"

    rows[-1] = [1000, 102, 102.4, 101.0, 102.1, 1e6]   # 次日未過高 → 假突破
    assert M.two_day_rule(rows, 20) == "假突破"


def test_volume_ratio_uses_previous_20_days():
    rows = [[1000, 1, 1, 1, 1, 0] for _ in range(21)]
    rows.append([2000, 1, 1, 1, 1, 0])
    assert M.volume_ratio(rows, 20) == 2.0


def test_quality_metrics_annualizes_eps():
    row = pd.Series({"營業收入": 1000, "營業毛利（毛損）淨額": 300,
                     "營業利益（損失）": 100, "基本每股盈餘（元）": 2.5, "季別": 2})
    q = M.quality_metrics(row)
    assert q["毛利率%"] == 30.0
    assert q["營益率%"] == 10.0
    assert q["EPS_年化"] == 5.0


def test_parse_twt49u_cash_mixed_and_stock_only():
    import stock_sources as S
    old = {"fields": ["資料日期", "股票代號", "股票名稱", "權值", "息值", "權值+息值", "權/息"],
           "data": [["94年09月02日", "2317", "鴻海", 29.52, 2.447142, "31.967142", "權息"]]}
    assert S.parse_twt49u(old) == [["2317", "2005-09-02", 2.4471, False]]
    new = {"fields": ["資料日期", "股票代號", "股票名稱", "權值+息值", "權/息"],
           "data": [["114年07月02日", "2317", "鴻海", "5.799983", "息"],
                    ["114年06月04日", "1605", "華新", "0.536107", "權息"],
                    ["114年01月11日", "6280", "崇貿", "5.52", "權"]]}
    assert S.parse_twt49u(new) == [["2317", "2025-07-02", 5.8, False],
                                   ["1605", "2025-06-04", 0.5361, True]]
    assert S.parse_twt49u({}) == []
