import argparse
from contextlib import contextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import machines, queries, webpage
from .config import load_config
from .db import connect
from .live import LivePoller


def create_app(db_path: str, machines_path: str = "machines.txt",
               verify_tls: bool = False, client_factory=None) -> FastAPI:
    """新参均带默认:既有 create_app(path) 调用方与测试不破。"""
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

    # ---- 机器清单与按需轮询:远程 TUI 的配置页/实时页都经由这两个 API ----
    # 代理的意义:远端客户端无需到各 BMC 的网络路由,拉取全部发生在 serve 侧

    def read_machines_text() -> tuple[str, list[str]]:
        """读清单原文;缺失/权限/非 UTF-8 统一回 (空文本, 一条可诊断错误),
        绝不让 API 因文件问题 500(与 collect 的清单监视同口径)。"""
        try:
            return Path(machines_path).read_text(encoding="utf-8"), []
        except (OSError, UnicodeDecodeError) as e:
            return "", [f"清单文件不存在或不可读: {machines_path} ({e})"]

    def masked_dicts(ms: list[machines.Machine]) -> list[dict]:
        # 对外只出掩码 dict:任何响应里都不出现明文密码
        return [{"ip": m.ip, "user": m.user, "password": machines.MASK,
                 "alias": m.alias} for m in ms]

    # 持引用:POST 保存后原地刷新;/api/live 每次请求前与文件对账
    poller = LivePoller(machines.load_machines(machines_path),
                        verify_tls=verify_tls, client_factory=client_factory)
    last_text: list = [None]  # 闭包可变状态:最近一次同步进 poller 的清单原文

    def sync_poller() -> None:
        """/api/live 前重读清单:原文未变直接返回(只剩一次小读取),
        有变才 set_machines——凭据未变的机器缓存原样保留,不重连不重拉。"""
        text, _errs = read_machines_text()
        if text == last_text[0]:
            return
        last_text[0] = text
        # 坏行只影响坏行:合法机器照常轮询,解析错误由 GET /api/machines 呈现
        poller.set_machines(machines.parse_machines(text)[0])

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
        if max_points < 1 or max_points > 10000:
            raise HTTPException(400, "max_points 须在 1..10000")
        with conn() as c:
            return queries.get_history(c, sensor, range_key, max_points)

    @app.get("/api/extremes")
    def api_extremes(sids: str, range_key: str = Query(default="1h", alias="range")):
        try:
            sid_list = [int(x) for x in sids.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(400, "sids 须为逗号分隔整数")
        if range_key not in queries.RANGES:
            raise HTTPException(400, f"range 取值: {sorted(queries.RANGES)}")
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

    @app.get("/api/machines")
    def api_machines_get():
        text, read_errs = read_machines_text()
        ms, parse_errs = machines.parse_machines(text)
        # 编辑器文本用 mask_text:注释/空行/坏行原样保留(手写内容不被
        # 第一次保存静默删掉);machines/errors 仍由 parse 结果提供
        return {"text": machines.mask_text(text),
                "machines": masked_dicts(ms),
                "errors": read_errs + parse_errs}

    class MachinesBody(BaseModel):
        text: str

    @app.post("/api/machines")
    def api_machines_post(body: MachinesBody):
        # merge_masked:密码列仍是 **** 视为未修改,从旧清单回填明文再落盘
        merged, errs = machines.merge_masked(
            body.text, machines.load_machines(machines_path))
        if errs:
            # 含错整体拒绝,不落盘半新半旧的状态
            return JSONResponse(status_code=400,
                                content={"ok": False, "errors": errs})
        machines.save_machines(machines_path, merged)
        poller.set_machines(merged)  # 保存后立即生效,不等下一次 /api/live
        # POST 改了文件却没走 sync_poller 的对账:last_text 失效,让下次
        # /api/live 无条件重读 —— 否则文件被外部字节级还原回旧原文时,
        # 会因"原文未变"跳过同步,轮询器滞留在 POST 的清单上
        last_text[0] = None
        return {"ok": True, "machines": masked_dicts(merged), "errors": []}

    @app.get("/api/live/{alias}/sensors")
    def api_live_sensors(alias: str):
        sync_poller()
        payload = poller.read(alias)
        if payload.get("ok"):
            return payload
        # poller 契约:404=未知别名,502=BMC 拉取失败;body 统一 {"ok": False, "error"}
        return JSONResponse(status_code=payload.get("code", 502),
                            content={"ok": False, "error": payload.get("error", "")})

    @app.get("/", response_class=HTMLResponse)
    def index(range_key: str = Query(default="24h", alias="range")):
        if range_key not in queries.RANGES:
            raise HTTPException(400, f"range 取值: {sorted(queries.RANGES)}")
        with conn() as c:
            return webpage.render_index(c, range_key)

    @app.get("/group/{grp}", response_class=HTMLResponse)
    def group_page(grp: str, range_key: str = Query(default="24h", alias="range")):
        if range_key not in queries.RANGES:
            raise HTTPException(400, f"range 取值: {sorted(queries.RANGES)}")
        with conn() as c:
            return webpage.render_group(c, grp, range_key)

    @app.get("/sel", response_class=HTMLResponse)
    def sel_page():
        with conn() as c:
            return webpage.render_sel(c)

    @app.get("/sdc", response_class=HTMLResponse)
    def sdc_page():
        with conn() as c:
            return webpage.render_sdc(c)

    return app


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bmon-serve")
    ap.add_argument("--config", default="config.toml")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    uvicorn.run(create_app(cfg.db_path, machines_path=cfg.machines_file,
                           verify_tls=cfg.bmc_verify_tls),
                host=cfg.serve_host, port=cfg.serve_port)
