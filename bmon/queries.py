import time

from .db import (get_sensor, group_extremes, latest_sdc, list_sel_events,
                 list_sensors, query_series, sensor_id)
from .osmetrics import OsMetrics

RANGES = {"1h": 3600, "6h": 21600, "12h": 43200, "24h": 86400,
          "7d": 604800, "30d": 2592000}

KPI_DEFS = [
    ("os", "cpu_avg_usage", "CPU Avg Usage"),
    ("os", "cpu_max_core_usage", "CPU Max Core Usage"),
    ("os", "mem_usage_pct", "Mem Usage"),
    ("os", "load_1m", "Load 1min"),
    ("os", "disk_usage_pct", "Disk Usage"),
    ("psu", "Total Power", "Total PWR"),  # parse_power 注册为 source='psu'(PowerControl 总功耗)
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
    # 终版评审修复 2:携带服务器静态信息(static_info 在 bmon-serve 端
    # 运行,即服务器本机)—— 远程 TUI 头部据此显示服务器规格,而非客户端
    return {"node_ts": now, "kpis": kpis, "groups": groups,
            "static": OsMetrics.static_info()}


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
