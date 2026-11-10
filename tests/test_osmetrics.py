import os
import time

from bmon.osmetrics import (OsMetrics, CPU_MODEL, core_usage, disk_usage_gb,
                            parse_loadavg, parse_meminfo, parse_proc_stat)

STAT = """cpu  100 0 100 300 0 0 0 0 0 0
cpu0 10 0 10 70 0 5 0 0 0 0
cpu1 20 0 10 60 0 5 0 0 0 0
intr 1
"""

MEMINFO = """MemTotal:       100 kB
MemAvailable:    40 kB
SwapTotal:        4 kB
SwapFree:         2 kB
"""


def test_parse_proc_stat_skips_summary():
    cores = parse_proc_stat(STAT)
    assert cores == [(70, 95), (60, 95)]  # idle+iowait=70/60, total=95


def test_parse_proc_stat_excludes_guest_from_total():
    # guest/guest_nice (cols 9-10) 已包含在 user/nice 中,不能重复计入 total
    assert parse_proc_stat("cpu0 10 0 10 70 0 5 0 0 3 1\n") == [(70, 95)]


def test_core_usage_pct():
    # 元组为 (idle, total):delta total=100,delta idle=95/80 -> 占用 5%/20%
    assert core_usage([(70, 100), (60, 100)], [(165, 200), (140, 200)]) == [5.0, 20.0]
    assert core_usage([(70, 100)], [(70, 100)]) == [0.0]  # total 未变防零除


def test_parse_meminfo_kb_to_bytes():
    m = parse_meminfo(MEMINFO)
    assert m["MemTotal"] == 100 * 1024
    assert m["SwapFree"] == 2 * 1024


def test_parse_loadavg():
    assert parse_loadavg("0.10 0.20 0.30 1/2 3\n") == (0.10, 0.20, 0.30)


def test_disk_usage_gb_positive():
    total, used, pct = disk_usage_gb("/")
    assert total > 0 and used > 0 and 0 < pct < 100


def test_osmetrics_tick_real_machine():
    osm = OsMetrics()
    first = osm.tick()
    assert not any(r.name == "cpu_avg_usage" for r in first)  # 第一拍无差分
    mem_names = {r.name for r in first}
    assert {"mem_usage_pct", "mem_total_gb", "swap_usage_pct", "load_1m",
            "disk_usage_pct"} <= mem_names
    assert len(first) == 13
    time.sleep(0.05)
    second = osm.tick()
    cpu = next(r for r in second if r.name == "cpu_avg_usage")
    assert 0.0 <= cpu.value <= 100.0 and cpu.grp == "cpu"
    assert next(r for r in second if r.name == "cpu_max_core_usage").value >= cpu.value


def test_per_core_matches_cpu_count():
    osm = OsMetrics()
    osm.tick(); time.sleep(0.05); osm.tick()
    cores = osm.per_core()
    # 动态比对:本机(压测宿主)会瞬时下线个别核,写死 192 会随机挂
    assert len(cores) == os.cpu_count()
    assert cores[0].name == "cpu0_usage" and cores[0].grp == "core"
    assert all(0.0 <= c.value <= 100.0 for c in cores)


def test_static_info():
    info = OsMetrics.static_info()
    assert info["cores"] == os.cpu_count()
    assert info["mem_total_gb"] > 700
    assert "Kunpeng" in CPU_MODEL and info["hostname"] == "localhost0102"
