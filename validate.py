#!/usr/bin/env python3
"""快照驗收：檢查資料完整度、口徑一致性與評分是否又變成 PB 代理。

用法：
  python3 validate.py                 # 驗最新快照
  python3 validate.py data/snapshot_xxx.json
結束碼：0 = 全部 PASS/WARN；1 = 有 FAIL（可直接放進 CI）。
"""
import json
import math
import os
import sys

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def latest_path():
    with open(os.path.join(DATA_DIR, "manifest.json"), encoding="utf-8") as f:
        man = json.load(f)
    return os.path.join(DATA_DIR, man["snapshots"][0]["file"])


def spearman(a, b):
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0] * len(v)
        for pos, i in enumerate(order):
            r[i] = pos + 1
        return r
    ra, rb = ranks(a), ranks(b)
    n = len(a)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = math.sqrt(sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb))
    return num / den if den else 0.0


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else latest_path()
    with open(path, encoding="utf-8") as f:
        snap = json.load(f)
    rows = snap["sheets"]["all"]["rows"]
    n = len(rows)
    checks = []

    def add(level, name, detail):
        checks.append((level, name, detail))

    add("PASS" if n > 800 else "FAIL", "全市場筆數", f"{n} 檔")

    ok_div = sum(1 for r in rows if r.get("股利資料狀態") == "ok")
    cov = ok_div / n * 100
    add("PASS" if cov >= 80 else "WARN", "股利 20 年完整度",
        f"{cov:.1f}%（{ok_div}/{n}）；<80% 代表 Yahoo 429 或種子不足")

    for col, label in [("ROE%", "品質"), ("外資持股%", "籌碼"), ("RS20", "技術")]:
        have = sum(1 for r in rows if r.get(col) is not None)
        pct = have / n * 100
        add("PASS" if pct >= 80 else "WARN", f"{label}模組覆蓋",
            f"{pct:.1f}%（{have}/{n}）")

    # 0 與 null 不可混用
    fake_zero = sum(1 for r in rows
                    if r.get("連續獲利年數") == 0 and not r.get("獲利資料年數"))
    add("PASS" if fake_zero == 0 else "FAIL", "連續獲利缺值處理",
        f"{fake_zero} 檔把「無資料」寫成 0")

    # 2317 口徑 sanity
    r2317 = next((r for r in rows if r.get("代號") == "2317"), None)
    if r2317:
        checks.append(("PASS" if (r2317.get("累積配息年數") or 0) >= 20 else "FAIL",
                       "2317 累積配息年數", f"{r2317.get('累積配息年數')} 年（應 ≥20）"))
        checks.append(("PASS" if (r2317.get("連續配發現金股利年數") or 0) >= 10 else "FAIL",
                       "2317 連續配息年數", f"{r2317.get('連續配發現金股利年數')} 年（應 ≥10）"))
        diff = abs((r2317.get("殖利率") or 0) - (r2317.get("殖利率_官方") or 0))
        checks.append(("PASS" if diff <= 0.2 else "WARN", "2317 殖利率口徑差", f"{diff:.2f}pp"))

    # 評分不可退化成 PB 倒數
    pairs = [(r["綜合分數"], 1 / r["PB"]) for r in rows
             if r.get("綜合分數") is not None and (r.get("PB") or 0) > 0]
    if len(pairs) > 50:
        rho = spearman([p[0] for p in pairs], [p[1] for p in pairs])
        add("PASS" if abs(rho) < 0.5 else "FAIL", "綜合分數 vs 1/PB",
            f"Spearman {rho:+.3f}（|ρ|<0.5 才算不是 PB 代理）")

    # 價值清單是否符合自身門檻（避免匯出/篩選不同步）
    value = snap["sheets"].get("value", {}).get("rows", [])
    bad = [r["代號"] for r in value
           if (r.get("PE") or 0) <= 0 or (r.get("PB") or 0) <= 0
           or (r.get("PE") or 99) > 15 or (r.get("PB") or 99) > 2
           or (r.get("安全邊際%") is None) or r["安全邊際%"] < 0
           or (r.get("盈餘發放率%") is None) or r["盈餘發放率%"] > 100
           or (r.get("股利穩定性倍數") or 1) < 0.8
           or (r.get("連續配發現金股利年數") or 0) < 5]
    add("PASS" if not bad else "FAIL", "價值清單門檻一致性",
        f"{len(value)} 檔，違反門檻 {len(bad)} 檔" + (f"：{bad[:6]}" if bad else ""))
    add("PASS" if value else "WARN", "價值清單非空",
        f"{len(value)} 檔（過少代表市場殖利率偏低或門檻太嚴，不一定是 bug）")

    print(f"快照：{os.path.basename(path)}｜v{snap.get('screener_version')}"
          f"｜資料日 {snap.get('meta', {}).get('data_date')}")
    print("=" * 78)
    for level, name, detail in checks:
        icon = {"PASS": "✅", "WARN": "⚠️ ", "FAIL": "❌"}[level]
        print(f"{icon} {name:<22} {detail}")
    fails = sum(1 for c in checks if c[0] == "FAIL")
    warns = sum(1 for c in checks if c[0] == "WARN")
    print("=" * 78)
    print(f"結果：{len(checks) - fails - warns} PASS / {warns} WARN / {fails} FAIL")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
