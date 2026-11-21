#!/bin/bash
# 台股存股選股器 — 一鍵啟動
# 雙擊此檔即可：可選擇先抓最新資料 → 啟動本機伺服器 → 自動開瀏覽器

cd "$(dirname "$0")" || exit 1
PORT=8000

clear
echo "================================"
echo "  📊 台股存股選股器"
echo "================================"
echo

# 1) 是否先抓取最新資料（連線 TWSE，可能需數分鐘）
read -r -p "是否先抓取最新資料？[y/N] " ans
if [[ "$ans" =~ ^[Yy]$ ]]; then
  # 1b) 是否用 FinMind 多年配息/獲利資料（首次約 2~3 分鐘，之後走快取很快）
  GI=""
  read -r -p "  要 FinMind 多年配息/獲利資料嗎？(首次稍久，之後快取) [y/N] " gans
  if [[ "$gans" =~ ^[Yy]$ ]]; then GI="FINMIND=1"; fi
  echo
  env $GI python3 stock_screener.py
  if [[ $? -ne 0 ]]; then
    echo
    echo "❌ 抓取失敗，請檢查網路後再試。"
    read -r -p "按 Enter 關閉視窗…"
    exit 1
  fi
  echo
fi

# 2) 找一個沒被占用的埠
while lsof -i ":$PORT" >/dev/null 2>&1; do
  PORT=$((PORT + 1))
done

URL="http://localhost:$PORT/index.html"
echo "🌐 啟動伺服器：$URL"
echo "   （關閉此視窗或按 Ctrl-C 即可停止）"
echo

# 3) 稍候開啟瀏覽器，再於前景啟動伺服器
( sleep 1; open "$URL" ) &
python3 -m http.server "$PORT"
