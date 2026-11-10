import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from bmon.bmc import (BmcClient, BmcError, parse_discrete_sensors,
                      parse_power, parse_sel_entry, parse_threshold_sensors)

FIX = Path(__file__).parent / "fixtures"


def test_parse_threshold_sensors():
    doc = json.loads((FIX / "threshold_sensors.json").read_text())
    rs = parse_threshold_sensors(doc)
    assert len(rs) == 76
    by_name = {r.name: r for r in rs}
    cpu = by_name["CPU1_TEMP"]
    assert cpu.grp == "temp" and cpu.unit == "degrees C"
    # 该 BMC 的 CPU1_TEMP 只上报非临界上限,临界阈值在电压传感器上
    assert cpu.thresholds["upper_nc"] is not None
    assert by_name["V_1V8_CPU_1"].thresholds["upper_crit"] is not None
    assert cpu.source == "bmc"
    fans = [r for r in rs if r.unit == "RPM"]
    assert fans and fans[0].grp == "fan"
    volts = [r for r in rs if r.unit == "Volts"]
    assert volts and volts[0].grp == "volt"
    watts = [r for r in rs if r.unit == "Watts"]
    assert watts and watts[0].grp == "power"


def test_null_reading_sensors_have_no_value():
    doc = json.loads((FIX / "threshold_sensors.json").read_text())
    by_name = {r.name: r for r in parse_threshold_sensors(doc)}
    for name in ("NIC OM Temp", "Raid BBU Temp"):
        r = by_name[name]
        assert r.value is None
        assert r.status == "ns"


def test_parse_discrete_sensors():
    doc = json.loads((FIX / "discrete_sensors.json").read_text())
    rs = parse_discrete_sensors(doc)
    assert len(rs) == 137
    assert all(r.grp == "status" and r.unit == "status" for r in rs)
    assert all(isinstance(r.value, float) for r in rs)


def test_parse_power_skips_absent_psu():
    doc = json.loads((FIX / "power.json").read_text())
    rs = parse_power(doc)
    names = [r.name for r in rs]
    assert "Total Power" in names
    assert any(n.endswith("Output W") for n in names)     # 在役 PSU2
    assert not any(n.startswith("PSU1 ") for n in names)  # PSU1 Absent 被跳过
    assert all(r.source == "psu" for r in rs)
    total = next(r for r in rs if r.name == "Total Power")
    assert total.grp == "power" and 0 < total.value < 2000


def test_parse_sel_entry():
    doc = json.loads((FIX / "sel_entry.json").read_text())
    e = parse_sel_entry(doc)
    assert e["id"] == 90
    assert e["ts"] == int(datetime.fromisoformat(
        "2026-10-27T13:01:46+08:00").timestamp())
    assert e["message"].startswith("BMC operation log")
    assert "LogEntry" in e["raw"]


def test_get_sel_entries_follows_pagination(monkeypatch):
    # 该 BMC 的 Entries 集合分页(每页 32 条,带 Members@odata.nextLink):
    # 必须翻页收集全部 id,否则首次同步只能拿到第一页。
    c = BmcClient("https://bmc.example", "u", "p", verify=False)
    base = "/redfish/v1/Systems/1/LogServices/Log1/Entries"
    pages = {
        base: {"Members": [
                   {"@odata.id": f"{base}/90"}, {"@odata.id": f"{base}/89"}],
               "Members@odata.nextLink": f"{base}?$skip=2&$top=32"},
        f"{base}?$skip=2&$top=32": {"Members": [
                   {"@odata.id": f"{base}/88"}, {"@odata.id": f"{base}/87"}]},
    }

    def fake_get(path):
        if path in pages:
            return pages[path]
        return {"Id": path.rsplit("/", 1)[-1],
                "EventTimestamp": "2026-10-27T13:01:46+08:00",
                "Severity": "OK", "EventType": "Event",
                "Message": "m", "MessageId": "x.1"}

    monkeypatch.setattr(c, "_get", fake_get)
    entries = c.get_sel_entries(0)
    assert [e["id"] for e in entries] == [90, 89, 88, 87]  # 最新在前,跨页
    assert [e["id"] for e in c.get_sel_entries(89)] == [90]  # 增量


def test_get_sel_entries_pagination_loop_guard(monkeypatch):
    # nextLink 成环时不得死循环:页数上限兜底
    c = BmcClient("https://bmc.example", "u", "p", verify=False)
    base = "/redfish/v1/Systems/1/LogServices/Log1/Entries"
    doc = {"Members": [{"@odata.id": f"{base}/90"}],
           "Members@odata.nextLink": f"{base}?$skip=32&$top=32"}

    def fake_get(path):
        if path.startswith(f"{base}/"):
            return {"Id": path.rsplit("/", 1)[-1],
                    "EventTimestamp": "2026-10-27T13:01:46+08:00",
                    "Severity": "OK", "EventType": "Event",
                    "Message": "m", "MessageId": "x.1"}
        return doc

    monkeypatch.setattr(c, "_get", fake_get)
    assert [e["id"] for e in c.get_sel_entries(0)] == [90]


def test_client_error_raises_bmc_error():
    c = BmcClient("https://127.0.0.1:1", "u", "p", verify=False)
    with pytest.raises(BmcError):
        c.get_all_readings()


# live 用例凭据不入库:从环境变量读取(BMC_BASE 可缺省,
# BMC_USER/BMC_PASS 未设置时与 BMC_LIVE 未开一样跳过)
BMC_BASE_ENV = os.getenv("BMC_BASE", "https://9.3.12.13")
BMC_USER_ENV = os.getenv("BMC_USER")
BMC_PASS_ENV = os.getenv("BMC_PASS")
_LIVE_READY = (os.getenv("BMC_LIVE") == "1"
               and bool(BMC_USER_ENV) and bool(BMC_PASS_ENV))
_LIVE_REASON = "需要真实 BMC(BMC_LIVE=1 且设置 BMC_USER/BMC_PASS 环境变量)"


@pytest.mark.skipif(not _LIVE_READY, reason=_LIVE_REASON)
def test_live_all_readings():
    c = BmcClient(BMC_BASE_ENV, BMC_USER_ENV, BMC_PASS_ENV)
    rs = c.get_all_readings()
    assert len(rs) >= 200


@pytest.mark.skipif(not _LIVE_READY, reason=_LIVE_REASON)
def test_live_sel_entries():
    c = BmcClient(BMC_BASE_ENV, BMC_USER_ENV, BMC_PASS_ENV)
    entries = c.get_sel_entries(0)
    assert entries and entries[0]["id"] > 0
    assert c.get_sel_entries(entries[0]["id"]) == []  # 增量为空
    # 分页完整性:首次同步应取回集合声明的全部条数(上限 200)
    doc = c._get("/redfish/v1/Systems/1/LogServices/Log1/Entries")
    total = doc.get("Members@odata.count")
    if isinstance(total, int):
        assert len(entries) == min(total, 200)
