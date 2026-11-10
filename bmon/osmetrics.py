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
        # guest/guest_nice 已计入 user/nice,只累加前 8 列避免重复计数
        cores.append((idle, sum(vals[:8])))
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
