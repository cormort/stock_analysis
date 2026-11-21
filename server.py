"""本機檢視伺服器：提供靜態檔 + /api/quote 即時報價代理。

TWSE 即時報價 (MIS) 不允許瀏覽器跨域呼叫，所以由這裡轉發；只在檢視個股時按需查詢，
選股器本身不抓盤中資料。只用標準庫。用法：python3 server.py [port]
"""
import json
import re
import sys
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen

MIS = 'https://mis.twse.com.tw/stock/api/getStockInfo.jsp?json=1&delay=0&ex_ch='
CODE = re.compile(r'^\d{4,6}[A-Z]?$')


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None                                   # '-' = 本盤尚無成交


def quote(codes: list) -> dict:
    """{代號: {price, prev_close, change_pct, volume, time}}（僅上市股票）"""
    url = MIS + '|'.join(f'tse_{c}.tw' for c in codes)
    with urlopen(Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=10) as r:
        data = json.load(r)
    out = {}
    for m in data.get('msgArray', []):
        price = _num(m.get('z')) or _num(m.get('pz'))  # z=最新成交；無則用前一筆成交
        prev = _num(m.get('y'))                        # 昨收（漲跌幅基準）
        out[m.get('c')] = {
            'name': m.get('n'),
            'price': price,
            'prev_close': prev,
            'change_pct': round((price / prev - 1) * 100, 2) if price and prev else None,
            'volume': int(_num(m.get('v')) or 0),      # 累積成交量（張）
            'time': f"{m.get('d', '')} {m.get('t', '')}".strip(),
        }
    return out


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        if u.path != '/api/quote':
            return super().do_GET()
        codes = [c for c in parse_qs(u.query).get('codes', [''])[0].split(',') if CODE.match(c)][:50]
        try:
            body, status = json.dumps(quote(codes) if codes else {}, ensure_ascii=False), 200
        except Exception as e:
            body, status = json.dumps({'error': str(e)}, ensure_ascii=False), 502
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body.encode('utf-8'))


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    print(f'🌐 http://localhost:{port}/index.html（Ctrl-C 停止）')
    ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()
