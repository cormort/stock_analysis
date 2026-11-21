# 台股存股選股器 v6

全市場（上市）存股篩選 + 估值 + 品質 + 籌碼 + 技術面的靜態報告工具。
跑一次產生 JSON 快照，用本機網頁檢視、排序、追蹤歷史變化。

## 快速開始

```bash
python3 -m pip install --user -r requirements.txt
python3 stock_screener.py          # 第一次約 3-5 分鐘（會快取）
open http://localhost:8000/index.html
python3 server.py                  # 或雙擊 啟動.command
```

常用參數：

```bash
python3 stock_screener.py --offline          # 只用快取（不發網路）
python3 stock_screener.py --no-yahoo          # 只跑 TWSE（最快）
python3 stock_screener.py --no-chips          # 不抓籌碼
python3 stock_screener.py --days 120          # 技術面回看 120 個交易日
python3 stock_screener.py --risk-premium 0.03 # 目標殖利率改 10Y+3%
```

若 Yahoo 對本機 IP 限流（429），可先匯入舊版 `dividend_cache_v8.json` 當種子（年份以 7/1 估計除息日），
並跳過 Yahoo 抓取：

```bash
python3 stock_screener.py --seed ~/.hermes/profiles/bot2/workspace/cache/dividend_cache_v8.json --no-yahoo
```

其餘沒有種子的股票會退回 TWSE 近 2 年股利，並在「資料狀態」標記 `股利⚠️近2年`。

## 資料源與口徑（先看這段）

| 資料 | 來源 | 口徑 / 限制 |
|---|---|---|
| PE / PB / 官方殖利率 | TWSE `BWIBBU_ALL` | PE 為 TTM；官方殖利率的基準年度可能與今年宣告不同，**只用來對帳** |
| 收盤價 | TWSE `STOCK_DAY_AVG_ALL` | 最近交易日 |
| 股利（20+ 年） | Yahoo `chart?events=div` | 以**除息日**為準；`dps` = 近 365 天實際除息總和（年配若時序漂移，取 500 天內最近一次） |
| 5 年平均股利 | 同上 | 最近 5 個**完整年度**（不足 3 年覆蓋 → 空白，不假裝穩定） |
| 年度 EPS / 成長率 | Yahoo `annualBasicEPS` | 免費層僅約 4 年；`連續獲利年數` 是**下限**，不是完整歷史 |
| 品質（負債比/三率/淨值） | TWSE `t187ap07_L_ci`、`t187ap06_L_ci` | 單季累計快照，不是歷史趨勢 |
| 月營收 | TWSE `t187ap05_L` | 當月 YoY / MoM |
| 三大法人 | TWSE `T86` | 股 → 張；含 ETF 對沖，不等於方向性 |
| 融資券 | TWSE `MI_MARGN` | 最新交易日全市場（OpenAPI 快照） |
| 外資持股 | TWSE `MI_QFIIS`（`ALLBUT0999`） | 持股比率；5 日變化需逐日累積 |
| 日行情 OHLCV | TWSE `MI_INDEX` | 逐日快取；含成交金額，供量比/ATR/RS 使用 |
| 10Y 美債 | Yahoo `^TNX` | 失敗時沿用快取或 4.5% 備援，並在 meta 標記 |

**所有抓取失敗都留 `null` 並在「資料狀態」標記，不會用 0 冒充。** 若 Yahoo 429，會中止該輪並保留已抓資料，下次只補缺的。

## 評分方法

`綜合分數` = 估值 35% + 品質 35% + 籌碼 15% + 動能 15%，每個因子先做**橫斷面 rank 百分位**再加權（不是把不同量綱的數字相加）。

- 估值：安全邊際、殖利率、PE（低）、PB（低）
- 品質：ROE、負債比（低）、毛利率、業外依賴（低）、連續配息、股利穩定
- 籌碼：外資持股 5 日變化、法人 20 日買超 / 日均量、外資持股率
- 動能：RS20（相對大盤）、距 60 日高點、量比

風險標記會直接從分數扣分（股利衰退 −8、獲利衰退 −10、建商 −8、負債偏高 −6、業外依賴 −5…），
所以「便宜但有問題」不會排在清單最前面；扣分後仍保留在清單並顯示原因，不靜默消失。

估值門檻連動利率：`合理殖利率 = 10Y + 2%`（可用 `--risk-premium` 改），便宜 / 昂貴 = ×1.25 / ×0.8。
估價用 **5 年平均股利**，避開單一年度的高峰股利陷阱。

## 篩選清單

- 💎 價值精選：PE ≤ 15、PB ≤ 2、殖利率 ≥ 4%、安全邊際 ≥ 0、連續配息 ≥ 5 年、發放率 30–110%，排除股利衰退／明確獲利衰退，同產業最多 5 檔
- 📈 廣泛價值：溢價回收 ≤ 15 年、殖利率 ≥ 3%、獲利率 ≥ 3%、發放率 30–110%
- 🛡️ 防禦存股：連續配息 ≥ 10 年、發放率 40–90%、殖利率 ≥ 4%、ROE ≥ 6%
- 🎯 核心 / 🛰️ 衛星：前端由價值精選衍生（連續配息 ≥10、ROE ≥8、溢價 ≤10、殖利率 ≥4.5）

KY 股、違規裁處、日均量 <500 張會直接排除；其餘風險只標記，不靜默消失。

## 檔案

```
stock_screener.py   主流程（抓取 → 合併 → 篩選 → 快照）
stock_sources.py    TWSE / Yahoo 資料源與逐日快取
stock_metrics.py    純計算（股利 streak、估值、品質、技術、rank 評分）
server.py           靜態檔 + /api/quote（上市即時報價代理）
index.html          報告檢視（響應式；窄視窗個股追蹤改上下排列）
tests/              pytest（不打網路）
finmind_source.py   v5 遺留，v6 未使用（FinMind 免費額度易耗盡）
```

## 已知限制

- 只抓上市（MI_INDEX / BWIBBU）；上櫃（.TWO）不在全市場清單，即時報價也不支援。
- Yahoo 年度 EPS 僅約 4 年，`連續獲利年數` 不能當 10 年證據；要用完整歷史需接 FinMind（付費/有額度）。
- 品質指標是**單季**快照，沒有 5 年趨勢；毛利率趨勢需另外累積。
- 建商（完工入帳）、壽險、金控的估值與負債比需要產業別例外，目前只排除金融保險的負債比。
- 首次執行需抓 80 個交易日 × 3 個籌碼端點，耗時數分鐘；之後走快取。
- Yahoo 對同一 IP 短時間大量請求會回 429；程式會中止該輪、保留已抓資料，1 小時後重試。

## 測試

```bash
python3 -m pytest -q
```
