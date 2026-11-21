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

# 1) 是否先抓取最新資料（TWSE 全市場 + 籌碼，首次約 3-5 分鐘）
read -r -p "是否先抓取最新資料？[y/N] " ans
if [[ "$ans" =~ ^[Yy]$ ]]; then
  # 1b) 是否抓 Yahoo 多年股利/年度 EPS（首次較久，之後走快取）
  YA=""
  read -r -p "  要抓 Yahoo 多年股利/年度 EPS 嗎？(首次數分鐘，之後快取) [Y/n] " yans
  if [[ "$yans" =~ ^[Nn]$ ]]; then YA="--no-yahoo"; fi
  echo
  python3 stock_screener.py $YA
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
python3 server.py "$PORT"
