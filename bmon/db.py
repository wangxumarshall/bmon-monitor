import json
import math
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
             thr_upper_nc=COALESCE(excluded.thr_upper_nc, sensors.thr_upper_nc),
             thr_upper_crit=COALESCE(excluded.thr_upper_crit, sensors.thr_upper_crit),
             thr_upper_fatal=COALESCE(excluded.thr_upper_fatal, sensors.thr_upper_fatal),
             thr_lower_nc=COALESCE(excluded.thr_lower_nc, sensors.thr_lower_nc),
             thr_lower_crit=COALESCE(excluded.thr_lower_crit, sensors.thr_lower_crit),
             thr_lower_fatal=COALESCE(excluded.thr_lower_fatal, sensors.thr_lower_fatal)""",
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
    if span < 0:
        return []
    table, step = ("raw", 1) if span <= 25 * 3600 else ("rollup_1m", 60)
    val = "value" if table == "raw" else "avgv"
    bucket = step * max(1, math.ceil(span / step / max_points))
    rows = conn.execute(
        f"SELECT (ts/{bucket})*{bucket} AS bts, AVG({val}) AS v "
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
