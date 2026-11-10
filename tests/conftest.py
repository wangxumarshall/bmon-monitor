import time

import pytest

from bmon.db import (connect, init_db, insert_readings, insert_sel_events,
                     sensor_id, update_latest, upsert_sensor)


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "t.db")
    conn = connect(path)
    init_db(conn)
    conn.close()
    return path


@pytest.fixture
def seeded_db(tmp_path):
    """返回 (db_path, mem_sensor_id);预置 mem 传感器 120 点 + bmc CPU1_TEMP 最新值 + 1 条 SEL。"""
    path = str(tmp_path / "t.db")
    conn = connect(path)
    init_db(conn)
    sid = upsert_sensor(conn, "os", "mem_usage_pct", "mem", "%")
    now = int(time.time())
    # 近 1h 每隔 30s 共 120 点,值在 50/51 间交替且最新点为 51.0:
    # 这样 1h 窗口可覆盖 60 个分钟桶,极值为 {50, 51},末桶均值恰为 51.0。
    for i in range(120):
        insert_readings(conn, now - 3600 + i * 30, [(sid, 50.0 + (i % 2))])
    update_latest(conn, now, [(sid, 51.0, "ok")])
    tsid = upsert_sensor(conn, "bmc", "CPU1_TEMP", "temp", "degrees C",
                         {"upper_nc": 78, "upper_crit": 85})
    update_latest(conn, now, [(tsid, 48.0, "ok")])
    insert_sel_events(conn, [{"id": 1, "ts": now, "severity": "OK",
                              "event_type": "Alert", "message": "test event",
                              "message_id": "m1", "raw": "{}"}])
    conn.close()
    yield path, sid
