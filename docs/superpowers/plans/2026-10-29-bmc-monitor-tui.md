# 0102 BMC 监控 TUI 工具 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 0102 服务器构建秒级 BMC+OS 监控系统:Redfish 采集 → SQLite → Textual TUI 图表 + FastAPI 对外 REST/内置 Web 页。

**Architecture:** 三个进程共享一个 SQLite(WAL)文件:`bmon-collect` 守护进程(唯一写入方:1s 采集 Redfish+OS 汇总、10s 每核、60s SEL/SDC、5min 聚合、1h 清理);`bmon-serve` 守护进程(FastAPI REST + 服务端 SVG Web 页);`bmon tui`(Textual,本机直读 DB,或 `--remote` 走 HTTP)。

**Tech Stack:** Python 3.11.6(系统 `python3`)、sqlite3(标准库)、requests、FastAPI+uvicorn、Textual+plotext、pytest。

**设计文档:** `docs/superpowers/specs/2026-10-29-bmc-monitor-tui-design.md`(已获用户批准,含 2026-10-29 频率修正)

## Global Constraints

- 项目根目录 `/home/sdc/wangxu/monitor`,venv 位于 `.venv`(用系统 python3 创建);所有命令在项目根执行
- 依赖:`requests`、`fastapi`、`uvicorn`、`textual`、`plotext`、`httpx`(TestClient 需要);开发依赖 `pytest`、`pytest-asyncio`
- BMC:`https://9.3.12.13`,用户 `Administrator`,密码 `******`,`verify=False`
- Redfish 端点(2026-10-29 实测):`/redfish/v1/Chassis/1/ThresholdSensors`(76 模拟量)、`/redfish/v1/Chassis/1/DiscreteSensors`(137 状态位)、`/redfish/v1/Chassis/1/Power`(总功耗+PSU 电气)、`/redfish/v1/Systems/1/LogServices/Log1/Entries`(SEL,条目按 Id 单独 GET)
- 本机无 cpufreq sysfs、/proc/cpuinfo 无 MHz、BMC 无频率传感器 → **不采集动态频率**;静态 Kunpeng920_5250×4@2600MHz 仅展示
- 节奏:1s(BMC+OS 汇总)、10s(每核 192 核占用)、60s(SEL/SDC 日志)、300s(1m 聚合)、3600s(清理),全部可配置
- 保留:raw 48h(172800s)、rollup_1m 35d(3024000s)
- 服务:`0.0.0.0:8102`,无鉴权,CORS 全开
- 时间戳:统一 `int(time.time())` epoch 秒
- sensor_id:`zlib.crc32(f"{source}:{name}".encode())`,source ∈ {"bmc","psu","os"}
- SQLite:WAL、采集器唯一写入方、读方只读连接;传感器分组列名用 `grp`(避开 SQL 关键字 GROUP)
- 每任务 TDD:先写失败测试→实现→通过→提交;git 每任务至少一个 commit,提交信息中文
- 真实 BMC/网络测试仅当环境变量 `BMC_LIVE=1` 时运行(`pytest.mark.skipif`)
- 配置文件 `config.toml` 在项目根,节/键与 `bmon/config.py` 的映射一致
- 测试命令统一用 `.venv/bin/pytest`(不是裸 pytest)

## 文件结构

```
bmon/
├── __init__.py        # 版本号
├── config.py          # Config dataclass + load_config
├── db.py              # 连接/schema/传感器注册/读写/聚合/清理/SEL/SDC
├── bmc.py             # SensorReading、Redfish 解析器与 BmcClient
├── osmetrics.py       # /proc 解析与 OsMetrics 采样器
├── sdclog.py          # stress-ng 汇总日志解析
├── collect.py         # 采集守护进程(Collector + main)
├── queries.py         # overview/sensors/history/extremes/sel/sdc 查询(serve 与 TUI 共用)
├── serve.py           # FastAPI 应用 + main
├── webpage.py         # 服务端渲染 HTML+SVG(Web 页)
├── cli.py             # bmon 入口(tui 子命令)
└── tui/
    ├── __init__.py
    ├── datasource.py  # LocalDataSource / RemoteDataSource
    ├── app.py         # BmonApp(Textual)
    └── views.py       # KpiBar / GroupView / CoreView / SdcView / SelView
tests/
├── conftest.py        # db_path / seeded_db fixtures
├── fixtures/*.json    # Redfish 真实响应快照(采集自 BMC)
└── test_*.py
deploy/
├── bmon-collect.service
└── bmon-serve.service
```

---

### Task 1: 项目脚手架与配置加载

**Files:**
- Create: `pyproject.toml`、`config.toml`、`README.md`(占位)、`bmon/__init__.py`、`bmon/config.py`、`tests/__init__.py`(空)、`tests/conftest.py`(空)、`tests/test_config.py`

**Interfaces:**
- Produces: `bmon.config.load_config(path: str | None = None) -> Config`;`Config` 字段(后续任务引用):`bmc_base`、`bmc_user`、`bmc_password`、`bmc_verify_tls`、`db_path`、`raw_keep_s`、`rollup_keep_s`、`interval_core_s`、`interval_slow_s`、`interval_rollup_s`、`interval_prune_s`、`serve_host`、`serve_port`、`sdc_log_path`、`disk_path`、`node_id`

- [ ] **Step 1: 创建 venv 并安装骨架依赖**

```bash
cd /home/sdc/wangxu/monitor
python3 -m venv .venv
.venv/bin/pip install -U pip
```

- [ ] **Step 2: 写 `pyproject.toml`**

```toml
[project]
name = "bmon"
version = "0.1.0"
description = "0102 server BMC monitor: collect, store, TUI, serve"
requires-python = ">=3.11"
dependencies = [
  "requests>=2.28",
  "fastapi>=0.110",
  "uvicorn>=0.29",
  "textual>=0.50",
  "plotext>=5.0",
  "httpx>=0.27",
]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.23"]

[project.scripts]
bmon-collect = "bmon.collect:main"
bmon-serve = "bmon.serve:main"
bmon = "bmon.cli:main"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools]
packages = ["bmon", "bmon.tui"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 3: 写 `config.toml`**

```toml
[server]
node_id = "0102"

[bmc]
base = "https://9.3.12.13"
user = "Administrator"
password = "******"
verify_tls = false

[storage]
db_path = "data/bmon.db"
raw_keep_s = 172800      # raw 保留 48h
rollup_keep_s = 3024000  # 聚合保留 35d

[collect]
interval_core_s = 10      # 每核占用入库节奏
interval_slow_s = 60       # SEL / SDC 日志
interval_rollup_s = 300    # 1m 聚合
interval_prune_s = 3600    # 清理

[serve]
host = "0.0.0.0"
port = 8102

[sdc]
log_path = "/home/wangxu/sdc-stressng/sdc_stress_summary.log"

[os]
disk_path = "/"
```

- [ ] **Step 4: 写失败测试 `tests/test_config.py`**

```python
from bmon.config import load_config


def test_defaults_without_file(tmp_path):
    cfg = load_config(str(tmp_path / "missing.toml"))
    assert cfg.bmc_base == "https://9.3.12.13"
    assert cfg.bmc_user == "Administrator"
    assert cfg.bmc_password == "******"
    assert cfg.db_path == "data/bmon.db"
    assert cfg.raw_keep_s == 172800
    assert cfg.rollup_keep_s == 3024000
    assert cfg.interval_core_s == 10
    assert cfg.serve_host == "0.0.0.0"
    assert cfg.serve_port == 8102
    assert cfg.sdc_log_path == "/home/wangxu/sdc-stressng/sdc_stress_summary.log"


def test_load_project_config():
    cfg = load_config("config.toml")  # 项目根的真实配置
    assert cfg.node_id == "0102"
    assert cfg.bmc_verify_tls is False
    assert cfg.disk_path == "/"


def test_partial_override_keeps_defaults(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[serve]\nport = 9999\n')
    cfg = load_config(str(p))
    assert cfg.serve_port == 9999
    assert cfg.bmc_base == "https://9.3.12.13"
```

- [ ] **Step 5: 安装可编辑包 + pytest,运行测试确认失败**

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest tests/test_config.py -v
```

Expected: FAIL(`ModuleNotFoundError: bmon.config`)

- [ ] **Step 6: 实现 `bmon/__init__.py` 与 `bmon/config.py`**

`bmon/__init__.py`:
```python
__version__ = "0.1.0"
```

`bmon/config.py`:
```python
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = "config.toml"


@dataclass
class Config:
    node_id: str = "0102"
    bmc_base: str = "https://9.3.12.13"
    bmc_user: str = "Administrator"
    bmc_password: str = "******"
    bmc_verify_tls: bool = False
    db_path: str = "data/bmon.db"
    raw_keep_s: int = 172800
    rollup_keep_s: int = 3024000
    interval_core_s: int = 10
    interval_slow_s: int = 60
    interval_rollup_s: int = 300
    interval_prune_s: int = 3600
    serve_host: str = "0.0.0.0"
    serve_port: int = 8102
    sdc_log_path: str = "/home/wangxu/sdc-stressng/sdc_stress_summary.log"
    disk_path: str = "/"


def load_config(path: str | None = None) -> Config:
    """加载 config.toml;文件不存在/缺键时使用默认值。"""
    cfg = Config()
    p = Path(path or DEFAULT_CONFIG_PATH)
    if not p.exists():
        return cfg
    raw = tomllib.loads(p.read_text())

    def take(section: str, key: str, field: str) -> None:
        v = raw.get(section, {}).get(key)
        if v is not None:
            setattr(cfg, field, v)

    take("server", "node_id", "node_id")
    take("bmc", "base", "bmc_base")
    take("bmc", "user", "bmc_user")
    take("bmc", "password", "bmc_password")
    take("bmc", "verify_tls", "bmc_verify_tls")
    take("storage", "db_path", "db_path")
    take("storage", "raw_keep_s", "raw_keep_s")
    take("storage", "rollup_keep_s", "rollup_keep_s")
    take("collect", "interval_core_s", "interval_core_s")
    take("collect", "interval_slow_s", "interval_slow_s")
    take("collect", "interval_rollup_s", "interval_rollup_s")
    take("collect", "interval_prune_s", "interval_prune_s")
    take("serve", "host", "serve_host")
    take("serve", "port", "serve_port")
    take("sdc", "log_path", "sdc_log_path")
    take("os", "disk_path", "disk_path")
    return cfg
```

- [ ] **Step 7: 写占位 `README.md`**

```markdown
# bmon — 0102 服务器 BMC 监控

采集 → SQLite → TUI / HTTP API。部署与用法见本文件,实现完成后(Task 12)完善。
```

- [ ] **Step 8: 运行测试确认通过**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: 3 passed

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml config.toml README.md bmon/ tests/
git commit -m "feat: 项目脚手架与配置加载"
```

---

### Task 2: DB 基础层(连接、schema、传感器注册)

**Files:**
- Create: `bmon/db.py`、`tests/test_db.py`

**Interfaces:**
- Consumes: 无(纯 sqlite3)
- Produces(后续任务依赖的精确签名):
  - `sensor_id(source: str, name: str) -> int`
  - `connect(path: str, readonly: bool = False) -> sqlite3.Connection`
  - `init_db(conn) -> None`
  - `upsert_sensor(conn, source: str, name: str, grp: str, unit: str = "", thresholds: dict | None = None) -> int`
  - `list_sensors(conn, group: str | None = None) -> list[dict]`
  - `get_sensor(conn, sid: int) -> dict | None`
  - `sensors` 表列:`id, source, name, grp, unit, thr_upper_nc, thr_upper_crit, thr_upper_fatal, thr_lower_nc, thr_lower_crit, thr_lower_fatal, last_value, last_ts, last_status`

- [ ] **Step 1: 写失败测试 `tests/test_db.py`**

```python
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
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_db.py -v`
Expected: FAIL(`cannot import name 'connect'`)

- [ ] **Step 3: 实现 `bmon/db.py`(本任务只含基础部分,Task 3 追加读写)**

```python
import sqlite3
import zlib

SCHEMA = """
CREATE TABLE IF NOT EXISTS sensors (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  name TEXT NOT NULL,
  grp TEXT NOT NULL,
  unit TEXT NOT NULL DEFAULT '',
  thr_upper_nc REAL, thr_upper_crit REAL, thr_upper_fatal REAL,
  thr_lower_nc REAL, thr_lower_crit REAL, thr_lower_fatal REAL,
  last_value REAL, last_ts INTEGER, last_status TEXT,
  UNIQUE(source, name)
);
CREATE TABLE IF NOT EXISTS raw (
  sensor_id INTEGER NOT NULL, ts INTEGER NOT NULL, value REAL NOT NULL,
  PRIMARY KEY (sensor_id, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS raw_ts ON raw(ts);
CREATE TABLE IF NOT EXISTS rollup_1m (
  sensor_id INTEGER NOT NULL, ts INTEGER NOT NULL,
  minv REAL NOT NULL, avgv REAL NOT NULL, maxv REAL NOT NULL,
  PRIMARY KEY (sensor_id, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS sel_events (
  id INTEGER PRIMARY KEY, ts INTEGER NOT NULL,
  severity TEXT, event_type TEXT, message TEXT, message_id TEXT, raw TEXT
);
CREATE INDEX IF NOT EXISTS sel_ts ON sel_events(ts);
CREATE TABLE IF NOT EXISTS sdc_runs (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
  log_mtime REAL NOT NULL, parsed_at INTEGER NOT NULL,
  totals TEXT NOT NULL, suites TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sdc_results (
  run_id INTEGER NOT NULL, test TEXT NOT NULL, core INTEGER NOT NULL,
  pass INTEGER DEFAULT 0, fail INTEGER DEFAULT 0, interrupt INTEGER DEFAULT 0,
  invalid INTEGER DEFAULT 0, skip INTEGER DEFAULT 0,
  PRIMARY KEY (run_id, test, core)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def sensor_id(source: str, name: str) -> int:
    return zlib.crc32(f"{source}:{name}".encode())


def connect(path: str, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    else:
        conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(conn) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def upsert_sensor(conn, source: str, name: str, grp: str, unit: str = "",
                  thresholds: dict | None = None) -> int:
    sid = sensor_id(source, name)
    t = thresholds or {}
    conn.execute(
        """INSERT INTO sensors (id, source, name, grp, unit,
             thr_upper_nc, thr_upper_crit, thr_upper_fatal,
             thr_lower_nc, thr_lower_crit, thr_lower_fatal)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(source, name) DO UPDATE SET
             grp=excluded.grp, unit=excluded.unit,
             thr_upper_nc=excluded.thr_upper_nc,
             thr_upper_crit=excluded.thr_upper_crit,
             thr_upper_fatal=excluded.thr_upper_fatal,
             thr_lower_nc=excluded.thr_lower_nc,
             thr_lower_crit=excluded.thr_lower_crit,
             thr_lower_fatal=excluded.thr_lower_fatal""",
        (sid, source, name, grp, unit,
         t.get("upper_nc"), t.get("upper_crit"), t.get("upper_fatal"),
         t.get("lower_nc"), t.get("lower_crit"), t.get("lower_fatal")))
    conn.commit()
    return sid


def list_sensors(conn, group: str | None = None) -> list[dict]:
    if group:
        rows = conn.execute(
            "SELECT * FROM sensors WHERE grp=? ORDER BY name", (group,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM sensors ORDER BY grp, name").fetchall()
    return [dict(r) for r in rows]


def get_sensor(conn, sid: int) -> dict | None:
    r = conn.execute("SELECT * FROM sensors WHERE id=?", (sid,)).fetchone()
    return dict(r) if r else None
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_db.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add bmon/db.py tests/test_db.py
git commit -m "feat: DB 连接/schema/传感器注册"
```

---

### Task 3: DB 读写层(入库、桶查询、聚合、清理、SEL、SDC)

**Files:**
- Modify: `bmon/db.py`(追加函数)
- Modify: `tests/test_db.py`(追加测试)

**Interfaces:**
- Consumes: Task 2 的全部
- Produces(后续任务依赖的精确签名):
  - `insert_readings(conn, ts: int, samples: Iterable[tuple[int, float]]) -> None`
  - `update_latest(conn, ts: int, samples: Iterable[tuple[int, float, str]]) -> None`(三元组:sid、值、状态)
  - `query_series(conn, sid: int, since: int, until: int, max_points: int = 300) -> list[tuple[int, float]]`(≤25h 用 raw/1s 粒度,更长用 rollup_1m/60s 粒度;返回 [(桶起点ts, 平均值)])
  - `rollup_window(conn, since: int, until: int) -> None`(raw → rollup_1m 按分钟聚合,幂等)
  - `prune(conn, now_ts: int, raw_keep_s: int, rollup_keep_s: int, batch: int = 100000) -> tuple[int, int]`
  - `group_extremes(conn, sids: list[int], since: int, until: int) -> dict[int, tuple[float, float]]`(≤6h 用 raw,>6h 用 rollup)
  - `insert_sel_events(conn, entries: list[dict]) -> int`(dict 键:`id, ts, severity, event_type, message, message_id, raw`;按 id 幂等)
  - `max_sel_id(conn) -> int`、`list_sel_events(conn, limit: int = 100) -> list[dict]`
  - `save_sdc_run(conn, summary: dict) -> int`(summary 键:`log_mtime, parsed_at, totals, suites, per_core`;整体替换)
  - `latest_sdc(conn) -> dict | None`(返回 `{parsed_at, log_mtime, totals, suites, per_core}`)

- [ ] **Step 1: 追加失败测试到 `tests/test_db.py`**

```python
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
    pts = query_series(conn, sid, 0, 100_001)
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
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_db.py -v`
Expected: 新增测试 FAIL(ImportError),原 2 个仍 PASS

- [ ] **Step 3: 在 `bmon/db.py` 追加实现(文件顶部补 `import json, math`)**

```python
import json
import math


def insert_readings(conn, ts: int, samples) -> None:
    """samples: [(sensor_id, value)];同秒重复写幂等。"""
    conn.executemany(
        "INSERT OR REPLACE INTO raw (sensor_id, ts, value) VALUES (?,?,?)",
        [(sid, ts, v) for sid, v in samples])
    conn.commit()


def update_latest(conn, ts: int, samples) -> None:
    """samples: [(sensor_id, value, status)]。"""
    conn.executemany(
        "UPDATE sensors SET last_value=?, last_ts=?, last_status=? WHERE id=?",
        [(v, ts, st, sid) for sid, v, st in samples])
    conn.commit()


def query_series(conn, sid: int, since: int, until: int,
                 max_points: int = 300) -> list[tuple[int, float]]:
    """范围 ≤25h 读 raw(1s),更长读 rollup_1m(60s);桶聚合并降采样。"""
    span = until - since
    if span <= 0:
        return []
    table, step = ("raw", 1) if span <= 25 * 3600 else ("rollup_1m", 60)
    bucket = step * max(1, math.ceil(span / step / max_points))
    rows = conn.execute(
        f"SELECT (ts/{bucket})*{bucket} AS bts, AVG(value) AS v "
        f"FROM {table} WHERE sensor_id=? AND ts BETWEEN ? AND ? "
        "GROUP BY bts ORDER BY bts", (sid, since, until)).fetchall()
    return [(r["bts"], round(r["v"], 4)) for r in rows]


def rollup_window(conn, since: int, until: int) -> None:
    """把 raw 的 [since, until) 聚合进 rollup_1m,按分钟桶,幂等。"""
    conn.execute(
        """INSERT OR REPLACE INTO rollup_1m (sensor_id, ts, minv, avgv, maxv)
           SELECT sensor_id, (ts/60)*60, MIN(value), AVG(value), MAX(value)
           FROM raw WHERE ts >= ? AND ts < ?
           GROUP BY sensor_id, (ts/60)*60""", (since, until))
    conn.commit()


def prune(conn, now_ts: int, raw_keep_s: int, rollup_keep_s: int,
          batch: int = 100_000) -> tuple[int, int]:
    total_raw = total_roll = 0
    while True:
        cur = conn.execute(
            "DELETE FROM raw WHERE ts IN (SELECT ts FROM raw WHERE ts < ? LIMIT ?)",
            (now_ts - raw_keep_s, batch)).rowcount
        conn.commit()
        total_raw += cur
        if cur < batch:
            break
    while True:
        cur = conn.execute(
            "DELETE FROM rollup_1m WHERE ts IN "
            "(SELECT ts FROM rollup_1m WHERE ts < ? LIMIT ?)",
            (now_ts - rollup_keep_s, batch)).rowcount
        conn.commit()
        total_roll += cur
        if cur < batch:
            break
    return total_raw, total_roll


def group_extremes(conn, sids: list[int], since: int, until: int) -> dict:
    """一组传感器的范围极值:≤6h 用 raw,更长用 rollup(聚合滞后 ≤5min 可接受)。"""
    if not sids:
        return {}
    ph = ",".join("?" * len(sids))
    if until - since <= 6 * 3600:
        rows = conn.execute(
            f"SELECT sensor_id, MIN(value), MAX(value) FROM raw "
            f"WHERE sensor_id IN ({ph}) AND ts BETWEEN ? AND ? GROUP BY sensor_id",
            (*sids, since, until)).fetchall()
    else:
        rows = conn.execute(
            f"SELECT sensor_id, MIN(minv), MAX(maxv) FROM rollup_1m "
            f"WHERE sensor_id IN ({ph}) AND ts BETWEEN ? AND ? GROUP BY sensor_id",
            (*sids, since, until)).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def insert_sel_events(conn, entries: list[dict]) -> int:
    n = 0
    for e in entries:
        n += conn.execute(
            """INSERT OR IGNORE INTO sel_events
                 (id, ts, severity, event_type, message, message_id, raw)
               VALUES (?,?,?,?,?,?,?)""",
            (e["id"], e["ts"], e.get("severity"), e.get("event_type"),
             e.get("message"), e.get("message_id"), e.get("raw"))).rowcount
    conn.commit()
    return n


def max_sel_id(conn) -> int:
    r = conn.execute("SELECT MAX(id) FROM sel_events").fetchone()
    return r[0] or 0


def list_sel_events(conn, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        "SELECT id, ts, severity, event_type, message, message_id FROM sel_events "
        "ORDER BY ts DESC, id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def save_sdc_run(conn, summary: dict) -> int:
    conn.execute("DELETE FROM sdc_results")
    conn.execute("DELETE FROM sdc_runs")
    cur = conn.execute(
        "INSERT INTO sdc_runs (log_mtime, parsed_at, totals, suites) VALUES (?,?,?,?)",
        (summary["log_mtime"], summary["parsed_at"],
         json.dumps(summary["totals"]), json.dumps(summary["suites"])))
    run_id = cur.lastrowid
    rows = []
    for core_s, tests in summary["per_core"].items():
        for test, c in tests.items():
            rows.append((run_id, test, int(core_s), c.get("pass", 0), c.get("fail", 0),
                         c.get("interrupt", 0), c.get("invalid", 0), c.get("skip", 0)))
    conn.executemany(
        "INSERT OR REPLACE INTO sdc_results "
        "(run_id, test, core, pass, fail, interrupt, invalid, skip) VALUES (?,?,?,?,?,?,?,?)",
        rows)
    conn.commit()
    return run_id


def latest_sdc(conn) -> dict | None:
    r = conn.execute("SELECT * FROM sdc_runs ORDER BY run_id DESC LIMIT 1").fetchone()
    if not r:
        return None
    per_core: dict = {}
    for row in conn.execute(
            "SELECT test, core, pass, fail, interrupt, invalid, skip "
            "FROM sdc_results WHERE run_id=?", (r["run_id"],)):
        per_core.setdefault(str(row["core"]), {})[row["test"]] = {
            "pass": row["pass"], "fail": row["fail"], "interrupt": row["interrupt"],
            "invalid": row["invalid"], "skip": row["skip"]}
    return {"parsed_at": r["parsed_at"], "log_mtime": r["log_mtime"],
            "totals": json.loads(r["totals"]), "suites": json.loads(r["suites"]),
            "per_core": per_core}
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_db.py -v`
Expected: 9 passed(2 旧 + 7 新)

- [ ] **Step 5: Commit**

```bash
git add bmon/db.py tests/test_db.py
git commit -m "feat: DB 读写/桶查询/聚合/清理/SEL/SDC"
```

---

### Task 4: BMC Redfish 客户端

**Files:**
- Create: `bmon/bmc.py`、`tests/test_bmc.py`、`tests/fixtures/*.json`(4 个真实响应快照)

**Interfaces:**
- Produces:
  - `@dataclass SensorReading`:字段 `source: str, name: str, value: float, unit: str, grp: str, status: str, thresholds: dict | None = None`(Task 5/7 直接使用)
  - `class BmcError(Exception)`
  - `parse_threshold_sensors(doc: dict) -> list[SensorReading]`(grp 由单位映射:degrees C→temp、Volts→volt、RPM→fan、Watts→power、其他→other;source="bmc")
  - `parse_discrete_sensors(doc: dict) -> list[SensorReading]`(grp="status",unit="status",value=十六进制状态整数)
  - `parse_power(doc: dict) -> list[SensorReading]`(source="psu";"Total Power"→grp power;在役 PSU 的 Input V/Output W/Input W/Output A/Output V → grp psu;忽略 Absent PSU)
  - `parse_sel_entry(doc: dict) -> dict`(键 `id:int, ts:int(epoch秒), severity, event_type, message, message_id, raw:json串`)
  - `class BmcClient(base, user, password, verify=False)`:`get_all_readings() -> list[SensorReading]`、`get_sel_entries(since_id: int = 0) -> list[dict]`

- [ ] **Step 1: 抓取真实响应作为离线 fixture**

```bash
mkdir -p tests/fixtures
curl -sk -u Administrator:****** \
  https://9.3.12.13/redfish/v1/Chassis/1/ThresholdSensors -o tests/fixtures/threshold_sensors.json
curl -sk -u Administrator:****** \
  https://9.3.12.13/redfish/v1/Chassis/1/DiscreteSensors -o tests/fixtures/discrete_sensors.json
curl -sk -u Administrator:****** \
  https://9.3.12.13/redfish/v1/Chassis/1/Power -o tests/fixtures/power.json
curl -sk -u Administrator:****** \
  https://9.3.12.13/redfish/v1/Systems/1/LogServices/Log1/Entries/90 -o tests/fixtures/sel_entry.json
python3 -c "import json
for f in ['threshold_sensors','discrete_sensors','power','sel_entry']:
    json.load(open(f'tests/fixtures/{f}.json'))
print('fixtures OK')"
```

- [ ] **Step 2: 写失败测试 `tests/test_bmc.py`**

```python
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
    assert cpu.thresholds["upper_crit"] is not None
    assert cpu.source == "bmc"
    fans = [r for r in rs if r.unit == "RPM"]
    assert fans and fans[0].grp == "fan"
    volts = [r for r in rs if r.unit == "Volts"]
    assert volts and volts[0].grp == "volt"
    watts = [r for r in rs if r.unit == "Watts"]
    assert watts and watts[0].grp == "power"


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


def test_client_error_raises_bmc_error():
    c = BmcClient("https://127.0.0.1:1", "u", "p", verify=False)
    with pytest.raises(BmcError):
        c.get_all_readings()


@pytest.mark.skipif(os.getenv("BMC_LIVE") != "1", reason="需要真实 BMC")
def test_live_all_readings():
    c = BmcClient("https://9.3.12.13", "Administrator", "******")
    rs = c.get_all_readings()
    assert len(rs) >= 200


@pytest.mark.skipif(os.getenv("BMC_LIVE") != "1", reason="需要真实 BMC")
def test_live_sel_entries():
    c = BmcClient("https://9.3.12.13", "Administrator", "******")
    entries = c.get_sel_entries(0)
    assert entries and entries[0]["id"] > 0
    assert c.get_sel_entries(entries[0]["id"]) == []  # 增量为空
```

注:文件顶部需要 `import json`(fixture 加载)。

- [ ] **Step 3: 运行确认失败**

Run: `.venv/bin/pytest tests/test_bmc.py -v`
Expected: FAIL(ImportError)

- [ ] **Step 4: 实现 `bmon/bmc.py`**

```python
import json
from dataclasses import dataclass
from datetime import datetime

import requests
import urllib3


@dataclass
class SensorReading:
    source: str
    name: str
    value: float
    unit: str
    grp: str
    status: str
    thresholds: dict | None = None


class BmcError(Exception):
    pass


_GROUP_BY_UNIT = {"degrees C": "temp", "Volts": "volt", "RPM": "fan",
                  "Watts": "power"}


def parse_threshold_sensors(doc: dict) -> list[SensorReading]:
    out = []
    for s in doc.get("Sensors", []):
        out.append(SensorReading(
            source="bmc", name=s["Name"],
            value=float(s.get("ReadingValue", 0) or 0),
            unit=s.get("Unit", ""),
            grp=_GROUP_BY_UNIT.get(s.get("Unit", ""), "other"),
            status=str(s.get("Status", "")),
            thresholds={
                "upper_nc": s.get("UpperThresholdNonCritical"),
                "upper_crit": s.get("UpperThresholdCritical"),
                "upper_fatal": s.get("UpperThresholdFatal"),
                "lower_nc": s.get("LowerThresholdNonCritical"),
                "lower_crit": s.get("LowerThresholdCritical"),
                "lower_fatal": s.get("LowerThresholdFatal")}))
    return out


def parse_discrete_sensors(doc: dict) -> list[SensorReading]:
    out = []
    for s in doc.get("Sensors", []):
        status = str(s.get("Status", ""))
        try:
            value = float(int(status, 16)) if status.lower().startswith("0x") else 0.0
        except ValueError:
            value = 0.0
        out.append(SensorReading(source="bmc", name=s["Name"], value=value,
                                 unit="status", grp="status", status=status))
    return out


def parse_power(doc: dict) -> list[SensorReading]:
    out = []
    pc = (doc.get("PowerControl") or [{}])[0]
    if pc.get("PowerConsumedWatts") is not None:
        out.append(SensorReading(
            source="psu", name="Total Power",
            value=float(pc["PowerConsumedWatts"]), unit="Watts",
            grp="power", status="ok"))
    for ps in doc.get("PowerSupplies") or []:
        if (ps.get("Status") or {}).get("State") != "Enabled":
            continue  # PSU1 是 Absent
        nm = ps.get("Name", "PSU")
        oem = (ps.get("Oem") or {}).get("Public") or {}
        fields = [("Input V", ps.get("LineInputVoltage"), "Volts"),
                  ("Output W", ps.get("PowerOutputWatts"), "Watts"),
                  ("Input W", oem.get("PowerInputWatts"), "Watts"),
                  ("Output A", oem.get("OutputAmperage"), "Amps"),
                  ("Output V", oem.get("OutputVoltage"), "Volts")]
        for label, v, unit in fields:
            if v is not None:
                out.append(SensorReading(
                    source="psu", name=f"{nm} {label}", value=float(v),
                    unit=unit, grp="psu", status="ok"))
    return out


def parse_sel_entry(doc: dict) -> dict:
    ts_str = doc.get("EventTimestamp") or doc.get("Created") or ""
    ts = int(datetime.fromisoformat(ts_str).timestamp()) if ts_str else 0
    return {"id": int(doc["Id"]), "ts": ts, "severity": doc.get("Severity", ""),
            "event_type": doc.get("EventType", ""), "message": doc.get("Message", ""),
            "message_id": doc.get("MessageId", ""),
            "raw": json.dumps(doc, ensure_ascii=False)}


class BmcClient:
    def __init__(self, base: str, user: str, password: str, verify: bool = False):
        self.base = base.rstrip("/")
        self._s = requests.Session()
        self._s.auth = (user, password)
        self._s.verify = verify
        self._s.headers["Accept"] = "application/json"
        if not verify:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def _get(self, path: str) -> dict:
        try:
            r = self._s.get(f"{self.base}{path}", timeout=8)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            raise BmcError(f"GET {path} 失败: {e}") from e

    def get_all_readings(self) -> list[SensorReading]:
        readings = []
        readings += parse_threshold_sensors(
            self._get("/redfish/v1/Chassis/1/ThresholdSensors"))
        readings += parse_discrete_sensors(
            self._get("/redfish/v1/Chassis/1/DiscreteSensors"))
        readings += parse_power(self._get("/redfish/v1/Chassis/1/Power"))
        return readings

    def get_sel_entries(self, since_id: int = 0) -> list[dict]:
        doc = self._get("/redfish/v1/Systems/1/LogServices/Log1/Entries")
        ids = []
        for m in doc.get("Members", []):
            tail = m.get("@odata.id", "").rstrip("/").rsplit("/", 1)[-1]
            if tail.isdigit():
                ids.append(int(tail))
        new = sorted(i for i in ids if i > since_id)[-200:]  # 首次同步限 200 条
        return [parse_sel_entry(
                    self._get(f"/redfish/v1/Systems/1/LogServices/Log1/Entries/{i}"))
                for i in new]
```

- [ ] **Step 5: 运行确认通过(含一次 LIVE 验证)**

```bash
.venv/bin/pytest tests/test_bmc.py -v
BMC_LIVE=1 .venv/bin/pytest tests/test_bmc.py -v -k live
```
Expected: 离线 5 passed;live 2 passed(BMC 在线)

- [ ] **Step 6: Commit**

```bash
git add bmon/bmc.py tests/test_bmc.py tests/fixtures/
git commit -m "feat: BMC Redfish 客户端(传感器/PSU/SEL 解析)"
```

---

### Task 5: OS 指标采集

**Files:**
- Create: `bmon/osmetrics.py`、`tests/test_osmetrics.py`

**Interfaces:**
- Consumes: `bmon.bmc.SensorReading`(复用同一数据类,不产生循环依赖)
- Produces:
  - `parse_proc_stat(text: str) -> list[tuple[int, int]]`(每核 `(idle_jiffies, total_jiffies)`,跳过 `cpu` 汇总行;idle=第4列+第5列 iowait)
  - `core_usage(prev, cur) -> list[float]`(每核占用百分比,保留2位)
  - `parse_meminfo(text: str) -> dict`(键如 `MemTotal`/`MemAvailable`/`SwapTotal`/`SwapFree`,单位字节,kB×1024)
  - `parse_loadavg(text: str) -> tuple[float, float, float]`
  - `disk_usage_gb(path: str = "/") -> tuple[float, float, float]`(total_gb, used_gb, usage_pct)
  - `class OsMetrics(disk_path="/")`:`tick() -> list[SensorReading]`(source="os";第一拍无 CPU delta、不含 cpu_* 两项;含 mem×7、load×3、disk×3 共 13 项 + cpu_avg_usage/cpu_max_core_usage)、`per_core() -> list[SensorReading]`(名称 `cpu{i}_usage`,grp="core",来自最近一次 tick 的差分)、`static_info() -> dict`
  - `CPU_MODEL = "Kunpeng920_5250 ×4 @2.6GHz"`

- [ ] **Step 1: 写失败测试 `tests/test_osmetrics.py`**

```python
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


def test_core_usage_pct():
    assert core_usage([(70, 100), (60, 100)], [(75, 100), (80, 100)]) == [5.0, 20.0]
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
    assert len(cores) == 192            # 本机 192 核
    assert cores[0].name == "cpu0_usage" and cores[0].grp == "core"
    assert all(0.0 <= c.value <= 100.0 for c in cores)


def test_static_info():
    info = OsMetrics.static_info()
    assert info["cores"] == 192
    assert info["mem_total_gb"] > 700
    assert "Kunpeng" in CPU_MODEL and info["hostname"] == "localhost0102"
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_osmetrics.py -v`
Expected: FAIL(ImportError)

- [ ] **Step 3: 实现 `bmon/osmetrics.py`**

```python
import os
import socket
from pathlib import Path

from .bmc import SensorReading

CPU_MODEL = "Kunpeng920_5250 ×4 @2.6GHz"


def parse_proc_stat(text: str) -> list[tuple[int, int]]:
    """每核 (idle_jiffies, total_jiffies);idle=第4列+第5列(iowait)。"""
    cores = []
    for line in text.splitlines():
        if not line.startswith("cpu") or not line[3:4].isdigit():
            continue  # 跳过 "cpu " 汇总行
        vals = [int(x) for x in line.split()[1:] if x.isdigit()]
        if len(vals) < 5:
            continue
        idle = vals[3] + vals[4]
        cores.append((idle, sum(vals)))
    return cores


def core_usage(prev, cur) -> list[float]:
    out = []
    for (pi, pt), (ci, ct) in zip(prev, cur):
        dt, di = ct - pt, ci - pi
        out.append(round(100.0 * (dt - di) / dt, 2) if dt > 0 else 0.0)
    return out


def parse_meminfo(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            parts = v.split()
            if parts:
                out[k.strip()] = int(parts[0]) * 1024  # kB -> bytes
    return out


def parse_loadavg(text: str) -> tuple[float, float, float]:
    p = text.split()
    return float(p[0]), float(p[1]), float(p[2])


def disk_usage_gb(path: str = "/") -> tuple[float, float, float]:
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used = total - free
    pct = 100.0 * used / total if total else 0.0
    return round(total / 2**30, 1), round(used / 2**30, 1), round(pct, 3)


class OsMetrics:
    def __init__(self, disk_path: str = "/"):
        self.disk_path = disk_path
        self._prev = None
        self._core_pct: list[float] = []

    def tick(self) -> list[SensorReading]:
        readings = []
        cur = parse_proc_stat(Path("/proc/stat").read_text())
        if self._prev is not None and len(cur) == len(self._prev):
            pct = core_usage(self._prev, cur)
            self._core_pct = pct
            readings.append(SensorReading(
                "os", "cpu_avg_usage", round(sum(pct) / len(pct), 2), "%", "cpu", "ok"))
            readings.append(SensorReading(
                "os", "cpu_max_core_usage", round(max(pct), 2), "%", "cpu", "ok"))
        self._prev = cur

        mem = parse_meminfo(Path("/proc/meminfo").read_text())
        total, avail = mem.get("MemTotal", 0), mem.get("MemAvailable", 0)
        used = total - avail
        swap_t, swap_f = mem.get("SwapTotal", 0), mem.get("SwapFree", 0)
        readings += [
            SensorReading("os", "mem_usage_pct",
                          round(100 * used / total, 2) if total else 0.0, "%", "mem", "ok"),
            SensorReading("os", "mem_used_gb", round(used / 2**30, 1), "GB", "mem", "ok"),
            SensorReading("os", "mem_avail_gb", round(avail / 2**30, 1), "GB", "mem", "ok"),
            SensorReading("os", "mem_total_gb", round(total / 2**30, 1), "GB", "mem", "ok"),
            SensorReading("os", "swap_usage_pct",
                          round(100 * (swap_t - swap_f) / swap_t, 3) if swap_t else 0.0,
                          "%", "mem", "ok"),
            SensorReading("os", "swap_used_gb", round((swap_t - swap_f) / 2**30, 2),
                          "GB", "mem", "ok"),
            SensorReading("os", "swap_total_gb", round(swap_t / 2**30, 1), "GB", "mem", "ok"),
        ]

        l1, l5, l15 = parse_loadavg(Path("/proc/loadavg").read_text())
        readings += [
            SensorReading("os", "load_1m", l1, "", "load", "ok"),
            SensorReading("os", "load_5m", l5, "", "load", "ok"),
            SensorReading("os", "load_15m", l15, "", "load", "ok"),
        ]

        d_total, d_used, d_pct = disk_usage_gb(self.disk_path)
        readings += [
            SensorReading("os", "disk_usage_pct", d_pct, "%", "disk", "ok"),
            SensorReading("os", "disk_used_gb", d_used, "GB", "disk", "ok"),
            SensorReading("os", "disk_total_gb", d_total, "GB", "disk", "ok"),
        ]
        return readings

    def per_core(self) -> list[SensorReading]:
        return [SensorReading("os", f"cpu{i}_usage", p, "%", "core", "ok")
                for i, p in enumerate(self._core_pct)]

    @staticmethod
    def static_info() -> dict:
        mem = parse_meminfo(Path("/proc/meminfo").read_text())
        return {"hostname": socket.gethostname(),
                "cores": os.cpu_count() or 0,
                "mem_total_gb": round(mem.get("MemTotal", 0) / 2**30),
                "cpu_model": CPU_MODEL}
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_osmetrics.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add bmon/osmetrics.py tests/test_osmetrics.py
git commit -m "feat: OS 指标采集(/proc 解析与差分采样)"
```

---

### Task 6: SDC 压测日志解析

**Files:**
- Create: `bmon/sdclog.py`、`tests/test_sdclog.py`

**Interfaces:**
- Produces: `parse_sdc_log(path: str) -> dict | None`
  - 文件不存在/不可读 → `None`;无任何可识别行 → `None`
  - 成功返回:`{"log_mtime": float, "totals": {pass,fail,skip,interrupt,invalid,total}, "suites": [{"name", "pass", "fail", "skip", "interrupt", "invalid", "total", "tests": [{"name", ...计数}]}], "per_core": {"<core>": {"<test>": {计数}}}}`
  - v1 日志格式(与参考页面展示字段对齐;真实日志出现后按此适配):`overall`/`suite=`/`test=`/`core=` 行,`key=value` 计数;`interrupted` 视同 `interrupt`,`v` 视同 `invalid`;suite 行缺计数时按其 test 行累加;未知行忽略

- [ ] **Step 1: 写失败测试 `tests/test_sdclog.py`**

```python
from bmon.sdclog import parse_sdc_log

FIXTURE = """=== SDC STRESS SUMMARY 2026-10-29 15:31:18 ===
overall pass=183 fail=0 skip=59 interrupted=88 invalid=88 total=330
suite=eigen
test=eigen_gemm_cdouble_dynamic_square pass=6 fail=0 skip=3 interrupt=3 invalid=4 total=16
test=eigen_svd pass=5 fail=0 skip=10 interrupt=10 invalid=15 total=40
suite=ldr
test=neon_rot_ldr_at_top pass=19 fail=0 skip=1 interrupt=0 invalid=0 total=20
core=0 test=eigen_gemm_cdouble_dynamic_square pass=6 fail=0 interrupt=3 invalid=4
core=0 test=eigen_svd pass=5 fail=0 interrupt=10 invalid=15
core=30 test=eigen_svd pass=0 fail=0 interrupt=9 invalid=1
"""


def test_parse_full(tmp_path):
    p = tmp_path / "sdc.log"
    p.write_text(FIXTURE)
    s = parse_sdc_log(str(p))
    assert s["totals"] == {"pass": 183, "fail": 0, "skip": 59, "interrupt": 88,
                           "invalid": 88, "total": 330}
    by_name = {x["name"]: x for x in s["suites"]}
    assert by_name["eigen"]["pass"] == 11          # 6+5 由 test 累加
    assert by_name["eigen"]["total"] == 56
    assert by_name["ldr"]["tests"][0]["name"] == "neon_rot_ldr_at_top"
    assert s["per_core"]["0"]["eigen_svd"]["invalid"] == 15
    assert s["per_core"]["30"]["eigen_svd"]["pass"] == 0
    assert s["log_mtime"] > 0


def test_missing_file_returns_none(tmp_path):
    assert parse_sdc_log(str(tmp_path / "nope.log")) is None


def test_unrecognized_content_returns_none(tmp_path):
    p = tmp_path / "g.log"
    p.write_text("hello world\nnothing parseable\n")
    assert parse_sdc_log(str(p)) is None
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_sdclog.py -v`
Expected: FAIL(ImportError)

- [ ] **Step 3: 实现 `bmon/sdclog.py`**

```python
import os
import re

_STATUS_KEYS = {"pass": "pass", "fail": "fail", "skip": "skip",
                "interrupt": "interrupt", "interrupted": "interrupt",
                "invalid": "invalid", "v": "invalid", "total": "total"}


def _counts(kv: dict) -> dict:
    c = {}
    for k, v in kv.items():
        key = _STATUS_KEYS.get(k.lower())
        if key:
            try:
                c[key] = int(v)
            except ValueError:
                pass
    return c


def parse_sdc_log(path: str) -> dict | None:
    """解析 stress-ng 汇总日志;缺失/不可读/无有效行返回 None。

    v1 格式:overall / suite= / test= / core= 行,key=value 计数。
    真实日志出现后需按实际格式适配一次(见设计文档风险说明)。
    """
    try:
        mtime = os.path.getmtime(path)
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None

    totals: dict = {}
    suites: dict = {}
    per_core: dict = {}
    cur_suite = None
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        kv = dict(re.findall(r"([A-Za-z_]+)=(\S+)", s))
        if s.startswith("overall"):
            totals = _counts(kv)
        elif s.startswith("suite"):
            name = kv.get("suite", "")
            cur_suite = {"name": name, "tests": [], **_counts(kv)}
            suites[name] = cur_suite
        elif s.startswith("test"):
            if cur_suite is None:
                cur_suite = {"name": "", "tests": []}
                suites[""] = cur_suite
            cur_suite["tests"].append({"name": kv.get("test", ""), **_counts(kv)})
        elif s.startswith("core"):
            core, test = kv.get("core"), kv.get("test")
            if core is None or test is None:
                continue
            per_core.setdefault(core, {})[test] = _counts(kv)

    if not totals and not suites:
        return None
    for suite in suites.values():
        for key in ("pass", "fail", "skip", "interrupt", "invalid", "total"):
            if not suite.get(key):
                suite[key] = sum(t.get(key, 0) for t in suite["tests"])
    return {"log_mtime": mtime, "totals": totals,
            "suites": list(suites.values()), "per_core": per_core}
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_sdclog.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add bmon/sdclog.py tests/test_sdclog.py
git commit -m "feat: SDC 压测日志解析器"
```

---

### Task 7: 采集守护进程

**Files:**
- Create: `bmon/collect.py`、`tests/test_collect.py`

**Interfaces:**
- Consumes: Task 2/3 `db.*`、Task 4 `BmcClient/BmcError/SensorReading`、Task 5 `OsMetrics`、Task 6 `parse_sdc_log`、Task 1 `load_config`
- Produces:
  - `class Collector(cfg, conn, bmc, osm)`:`tick(ts: int) -> None`(单拍:BMC+OS 汇总存库、按计数器跑每核/慢速/聚合/清理);`_register(reading) -> int`;`_store(readings, ts)`;`_poll_sel()`;`_poll_sdc()`
  - `main(argv=None) -> int`(argparse `--config`,信号退出,1s 主循环)

- [ ] **Step 1: 写失败测试 `tests/test_collect.py`**

```python
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
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_collect.py -v`
Expected: FAIL(ImportError)

- [ ] **Step 3: 实现 `bmon/collect.py`**

```python
import argparse
import logging
import signal
import time
from pathlib import Path

from .bmc import BmcClient, BmcError
from .config import load_config
from .db import (connect, init_db, insert_readings, insert_sel_events, max_sel_id,
                 prune, rollup_window, save_sdc_run, update_latest, upsert_sensor)
from .osmetrics import OsMetrics
from .sdclog import parse_sdc_log

log = logging.getLogger("bmon.collect")


class Collector:
    def __init__(self, cfg, conn, bmc, osm):
        self.cfg, self.conn, self.bmc, self.osm = cfg, conn, bmc, osm
        self._ids: dict[tuple[str, str], int] = {}
        self._sdc_mtime = None
        self._sel_max = max_sel_id(conn)
        self._last = {"core": 0, "slow": 0, "rollup": 0, "prune": 0}
        for row in conn.execute("SELECT id, source, name FROM sensors"):
            self._ids[(row["source"], row["name"])] = row["id"]

    def _register(self, reading) -> int:
        key = (reading.source, reading.name)
        if key not in self._ids:
            self._ids[key] = upsert_sensor(
                self.conn, reading.source, reading.name,
                reading.grp, reading.unit, reading.thresholds)
        return self._ids[key]

    def _store(self, readings, ts: int) -> None:
        samples = [(self._register(r), r.value, r.status) for r in readings]
        insert_readings(self.conn, ts, [(sid, v) for sid, v, _ in samples])
        update_latest(self.conn, ts, samples)

    def tick(self, ts: int) -> None:
        try:
            self._store(self.bmc.get_all_readings(), ts)
        except BmcError as e:
            log.warning("BMC 采集失败(数据将标记过期): %s", e)
        try:
            self._store(self.osm.tick(), ts)
        except Exception:
            log.exception("OS 采集失败")

        if ts - self._last["core"] >= self.cfg.interval_core_s:
            self._last["core"] = ts
            try:
                self._store(self.osm.per_core(), ts)
            except Exception:
                log.exception("每核采集失败")

        if ts - self._last["slow"] >= self.cfg.interval_slow_s:
            self._last["slow"] = ts
            self._poll_sel()
            self._poll_sdc()

        if ts - self._last["rollup"] >= self.cfg.interval_rollup_s:
            self._last["rollup"] = ts
            minute = (ts // 60) * 60
            rollup_window(self.conn, minute - self.cfg.interval_rollup_s - 60,
                         minute - 60)

        if ts - self._last["prune"] >= self.cfg.interval_prune_s:
            self._last["prune"] = ts
            n = prune(self.conn, ts, self.cfg.raw_keep_s, self.cfg.rollup_keep_s)
            log.info("清理完成: raw=%d rollup=%d", *n)

    def _poll_sel(self) -> None:
        try:
            entries = self.bmc.get_sel_entries(self._sel_max)
        except BmcError as e:
            log.warning("SEL 拉取失败: %s", e)
            return
        if entries:
            insert_sel_events(self.conn, entries)
            self._sel_max = max(self._sel_max, max(e["id"] for e in entries))

    def _poll_sdc(self) -> None:
        summary = parse_sdc_log(self.cfg.sdc_log_path)
        if not summary or summary["log_mtime"] == self._sdc_mtime:
            return
        self._sdc_mtime = summary["log_mtime"]
        summary["parsed_at"] = int(time.time())
        save_sdc_run(self.conn, summary)
        log.info("SDC 日志已更新并解析: %s", self.cfg.sdc_log_path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bmon-collect")
    ap.add_argument("--config", default="config.toml")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    Path(cfg.db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(cfg.db_path)
    init_db(conn)
    collector = Collector(
        cfg, conn,
        BmcClient(cfg.bmc_base, cfg.bmc_user, cfg.bmc_password, cfg.bmc_verify_tls),
        OsMetrics(cfg.disk_path))

    stop = {"flag": False}

    def _sig(signum, frame):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    log.info("bmon-collect 启动 (db=%s)", cfg.db_path)
    while not stop["flag"]:
        t0 = time.monotonic()
        try:
            collector.tick(int(time.time()))
        except Exception:
            log.exception("tick 异常")
        time.sleep(max(0.0, 1.0 - (time.monotonic() - t0)))
    conn.close()
    log.info("bmon-collect 退出")
    return 0
```

- [ ] **Step 4: 运行确认通过 + 5 秒实采验证**

```bash
.venv/bin/pytest tests/test_collect.py -v
.venv/bin/bmon-collect --config config.toml &   # 实跑
COLLECT_PID=$!
sleep 5
.venv/bin/python -c "
import sqlite3
c = sqlite3.connect('data/bmon.db')
print('raw 行数(5秒):', c.execute('SELECT COUNT(*) FROM raw').fetchone()[0])
print('传感器数:', c.execute('SELECT COUNT(*) FROM sensors').fetchone()[0])
print('最近 last_ts:', c.execute('SELECT MAX(last_ts) FROM sensors').fetchone()[0])"
kill $COLLECT_PID
```
Expected: 测试 3 passed;实采 raw ≈ 20000+(235 序列×5 秒 + 每核 192),传感器数 ≈ 545(76+137+PSU+OS 汇总+192 核)

- [ ] **Step 5: Commit**

```bash
git add bmon/collect.py tests/test_collect.py
git commit -m "feat: 采集守护进程(1s 主循环+定时任务)"
```

---

### Task 8: 查询层与 REST API

**Files:**
- Create: `bmon/queries.py`、`bmon/serve.py`、`tests/conftest.py`(seeded_db fixture)、`tests/test_api.py`

**Interfaces:**
- Consumes: Task 3 `db.*`
- Produces(精确签名,Task 9/10 依赖):
  - `bmon/queries.py`:
    - `RANGES = {"1h":3600, "6h":21600, "12h":43200, "24h":86400, "7d":604800, "30d":2592000}`
    - `KPI_DEFS: list[tuple[str, str, str]]`(source, name, label)
    - `get_overview(conn, now: int | None = None) -> dict`(键 `node_ts, kpis:[{id,name,label,value,unit,ts,age_s,history}], groups:{grp:{count,last_ts}}`;kpis 含 history=近1h 60 桶;PSU POut 附加在末尾)
    - `get_sensors(conn, group: str | None = None) -> list[dict]`(list_sensors 结果 + `age_s`)
    - `get_history(conn, sid: int, range_key: str, max_points: int = 300) -> dict`(键 `sensor:{id,name,unit,grp} | None, points`)
    - `get_extremes(conn, sids: list[int], range_key: str) -> dict[str, dict]`(键 str(sid) → `{"min":…,"max":…}`)
    - `get_sel(conn, limit: int = 100) -> list[dict]`、`get_sdc(conn) -> dict | None`
  - `bmon/serve.py`:`create_app(db_path: str) -> FastAPI`、`main(argv=None) -> int`
  - 路由:`GET /api/overview`、`GET /api/sensors?group=`、`GET /api/history?sensor=<int>&range=<1h..30d>&max_points=`、`GET /api/extremes?sids=1,2&range=`、`GET /api/sel?limit=`、`GET /api/sdc`

- [ ] **Step 1: 写共享 fixture `tests/conftest.py`**

```python
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
    for i in range(120):
        insert_readings(conn, now - 120 + i, [(sid, 50.0 + i)])
    update_latest(conn, now, [(sid, 51.0, "ok")])
    tsid = upsert_sensor(conn, "bmc", "CPU1_TEMP", "temp", "degrees C",
                         {"upper_nc": 78, "upper_crit": 85})
    update_latest(conn, now, [(tsid, 48.0, "ok")])
    insert_sel_events(conn, [{"id": 1, "ts": now, "severity": "OK",
                              "event_type": "Alert", "message": "test event",
                              "message_id": "m1", "raw": "{}"}])
    conn.close()
    yield path, sid
```

- [ ] **Step 2: 写失败测试 `tests/test_api.py`**

```python
from fastapi.testclient import TestClient

from bmon.db import sensor_id
from bmon.serve import create_app


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
```

- [ ] **Step 3: 运行确认失败**

Run: `.venv/bin/pytest tests/test_api.py -v`
Expected: FAIL(ImportError)

- [ ] **Step 4: 实现 `bmon/queries.py`**

```python
import time

from .db import (get_sensor, group_extremes, latest_sdc, list_sel_events,
                 list_sensors, query_series, sensor_id)

RANGES = {"1h": 3600, "6h": 21600, "12h": 43200, "24h": 86400,
          "7d": 604800, "30d": 2592000}

KPI_DEFS = [
    ("os", "cpu_avg_usage", "CPU Avg Usage"),
    ("os", "cpu_max_core_usage", "CPU Max Core Usage"),
    ("os", "mem_usage_pct", "Mem Usage"),
    ("os", "load_1m", "Load 1min"),
    ("os", "disk_usage_pct", "Disk Usage"),
    ("bmc", "Total Power", "Total PWR"),
    ("bmc", "CPU1_TEMP", "CPU1 Temp"),
    ("bmc", "CPU2_TEMP", "CPU2 Temp"),
    ("bmc", "CPU3_TEMP", "CPU3 Temp"),
    ("bmc", "CPU4_TEMP", "CPU4 Temp"),
]


def get_overview(conn, now: int | None = None) -> dict:
    now = int(now or time.time())
    sensors = {s["id"]: s for s in list_sensors(conn)}
    kpis = []
    for source, name, label in KPI_DEFS:
        sid = sensor_id(source, name)
        s = sensors.get(sid)
        if not s or s["last_ts"] is None:
            continue
        kpis.append({"id": sid, "name": name, "label": label,
                     "value": s["last_value"], "unit": s["unit"], "ts": s["last_ts"],
                     "age_s": now - s["last_ts"],
                     "history": query_series(conn, sid, now - RANGES["1h"], now, 60)})
    psus = [s for s in sensors.values()
            if s["grp"] == "psu" and s["name"].endswith("Output W")
            and s["last_ts"] is not None]
    if psus:
        s = max(psus, key=lambda x: x["last_value"] or 0)
        kpis.append({"id": s["id"], "name": s["name"], "label": "PSU POut",
                     "value": s["last_value"], "unit": s["unit"], "ts": s["last_ts"],
                     "age_s": now - s["last_ts"],
                     "history": query_series(conn, s["id"], now - RANGES["1h"], now, 60)})
    groups: dict = {}
    for s in sensors.values():
        g = groups.setdefault(s["grp"], {"count": 0, "last_ts": None})
        g["count"] += 1
        if s["last_ts"] and (g["last_ts"] is None or s["last_ts"] > g["last_ts"]):
            g["last_ts"] = s["last_ts"]
    return {"node_ts": now, "kpis": kpis, "groups": groups}


def get_sensors(conn, group: str | None = None) -> list[dict]:
    now = int(time.time())
    out = []
    for s in list_sensors(conn, group):
        s["age_s"] = now - s["last_ts"] if s["last_ts"] else None
        out.append(s)
    return out


def get_history(conn, sid: int, range_key: str, max_points: int = 300) -> dict:
    now = int(time.time())
    s = get_sensor(conn, sid)
    if not s:
        return {"sensor": None, "points": []}
    return {"sensor": {"id": s["id"], "name": s["name"], "unit": s["unit"],
                       "grp": s["grp"]},
            "points": query_series(conn, sid, now - RANGES[range_key], now,
                                   max_points)}


def get_extremes(conn, sids: list[int], range_key: str) -> dict[str, dict]:
    now = int(time.time())
    ex = group_extremes(conn, sids, now - RANGES[range_key], now)
    return {str(sid): {"min": lo, "max": hi} for sid, (lo, hi) in ex.items()}


def get_sel(conn, limit: int = 100) -> list[dict]:
    return list_sel_events(conn, limit)


def get_sdc(conn) -> dict | None:
    return latest_sdc(conn)
```

- [ ] **Step 5: 实现 `bmon/serve.py`(Web 页路由 Task 9 追加)**

```python
import argparse
from contextlib import contextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from . import queries
from .config import load_config
from .db import connect


def create_app(db_path: str) -> FastAPI:
    app = FastAPI(title="bmon", version="0.1.0")
    app.add_middleware(CORSMiddleware, allow_origins=["*"],
                       allow_methods=["*"], allow_headers=["*"])

    @contextmanager
    def conn():
        c = connect(db_path, readonly=True)
        try:
            yield c
        finally:
            c.close()

    @app.get("/api/overview")
    def api_overview():
        with conn() as c:
            return queries.get_overview(c)

    @app.get("/api/sensors")
    def api_sensors(group: str | None = None):
        with conn() as c:
            return queries.get_sensors(c, group)

    @app.get("/api/history")
    def api_history(sensor: int,
                    range_key: str = Query(default="1h", alias="range"),
                    max_points: int = 300):
        if range_key not in queries.RANGES:
            raise HTTPException(400, f"range 取值: {sorted(queries.RANGES)}")
        with conn() as c:
            return queries.get_history(c, sensor, range_key, max_points)

    @app.get("/api/extremes")
    def api_extremes(sids: str, range_key: str = Query(default="1h", alias="range")):
        try:
            sid_list = [int(x) for x in sids.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(400, "sids 须为逗号分隔整数")
        with conn() as c:
            return queries.get_extremes(c, sid_list, range_key)

    @app.get("/api/sel")
    def api_sel(limit: int = 100):
        with conn() as c:
            return queries.get_sel(c, limit)

    @app.get("/api/sdc")
    def api_sdc():
        with conn() as c:
            return queries.get_sdc(c)

    return app


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bmon-serve")
    ap.add_argument("--config", default="config.toml")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    uvicorn.run(create_app(cfg.db_path), host=cfg.serve_host, port=cfg.serve_port)
```

- [ ] **Step 6: 运行确认通过**

Run: `.venv/bin/pytest tests/test_api.py -v`
Expected: 5 passed

- [ ] **Step 7: Commit**

```bash
git add bmon/queries.py bmon/serve.py tests/conftest.py tests/test_api.py
git commit -m "feat: 查询层与 REST API"
```

---

### Task 9: 内置 Web 仪表盘(服务端 SVG)

**Files:**
- Create: `bmon/webpage.py`、`tests/test_webpage.py`
- Modify: `bmon/serve.py`(追加 4 个 HTML 路由)

**Interfaces:**
- Consumes: Task 8 `queries.*`、Task 3 `db.query_series`
- Produces:
  - `svg_chart(points: list, w=560, h=90, color="#4fc3f7") -> str`(points=[(ts,v)];空数据返回 "no data" 占位 SVG)
  - `render_index(conn, range_key="24h") -> str`、`render_group(conn, grp, range_key="24h") -> str`、`render_sel(conn) -> str`、`render_sdc(conn) -> str`(均返回完整 HTML;`<meta http-equiv="refresh" content="10">`;status/core 组只出表格卡片不出折线图)
  - serve 新路由:`GET /`、`GET /group/{grp}`、`GET /sel`、`GET /sdc`(均带 `?range=`)

- [ ] **Step 1: 写失败测试 `tests/test_webpage.py`**

```python
from bmon.serve import create_app
from bmon.webpage import svg_chart
from fastapi.testclient import TestClient


def test_svg_chart_basic_and_empty():
    svg = svg_chart([(1, 10.0), (2, 20.0), (3, 15.0)])
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert "polyline" in svg and "10.00 ~ 20.00" in svg
    assert "no data" in svg_chart([])


def test_index_page(seeded_db):
    client = TestClient(create_app(seeded_db[0]))
    html = client.get("/").text
    assert "0102 Monitor" in html
    assert "Mem Usage" in html
    assert "polyline" in html            # KPI sparkline
    assert 'href="/group/temp"' in html  # 分组导航


def test_group_page(seeded_db):
    client = TestClient(create_app(seeded_db[0]))
    html = client.get("/group/temp").text
    assert "CPU1_TEMP" in html and "48.00" in html
    html_core = client.get("/group/core").text  # core 组无折线
    assert "polyline" not in html_core


def test_sel_and_sdc_pages(seeded_db):
    client = TestClient(create_app(seeded_db[0]))
    assert "test event" in client.get("/sel").text
    assert "暂无数据" in client.get("/sdc").text
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_webpage.py -v`
Expected: FAIL(ImportError)

- [ ] **Step 3: 实现 `bmon/webpage.py`**

```python
import html
import time

from . import queries
from .db import query_series

PAGE_HEAD = """<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="10">
<title>bmon — 0102 Monitor</title>
<style>
 body{font-family:sans-serif;background:#0f1115;color:#ddd;margin:0;padding:12px}
 h1{font-size:18px} .kpis{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}
 .kpi{background:#1a1e26;border-radius:6px;padding:8px 10px}
 .kpi .v{font-size:20px;font-weight:700} .kpi .l{color:#888;font-size:12px}
 a{color:#6ab0f3} table{border-collapse:collapse;font-size:13px;margin:8px 0}
 td,th{border:1px solid #2a2f3a;padding:3px 8px} .nav a{margin-right:10px}
 .grid{display:flex;flex-wrap:wrap;gap:10px}
 .card{background:#1a1e26;border-radius:6px;padding:8px;margin:4px}
 .warn{color:#f66} .ok{color:#6c6}
</style></head><body>"""

GROUP_LABELS = [("temp", "温度"), ("volt", "电压"), ("fan", "风扇"),
                ("power", "功率"), ("psu", "电源"), ("status", "状态位"),
                ("load", "负载"), ("mem", "内存"), ("disk", "磁盘")]


def svg_chart(points, w=560, h=90, color="#4fc3f7") -> str:
    if not points:
        return (f'<svg width="{w}" height="{h}">'
                f'<text x="8" y="{h // 2}" fill="#555">no data</text></svg>')
    ts0, ts1 = points[0][0], points[-1][0]
    vs = [v for _, v in points]
    vmin, vmax = min(vs), max(vs)
    if vmax - vmin < 1e-9:
        vmax = vmin + 1.0
    coords = []
    for ts, v in points:
        x = (ts - ts0) / max(1, ts1 - ts0) * (w - 8) + 4
        y = h - 8 - (v - vmin) / (vmax - vmin) * (h - 16)
        coords.append(f"{x:.1f},{y:.1f}")
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
            f'<polyline fill="none" stroke="{color}" stroke-width="1.5" '
            f'points="{" ".join(coords)}"/>'
            f'<text x="4" y="12" fill="#888" font-size="10">'
            f'{vmin:.2f} ~ {vmax:.2f}</text></svg>')


def _nav() -> str:
    links = ['<a href="/">总览</a>']
    links += [f'<a href="/group/{g}">{label}</a>' for g, label in GROUP_LABELS]
    links += ['<a href="/sel">SEL</a><a href="/sdc">SDC</a>']
    return '<div class="nav">' + "".join(links) + "</div>"


def _fmt_ts(ts: int) -> str:
    return time.strftime("%m-%d %H:%M:%S", time.localtime(ts))


def render_index(conn, range_key: str = "24h") -> str:
    ov = queries.get_overview(conn)
    cards = []
    for k in ov["kpis"]:
        val = f'{k["value"]:.2f}{k["unit"]}' if k["value"] is not None else "N/A"
        age = (f'<div class="l warn">过期 {k["age_s"]}s</div>'
               if k["age_s"] > 10 else "")
        cards.append(
            f'<div class="kpi"><div class="l">{html.escape(k["label"])}</div>'
            f'<div class="v">{val}</div>{age}'
            f'{svg_chart(k["history"], 150, 36)}</div>')
    groups_line = " ".join(
        f'<span class="card">{g}: {info["count"]} 个, 更新于 '
        f'{_fmt_ts(info["last_ts"]) if info["last_ts"] else "-"}</span>'
        for g, info in sorted(ov["groups"].items()))
    return (PAGE_HEAD
            + f"<h1>bmon — 0102 Monitor ({time.strftime('%Y-%m-%d %H:%M:%S')})</h1>"
            + _nav() + '<div class="kpis">' + "".join(cards) + "</div>"
            + f'<div class="grid">{groups_line}</div></body></html>')


def render_group(conn, grp: str, range_key: str = "24h") -> str:
    now = int(time.time())
    span = queries.RANGES[range_key]
    parts = []
    for s in queries.get_sensors(conn, grp):
        if s["last_value"] is None:
            continue
        latest = f'{s["last_value"]:.2f} {s["unit"]}'.strip()
        stale = ' class="warn"' if (s["age_s"] or 999) > 10 else ' class="ok"'
        if grp in ("status", "core"):
            parts.append(f'<div class="card"><b>{html.escape(s["name"])}</b>'
                         f'<span{stale}>: {latest}</span></div>')
        else:
            hist = query_series(conn, s["id"], now - span, now, 200)
            parts.append(f'<div class="card"><b>{html.escape(s["name"])}</b>'
                         f'<span{stale}> {latest}</span>{svg_chart(hist)}</div>')
    return (PAGE_HEAD + f"<h1>{grp} — {range_key}</h1>" + _nav()
            + '<div class="grid">' + "".join(parts) + "</div></body></html>")


def render_sel(conn) -> str:
    events = queries.get_sel(conn, 200)
    rows = "".join(
        f"<tr><td>{_fmt_ts(e['ts'])}</td><td>{html.escape(e['severity'] or '')}</td>"
        f"<td>{html.escape(e['event_type'] or '')}</td>"
        f"<td>{html.escape(e['message'] or '')}</td></tr>" for e in events)
    return (PAGE_HEAD + "<h1>SEL 事件</h1>" + _nav()
            + "<table><tr><th>时间</th><th>级别</th><th>类型</th><th>消息</th></tr>"
            + rows + "</table></body></html>")


def render_sdc(conn) -> str:
    s = queries.get_sdc(conn)
    if not s:
        return (PAGE_HEAD + "<h1>SDC 压测结果</h1>" + _nav()
                + "<p>暂无数据(未配置日志或日志不存在)</p></body></html>")
    t = s["totals"]
    head = ("<h1>SDC 压测结果</h1>" + _nav()
            + f"<p>Pass <b>{t.get('pass', 0)}</b> / "
            f"Fail <b class='warn'>{t.get('fail', 0)}</b> / "
            f"Skip {t.get('skip', 0)} / Interrupted {t.get('interrupt', 0)} / "
            f"Invalid {t.get('invalid', 0)} / Total {t.get('total', 0)}</p>")
    tbl = ("<table><tr><th>套件</th><th>用例</th><th>P</th><th>F</th>"
           "<th>S</th><th>I</th><th>V</th><th>T</th></tr>")
    for suite in s["suites"]:
        for te in suite.get("tests", []):
            tbl += (f"<tr><td>{html.escape(str(suite.get('name', '')))}</td>"
                    f"<td>{html.escape(str(te.get('name', '')))}</td>"
                    f"<td>{te.get('pass', 0)}</td>"
                    f"<td class='warn'>{te.get('fail', 0)}</td>"
                    f"<td>{te.get('skip', 0)}</td>"
                    f"<td>{te.get('interrupt', 0)}</td>"
                    f"<td>{te.get('invalid', 0)}</td>"
                    f"<td>{te.get('total', 0)}</td></tr>")
    tbl += "</table>"
    return PAGE_HEAD + head + tbl + "</body></html>"
```

- [ ] **Step 4: `bmon/serve.py` 追加 HTML 路由(`return app` 之前)**

```python
from . import webpage   # 文件顶部与 queries 并列导入


    @app.get("/")
    def index(range_key: str = Query(default="24h", alias="range")):
        with conn() as c:
            return webpage.render_index(c, range_key)

    @app.get("/group/{grp}")
    def group_page(grp: str, range_key: str = Query(default="24h", alias="range")):
        with conn() as c:
            return webpage.render_group(c, grp, range_key)

    @app.get("/sel")
    def sel_page():
        with conn() as c:
            return webpage.render_sel(c)

    @app.get("/sdc")
    def sdc_page():
        with conn() as c:
            return webpage.render_sdc(c)
```

- [ ] **Step 5: 运行确认通过**

Run: `.venv/bin/pytest tests/test_webpage.py tests/test_api.py -v`
Expected: 9 passed

- [ ] **Step 6: 实跑肉眼验证后提交**

```bash
.venv/bin/bmon-serve --config config.toml &
SERVE_PID=$!
sleep 2
curl -s http://127.0.0.1:8102/ | head -30
curl -s "http://127.0.0.1:8102/group/temp" | grep -o CPU1_TEMP | head -1
kill $SERVE_PID
git add bmon/webpage.py bmon/serve.py tests/test_webpage.py
git commit -m "feat: 内置 Web 仪表盘(服务端 SVG)"
```
Expected: HTML 含 KPI 卡与折线;group 页含传感器名

---

### Task 10: TUI 数据源(本机直读 / 远程 HTTP)

**Files:**
- Create: `bmon/tui/__init__.py`(空)、`bmon/tui/datasource.py`、`tests/test_datasource.py`
- Create: `bmon/cli.py`(TUI 入口,本任务实现)

**Interfaces:**
- Consumes: Task 8 `queries.*` 与 `create_app`
- Produces(Task 11 依赖):
  - `class LocalDataSource(db_path: str)`:`overview() -> dict`、`sensors(group=None) -> list[dict]`、`history(sid, range_key, max_points=300) -> list[tuple[int, float]]`、`extremes(sids: list[int], range_key) -> dict[str, dict]`、`sel(limit=100) -> list[dict]`、`sdc() -> dict | None`
  - `class RemoteDataSource(base_url: str)`:同上 6 个方法(经 HTTP)
  - `bmon.cli.main(argv=None) -> int`:`bmon tui [--remote URL] [--config config.toml]`

- [ ] **Step 1: 写失败测试 `tests/test_datasource.py`**

```python
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
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_datasource.py -v`
Expected: FAIL(ImportError)

- [ ] **Step 3: 实现 `bmon/tui/datasource.py`**

```python
import requests

from .. import queries
from ..db import connect


class LocalDataSource:
    """本机直读 SQLite。"""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def _run(self, fn, *args, **kwargs):
        conn = connect(self.db_path, readonly=True)
        try:
            return fn(conn, *args, **kwargs)
        finally:
            conn.close()

    def overview(self) -> dict:
        return self._run(queries.get_overview)

    def sensors(self, group: str | None = None) -> list[dict]:
        return self._run(queries.get_sensors, group)

    def history(self, sid: int, range_key: str, max_points: int = 300):
        return self._run(queries.get_history, sid, range_key,
                         max_points)["points"]

    def extremes(self, sids: list[int], range_key: str) -> dict:
        return self._run(queries.get_extremes, sids, range_key)

    def sel(self, limit: int = 100) -> list[dict]:
        return self._run(queries.get_sel, limit)

    def sdc(self) -> dict | None:
        return self._run(queries.get_sdc)


class RemoteDataSource:
    """经 bmon-serve HTTP API 访问(供其他机器远程跑 TUI)。"""

    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")

    def _get(self, path: str, **params):
        r = requests.get(self.base + path, params=params, timeout=10)
        r.raise_for_status()
        return r.json()

    def overview(self) -> dict:
        return self._get("/api/overview")

    def sensors(self, group: str | None = None) -> list[dict]:
        return self._get("/api/sensors", **({"group": group} if group else {}))

    def history(self, sid: int, range_key: str, max_points: int = 300):
        return self._get("/api/history", sensor=sid, range=range_key,
                         max_points=max_points)["points"]

    def extremes(self, sids: list[int], range_key: str) -> dict:
        return self._get("/api/extremes", sids=",".join(str(s) for s in sids),
                         range=range_key)

    def sel(self, limit: int = 100) -> list[dict]:
        return self._get("/api/sel", limit=limit)

    def sdc(self) -> dict | None:
        return self._get("/api/sdc")
```

- [ ] **Step 4: 实现 `bmon/cli.py`**

```python
import argparse

from .config import load_config


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bmon", description="0102 监控 TUI")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("tui", help="启动 TUI")
    p.add_argument("--remote",
                   help="远程 bmon-serve 地址,如 http://172.168.160.42:8102")
    p.add_argument("--config", default="config.toml")
    args = ap.parse_args(argv)
    if args.cmd != "tui":
        ap.print_help()
        return 2
    from .tui.app import BmonApp
    from .tui.datasource import LocalDataSource, RemoteDataSource
    if args.remote:
        ds = RemoteDataSource(args.remote)
    else:
        ds = LocalDataSource(load_config(args.config).db_path)
    BmonApp(ds).run()
    return 0
```

- [ ] **Step 5: 运行确认通过**

Run: `.venv/bin/pytest tests/test_datasource.py -v`
Expected: 2 passed

- [ ] **Step 6: Commit**

```bash
git add bmon/tui/__init__.py bmon/tui/datasource.py bmon/cli.py tests/test_datasource.py
git commit -m "feat: TUI 数据源(本地直读+远程HTTP)与CLI入口"
```

---

### Task 11: Textual TUI 界面

**Files:**
- Create: `bmon/tui/views.py`、`bmon/tui/app.py`、`tests/test_tui.py`

**Interfaces:**
- Consumes: Task 10 `LocalDataSource/RemoteDataSource`(测试用 FakeDS 同接口)
- Produces:
  - `views.py`:`KpiBar(ds)`、`GroupView(grp, ds, label)`、`CoreView(ds)`、`SdcView(ds)`、`SelView(ds)`、`GROUP_LABELS`、`threshold_style(value, thresholds, unit) -> str`、`core_grid_text(latest: dict) -> str`(192 核着色网格,32 个/行)
  - `app.py`:`class BmonApp(App)`;绑定 `1-6` 切范围、`r` 循环刷新率、`m` 手动刷新、`q` 退出;公开属性 `range_key: str`、`refresh_idx: int`;`REFRESH_STEPS = [1, 5, 10, 30, 0]`(0=Manual)
  - 视图统一实现 `async def refresh_data()`(由 App 刷新循环驱动)

- [ ] **Step 0: 确认 PlotextPlot 可用**

```bash
.venv/bin/python -c "from textual.widgets import PlotextPlot; print('textual 内置')"
# 若上一行失败:
.venv/bin/pip install textual-plotext
.venv/bin/python -c "from textual_plotext import PlotextPlot; print('独立包')"
```
在 `views.py` 顶部按结果选择导入方式(下文按 textual 内置写,若为独立包则把导入行换成 `from textual_plotext import PlotextPlot`):
```python
from textual.widgets import DataTable, Static, PlotextPlot
```

- [ ] **Step 1: 写失败测试 `tests/test_tui.py`**

```python
from bmon.tui.app import BmonApp
from bmon.tui.views import KpiBar, core_grid_text, threshold_style


class FakeDS:
    def overview(self):
        return {"node_ts": 1, "groups": {"temp": {"count": 1, "last_ts": 1}},
                "kpis": [{"id": 1, "name": "cpu_avg_usage", "label": "CPU Avg Usage",
                          "value": 42.0, "unit": "%", "ts": 1, "age_s": 0,
                          "history": [(1, 42.0)]}]}

    def sensors(self, group=None):
        if group == "temp":
            return [{"id": 2, "name": "CPU1_TEMP", "grp": "temp",
                      "unit": "degrees C", "last_value": 48.0, "last_ts": 1,
                      "age_s": 0, "thr_upper_nc": 78, "thr_upper_crit": 85}]
        if group == "core":
            return [{"id": 3, "name": f"cpu{i}_usage", "grp": "core", "unit": "%",
                     "last_value": 50.0 + i, "last_ts": 1, "age_s": 0}
                    for i in range(192)]
        return []

    def history(self, sid, range_key, max_points=300):
        return [(1, 48.0), (2, 49.0)]

    def extremes(self, sids, range_key):
        return {str(s): {"min": 47.0, "max": 49.0} for s in sids}

    def sel(self, limit=100):
        return [{"id": 1, "ts": 1000, "severity": "OK", "event_type": "Alert",
                 "message": "evt", "message_id": "m"}]

    def sdc(self):
        return {"totals": {"pass": 3, "fail": 0, "skip": 0, "interrupt": 0,
                          "invalid": 0, "total": 3},
                "suites": [{"name": "eigen", "pass": 3, "tests":
                            [{"name": "eigen_svd", "pass": 3}]}],
                "per_core": {"0": {"eigen_svd": {"pass": 3}}}}


def test_threshold_style():
    th = {"upper_nc": 78, "upper_crit": 85}
    assert threshold_style(48.0, th, "degrees C") == "green"
    assert threshold_style(80.0, th, "degrees C") == "yellow"
    assert threshold_style(90.0, th, "degrees C") == "red"
    assert threshold_style(0.0, None, "status") == "green"
    assert threshold_style(1.0, None, "status") == "red"
    assert threshold_style(None, th, "C") == "dim"


def test_core_grid_text():
    latest = {f"cpu{i}_usage": 100.0 if i < 5 else 50.0 for i in range(192)}
    text = core_grid_text(latest)
    assert text.count("██") == 192 and text.count("\n") == 5  # 32×6 行


async def test_app_boots_and_keybindings():
    app = BmonApp(FakeDS())
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.range_key == "1h"
        await pilot.press("3")
        assert app.range_key == "12h"
        await pilot.press("r")
        assert app.refresh_idx == 1
        await pilot.press("m")
        await app.refresh_all()
        bar = app.query_one(KpiBar)
        assert "CPU Avg Usage" in str(bar.query_one("#kpi-clock", Static).renderable)
        await pilot.press("q")
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_tui.py -v`
Expected: FAIL(ImportError)

- [ ] **Step 3: 实现 `bmon/tui/views.py`**

```python
import plotext as plt
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Static, PlotextPlot

from ..osmetrics import static_info

GROUP_LABELS = [("temp", "温度"), ("volt", "电压"), ("fan", "风扇"),
               ("power", "功率"), ("psu", "电源PSU"), ("status", "状态位"),
               ("load", "负载"), ("mem", "内存"), ("disk", "磁盘")]

INFO = static_info()


def threshold_style(value, thresholds, unit) -> str:
    """值→终端样式:dim(无值)/红(越临界或状态位非0)/琥珀(越非临界)/绿。"""
    if value is None:
        return "dim"
    if unit == "status":
        return "green" if value == 0 else "red"
    t = thresholds or {}
    if t.get("upper_crit") is not None and value >= t["upper_crit"]:
        return "red"
    if t.get("lower_crit") is not None and value <= t["lower_crit"]:
        return "red"
    if t.get("upper_nc") is not None and value >= t["upper_nc"]:
        return "yellow"
    if t.get("lower_nc") is not None and value <= t["lower_nc"]:
        return "yellow"
    return "green"


def _th(s: dict) -> dict:
    return {k.replace("thr_", ""): s[k] for k in s if k.startswith("thr_")}


def _usage_style(v) -> str:
    if v is None:
        return "dim"
    return "green" if v < 70 else "yellow" if v < 90 else "red"


def core_grid_text(latest: dict) -> str:
    """192 核按占用着色:绿<70 / 琥珀<90 / 红≥90,每行 32 个。"""
    lines = []
    for row in range(0, 192, 32):
        blocks = []
        for i in range(row, min(row + 32, 192)):
            v = latest.get(f"cpu{i}_usage")
            blocks.append(f"[{_usage_style(v)}]██[/{_usage_style(v)}]")
        lines.append("".join(blocks))
    return "\n".join(lines)


class KpiBar(Horizontal):
    def __init__(self, ds):
        super().__init__(id="kpibar")
        self.ds = ds

    def compose(self):
        yield Static("", id="kpi-clock")

    async def refresh_data(self, range_key: str) -> None:
        ov = self.ds.overview()
        parts = []
        for k in ov["kpis"]:
            val = f"{k['value']:.2f}{k['unit']}" if k["value"] is not None else "N/A"
            flag = "[red]⚠[/red]" if k["age_s"] > 10 else ""
            parts.append(f"{k['label']} [b]{val}[/b]{flag}")
        info = f"{INFO['cpu_model']} / {INFO['cores']} cores / {INFO['mem_total_gb']}GB"
        self.query_one("#kpi-clock", Static).update(
            f"[b]B0102 SDC Monitor — {info}[/b]   " + "  │  ".join(parts))


class GroupView(Vertical):
    def __init__(self, grp: str, ds, label: str):
        super().__init__()
        self.grp, self.ds, self.label = grp, ds, label
        self.range_key = "1h"

    def compose(self):
        yield DataTable(id=f"tbl-{self.grp}")
        yield PlotextPlot(id=f"plot-{self.grp}")

    def on_mount(self) -> None:
        tbl = self.query_one(DataTable)
        tbl.add_columns("传感器", "当前值", "单位", "状态", "范围极值")
        tbl.cursor_type = "row"

    async def refresh_data(self) -> None:
        sensors = self.ds.sensors(self.grp)
        extremes = (self.ds.extremes([s["id"] for s in sensors
                                      if s["last_ts"] is not None],
                                     self.range_key)
                    if any(s["last_ts"] is not None for s in sensors) else {})
        tbl = self.query_one(DataTable)
        tbl.clear()
        for s in sensors:
            ex = extremes.get(str(s["id"]))
            extxt = f"{ex['min']:.2f}~{ex['max']:.2f}" if ex else "—"
            v, style = s["last_value"], threshold_style(
                s["last_value"], _th(s), s["unit"])
            val = f"{v:.2f}" if v is not None else "N/A"
            state = "过期" if (s["age_s"] or 9999) > 10 else "ok"
            tbl.add_row(f"[{style}]{s['name']}[/{style}]",
                        f"[{style}]{val}[/{style}]", s["unit"], state, extxt,
                        key=str(s["id"]))
        row = tbl.cursor_row
        if sensors and row < len(sensors):
            await self._plot(sensors[row])

    async def _plot(self, sensor: dict) -> None:
        pts = self.ds.history(sensor["id"], self.range_key, 200)
        plot_widget = self.query_one(PlotextPlot)
        plt.clear_figure()
        plt.title(f"{sensor['name']} ({self.range_key})")
        plt.plot([p[0] for p in pts], [p[1] for p in pts], color="cyan")
        plot_widget.refresh()


class CoreView(Vertical):
    def __init__(self, ds):
        super().__init__()
        self.ds = ds

    def compose(self):
        yield Static("", id="core-grid")
        yield DataTable(id="core-tbl")

    def on_mount(self) -> None:
        tbl = self.query_one(DataTable)
        tbl.add_columns("核", "占用 %")

    async def refresh_data(self) -> None:
        sensors = self.ds.sensors("core")
        latest = {s["name"]: s["last_value"] for s in sensors}
        self.query_one("#core-grid", Static).update(core_grid_text(latest))
        tbl = self.query_one(DataTable)
        tbl.clear()
        for s in sensors:
            v = s["last_value"]
            st = _usage_style(v)
            tbl.add_row(s["name"], f"[{st}]{v:.1f}[/{st}]" if v is not None
                        else "N/A")


class SdcView(Vertical):
    def __init__(self, ds):
        super().__init__()
        self.ds = ds

    def compose(self):
        yield Static("", id="sdc-summary")
        yield DataTable(id="sdc-tbl")

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns(
            "套件", "用例", "P", "F", "S", "I", "V", "T")

    async def refresh_data(self) -> None:
        s = self.ds.sdc()
        summary = self.query_one("#sdc-summary", Static)
        tbl = self.query_one(DataTable)
        tbl.clear()
        if not s:
            summary.update("[yellow]暂无数据(未配置日志或日志不存在)[/yellow]")
            return
        t = s["totals"]
        summary.update(
            f"[green]Pass {t.get('pass', 0)}[/green]  [red]Fail {t.get('fail', 0)}[/red]"
            f"  Skip {t.get('skip', 0)}  Interrupted {t.get('interrupt', 0)}"
            f"  Invalid {t.get('invalid', 0)}  Total {t.get('total', 0)}")
        for suite in s["suites"]:
            for te in suite.get("tests", []):
                fail = te.get("fail", 0)
                row = [suite.get("name", ""), te.get("name", "")]
                row += [str(te.get(k, 0)) for k in
                        ("pass", "fail", "skip", "interrupt", "invalid", "total")]
                if fail:
                    row[3] = f"[red]{fail}[/red]"
                tbl.add_row(*row)


class SelView(Vertical):
    def __init__(self, ds):
        super().__init__()
        self.ds = ds

    def compose(self):
        yield DataTable(id="sel-tbl")

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns("时间", "级别", "类型", "消息")

    async def refresh_data(self) -> None:
        import time as _time
        tbl = self.query_one(DataTable)
        tbl.clear()
        for e in self.ds.sel(100):
            ts = _time.strftime("%m-%d %H:%M:%S", _time.localtime(e["ts"]))
            sev = e.get("severity") or ""
            style = "red" if sev not in ("OK", "Informational", "") else "green"
            tbl.add_row(ts, f"[{style}]{sev}[/{style}]",
                        e.get("event_type", ""), e.get("message", ""))
```

- [ ] **Step 4: 实现 `bmon/tui/app.py`**

```python
from textual.app import App, ComposeResult
from textual.widgets import Footer, Header, TabbedContent, TabPane

from .datasource import LocalDataSource, RemoteDataSource  # noqa: F401(导出便利)
from .views import (GROUP_LABELS, CoreView, GroupView, KpiBar, SdcView, SelView)

REFRESH_STEPS = [1, 5, 10, 30, 0]  # 0 = Manual


class BmonApp(App):
    CSS = """
    Screen { layout: vertical; }
    #kpibar { height: 3; padding: 0 1; }
    DataTable { height: 1fr; }
    PlotextPlot { height: 40%; border: round #444; }
    #core-grid { padding: 0 1; }
    #sdc-summary { padding: 0 1; color: $text; }
    """

    BINDINGS = [
        ("1", "set_range('1h')", "1h"), ("2", "set_range('6h')", "6h"),
        ("3", "set_range('12h')", "12h"), ("4", "set_range('24h')", "24h"),
        ("5", "set_range('7d')", "7d"), ("6", "set_range('30d')", "30d"),
        ("r", "cycle_refresh", "刷新率"), ("m", "manual_refresh", "手动刷新"),
        ("q", "quit", "退出"),
    ]

    def __init__(self, ds):
        super().__init__()
        self.ds = ds
        self.range_key = "1h"
        self.refresh_idx = 0
        self._interval = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield KpiBar(self.ds)
        with TabbedContent():
            for grp, label in GROUP_LABELS:
                with TabPane(label, id=f"pane-{grp}"):
                    yield GroupView(grp, self.ds, label)
            with TabPane("每核CPU", id="pane-core"):
                yield CoreView(self.ds)
            with TabPane("SDC压测", id="pane-sdc"):
                yield SdcView(self.ds)
            with TabPane("SEL事件", id="pane-sel"):
                yield SelView(self.ds)
        yield Footer()

    def on_mount(self) -> None:
        self._apply_interval()
        self.call_after_refresh(self.refresh_all)

    def _apply_interval(self) -> None:
        if self._interval is not None:
            self._interval.stop()
            self._interval = None
        sec = REFRESH_STEPS[self.refresh_idx]
        if sec:
            self._interval = self.set_interval(sec, self._tick_refresh)
        self.sub_title = (f"range={self.range_key} "
                          f"refresh={'Manual' if not sec else f'{sec}s'}")

    def _tick_refresh(self) -> None:
        self.run_worker(self.refresh_all(), exclusive=False)

    async def refresh_all(self) -> None:
        try:
            await self.query_one(KpiBar).refresh_data(self.range_key)
            pane = self.query_one(TabbedContent).active_pane
            view = pane.children[0] if pane and pane.children else None
            if view is not None and hasattr(view, "refresh_data"):
                await view.refresh_data()
        except Exception:
            self.sub_title += " (refresh error)"

    def action_set_range(self, key: str) -> None:
        self.range_key = key
        for v in self.query(GroupView):
            v.range_key = key
        self._apply_interval()
        self.run_worker(self.refresh_all(), exclusive=True)

    def action_cycle_refresh(self) -> None:
        self.refresh_idx = (self.refresh_idx + 1) % len(REFRESH_STEPS)
        self._apply_interval()

    def action_manual_refresh(self) -> None:
        self.run_worker(self.refresh_all(), exclusive=True)
```

- [ ] **Step 5: 运行确认通过**

Run: `.venv/bin/pytest tests/test_tui.py -v`
Expected: 3 passed(注意 asyncio_mode=auto 已在 pyproject 配置)

- [ ] **Step 6: 实跑 TUI 肉眼验证**

启动采集器与 API(若未运行),然后 `.venv/bin/bmon tui` 运行 30 秒:确认 KPI 行、9 个分组标签 + 每核/SDC/SEL 三个标签、`3` 切 12h、`r` 切刷新率、表格选中行出图、`q` 退出。若显示异常,修 CSS 后再提交。

- [ ] **Step 7: Commit**

```bash
git add bmon/tui/views.py bmon/tui/app.py tests/test_tui.py
git commit -m "feat: Textual TUI 界面(KPI/分组/每核/SDC/SEL)"
```

---

### Task 12: systemd 部署与 README

**Files:**
- Create: `deploy/bmon-collect.service`、`deploy/bmon-serve.service`
- Modify: `README.md`(完整化)

**Interfaces:**
- Consumes: Task 7 `bmon-collect:main`、Task 8/9 `bmon-serve:main`

- [ ] **Step 1: 写 systemd 单元 `deploy/bmon-collect.service`**

```ini
[Unit]
Description=bmon BMC/OS monitor collector for 0102
After=network-online.target

[Service]
Type=simple
User=sdc
WorkingDirectory=/home/sdc/wangxu/monitor
ExecStart=/home/sdc/wangxu/monitor/.venv/bin/bmon-collect --config /home/sdc/wangxu/monitor/config.toml
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: 写 `deploy/bmon-serve.service`**

```ini
[Unit]
Description=bmon HTTP API + web dashboard for 0102
After=network-online.target bmon-collect.service

[Service]
Type=simple
User=sdc
WorkingDirectory=/home/sdc/wangxu/monitor
ExecStart=/home/sdc/wangxu/monitor/.venv/bin/bmon-serve --config /home/sdc/wangxu/monitor/config.toml
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 3: 安装并启动服务**

```bash
sudo cp deploy/bmon-collect.service deploy/bmon-serve.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bmon-collect bmon-serve
sleep 5
systemctl status bmon-collect bmon-serve --no-pager | head -20
journalctl -u bmon-collect -n 10 --no-pager
```
Expected: 两个服务 active (running),journal 无 ERROR

- [ ] **Step 4: 验证对外可达(模拟外部机器)**

```bash
curl -s http://172.168.160.42:8102/api/overview | head -c 300
curl -s http://172.168.160.42:8102/ | grep -c polyline
```
Expected: overview 返回 JSON;首页含 sparkline

- [ ] **Step 5: 完整化 `README.md`**

```markdown
# bmon — 0102 服务器 BMC 监控

长江计算 R240K V2(4×Kunpeng920_5250,192 核,754Gi)的 BMC+OS 秒级监控系统。
Redfish 采集 → SQLite(WAL)→ Textual TUI / FastAPI REST / 内置 Web 页。

## 组件
- `bmon-collect`(systemd):1s 采集 BMC 213 传感器 + PSU 电气 + OS 汇总;10s 每核占用;
  60s SEL/SDC 日志;5min 分钟级聚合;1h 清理(raw 48h / 聚合 35d)
- `bmon-serve`(systemd):`0.0.0.0:8102` REST + Web 页
- `bmon tui`:本机 TUI;`bmon tui --remote http://172.168.160.42:8102` 远程 TUI

## 常用命令
```bash
sudo systemctl status bmon-collect bmon-serve   # 服务状态
journalctl -u bmon-collect -f                   # 采集日志
.venv/bin/bmon tui                              # 本机 TUI(键 1-6 切范围,r 切刷新率,q 退出)
.venv/bin/pytest                                # 全部测试
BMC_LIVE=1 .venv/bin/pytest -k live             # 含真实 BMC 的测试
```

## REST API
| 端点 | 说明 |
|---|---|
| `/api/overview` | KPI(含近 1h 迷你序列)+ 分组概览 |
| `/api/sensors?group=temp` | 传感器清单(阈值/最新值/age) |
| `/api/history?sensor=<id>&range=1h&max_points=300` | 桶聚合序列(1h/6h/12h/24h/7d/30d) |
| `/api/extremes?sids=1,2&range=24h` | 一批传感器的范围极值 |
| `/api/sel` | BMC SEL 事件(倒序) |
| `/api/sdc` | SDC 压测结果(无日志为 null) |
| `/`、`/group/{grp}`、`/sel`、`/sdc` | 内置 Web 页(10s 自刷新) |

## 配置(`config.toml`)
BMC 地址/账密、采集节奏、保留时长、端口、SDC 日志路径、监控磁盘路径。
本机无 cpufreq/频率传感器,动态频率不采集(静态 2.6GHz 见 TUI 标题)。
```

- [ ] **Step 6: Commit**

```bash
git add deploy/ README.md
git commit -m "feat: systemd 部署单元与 README"
```

---

### Task 13: 端到端验证

**Files:**
- 无新文件;只运行验证并提交可能的修复

**Interfaces:**
- Consumes: 全部

- [ ] **Step 1: 全量测试(含 LIVE)**

```bash
.venv/bin/pytest -v
BMC_LIVE=1 .venv/bin/pytest -v
```
Expected: 全部 passed;BMC_LIVE 下 live 用例也 passed

- [ ] **Step 2: 采集持久性验证(60 秒两次采样)**

```bash
COUNT() { .venv/bin/python -c "
import sqlite3
c = sqlite3.connect('file:data/bmon.db?mode=ro', uri=True)
print(c.execute('SELECT COUNT(*), MAX(ts) FROM raw').fetchone())"; }
COUNT; sleep 60; COUNT
```
Expected: 行数两次差 ≈ 15,000(235 序列×60s + 192 每核×6 次 ≈ 15,250),`MAX(ts)` 递增到当前秒。

- [ ] **Step 3: 数据完整性抽查**

```bash
.venv/bin/python -c "
import sqlite3, time
c = sqlite3.connect('file:data/bmon.db?mode=ro', uri=True)
now = int(time.time())
print('近60s raw:', c.execute('SELECT COUNT(*) FROM raw WHERE ts>=?', (now-60,)).fetchone()[0])
for name, grp in [('CPU1_TEMP','bmc/温度'), ('Power','bmc/功率'), ('PSU2 Output W','psu'),
                  ('cpu_avg_usage','os'), ('cpu47_usage','os/每核')]:
    r = c.execute(\"SELECT name, last_value, last_status, last_ts FROM sensors WHERE name=?\", (name,)).fetchone()
    print(grp, '→', r)
print('SEL:', c.execute('SELECT COUNT(*) FROM sel_events').fetchone()[0])"
```
Expected: 近 60s raw ≈ 14,000+;各传感器 last_ts 距当前 <5s;PSU2 Output W 有值(如 ~500);SEL ≥ 90

- [ ] **Step 4: 聚合与清理生效验证**

```bash
# 安全验证:插入一条 3 天前的样本,直接调用 prune(只删 >48h 的行,不会碰实时数据),
# 确认样本被清掉、其余行数不变。绝不手动 DELETE raw!
.venv/bin/python -c "
from bmon.db import connect, prune
import sqlite3, time
c = connect('data/bmon.db')
now = int(time.time())
sid = c.execute(\"SELECT id FROM sensors WHERE name='CPU1_TEMP' AND source='bmc'\").fetchone()[0]
c.execute('INSERT OR REPLACE INTO raw VALUES (?,?,?)', (sid, now - 3*24*3600, 42.0))
c.commit()
before = c.execute('SELECT COUNT(*) FROM raw').fetchone()[0]
dr, du = prune(c, now, raw_keep_s=172800, rollup_keep_s=3024000)
after = c.execute('SELECT COUNT(*) FROM raw').fetchone()[0]
print(f'prune 删除 {dr} 行(应≥1,即3天前样本); 行数 {before} → {after}(差值即删除数,实时数据无损)')
assert dr >= 1 and before - after == dr"
# rollup 生效验证:服务每 5min 自动聚合,直接查表(勿手动触发,以证明服务自身在工作)
.venv/bin/python -c "
import sqlite3
c = sqlite3.connect('file:data/bmon.db?mode=ro', uri=True)
print('rollup rows:', c.execute('SELECT COUNT(*) FROM rollup_1m').fetchone()[0])
print('最近聚合桶 ts % 60 ==', c.execute('SELECT MAX(ts) FROM rollup_1m').fetchone()[0] % 60)"
```
Expected: prune 删除 ≥1 行且实时数据无损;rollup rows > 0 且桶 ts 为 60 的倍数

- [ ] **Step 5: TUI 与 Web 双通道肉眼验证**

```bash
timeout 20 .venv/bin/bmon tui </dev/null || true     # 能启动渲染即可(20s 自动退出)
curl -s http://172.168.160.42:8102/group/core | grep -o 'cpu0_usage' | head -1
curl -s http://172.168.160.42:8102/api/extremes?sids=$(.venv/bin/python -c "
import sqlite3
c = sqlite3.connect('file:data/bmon.db?mode=ro', uri=True)
print(c.execute(\"SELECT id FROM sensors WHERE name='CPU1_TEMP'\").fetchone()[0])")'&range=1h'
```
Expected: group/core 页含 cpu0_usage;extremes 返回 min/max

- [ ] **Step 6: 最终提交与清理**

```bash
rm -rf .plan_parts   # 计划文档临时分片(如仍存在)
git status
git add -A && git commit -m "chore: 端到端验证通过" --allow-empty
```

---

## 计划自审记录(已完成)

1. **规格覆盖**:规格 §4 采集源→Task 4/5/6/7;§5 存储/保留/聚合/清理→Task 2/3;§6 TUI 全部板块→Task 11;§7 API+Web→Task 8/9;§8 部署→Task 12;§9 错误处理(BMC 失败重试/幂等/防御解析)→Task 4/6/7 单测;§10 测试→各任务 TDD + Task 13;频率修正(本机无频率)→Task 5 只采占用。无缺口。
2. **占位符扫描**:无 TBD/TODO/"适当处理";所有代码块完整可执行。
3. **类型一致性**:`SensorReading` 字段、`db.*`/`queries.*` 签名、`DataSource` 6 方法在 Task 5/7/8/9/10/11 间已逐一核对一致;`grp` 列名全局统一;`update_latest` 三元组与 `_store` 调用一致。
4. **已知偏差(均已声明)**:SDC 日志格式为 v1 猜测(规格风险条款);TUI 极值列 >6h 用 rollup(滞后 ≤5min);SEL 首次同步限 200 条。
