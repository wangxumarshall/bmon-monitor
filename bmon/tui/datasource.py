from pathlib import Path
import urllib.parse

import requests

from .. import machines, queries
from ..db import connect
from ..live import LivePoller


def _masked_dicts(ms: list[machines.Machine]) -> list[dict]:
    """掩码 dict 列表:与 serve /api/machines 响应同口径,任何返回不含明文密码。"""
    return [{"ip": m.ip, "user": m.user, "password": machines.MASK,
             "alias": m.alias} for m in ms]


class LocalDataSource:
    """本机直读 SQLite;机器清单本地读写,实时读数由内置 LivePoller 直连 BMC。"""

    def __init__(self, db_path: str, machines_path: str = "machines.txt",
                 verify_tls: bool = False, client_factory=None):
        # machines_path/verify_tls 均带默认:既有 LocalDataSource(db_path) 调用方不破;
        # client_factory 仅供测试注入假客户端,默认走 LivePoller 的真 BmcClient 工厂
        self.db_path = db_path
        self.machines_path = machines_path
        # 本机模式 TUI 进程自己就是轮询方(与 serve 代理共用同一引擎):
        # 清单缺失/不可读时 load_machines 返回 [],构造绝不抛错
        self._poller = LivePoller(machines.load_machines(machines_path),
                                  verify_tls=verify_tls,
                                  client_factory=client_factory)
        # 最近一次同步进 poller 的清单原文(与 serve.sync_poller 的
        # last_text 同角色):live() 据此感知带外(手改文件)的清单变更
        self._last_text: str | None = None

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

    def machines(self) -> dict:
        """读清单并掩码返回(每次现读文件,外部改动即时可见)。

        文件缺失/权限/非 UTF-8 归一为一条可诊断错误而非抛异常——
        与 serve GET /api/machines 同语义,配置页据此提示而不崩页面。
        """
        try:
            text = Path(self.machines_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            return {"text": "", "machines": [],
                    "errors": [f"清单文件不存在或不可读: "
                               f"{self.machines_path} ({e})"]}
        ms, errs = machines.parse_machines(text)
        # 编辑器文本用 mask_text:注释/空行/坏行原样保留,首次保存不丢
        return {"text": machines.mask_text(text),
                "machines": _masked_dicts(ms),
                "errors": errs}

    def save_machines(self, text: str) -> dict:
        """保存编辑后的清单:密码列仍是 **** 视为未修改,从旧文件回填明文。

        有错(解析失败/**** 对不上)整体拒绝、不落盘半新半旧的状态;
        成功后热更新 poller,新清单立即对 live() 生效,无需重启。
        """
        merged, errs = machines.merge_masked(
            text, machines.load_machines(self.machines_path))
        if errs:
            return {"ok": False, "errors": errs}
        machines.save_machines(self.machines_path, merged)  # 明文落盘 + 0600
        self._poller.set_machines(merged)
        # save 绕过了 _sync_poller 的文件对账:同样失效 last_text,下次
        # live() 无条件重读(与 serve POST 的处理一致),文件若被外部
        # 字节级还原也不会让 poller 滞留在本次保存的清单上
        self._last_text = None
        return {"ok": True, "machines": _masked_dicts(merged), "errors": []}

    def _sync_poller(self) -> None:
        """live() 前与清单文件对账:手改文件(带外编辑)无需重启即生效。

        与 serve.sync_poller 同思路:原文未变直接返回(只剩一次小读取),
        有变才 set_machines —— 凭据未变的机器缓存原样保留,不重连不重拉。
        """
        try:
            text = Path(self.machines_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return  # 缺失/非 UTF-8:保持当前轮询状态,不在 live 路径抛错
        if text == self._last_text:
            return
        self._last_text = text
        # 坏行只影响坏行:合法机器照常轮询,解析错误由 machines() 呈现
        self._poller.set_machines(machines.parse_machines(text)[0])

    def live(self, alias: str) -> dict:
        self._sync_poller()   # 先对账:Select 里新出现的机器 live 得到响应
        payload = self._poller.read(alias)
        if payload.get("ok"):
            return payload
        # 剥掉内部 code,对齐远程 404/502 的 {"ok": False, "error"} 形态,
        # 视图层(配置页/远程机器页)两端免分支
        return {"ok": False, "error": payload.get("error", "")}


class RemoteDataSource:
    """经 bmon-serve HTTP API 访问(供其他机器远程跑 TUI)。

    机器清单与实时读数同样走 serve 代理:远端主机无需到各 BMC 的网络路由,
    拉取全部发生在 serve 侧。
    """

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

    def machines(self) -> dict:
        # 掩码已在 serve 侧完成,响应体即目标形态,原样透传
        return self._get("/api/machines")

    def save_machines(self, text: str) -> dict:
        # 400 是清单校验失败的业务响应:回传 body 而非抛异常,与本地端语义一致;
        # 其余非 2xx(422/500 等)与传输错误仍按 _get 的抛出语义处理
        r = requests.post(self.base + "/api/machines", json={"text": text},
                          timeout=10)
        if r.status_code != 400:
            r.raise_for_status()
        return r.json()

    def live(self, alias: str) -> dict:
        # 别名进入 URL 路径段:含 / ? # 等保留字符的别名必须整体转义,
        # 否则路径被拆歪、请求打到非预期的路由
        quoted = urllib.parse.quote(alias, safe="")
        try:
            return self._get(f"/api/live/{quoted}/sensors")
        except requests.HTTPError as e:
            # 404/502 是 serve 的业务错误 body({"ok": False, "error"}):
            # 与本地端对齐返回而非抛;其余状态维持 _get 原语义抛出
            if e.response is not None and e.response.status_code in (404, 502):
                return e.response.json()
            raise
