"""API payload 測試（不啟伺服器、不打網路）。"""
import json

import server


def _snap():
    return {
        "generated_at": "2026-11-21T11:35:14",
        "screener_version": "6.0",
        "meta": {"data_date": "2026-11-20"},
        "sheets": {
            "value": {"title": "價值", "criteria": {"PE_上限": 15}, "rows": [
                {"代號": "2317", "Name": "鴻海", "綜合分數": 46.9, "殖利率": 3.42},
                {"代號": "2330", "Name": "台積電", "綜合分數": 40.0, "殖利率": 2.1},
            ]},
            "all": {"title": "全市場", "criteria": {}, "rows": [
                {"代號": "2317", "Name": "鴻海", "綜合分數": 46.9, "殖利率": 3.42},
                {"代號": "2330", "Name": "台積電", "綜合分數": 40.0, "殖利率": 2.1},
                {"代號": "2454", "Name": "聯發科", "綜合分數": None, "殖利率": 1.5},
            ]},
        },
    }


def test_analysis_returns_requested_fields():
    out = server.analysis_payload(_snap(), ["2317"], ["Name", "殖利率"])
    assert out["data_date"] == "2026-11-20"
    assert out["stocks"]["2317"] == {"Name": "鴻海", "殖利率": 3.42}


def test_analysis_full_row_and_missing_code():
    out = server.analysis_payload(_snap(), ["2330", "9999"])
    assert out["stocks"]["2330"]["綜合分數"] == 40.0
    assert out["stocks"]["9999"]["error"] == "not_found"


def test_screen_sorts_desc_and_limits():
    out = server.screen_payload(_snap(), "value", limit=1, sort="綜合分數")
    assert out["total"] == 2
    assert len(out["rows"]) == 1
    assert out["rows"][0]["代號"] == "2317"


def test_screen_ascending_and_none_last():
    out = server.screen_payload(_snap(), "all", limit=0, sort="綜合分數", ascending=True)
    assert out["rows"][-1]["代號"] == "2454"      # None 排最後
    assert out["rows"][0]["代號"] == "2330"


def test_screen_unknown_sheet():
    out = server.screen_payload(_snap(), "nope")
    assert out["error"] == "unknown sheet"


def test_latest_snapshot_reads_manifest(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    snap = _snap()
    (data / "s.json").write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    (data / "manifest.json").write_text(json.dumps({"snapshots": [{"file": "s.json"}]}),
                                        encoding="utf-8")
    got = server.latest_snapshot(str(data))
    assert got["screener_version"] == "6.0"
    assert got["sheets"]["value"]["rows"][0]["代號"] == "2317"
    assert server.latest_snapshot(str(tmp_path)) is None   # 沒有 manifest
