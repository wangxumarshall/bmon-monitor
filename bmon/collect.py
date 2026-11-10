import argparse
import logging
import signal
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from .bmc import BmcClient
from .config import load_config
from .db import (connect, init_db, insert_readings, insert_sel_events, max_sel_id,
                 prune, rollup_window, save_sdc_run, update_latest, upsert_sensor)
from .machines import Machine, parse_machines
from .osmetrics import OsMetrics
from .sdclog import parse_sdc_log

log = logging.getLogger("bmon.collect")


class MachinesWatcher:
    """监视 machines.txt 内容变化,凭据热加载的数据源。

    每 tick 全量读文件文本与上次比对:未变(或自始缺失)返回 None,
    变了才解析并返回快照,由 main 重建 BmcClient —— 避免每秒重复解析。
    文件曾在后又消失也算变更(视为清单清空),让 main 停掉全部 BMC 采集。
    """

    def __init__(self, path: str, wanted: list[str], node_id: str):
        self.path, self.wanted, self.node_id = path, wanted, node_id
        self._last_text: str | None = None  # None 表示文件缺失/从未见过

    def poll(self) -> dict | None:
        try:
            text = Path(self.path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # UnicodeDecodeError(如 GBK 存的中文注释)是 ValueError 不是 OSError,
            # 漏接会击穿 main 的兜底、杀死整个采集进程;归一到"不可读=缺失"路径。
            text = None
        if text == self._last_text:
            return None
        self._last_text = text
        if text is None:
            machines, errs = [], [f"清单文件不存在或不可读: {self.path}"]
        else:
            machines, errs = parse_machines(text)
        # 只挑 wanted 关心的别名:==node_id 的是本机,其余是远端;
        # wanted 里没找到的逐个给出警告,提示清单缺机器。
        by_alias = {m.alias: m for m in machines}
        local: Machine | None = None
        remotes: dict[str, Machine] = {}
        for alias in self.wanted:
            m = by_alias.get(alias)
            if m is None:
                errs.append(f"清单缺少节点 {alias}")
            elif alias == self.node_id:
                local = m
            else:
                remotes[alias] = m
        return {"machines": machines, "local": local,
                "remotes": remotes, "errors": errs}


class Collector:
    def __init__(self, cfg, conn, bmc, osm, remote_bmcs: dict[str, Any] | None = None):
        self.cfg, self.conn, self.bmc, self.osm = cfg, conn, bmc, osm
        # 远端节点只采 BMC 读数(source=bmc@别名);拷贝一份防止外部 dict
        # 后续被原地修改导致采集集合与 main 状态不一致
        self._remotes: dict[str, Any] = dict(remote_bmcs) if remote_bmcs else {}
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
        # value=None 的读数(Task4 修正,如 NIC OM Temp):只注册保证可见,
        # 不写 raw、不刷新 last_value/last_ts,避免幽灵 0 值历史与假新鲜度;
        # 仅更新 last_status 让界面能显示 ns 状态。
        samples, nulls = [], []
        for r in readings:
            sid = self._register(r)
            if r.value is None:
                nulls.append((sid, r.status))
            else:
                samples.append((sid, r.value, r.status))
        if samples:
            insert_readings(self.conn, ts, [(sid, v) for sid, v, _ in samples])
            update_latest(self.conn, ts, samples)
        if nulls:
            self.conn.executemany(
                "UPDATE sensors SET last_status=? WHERE id=?",
                [(st, sid) for sid, st in nulls])
            self.conn.commit()

    def set_remote_bmcs(self, remotes: dict[str, Any]) -> None:
        """machines.txt 变更后由 main 调用,整体替换远端客户端集合。"""
        self._remotes = dict(remotes)

    def tick(self, ts: int) -> None:
        if self.bmc:
            try:
                self._store(self.bmc.get_all_readings(), ts)
            except Exception:
                # 按计划口径"BMC 失败:按 tick 重试并标记过期,采集不崩溃":
                # 连 BmcError 之外的畸形数据异常(KeyError/ValueError 等)也兜住,
                # 保证下一行 OS 采集必然执行。
                log.exception("BMC 采集失败(数据将标记过期)")
        # 本机凭据缺失(bmc=None)时跳过 BMC,OS 路径照常;远端各自独立兜错,
        # 单台故障不能连累其他节点。
        for alias, rbmc in self._remotes.items():
            try:
                # 远端读数打上 bmc@别名 命名空间,与本机 bmc 及其他远端区分
                self._store([replace(r, source=f"bmc@{alias}")
                             for r in rbmc.get_all_readings()], ts)
            except Exception:
                log.exception("远端 BMC 采集失败 (alias=%s)", alias)
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
        if not self.bmc:
            return  # 本机凭据缺失,无 SEL 可拉;远端节点也不采 SEL
        try:
            entries = self.bmc.get_sel_entries(self._sel_max)
        except Exception:
            log.exception("SEL 拉取失败")
            return
        if entries:
            insert_sel_events(self.conn, entries)
            # 条目可能最新在前(get_sel_entries 降序返回),max() 与顺序无关
            self._sel_max = max(self._sel_max, max(e["id"] for e in entries))

    def _poll_sdc(self) -> None:
        try:
            summary = parse_sdc_log(self.cfg.sdc_log_path)
            if not summary or summary["log_mtime"] == self._sdc_mtime:
                return
            self._sdc_mtime = summary["log_mtime"]
            summary["parsed_at"] = int(time.time())
            save_sdc_run(self.conn, summary)
            log.info("SDC 日志已更新并解析: %s", self.cfg.sdc_log_path)
        except Exception:
            log.exception("SDC 轮询失败")


def _build_bmc(m: Machine | None, verify_tls: bool) -> BmcClient | None:
    """由清单条目建客户端;机器缺失返回 None,调用方据此跳过 BMC 采集。"""
    if m is None:
        return None
    return BmcClient(m.base_url, m.user, m.password, verify_tls)


def _apply_machines(cfg, collector: Collector, st: dict) -> None:
    """把 watcher 快照落到 collector;摘要与告警每次变更只打一次(poll 未变不返回)。"""
    collector.bmc = _build_bmc(st["local"], cfg.bmc_verify_tls)
    collector.set_remote_bmcs({alias: _build_bmc(m, cfg.bmc_verify_tls)
                               for alias, m in st["remotes"].items()})
    local_desc = st["local"].alias if st["local"] else "无(仅采 OS)"
    log.info("节点清单已应用: 本机=%s 远端=%s", local_desc,
             sorted(st["remotes"]) or "无")
    for e in st["errors"]:
        log.warning("machines.txt: %s", e)


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
    collector = Collector(cfg, conn, None, OsMetrics(cfg.disk_path))
    # 凭据全部来自 machines.txt:启动先取一次初始快照,之后每 tick 热加载
    watcher = MachinesWatcher(cfg.machines_file, cfg.collect_nodes, cfg.node_id)
    st = watcher.poll()
    if st is None:
        log.warning("machines 清单 %s 不存在,暂不采集 BMC,仅采 OS", cfg.machines_file)
    else:
        _apply_machines(cfg, collector, st)

    stop = {"flag": False}

    def _sig(signum, frame):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    log.info("bmon-collect 启动 (db=%s)", cfg.db_path)
    while not stop["flag"]:
        t0 = time.monotonic()
        st = watcher.poll()
        if st:
            _apply_machines(cfg, collector, st)
        try:
            collector.tick(int(time.time()))
        except Exception:
            log.exception("tick 异常")
        time.sleep(max(0.0, 1.0 - (time.monotonic() - t0)))
    conn.close()
    log.info("bmon-collect 退出")
    return 0
