import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bmon.bmc import SensorReading
from bmon.db import sensor_id
from bmon.machines import Machine, save_machines
from bmon.serve import create_app


class _FakeBmc:
    """离线假 BMC 客户端:返回固定 CPU1_TEMP 读数,验证 /api/live 代理链路。"""

    def get_all_readings(self):
        return [SensorReading(source="bmc", name="CPU1_TEMP", value=48.0,
                              unit="degrees C", grp="temp", status="ok",
                              thresholds={"upper_nc": 78, "upper_crit": 85})]


class _FailingBmc:
    """总是抛错的假 BMC:验证 502 通路。"""

    def get_all_readings(self):
        raise RuntimeError("测试注入的 BMC 连接失败")


def FakeFactory(machine):
    return _FakeBmc()


def FailingFactory(machine):
    return _FailingBmc()


@pytest.fixture
def tmp_machines(db_path, tmp_path):
    """tmp_path 写入真实密码清单,返回 (db_path, machines_path)。"""
    machines_path = str(tmp_path / "machines.txt")
    save_machines(machines_path, [Machine(ip="9.3.12.13", user="Administrator",
                                          password="Secret1", alias="0102")])
    return db_path, machines_path


def test_machines_get_masked_and_live(tmp_machines):
    db_path, machines_path = tmp_machines
    client = TestClient(create_app(db_path, machines_path=machines_path, client_factory=FakeFactory))
    body = client.get("/api/machines").json()
    assert "Secret1" not in body["text"] and "****" in body["text"]
    r = client.get("/api/live/0102/sensors")
    assert r.status_code == 200 and r.json()["readings"][0]["name"] == "CPU1_TEMP"
    assert client.get("/api/live/0109/sensors").status_code == 404


def test_machines_post_merge_and_400(tmp_machines):
    db_path, machines_path = tmp_machines
    client = TestClient(create_app(db_path, machines_path=machines_path, client_factory=FakeFactory))
    r = client.post("/api/machines", json={"text": "9.3.12.13 u **** 0102\nbadline\n"})
    assert r.status_code == 400 and any("第2行" in e for e in r.json()["errors"])
    r = client.post("/api/machines", json={"text": "9.3.12.13 u **** 0102\n"})
    assert r.status_code == 200
    assert "Secret1" in Path(machines_path).read_text()     # **** 已回填原密码


def test_machines_get_preserves_comments(tmp_machines):
    # 手写清单的注释/空行必须在编辑器文本里原样保留(mask_text 只动密码列),
    # 否则配置页首次 ctrl+s 保存就会把它们从文件里删掉
    db_path, machines_path = tmp_machines
    Path(machines_path).write_text(
        "# 生产机器\n9.3.12.13 Administrator Secret1 0102\n\n", encoding="utf-8")
    client = TestClient(create_app(db_path, machines_path=machines_path))
    body = client.get("/api/machines").json()
    assert body["text"] == "# 生产机器\n9.3.12.13 Administrator **** 0102\n\n"
    assert "Secret1" not in body["text"]
    assert body["errors"] == []


def test_live_picks_up_revert_after_post(tmp_machines):
    # POST 后文件被外部字节级还原回 POST 前内容:POST 必须失效 last_text,
    # 否则下次 /api/live 读到"未变"的旧原文会跳过同步,轮询器滞留在
    # POST 的清单上(文件说有 0102,live 却 404)
    db_path, machines_path = tmp_machines
    client = TestClient(create_app(db_path, machines_path=machines_path,
                                   client_factory=FakeFactory))
    original = Path(machines_path).read_text()             # A: 只有 0102
    assert client.get("/api/live/0102/sensors").status_code == 200  # last_text=A
    # POST 换成 0103:文件变 B、poller 即时切换(此后不再调 live)
    assert client.post("/api/machines",
                       json={"text": "9.3.12.15 u p 0103\n"}).status_code == 200
    Path(machines_path).write_text(original, encoding="utf-8")     # 还原回 A
    # 0102 必须重新可读:轮询器跟着文件走,不滞留在 POST 的 0103
    assert client.get("/api/live/0102/sensors").status_code == 200


def test_machines_get_missing_file(db_path, tmp_path):
    # 清单缺失/不可读:返回可诊断的空状态,绝不 500
    client = TestClient(create_app(db_path, machines_path=str(tmp_path / "none.txt")))
    body = client.get("/api/machines").json()
    assert body["text"] == "" and body["machines"] == []
    assert len(body["errors"]) == 1 and "none.txt" in body["errors"][0]


def test_machines_masked_dicts_no_plaintext(tmp_machines):
    # GET/POST 的 machines 列表一律掩码 dict,整个响应不出现明文密码
    db_path, machines_path = tmp_machines
    client = TestClient(create_app(db_path, machines_path=machines_path))
    body = client.get("/api/machines").json()
    assert body["machines"] == [{"ip": "9.3.12.13", "user": "Administrator",
                                 "password": "****", "alias": "0102"}]
    r = client.post("/api/machines", json={"text": "9.3.12.13 u **** 0102\n"})
    assert r.status_code == 200
    assert r.json()["machines"][0]["password"] == "****"
    assert "Secret1" not in r.text


def test_live_502_erroring_factory(tmp_machines):
    # BMC 拉取失败 → 502,body {"ok": False, "error"}
    db_path, machines_path = tmp_machines
    client = TestClient(create_app(db_path, machines_path=machines_path, client_factory=FailingFactory))
    r = client.get("/api/live/0102/sensors")
    assert r.status_code == 502
    assert r.json()["ok"] is False and r.json()["error"]


def test_live_picks_up_file_change(tmp_machines):
    # /api/live 每次请求前重读清单:外部改文件无需重启即可轮询新机器
    db_path, machines_path = tmp_machines
    client = TestClient(create_app(db_path, machines_path=machines_path, client_factory=FakeFactory))
    assert client.get("/api/live/0103/sensors").status_code == 404
    save_machines(machines_path, [Machine(ip="9.3.12.15", user="u",
                                          password="p", alias="0103")])
    r = client.get("/api/live/0103/sensors")
    assert r.status_code == 200 and r.json()["readings"][0]["name"] == "CPU1_TEMP"


def test_overview_kpis_and_groups(seeded_db):
    path, sid = seeded_db
    client = TestClient(create_app(path))
    ov = client.get("/api/overview").json()
    names = {k["name"] for k in ov["kpis"]}
    assert {"mem_usage_pct", "CPU1_TEMP"} <= names
    mem = next(k for k in ov["kpis"] if k["name"] == "mem_usage_pct")
    assert mem["value"] == 51.0 and len(mem["history"]) >= 60
    assert ov["groups"]["mem"]["count"] == 1
    cpu = next(k for k in ov["kpis"] if k["name"] == "CPU1_TEMP")
    assert cpu["label"] == "CPU1 Temp"


def test_overview_total_power_kpi(db_path):
    # parse_power 把 PowerControl 总功耗注册为 source='psu' 的 "Total Power";
    # KPI_DEFS 若写成 ('bmc', 'Total Power') 将永不匹配,KPI 悄悄缺失。
    import time

    from bmon.db import connect, update_latest, upsert_sensor
    from bmon.queries import get_overview

    conn = connect(db_path)
    sid = upsert_sensor(conn, "psu", "Total Power", "power", "Watts")
    update_latest(conn, int(time.time()), [(sid, 540.0, "ok")])
    conn.close()
    conn = connect(db_path)
    tp = next(k for k in get_overview(conn)["kpis"] if k["name"] == "Total Power")
    conn.close()
    assert tp["label"] == "Total PWR" and tp["value"] == 540.0


def test_overview_static_block(seeded_db):
    # 终版评审修复 2:overview 携带服务器静态信息(static_info 在
    # bmon-serve 端运行),远程 TUI 头部据此显示服务器规格而非客户端本机
    client = TestClient(create_app(seeded_db[0]))
    st = client.get("/api/overview").json()["static"]
    assert st["hostname"] == "localhost0102"
    # 动态核数:压测宿主会瞬时摘核,写死 192 会随机挂
    assert st["cores"] == os.cpu_count()
    assert st["mem_total_gb"] > 0
    assert "Kunpeng920" in st["cpu_model"]


def test_sensors_group_filter(seeded_db):
    path, sid = seeded_db
    client = TestClient(create_app(path))
    all_rows = client.get("/api/sensors").json()
    assert len(all_rows) == 2
    mem_rows = client.get("/api/sensors", params={"group": "mem"}).json()
    assert mem_rows[0]["name"] == "mem_usage_pct" and mem_rows[0]["age_s"] >= 0
    assert client.get("/api/sensors", params={"group": "nope"}).json() == []


def test_history_bucketing_and_errors(seeded_db):
    path, sid = seeded_db
    client = TestClient(create_app(path))
    r = client.get("/api/history", params={"sensor": sid, "range": "1h",
                                           "max_points": 200})
    body = r.json()
    assert body["sensor"]["name"] == "mem_usage_pct"
    assert body["points"][-1][1] == 51.0
    assert client.get("/api/history", params={"sensor": 999}).json()["sensor"] is None
    assert client.get("/api/history", params={"sensor": sid, "range": "bad"}).status_code == 400


def test_extremes(seeded_db):
    path, sid = seeded_db
    client = TestClient(create_app(path))
    r = client.get("/api/extremes", params={"sids": sid, "range": "1h"})
    ex = r.json()
    assert ex[str(sid)] == {"min": 50.0, "max": 51.0}


def test_sel_and_sdc_empty(seeded_db):
    path, sid = seeded_db
    client = TestClient(create_app(path))
    sel = client.get("/api/sel").json()
    assert sel[0]["message"] == "test event"
    assert client.get("/api/sdc").json() is None


def test_extremes_bad_range_400(seeded_db):
    path, sid = seeded_db
    client = TestClient(create_app(path))
    r = client.get("/api/extremes", params={"sids": "1", "range": "bad"})
    assert r.status_code == 400


def test_history_bad_max_points_400(seeded_db):
    path, sid = seeded_db
    client = TestClient(create_app(path))
    r = client.get("/api/history", params={"sensor": sid, "range": "1h",
                                          "max_points": 0})
    assert r.status_code == 400
