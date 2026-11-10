import types

from bmon.bmc import BmcError, SensorReading
from bmon.collect import Collector
from bmon.db import (connect, get_sensor, init_db, max_sel_id, query_series,
                     sensor_id)


class FakeBmc:
    def __init__(self, readings=None, error=False):
        self.readings = readings or []
        self.error = error

    def get_all_readings(self):
        if self.error:
            raise BmcError("boom")
        return self.readings

    def get_sel_entries(self, since_id=0):
        if since_id == 0:
            return [{"id": 5, "ts": 1000, "severity": "OK", "event_type": "Alert",
                     "message": "m", "message_id": "x", "raw": "{}"}]
        return []


class FakeOsm:
    def __init__(self):
        self.n = 0

    def tick(self):
        self.n += 1
        return [SensorReading("os", "mem_usage_pct", 50.0 + self.n, "%", "mem", "ok")]

    def per_core(self):
        return [SensorReading("os", "cpu0_usage", 1.0, "%", "core", "ok")]


class EvilBmc:
    """返回畸形数据(非 BmcError 的原生异常),模拟 Redfish JSON 损坏。"""

    def get_all_readings(self):
        raise ValueError("malformed")

    def get_sel_entries(self, since_id=0):
        raise ValueError("malformed")


def _cfg():
    return types.SimpleNamespace(
        interval_core_s=10, interval_slow_s=60, interval_rollup_s=300,
        interval_prune_s=3600, raw_keep_s=172800, rollup_keep_s=3024000,
        sdc_log_path="/nonexistent/sdc.log")


def test_collector_stores_bmc_os_sel(tmp_path):
    conn = connect(str(tmp_path / "t.db")); init_db(conn)
    bmc = [SensorReading("bmc", "CPU1_TEMP", 48.0, "degrees C", "temp", "ok",
                         {"upper_crit": 85})]
    c = Collector(_cfg(), conn, FakeBmc(bmc), FakeOsm())
    c.tick(1000)
    c.tick(1001)
    pts = query_series(conn, sensor_id("bmc", "CPU1_TEMP"), 1000, 1001)
    assert len(pts) == 2 and pts[1][1] == 48.0
    s = get_sensor(conn, sensor_id("os", "mem_usage_pct"))
    assert s["last_value"] == 52.0            # 50+n, n=2
    assert s["thr_upper_crit"] is None        # os 传感器无阈值
    assert max_sel_id(conn) == 5              # SEL 已入库


def test_collector_survives_bmc_error(tmp_path):
    conn = connect(str(tmp_path / "t.db")); init_db(conn)
    c = Collector(_cfg(), conn, FakeBmc(error=True), FakeOsm())
    c.tick(1000)   # 不得抛异常;OS 数据仍在
    assert query_series(conn, sensor_id("os", "mem_usage_pct"), 1000, 1000)


def test_collector_per_core_on_first_tick(tmp_path):
    conn = connect(str(tmp_path / "t.db")); init_db(conn)
    c = Collector(_cfg(), conn, FakeBmc(), FakeOsm())
    c.tick(1000)   # 1000-0 >= 10 → 每核入库
    assert query_series(conn, sensor_id("os", "cpu0_usage"), 1000, 1000)


def test_collector_null_reading_registered_without_raw(tmp_path):
    """Task4 修正:value=None 的读数(如 NIC OM Temp)只注册+更新 last_status,
    不写 raw、不刷新 last_value/last_ts(避免幽灵 0 值历史与假新鲜度)。"""
    conn = connect(str(tmp_path / "t.db")); init_db(conn)
    bmc = [SensorReading("bmc", "NIC OM Temp", None, "degrees C", "temp", "ns")]
    c = Collector(_cfg(), conn, FakeBmc(bmc), FakeOsm())
    c.tick(1000)
    c.tick(1001)
    s = get_sensor(conn, sensor_id("bmc", "NIC OM Temp"))
    assert s is not None                      # 传感器已注册可见
    assert s["last_value"] is None and s["last_ts"] is None
    assert s["last_status"] == "ns"
    assert query_series(conn, sensor_id("bmc", "NIC OM Temp"), 1000, 1001) == []


def test_collector_survives_non_bmc_error(tmp_path):
    """BMC 端返回畸形数据(KeyError/ValueError 等非 BmcError)时:
    tick 不得抛异常,OS 数据仍须入库(采集不崩溃,按 tick 重试)。"""
    conn = connect(str(tmp_path / "t.db")); init_db(conn)
    c = Collector(_cfg(), conn, EvilBmc(), FakeOsm())
    c.tick(1000)   # 不得抛异常;慢速块(1000-0>=60)也会触发 get_sel_entries
    assert query_series(conn, sensor_id("os", "mem_usage_pct"), 1000, 1000)


def test_watcher_detects_change_and_missing(tmp_path):
    from bmon.collect import MachinesWatcher
    p = tmp_path / "machines.txt"
    w = MachinesWatcher(str(p), ["0102", "0103"], node_id="0102")
    assert w.poll() is None                                # 文件不存在
    p.write_text("9.3.12.13 u p1 0102\n9.3.12.15 u p2 0103\n")
    st = w.poll(); assert st["local"].alias == "0102" and st["remotes"]["0103"].alias == "0103"
    assert w.poll() is None                                # 内容未变
    p.write_text("9.3.12.13 u p1 0102\n")                  # 0103 被删
    st = w.poll(); assert "0103" not in st["remotes"] and any("0103" in e for e in st["errors"])


def test_watcher_survives_non_utf8_file(tmp_path):
    """清单被存成非 UTF-8(如 GBK 中文注释)时 poll 不得抛异常:
    UnicodeDecodeError 不是 OSError,漏接会击穿 main 的 tick 兜底、杀死采集进程。"""
    from bmon.collect import MachinesWatcher
    p = tmp_path / "machines.txt"
    p.write_bytes("# 中文注释\n9.3.12.13 u p1 0102\n".encode("gbk"))
    w = MachinesWatcher(str(p), ["0102"], node_id="0102")
    assert w.poll() is None                    # 首次即不可读:等同缺失,不抛异常
    p.write_bytes("9.3.12.13 u p1 0102\n".encode())
    assert w.poll()["local"].alias == "0102"   # 编码修好后正常恢复
    p.write_bytes("# 又是GBK注释\n".encode("gbk"))
    st = w.poll()                              # 由好变坏:走变更路径,错误可诊断
    assert st["local"] is None and st["remotes"] == {}
    assert any("不可读" in e for e in st["errors"])
    assert w.poll() is None                    # 持续不可读不重复报告


def test_collector_remote_source_namespace(tmp_path):
    from dataclasses import replace
    conn = connect(str(tmp_path / "t.db")); init_db(conn)
    bmc = [SensorReading("bmc", "CPU1_TEMP", 48.0, "degrees C", "temp", "ok")]
    remote = [SensorReading("bmc", "CPU1_TEMP", 51.0, "degrees C", "temp", "ok")]
    c = Collector(_cfg(), conn, FakeBmc(bmc), FakeOsm(), remote_bmcs={"0103": FakeBmc(remote)})
    c.tick(1000)
    assert query_series(conn, sensor_id("bmc", "CPU1_TEMP"), 1000, 1000)[0][1] == 48.0
    assert query_series(conn, sensor_id("bmc@0103", "CPU1_TEMP"), 1000, 1000)[0][1] == 51.0


def test_collector_no_local_bmc_still_collects_os(tmp_path):
    conn = connect(str(tmp_path / "t.db")); init_db(conn)
    c = Collector(_cfg(), conn, None, FakeOsm())           # 本机凭据缺失
    c.tick(1000); c.tick(1061)                             # 含 slow 周期(SEL 路径)
    assert query_series(conn, sensor_id("os", "mem_usage_pct"), 1000, 1061)
