from bmon.db import connect, init_db, sensor_id, upsert_sensor, list_sensors, get_sensor


def test_sensor_id_stable_and_distinct():
    assert sensor_id("bmc", "CPU1_TEMP") == sensor_id("bmc", "CPU1_TEMP")
    assert sensor_id("bmc", "CPU1_TEMP") != sensor_id("os", "CPU1_TEMP")
    assert sensor_id("bmc", "CPU1_TEMP") != sensor_id("bmc", "CPU2_TEMP")


def test_upsert_and_list(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    init_db(conn)
    s1 = upsert_sensor(conn, "bmc", "CPU1_TEMP", "temp", "degrees C",
                       {"upper_nc": 78, "upper_crit": 85})
    s1b = upsert_sensor(conn, "bmc", "CPU1_TEMP", "temp", "degrees C")  # 二次注册同 id
    assert s1 == s1b
    upsert_sensor(conn, "os", "mem_usage_pct", "mem", "%")
    assert [s["name"] for s in list_sensors(conn, "temp")] == ["CPU1_TEMP"]
    assert len(list_sensors(conn)) == 2
    row = get_sensor(conn, s1)
    assert row["thr_upper_crit"] == 85 and row["grp"] == "temp"
    assert get_sensor(conn, 999999) is None


import time

from bmon.db import (insert_readings, update_latest, query_series, rollup_window,
                     prune, group_extremes, insert_sel_events, max_sel_id,
                     list_sel_events, save_sdc_run, latest_sdc)


def _mk(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    init_db(conn)
    sid = upsert_sensor(conn, "bmc", "CPU1_TEMP", "temp", "C")
    return conn, sid


def test_insert_query_exact(tmp_path):
    conn, sid = _mk(tmp_path)
    for ts in range(1000, 1100):
        insert_readings(conn, ts, [(sid, 50.0)])
    update_latest(conn, 1099, [(sid, 50.0, "ok")])
    pts = query_series(conn, sid, 1000, 1099, max_points=300)
    assert len(pts) == 100 and pts[0] == (1000, 50.0)
    s = get_sensor(conn, sid)
    assert s["last_value"] == 50.0 and s["last_ts"] == 1099


def test_query_bucketing_avg(tmp_path):
    conn, sid = _mk(tmp_path)
    for ts in range(1000, 1200):
        insert_readings(conn, ts, [(sid, float(ts))])
    pts = query_series(conn, sid, 1000, 1199, max_points=10)  # bucket=20
    assert len(pts) == 10 and pts[0][0] == 1000
    assert pts[0][1] == 1009.5  # 1000..1019 均值


def test_rollup_and_long_range_query(tmp_path):
    conn, sid = _mk(tmp_path)
    base = 1_700_000_000
    for ts in range(base, base + 26 * 3600, 60):
        insert_readings(conn, ts, [(sid, 1.0)])
    rollup_window(conn, base, base + 26 * 3600)
    rows = conn.execute("SELECT COUNT(*) FROM rollup_1m").fetchone()
    assert rows[0] == 26 * 60
    pts = query_series(conn, sid, base, base + 26 * 3600 - 1, max_points=100)
    assert 1 <= len(pts) <= 100  # >25h 走 rollup


def test_prune_only_old(tmp_path):
    conn, sid = _mk(tmp_path)
    insert_readings(conn, 1000, [(sid, 1.0)])
    insert_readings(conn, 100_000, [(sid, 2.0)])
    dr, du = prune(conn, 100_000, raw_keep_s=1000, rollup_keep_s=999_999)
    assert (dr, du) == (1, 0)
    # 窗口需 ≤25h 才走 raw;bucket=1 才返回原始 ts(见 query_series 契约)
    pts = query_series(conn, sid, 99_900, 100_001)
    assert pts == [(100_000, 2.0)]


def test_group_extremes(tmp_path):
    conn, sid = _mk(tmp_path)
    sid2 = upsert_sensor(conn, "bmc", "CPU2_TEMP", "temp", "C")
    for ts in range(1000, 1120):
        insert_readings(conn, ts, [(sid, 40.0), (sid2, 60.0)])
    ex = group_extremes(conn, [sid, sid2], 1000, 1119)
    assert ex[sid] == (40.0, 40.0) and ex[sid2] == (60.0, 60.0)


def test_sel_idempotent(tmp_path):
    conn, sid = _mk(tmp_path)
    entry = {"id": 5, "ts": 1000, "severity": "OK", "event_type": "Alert",
             "message": "m", "message_id": "x", "raw": "{}"}
    assert insert_sel_events(conn, [entry]) == 1
    assert insert_sel_events(conn, [entry]) == 0   # 幂等
    assert max_sel_id(conn) == 5
    events = list_sel_events(conn, 10)
    assert events[0]["message"] == "m"
    assert insert_sel_events(conn, [{**entry, "id": 6, "message": "m2"}]) == 1


def test_sdc_run_replace(tmp_path):
    conn, sid = _mk(tmp_path)
    s1 = {"log_mtime": 1.0, "parsed_at": 100,
          "totals": {"pass": 10, "fail": 0, "total": 10},
          "suites": [{"name": "eigen", "pass": 10}],
          "per_core": {"0": {"eigen_svd": {"pass": 3, "fail": 0, "interrupt": 1,
                                           "invalid": 1, "skip": 0}}}}
    save_sdc_run(conn, s1)
    s2 = {**s1, "log_mtime": 2.0, "totals": {"pass": 5, "fail": 0, "total": 5}}
    save_sdc_run(conn, s2)  # 整体替换
    got = latest_sdc(conn)
    assert got["totals"]["total"] == 5 and got["log_mtime"] == 2.0
    assert got["per_core"]["0"]["eigen_svd"]["interrupt"] == 1
    n = conn.execute("SELECT COUNT(*) FROM sdc_runs").fetchone()[0]
    assert n == 1
