import pytest
import requests

from bmon.bmc import BmcError, SensorReading
from bmon.machines import MASK
from bmon.serve import create_app
from bmon.tui.datasource import LocalDataSource, RemoteDataSource
from bmon.db import connect, query_series, get_sensor
from fastapi.testclient import TestClient


def test_local_datasource(seeded_db):
    path, sid = seeded_db
    ds = LocalDataSource(path)
    ov = ds.overview()
    assert any(k["name"] == "mem_usage_pct" for k in ov["kpis"])
    sensors = ds.sensors("mem")
    assert sensors[0]["name"] == "mem_usage_pct"
    pts = ds.history(sid, "1h")
    assert pts and pts[-1][1] == 51.0
    assert ds.extremes([sid], "1h")[str(sid)] == {"min": 50.0, "max": 51.0}
    assert ds.sel()[0]["message"] == "test event"
    assert ds.sdc() is None


def test_remote_datasource_via_api(monkeypatch, seeded_db):
    path, sid = seeded_db
    client = TestClient(create_app(path))

    class R:
        def __init__(self, body):
            self._body = body

        def raise_for_status(self):
            pass

        def json(self):
            return self._body

    def fake_get(url, params=None, timeout=None):
        path_q = url.replace("http://bmon", "")
        return R(client.get(path_q, params=params).json())

    monkeypatch.setattr("bmon.tui.datasource.requests.get", fake_get)
    ds = RemoteDataSource("http://bmon")
    ov = ds.overview()
    assert any(k["name"] == "mem_usage_pct" for k in ov["kpis"])
    assert ds.history(sid, "1h")[-1][1] == 51.0
    assert ds.sensors("mem")[0]["name"] == "mem_usage_pct"
    assert ds.extremes([sid], "1h")[str(sid)]["max"] == 51.0
    assert ds.sel()[0]["message"] == "test event"
    assert ds.sdc() is None


# ---- 机器清单 / 实时读数:两端同签名同形态(供配置页与远程机器页) ----


class FakeBmc:
    """离线假客户端,形态对齐 tests/test_live.py(注入读数/错误)。"""

    def __init__(self, readings=None, error=None):
        self.readings = readings or []
        self.error = error

    def get_all_readings(self):
        if self.error:
            raise self.error
        return self.readings


class FakeResponse:
    """离线假 HTTP 响应:只实现 datasource 用到的 status_code/json/raise。"""

    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)

    def json(self):
        return self._body


def _reading():
    return [SensorReading("bmc", "CPU1_TEMP", 48.0, "degrees C", "temp", "ok")]


def test_local_machines_masks_passwords(tmp_path):
    mp = tmp_path / "machines.txt"
    mp.write_text("9.3.12.13 admin s3cret 0102\n", encoding="utf-8")
    ds = LocalDataSource(str(tmp_path / "t.db"), str(mp))
    r = ds.machines()
    assert r["errors"] == []
    assert "s3cret" not in r["text"] and MASK in r["text"]
    assert r["machines"] == [{"ip": "9.3.12.13", "user": "admin",
                              "password": MASK, "alias": "0102"}]


def test_local_machines_defensive_on_unreadable(tmp_path):
    # 缺失与非 UTF-8 都归一为 (空文本, 空清单, 单条可诊断错误),不抛异常
    missing = LocalDataSource(str(tmp_path / "t.db"), str(tmp_path / "none.txt"))
    r = missing.machines()
    assert r["text"] == "" and r["machines"] == [] and len(r["errors"]) == 1

    bad = tmp_path / "bad.txt"
    bad.write_bytes(b"\xff\xfe admin")
    r2 = LocalDataSource(str(tmp_path / "t.db"), str(bad)).machines()
    assert r2["text"] == "" and r2["machines"] == [] and len(r2["errors"]) == 1


def test_local_machines_text_keeps_comments(tmp_path):
    # 手写清单的注释/空行/坏行必须在掩码文本里 round-trip:否则配置页
    # 第一次 ctrl+s 就会把它们从文件里删掉;坏行的解析错误照常呈现
    mp = tmp_path / "machines.txt"
    mp.write_text("# 生产机器\n9.3.12.13 admin s3cret 0102\n\nbadline\n",
                  encoding="utf-8")
    ds = LocalDataSource(str(tmp_path / "t.db"), str(mp))
    r = ds.machines()
    assert r["text"] == "# 生产机器\n9.3.12.13 admin **** 0102\n\nbadline\n"
    assert "s3cret" not in r["text"]
    assert any("第4行" in e for e in r["errors"])


def test_local_save_machines_mask_roundtrip(tmp_path):
    mp = tmp_path / "machines.txt"
    mp.write_text("9.3.12.13 admin s3cret 0102\n", encoding="utf-8")
    ds = LocalDataSource(str(tmp_path / "t.db"), str(mp),
                         client_factory=lambda m: FakeBmc(_reading()))
    # 掩码文本 + 新增一台(明文密码):旧密码应回填,新密码原样落盘
    text = ds.machines()["text"] + "9.3.12.14 admin newpass 0109\n"
    r = ds.save_machines(text)
    assert r["ok"] is True and r["errors"] == []
    assert r["machines"][0]["password"] == MASK      # 返回值仍是掩码
    saved = mp.read_text(encoding="utf-8")
    assert "s3cret" in saved and "newpass" in saved
    # poller 已热更新:新旧两台立即可以 live,无需重启
    assert ds.live("0102")["ok"] and ds.live("0109")["ok"]
    # 明文密码文件必须 0600
    assert (mp.stat().st_mode & 0o777) == 0o600


def test_local_save_machines_refuses_bad_text(tmp_path):
    mp = tmp_path / "machines.txt"
    mp.write_text("9.3.12.13 admin s3cret 0102\n", encoding="utf-8")
    ds = LocalDataSource(str(tmp_path / "t.db"), str(mp))
    before = mp.read_text(encoding="utf-8")
    # **** 的 ip/别名都对不上旧清单:整体拒绝
    r = ds.save_machines("9.3.12.99 admin **** 0109\n")
    assert r["ok"] is False and r["errors"]
    # 解析错误同样拒绝
    r2 = ds.save_machines("只有一列\n")
    assert r2["ok"] is False and r2["errors"]
    # 两次失败都不落盘,文件保持原样
    assert mp.read_text(encoding="utf-8") == before


def test_local_live_shapes(tmp_path):
    mp = tmp_path / "machines.txt"
    mp.write_text("9.3.12.13 admin s3cret 0102\n9.3.12.14 admin s3cret 0103\n",
                  encoding="utf-8")

    def factory(m):
        # 0102 正常,0103 模拟 BMC 拉取失败
        return (FakeBmc(_reading()) if m.alias == "0102"
                else FakeBmc(error=BmcError("boom")))

    ds = LocalDataSource(str(tmp_path / "t.db"), str(mp), client_factory=factory)
    ok = ds.live("0102")
    assert ok["ok"] is True and ok["readings"][0]["name"] == "CPU1_TEMP"
    # 错误形态与远程对齐:{"ok": False, "error"},不带内部 code
    assert ds.live("0103") == {"ok": False, "error": "boom"}
    assert ds.live("nope") == {"ok": False, "error": "未知机器: nope"}


def test_local_verify_tls_passed_to_bmc_client(tmp_path, monkeypatch):
    """默认工厂路径:verify_tls 必须一路透传到 BmcClient(不注入假工厂)。"""
    mp = tmp_path / "machines.txt"
    mp.write_text("9.3.12.13 admin s3cret 0102\n", encoding="utf-8")
    made = {}

    class FakeBmcClient:
        def __init__(self, base, user, password, verify):
            made.update(verify_tls=verify, password=password)

        def get_all_readings(self):
            return _reading()

    monkeypatch.setattr("bmon.live.BmcClient", FakeBmcClient)
    ds = LocalDataSource(str(tmp_path / "t.db"), str(mp), verify_tls=True)
    assert ds.live("0102")["ok"]
    assert made == {"verify_tls": True, "password": "s3cret"}


def test_local_live_picks_up_file_change(tmp_path):
    # 手改清单文件(带外编辑)新增机器:live() 前与文件对账,
    # 无需重启 TUI 即可轮询新机器(与 serve 的 /api/live 行为对齐)
    mp = tmp_path / "machines.txt"
    mp.write_text("9.3.12.13 admin s3cret 0102\n", encoding="utf-8")
    ds = LocalDataSource(str(tmp_path / "t.db"), str(mp),
                         client_factory=lambda m: FakeBmc(_reading()))
    assert ds.live("0109") == {"ok": False, "error": "未知机器: 0109"}
    mp.write_text("9.3.12.13 admin s3cret 0102\n"
                  "9.3.12.14 admin pw2 0109\n", encoding="utf-8")
    ok = ds.live("0109")
    assert ok["ok"] is True and ok["readings"][0]["name"] == "CPU1_TEMP"


def test_local_live_defensive_on_unreadable(tmp_path):
    # live() 的文件对账必须防御:清单变非 UTF-8 或被删时保持当前
    # 轮询状态,绝不在 live 路径上抛异常
    mp = tmp_path / "machines.txt"
    mp.write_text("9.3.12.13 admin s3cret 0102\n", encoding="utf-8")
    ds = LocalDataSource(str(tmp_path / "t.db"), str(mp),
                         client_factory=lambda m: FakeBmc(_reading()))
    assert ds.live("0102")["ok"] is True
    mp.write_bytes(b"\xff\xfe bad bytes")          # 非 UTF-8:保持现状
    assert ds.live("0102")["ok"] is True
    mp.unlink()                                    # 文件被删:保持现状
    assert ds.live("0102")["ok"] is True


def test_remote_machines_passthrough(monkeypatch):
    body = {"text": "9.3.12.13 admin **** 0102\n",
            "machines": [{"ip": "9.3.12.13", "user": "admin",
                          "password": MASK, "alias": "0102"}],
            "errors": []}
    monkeypatch.setattr(
        "bmon.tui.datasource.requests.get",
        lambda url, params=None, timeout=None: FakeResponse(200, body))
    assert RemoteDataSource("http://bmon").machines() == body


def test_remote_save_machines_maps_400(monkeypatch):
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append((url, json, timeout))
        if json["text"] == "bad":
            return FakeResponse(400, {"ok": False, "errors": ["第1行: 坏行"]})
        return FakeResponse(200, {"ok": True,
                                  "machines": [{"ip": "9.3.12.13", "user": "admin",
                                                "password": MASK, "alias": "0102"}],
                                  "errors": []})

    monkeypatch.setattr("bmon.tui.datasource.requests.post", fake_post)
    ds = RemoteDataSource("http://bmon")
    r = ds.save_machines("bad")
    assert r["ok"] is False and r["errors"]          # 400 不抛,原样回传
    r2 = ds.save_machines("9.3.12.13 admin **** 0102\n")
    assert r2["ok"] is True and r2["machines"][0]["password"] == MASK
    assert calls[0] == ("http://bmon/api/machines", {"text": "bad"}, 10)


def test_remote_live_maps_404_502_without_raising(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        if "unknown" in url:
            return FakeResponse(404, {"ok": False, "error": "未知机器: unknown"})
        if "bad" in url:
            return FakeResponse(502, {"ok": False, "error": "boom"})
        return FakeResponse(200, {"ok": True, "ts": 1, "readings": []})

    monkeypatch.setattr("bmon.tui.datasource.requests.get", fake_get)
    ds = RemoteDataSource("http://bmon")
    assert ds.live("unknown") == {"ok": False, "error": "未知机器: unknown"}
    assert ds.live("bad") == {"ok": False, "error": "boom"}
    assert ds.live("good")["ok"] is True


def test_remote_live_quotes_alias(monkeypatch):
    # 别名含 / 与空格等保留字符时必须按路径段整体转义,否则 URL 被
    # 拆歪、打到非预期的路由
    seen = []

    def fake_get(url, params=None, timeout=None):
        seen.append(url)
        return FakeResponse(200, {"ok": True, "ts": 1, "readings": []})

    monkeypatch.setattr("bmon.tui.datasource.requests.get", fake_get)
    assert RemoteDataSource("http://bmon").live("01/02 x")["ok"] is True
    assert seen == ["http://bmon/api/live/01%2F02%20x/sensors"]


def test_remote_live_other_failures_raise(monkeypatch):
    # serve 500 等非业务状态:维持 _get 的抛出语义;连接拒绝等传输错误同样抛
    monkeypatch.setattr("bmon.tui.datasource.requests.get",
                        lambda url, params=None, timeout=None:
                        FakeResponse(500, {}))
    with pytest.raises(requests.HTTPError):
        RemoteDataSource("http://bmon").live("x")

    def refused(url, params=None, timeout=None):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr("bmon.tui.datasource.requests.get", refused)
    with pytest.raises(requests.ConnectionError):
        RemoteDataSource("http://bmon").live("x")


def test_cli_wires_local_and_remote_datasource(monkeypatch, tmp_path):
    from bmon import cli

    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[storage]\ndb_path = "{tmp_path}/t.db"\n'
                   f'[machines]\nfile = "{tmp_path}/machines.txt"\n',
                   encoding="utf-8")
    captured = {}

    class FakeApp:
        def __init__(self, ds):
            captured["ds"] = ds

        def run(self):
            pass

    monkeypatch.setattr("bmon.tui.app.BmonApp", FakeApp)
    assert cli.main(["tui", "--config", str(cfg)]) == 0
    ds = captured["ds"]
    assert isinstance(ds, LocalDataSource)
    assert ds.db_path == str(tmp_path / "t.db")
    assert ds.machines_path == str(tmp_path / "machines.txt")

    assert cli.main(["tui", "--remote", "http://srv:8102"]) == 0
    assert isinstance(captured["ds"], RemoteDataSource)
    assert captured["ds"].base == "http://srv:8102"
