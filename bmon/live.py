"""按需轮询器:TUI 远程机器页与 serve 代理共用的 BMC 读取引擎。

设计原则:read(alias) 是唯一触发器——只有被查看的机器才会发起拉取,
无人问津的机器零请求;TTL 内的重复读取(多客户端/快速刷新)直接回缓存,
避免打爆 BMC。本模块被 serve 进程引用,不得引入 textual 等 UI 依赖,
requests/urllib3 也只经由 bmon.bmc 间接使用。
"""

import time
from dataclasses import dataclass

from bmon import machines
from bmon.bmc import BmcClient


@dataclass
class _Entry:
    """单台机器的轮询状态。"""

    machine: machines.Machine
    # client 懒建:首次 read 才创建,从未被查看的机器连 client 都不建
    client: object = None
    # time.monotonic() 口径的缓存过期时刻(不受系统改时间影响)
    expires_at: float = 0.0
    # 最近一次结果;成功与错误共用同一套 TTL 防抖
    payload: dict | None = None


class LivePoller:
    """按需轮询器:无后台线程、无定时器,一切由 read() 驱动。"""

    def __init__(self, machines: list[machines.Machine], verify_tls: bool = False,
                 ttl_s: float = 1.0, client_factory=None):
        self._ttl_s = ttl_s
        # 默认按 Machine 凭据建 Redfish 客户端;测试注入假工厂以离线验证
        self._client_factory = client_factory or (
            lambda m: BmcClient(m.base_url, m.user, m.password, verify_tls))
        self._entries: dict[str, _Entry] = {}
        self.set_machines(machines)

    def set_machines(self, machines: list[machines.Machine]) -> None:
        """重设机器清单,凭据 (ip, user, password) 未变的机器原样保留缓存
        (client 与 TTL 内的读数都不动,清单例行重载不触发重连/重拉);
        凭据变更或被删除的机器丢弃条目,下次 read 重新建连。

        按凭据而非别名匹配:别名重命名不算凭据变更,缓存跟着新别名走。
        """
        old_by_creds = {(e.machine.ip, e.machine.user, e.machine.password): e
                        for e in self._entries.values()}
        entries: dict[str, _Entry] = {}
        for m in machines:
            old = old_by_creds.get((m.ip, m.user, m.password))
            if old is not None:
                old.machine = m       # 换成最新对象(别名可能已改)
                entries[m.alias] = old
            else:
                entries[m.alias] = _Entry(m)
        self._entries = entries

    def read(self, alias: str) -> dict:
        """读取一台机器的全部传感器;成功/失败结果各缓存一个 TTL。

        成功:{"ok": True, "ts": int, "readings": [dict, ...]}
        未知别名:{"ok": False, "error": str, "code": 404}(纯查表,无需缓存)
        BMC 拉取失败:{"ok": False, "error": str, "code": 502}
        """
        entry = self._entries.get(alias)
        if entry is None:
            return {"ok": False, "error": f"未知机器: {alias}", "code": 404}
        now = time.monotonic()
        if entry.payload is not None and now < entry.expires_at:
            return entry.payload   # TTL 防抖:错误同样命中,维持 ~1s 重试节奏
        if entry.client is None:
            entry.client = self._client_factory(entry.machine)
        try:
            readings = entry.client.get_all_readings()
        except Exception as e:
            # 与 collect.py tick 同口径兜底:BmcClient._get 只包网络/HTTP/JSON
            # 错,parse_* 里的 s["Name"]/float(...) 等畸形数据异常是原生
            # KeyError/ValueError——不兜住会击穿 serve/TUI 的 ok/error/code
            # 契约,且异常在 expires_at 刷新前抛出,该机器将永远得不到防抖。
            entry.payload = {"ok": False, "error": str(e), "code": 502}
        else:
            entry.payload = {
                # ts 取拉取成功时刻的墙钟,供界面展示;缓存命中时随 payload 保留
                "ok": True,
                "ts": int(time.time()),
                "readings": [{"name": r.name, "value": r.value, "unit": r.unit,
                              "grp": r.grp, "status": r.status,
                              "thresholds": r.thresholds}
                             for r in readings]}
        # 成功与错误统一在此刷新过期时刻;ttl_s=0 时下次 read 必然重拉
        entry.expires_at = now + self._ttl_s
        return entry.payload
