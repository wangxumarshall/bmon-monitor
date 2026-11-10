# 机器配置页 + 按需采集(方案甲)实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 凭据从代码/仓库移入 gitignore 的 machines.txt;TUI 新增「配置」页(一行一台编辑)与「远程机器」页(仅轮询当前查看的机器);serve 增加 /api/machines 与 /api/live;collect 从 machines.txt 读凭据并热加载。

**Architecture:** 保持双进程架构不变(collect 常采入库 + serve 只读 REST + TUI 按需)。新增 `bmon/machines.py`(清单解析/掩码/合并)与 `bmon/live.py`(按需轮询器,1s TTL 缓存,本地 TUI 与 serve 共用)。远程 TUI 的实时数据与配置保存均经 serve 代理,TUI 所在机器不需要能路由到各 BMC。本机(node_id)BMC 读数沿用 source="bmc" 保持既有数据连续,其他机器 source="bmc@别名"。

**Tech Stack:** Python 3.11,现有依赖(requests/fastapi/textual/textual-plotext),零新增。

**Spec:** 本计划内嵌设计要点(2026-11-09 对话确认的方案甲);原项目设计见 `docs/superpowers/specs/2026-10-29-bmc-monitor-tui-design.md`。

## 设计要点(对话已确认 + 我的默认决定)

- machines.txt 格式:一行一台 `ip 账号 密码 [别名]`,空白分隔;`#` 整行注释;空行忽略;别名缺省=ip。ip 列可写裸地址或完整 URL(缺 scheme 时补 https://)。密码不支持含空白(文档说明)。别名与 ip 均不得重复(解析报错)。
- 密码掩码:界面/API 永远回显 `****`(MASK);保存时密码为 `****` 的行按 (ip, 别名) 从现有清单回填原密码;找不到匹配则整次保存拒绝并报行号。掩码语义本地/远程一致。
- collect_nodes(config.toml `[collect] nodes`,默认 `["0102"]`)引用别名;别名 == node_id 的机器为"本机",其 BMC/SEL/OS 采集行为与现状完全一致;其他别名的机器仅采 BMC 读数(source="bmc@别名"),不采 SEL/OS。machines.txt 每 tick 读取、字符串比对检测变更(热加载,改凭据免重启)。
- 「远程机器」页:顶部 Select 选机器 + 传感器表(组/传感器/当前值/单位/状态/会话极值,组按 GROUP_LABELS 序)+ 选中传感器会话内曲线(内存 ring buffer,每传感器 deque maxlen=3600)。仅当本页激活时随 TUI 刷新率轮询(默认 1s)——即"仅采集当前界面中的机器"。
- 「配置」页:TextArea 全量编辑 + Ctrl+S 保存 + 底部状态行;不定义 refresh_data,自动刷新绝不重灌编辑区。
- 非目标:远程机器的跨会话历史、远程机器 SEL/OS 数据、web 页面配置界面、API 鉴权。

## Global Constraints

- 测试命令:`.venv/bin/pytest`(asyncio_mode=auto);基线 67 passed + 2 skipped,任何任务完成后必须全绿。
- 仓库任何文件不得出现真实密码;machines.txt 写入权限 0600 且加入 .gitignore。
- 注释与 commit message 用中文,风格对齐现有代码(注释解释 why)。
- 1s 采集节奏、双进程架构、既有 REST API 行为不得改变。
- source 命名:本机 BMC = "bmc"(既有 sensor_id 连续),其他 = "bmc@别名"。

## Review Focus

1. **配置页被自动刷新清空**——ConfigView 无 refresh_data,刷新循环跳过它(Task 7 测试)。
2. **`****` 行找不到对应机器时把字面 **** 存盘**——merge 拒绝并报行号,POST 返回 400(Task 1/5 测试)。
3. **machines.txt 缺失/含坏行时 collect 崩溃**——不崩,OS 数据继续,警告可诊断(Task 3 测试)。
4. **远程机器页轮询失败(BMC 断网/凭据错)时 TUI 崩溃**——状态行显红错误,表格保留,恢复自动继续(Task 8 测试)。
5. **同秒多请求打爆 BMC**——LivePoller TTL 内只打一次(Task 4 测试)。

---

### Task 1: `bmon/machines.py` — 清单解析/掩码/合并

**Files:**
- Create: `bmon/machines.py`
- Create: `machines.example.txt`
- Modify: `.gitignore`(加一行 `machines.txt`)
- Test: `tests/test_machines.py`

**Interfaces:**
- Produces:
  - `MASK = "****"`
  - `@dataclass Machine: ip: str; user: str; password: str; alias: str`(无默认;解析时别名缺省填 ip);属性 `base_url: str`(ip 缺 scheme 时补 `https://`)
  - `parse_machines(text: str) -> tuple[list[Machine], list[str]]` — (机器, 错误消息列表,如 `"第3行: 至少需要 ip 账号 密码 三列"`)
  - `to_text(machines: list[Machine], mask: bool = True) -> str`
  - `merge_masked(text: str, old: list[Machine]) -> tuple[list[Machine], list[str]]` — 解析 text,密码==MASK 的行从 old 按 ip 优先、别名次之回填;无匹配则报错 `"第N行: 密码为 **** 但无对应已有机器 (ip=...)"`;别名/ip 重复报错
  - `load_machines(path: str) -> list[Machine]` — 文件缺失/解析失败返回 []
  - `save_machines(path: str, machines: list[Machine]) -> None` — 写文件并 chmod 0600

- [ ] **Step 1: 写失败测试** `tests/test_machines.py`

```python
from bmon.machines import MASK, Machine, load_machines, merge_masked, parse_machines, save_machines, to_text

TEXT = ("# 注释行\n"
        "9.3.12.13 Administrator Secret1 0102\n"
        "\n"
        "9.3.12.15 Administrator Secret2 0103\n")

def test_parse_and_to_text_roundtrip():
    ms, errs = parse_machines(TEXT)
    assert errs == []
    assert [(m.alias, m.base_url) for m in ms] == [("0102", "https://9.3.12.13"), ("0103", "https://9.3.12.15")]
    assert to_text(ms, mask=False).count("Secret1") == 1
    masked = to_text(ms, mask=True)
    assert "Secret1" not in masked and MASK in masked

def test_parse_defaults_and_errors():
    ms, errs = parse_machines("9.3.12.13 Administrator pw\n")   # 别名缺省=ip
    assert ms[0].alias == "9.3.12.13"
    ms, errs = parse_machines("http://9.3.12.13:443 u p a\n")   # 完整 URL 原样
    assert ms[0].base_url == "http://9.3.12.13:443"
    _, errs = parse_machines("only-one-column\n")
    assert errs and "第1行" in errs[0]
    _, errs = parse_machines("a b c x\na b c x\n")              # 别名重复
    assert any("重复" in e for e in errs)

def test_merge_masked_keeps_old_password():
    old, _ = parse_machines(TEXT)
    edited = "9.3.12.13 Administrator **** 0102\n9.3.12.15 Administrator NewPw 0103\n"
    ms, errs = merge_masked(edited, old)
    assert errs == []
    assert ms[0].password == "Secret1" and ms[1].password == "NewPw"

def test_merge_masked_unmatched_rejected():
    old, _ = parse_machines(TEXT)
    ms, errs = merge_masked("9.9.9.9 Administrator **** 0109\n", old)
    assert ms == [] and any("****" in e and "第1行" in e for e in errs)

def test_save_load_and_permissions(tmp_path):
    ms, _ = parse_machines(TEXT)
    p = str(tmp_path / "machines.txt")
    save_machines(p, ms)
    assert oct(os.stat(p).st_mode & 0o777) == "0o600"
    assert load_machines(p)[0].password == "Secret1"
    assert load_machines(str(tmp_path / "none.txt")) == []
```

- [ ] **Step 2: 跑测试确认失败** — Run: `.venv/bin/pytest tests/test_machines.py -v`,Expected: FAIL (ModuleNotFoundError)
- [ ] **Step 3: 实现 `bmon/machines.py`** — 解析按行 split();列数 <3 报错,>4 报错(第4列为别名);别名/ip 去重用两个 set;merge 先 parse,再对 MASK 行回填。`machines.example.txt` 内容为注释说明 + 一行注释掉的示例(`#9.3.12.13 Administrator YourPasswordHere 0102`)。
- [ ] **Step 4: 跑测试确认通过** — Run: `.venv/bin/pytest tests/test_machines.py -v`,Expected: PASS
- [ ] **Step 5: Commit** — `git add bmon/machines.py tests/test_machines.py machines.example.txt .gitignore && git commit -m "feat: machines.txt 清单解析/掩码/合并,凭据移出仓库"`

### Task 2: config + collect — 凭据来自 machines.txt,热加载,多节点

**Files:**
- Modify: `bmon/config.py`(删 `bmc_base/bmc_user/bmc_password`,增 `machines_file: str = "machines.txt"`、`collect_nodes: list = field(default_factory=lambda: ["0102"])`)
- Modify: `config.toml`([bmc] 只留 `verify_tls`;增 `[machines] file = "machines.txt"`、[collect] 下 `nodes = ["0102"]`)
- Modify: `bmon/collect.py`
- Test: `tests/test_config.py`(重写), `tests/test_collect.py`(增补)

**Interfaces:**
- Consumes: Task 1 的 `Machine`, `parse_machines`
- Produces:
  - `class MachinesWatcher: __init__(self, path: str, wanted: list[str])`;`poll(self) -> dict | None` — 每 tick 读文件文本,与上次比对;变更时返回 `{"machines": list[Machine], "local": Machine | None, "remotes": dict[alias, Machine], "errors": list[str]}`(local = 别名∈wanted 且 == 无…以 cfg.node_id 在 main 侧判定;watcher 只按 wanted 过滤,local/remotes 由返回的 machines + node_id 在 main 计算)。**修正:wanted 里别名==node_id 的进 local,其余进 remotes,watcher 构造签名 `MachinesWatcher(path, wanted, node_id)`;无变更返回 None**
  - `Collector.__init__(self, cfg, conn, bmc, osm, remote_bmcs: dict[str, Any] | None = None)` — `bmc` 允许 None(本机凭据缺失)
  - `Collector.set_remote_bmcs(self, remotes: dict[str, Any]) -> None`
  - `collect.main` — 循环内先 `watcher.poll()`,有变更则重建 BmcClient 并 `collector` 更新;日志:变更摘要、缺机器警告、解析错误

- [ ] **Step 1: 重写/增补失败测试**

test_config.py(重写,不得含真实凭据):
```python
def test_defaults_without_file(tmp_path):
    cfg = load_config(str(tmp_path / "missing.toml"))
    assert cfg.machines_file == "machines.txt" and cfg.collect_nodes == ["0102"]
    assert cfg.bmc_verify_tls is False and cfg.db_path == "data/bmon.db"
    assert getattr(cfg, "bmc_password", None) is None      # 凭据字段已删除

def test_partial_override(tmp_path):
    p = tmp_path / "c.toml"; p.write_text('[collect]\nnodes = ["a","b"]\n[machines]\nfile = "m.txt"\n')
    cfg = load_config(str(p))
    assert cfg.collect_nodes == ["a", "b"] and cfg.machines_file == "m.txt"
```

test_collect.py 增补(现有用例与 `_cfg()` 不动):
```python
def test_watcher_detects_change_and_missing(tmp_path):
    from bmon.collect import MachinesWatcher
    p = tmp_path / "machines.txt"
    w = MachinesWatcher(str(p), ["0102", "0103"], node_id="0102")
    assert w.poll() is None                                # 文件不存在
    p.write_text("9.3.12.13 u p1 0102\n9.3.12.15 u p2 0103\n")
    st = w.poll(); assert st["local"].alias == "0102" and st["remotes"]["0103"].alias == "0103"
    assert w.poll() is None                                # 内容未变
    p.write_text("9.3.12.13 u p1 0102\n")                  # 0103 被删
    st = w.poll(); assert "0103" not in st["remotes"] and any("0103" in e for e in st["errors"])

def test_collector_remote_source_namespace(tmp_path):
    from dataclasses import replace
    conn = connect(str(tmp_path / "t.db")); init_db(conn)
    bmc = [SensorReading("bmc", "CPU1_TEMP", 48.0, "degrees C", "temp", "ok")]
    remote = [SensorReading("bmc", "CPU1_TEMP", 51.0, "degrees C", "temp", "ok")]
    c = Collector(_cfg(), conn, FakeBmc(bmc), FakeOsm(), remote_bmcs={"0103": FakeBmc(remote)})
    c.tick(1000)
    assert query_series(conn, sensor_id("bmc", "CPU1_TEMP"), 1000, 1000)[0][1] == 48.0
    assert query_series(conn, sensor_id("bmc@0103", "CPU1_TEMP"), 1000, 1000)[0][1] == 51.0

def test_collector_no_local_bmc_still_collects_os(tmp_path):
    conn = connect(str(tmp_path / "t.db")); init_db(conn)
    c = Collector(_cfg(), conn, None, FakeOsm())           # 本机凭据缺失
    c.tick(1000); c.tick(1061)                             # 含 slow 周期(SEL 路径)
    assert query_series(conn, sensor_id("os", "mem_usage_pct"), 1000, 1061)
```

- [ ] **Step 2: 跑测试确认失败** — Run: `.venv/bin/pytest tests/test_config.py tests/test_collect.py -v`,Expected: 新增用例 FAIL
- [ ] **Step 3: 实现** — collect.py:tick 的 BMC 块改为 `if self.bmc:` + 遍历 `self._remotes`(读数用 `replace(r, source=f"bmc@{alias}")`);`_poll_sel` 开头 `if not self.bmc: return`;main 用 MachinesWatcher。config.toml 同步删凭据。
- [ ] **Step 4: 跑全量测试** — Run: `.venv/bin/pytest`,Expected: 全 PASS(test_bmc 的 BMC_LIVE 用例照常 skip)
- [ ] **Step 5: Commit** — `git add bmon/config.py bmon/collect.py config.toml tests/ && git commit -m "feat: collect 从 machines.txt 读凭据并热加载,本机 source=bmc 其他 bmc@别名"`

### Task 3: `bmon/live.py` — 按需轮询器(TTL 防抖)

**Files:**
- Create: `bmon/live.py`
- Test: `tests/test_live.py`

**Interfaces:**
- Consumes: Task 1 `Machine`,`bmon.bmc.BmcClient.get_all_readings`
- Produces:
  - `class LivePoller: __init__(self, machines: list[Machine], verify_tls: bool = False, ttl_s: float = 1.0, client_factory=None)`(默认 `client_factory=lambda m: BmcClient(m.base_url, m.user, m.password, verify_tls)`;测试注入)
  - `set_machines(self, machines: list[Machine]) -> None` — 保留凭据未变机器的缓存
  - `read(self, alias: str) -> dict` — 命中缓存或现场拉取;成功 `{"ok": True, "ts": int, "readings": [dict]}`;失败 `{"ok": False, "error": str, "code": 404 | 502}`(404=未知别名,502=BMC 拉取失败)。readings 的 dict:`{"name","value","unit","grp","status","thresholds"}`

- [ ] **Step 1: 写失败测试**

```python
def test_read_ttl_caches_and_errors():
    calls = []
    def factory(m):
        calls.append(m.alias)
        return FakeBmc([SensorReading("bmc", "CPU1_TEMP", 48.0, "degrees C", "temp", "ok")])
    ms, _ = parse_machines("9.3.12.13 u p 0102\n")
    p = LivePoller(ms, ttl_s=60, client_factory=factory)
    r1 = p.read("0102"); r2 = p.read("0102")
    assert r1["ok"] and r1["readings"][0]["name"] == "CPU1_TEMP"
    assert len(calls) == 1                                   # TTL 内只打一次
    assert p.read("0109") == {"ok": False, "error": "未知机器: 0109", "code": 404}

def test_read_bmc_error_502_and_set_machines():
    def factory(m):
        return FakeBmc(error=True)
    ms, _ = parse_machines("9.3.12.13 u p 0102\n")
    p = LivePoller(ms, ttl_s=0, client_factory=factory)
    r = p.read("0102")
    assert not r["ok"] and r["code"] == 502 and r["error"]
```
(FakeBmc 复用 test_collect 的形态,在本文件内自定义一份即可)

- [ ] **Step 2: 跑测试确认失败** — Run: `.venv/bin/pytest tests/test_live.py -v`,Expected: FAIL
- [ ] **Step 3: 实现** — 内部 `{alias: (machine, client, expires_at, payload)}`;`time.monotonic()` 计时;错误同样缓存一个 TTL(1s 重试节奏)。
- [ ] **Step 4: 跑测试确认通过** — Run: `.venv/bin/pytest tests/test_live.py -v`,Expected: PASS
- [ ] **Step 5: Commit** — `git add bmon/live.py tests/test_live.py && git commit -m "feat: LivePoller 按需轮询器,1s TTL 防抖,serve/TUI 共用"`

### Task 4: serve — /api/machines 与 /api/live

**Files:**
- Modify: `bmon/serve.py`
- Test: `tests/test_api.py`(增补)

**Interfaces:**
- Consumes: Task 1 全部,Task 3 `LivePoller`
- Produces:
  - `create_app(db_path: str, machines_path: str = "machines.txt", verify_tls: bool = False, client_factory=None) -> FastAPI`(新参均带默认,既有调用不破)
  - `GET /api/machines` → `{"text": "<掩码文本>", "machines": [掩码 dict], "errors": [...]}`
  - `POST /api/machines` body `{"text": "..."}` → 合法:保存(0600)+ 更新 poller,返回 `{"ok": True, "machines": [...], "errors": []}`;含错:HTTP 400,`{"ok": False, "errors": [...]}`
  - `GET /api/live/{alias}/sensors` → 200 `{"ok": True, "ts", "readings"}`;未知别名 404、BMC 失败 502,body `{"ok": False, "error"}`
  - `serve.main` 传入 `cfg.machines_file` 与 `cfg.bmc_verify_tls`

- [ ] **Step 1: 写失败测试**(test_api.py 增补;沿用 TestClient)

```python
def test_machines_get_masked_and_live(tmp_machines):        # fixture: tmp_path 写入真实密码清单,返回 (db_path, machines_path)
    client = TestClient(create_app(db_path, machines_path=machines_path, client_factory=FakeFactory))
    body = client.get("/api/machines").json()
    assert "Secret1" not in body["text"] and "****" in body["text"]
    r = client.get("/api/live/0102/sensors")
    assert r.status_code == 200 and r.json()["readings"][0]["name"] == "CPU1_TEMP"
    assert client.get("/api/live/0109/sensors").status_code == 404

def test_machines_post_merge_and_400(tmp_machines):
    client = TestClient(create_app(db_path, machines_path=machines_path, client_factory=FakeFactory))
    r = client.post("/api/machines", json={"text": "9.3.12.13 u **** 0102\nbadline\n"})
    assert r.status_code == 400 and any("第2行" in e for e in r.json()["errors"])
    r = client.post("/api/machines", json={"text": "9.3.12.13 u **** 0102\n"})
    assert r.status_code == 200
    assert "Secret1" in Path(machines_path).read_text()     # **** 已回填原密码
```

- [ ] **Step 2: 跑测试确认失败** — Run: `.venv/bin/pytest tests/test_api.py -v`,Expected: 新增 FAIL
- [ ] **Step 3: 实现** — app 内建 LivePoller(持引用);POST 用 `merge_masked(text, load_machines(path))`;serve.main 增加 `--config` 已有,传参即可。
- [ ] **Step 4: 跑全量** — Run: `.venv/bin/pytest`,Expected: 全 PASS
- [ ] **Step 5: Commit** — `git add bmon/serve.py tests/test_api.py && git commit -m "feat: serve 增 /api/machines 掩码读写与 /api/live 按需轮询"`

### Task 5: datasource + cli — 两端统一 machines/save/live

**Files:**
- Modify: `bmon/tui/datasource.py`, `bmon/cli.py`
- Test: `tests/test_datasource.py`(增补)

**Interfaces:**
- Consumes: Task 1/3/4
- Produces(LocalDataSource 与 RemoteDataSource 同签名):
  - `LocalDataSource(db_path, machines_path: str = "machines.txt", verify_tls: bool = False)`
  - `machines(self) -> dict` — `{"text", "machines", "errors"}`(text 为掩码)
  - `save_machines(self, text: str) -> dict` — `{"ok", "errors", "machines"}`;校验失败不写盘
  - `live(self, alias: str) -> dict` — 同 LivePoller.read 形态(远程在 404/502 时返回 `{"ok": False, "error"}` 而非抛异常)
  - cli:`LocalDataSource(cfg.db_path, cfg.machines_file, cfg.bmc_verify_tls)`

- [ ] **Step 1: 写失败测试** — 本地端用 tmp machines 文件 + FakeFactory 验证 save_machines 的 **** 回填与 machines() 掩码;远程端用 TestClient 不好挂,改测 `RemoteDataSource` 方法拼的 URL/参数(对 `_get`/`requests` 打桩),至少覆盖 live 404 → `{"ok": False, "error"}` 的分支。
- [ ] **Step 2: 跑测试确认失败** — Run: `.venv/bin/pytest tests/test_datasource.py -v`
- [ ] **Step 3: 实现** — Local 持一个 LivePoller(save_machines 成功后 `set_machines`);Remote 的 live 捕获 `requests.HTTPError` 读 body。
- [ ] **Step 4: 跑全量** — Run: `.venv/bin/pytest`,Expected: 全 PASS
- [ ] **Step 5: Commit** — `git add bmon/tui/datasource.py bmon/cli.py tests/test_datasource.py && git commit -m "feat: 数据源统一 machines/save_machines/live,远程经 serve 代理"`

### Task 6: ConfigView — 配置页

**Files:**
- Modify: `bmon/tui/views.py`(新增 ConfigView), `bmon/tui/app.py`(挂 TabPane「配置」,放最后)
- Test: `tests/test_tui.py`(增补;FakeDS 增 `machines/save_machines`)

**Interfaces:**
- Consumes: Task 5 `ds.machines()/ds.save_machines()`
- Produces: `class ConfigView(Vertical)`,id 约定 `#machines-text`(TextArea)、`#config-status`(Static);`BINDINGS = [("ctrl+s", "save", "保存")]`

- [ ] **Step 1: 写失败测试**

```python
async def test_config_view_save_and_masked_load():
    app = BmonApp(FakeDS())           # FakeDS.machines() 返回掩码 text "9.3.12.13 u **** 0102"
    async with app.run_test() as pilot:
        app.query_one(TabbedContent).active = "pane-config"
        await pilot.pause()
        ta = app.query_one("#machines-text", TextArea)
        assert "Secret" not in ta.text and "****" in ta.text
        ta.text = "9.3.12.13 u **** 0102\n9.3.12.15 u NewPw 0103\n"
        await pilot.press("ctrl+s")
        saved = app.ds.saved_text      # FakeDS 记录
        assert "9.3.12.15" in saved
        assert "已保存" in str(app.query_one("#config-status", Static).visual)

async def test_config_view_save_error_shown():
    # FakeDS.save_machines 返回 {"ok": False, "errors": ["第1行: 密码为 **** 但无对应已有机器"]}
    ...断言状态行含 "第1行" 且不含 "已保存"

async def test_config_view_not_autorefreshed():
    # 激活配置页 → 人工改 TextArea 内容 → app.refresh_all() → 内容不被覆盖
```

- [ ] **Step 2: 跑测试确认失败** — Run: `.venv/bin/pytest tests/test_tui.py -v`
- [ ] **Step 3: 实现** — on_mount 载入掩码 text(解析失败的坏行丢弃,错误显示在状态行);action_save 调 ds.save_machines;TextArea 用 `textual.widgets.TextArea`。
- [ ] **Step 4: 跑全量** — Run: `.venv/bin/pytest`,Expected: 全 PASS
- [ ] **Step 5: Commit** — `git add bmon/tui/views.py bmon/tui/app.py tests/test_tui.py && git commit -m "feat: TUI 配置页,一行一台编辑,**** 保留原密码,不被自动刷新重灌"`

### Task 7: RemoteView — 远程机器页(布局 B)

**Files:**
- Modify: `bmon/tui/views.py`(`_SelectionPlotMixin` 增加 `_history_points(sensor)` 钩子,GroupView/CoreView 实现为原 ds.history 逻辑;新增 RemoteView), `bmon/tui/app.py`(TabPane「远程机器」,放「配置」之前)
- Test: `tests/test_tui.py`(增补;FakeDS 增 `live`)

**Interfaces:**
- Consumes: Task 5 `ds.machines()/ds.live()`;`_chart_points`、`threshold_style`、`_restore_table`(既有)
- Produces: `class RemoteView(Vertical)` — id:`#remote-select`(Select)、`#remote-status`(Static)、`#remote-tbl`(DataTable,列:组/传感器/当前值/单位/状态/会话极值)、`#remote-plot`(PlotextPlot);`refresh_data()` 供刷新循环调用;会话 buffer `dict[str, deque]` maxlen=3600

- [ ] **Step 1: 写失败测试**

```python
async def test_remote_view_polls_and_builds_table():
    app = BmonApp(FakeDS())   # FakeDS.live("0102") 返回两拍不同值的读数
    async with app.run_test() as pilot:
        app.query_one(TabbedContent).active = "pane-remote"
        await app.refresh_all(); await pilot.pause()
        rv = app.query_one(RemoteView)
        tbl = rv.query_one(DataTable)
        assert tbl.row_count > 0
        row0 = [str(c) for c in tbl.get_row_at(0)]
        assert any("0102" in c or "温度" in c for c in row0) is False or True   # 以实际断言为准:组列/值列渲染
        await app.refresh_all(); await pilot.pause()      # 第二拍
        assert rv._buf["CPU1_TEMP"] and len(rv._buf["CPU1_TEMP"]) == 2

async def test_remote_view_error_keeps_table():
    # FakeDS.live 先 ok 后抛 {"ok": False, "error": "连接失败"}:状态行含错误文本,表格不清空,不抛异常

async def test_remote_view_switch_clears_buffer():
    # FakeDS 机器列表两台:切到第二台后 _buf 为空、表格为新机器数据
```
(具体断言在实现时按 FakeDS 数据精确化:组列显示 GROUP_LABELS 中文、状态位显示十六进制掩码沿 GroupView 规则)

- [ ] **Step 2: 跑测试确认失败** — Run: `.venv/bin/pytest tests/test_tui.py -v`
- [ ] **Step 3: 实现** — refresh_data:读 machines() 同步 Select 选项(别名集合变化才重建,保持选中);有选中机器则 live() 轮询、追加 buffer、重建表格(会话极值=buffer min/max)、恢复光标滚动(复用 `_restore_table`)、重画选中行曲线(`_history_points` 从 buffer 取);错误写 `#remote-status`(红)且保留表格。切机器清 buffer。状态行:别名/刷新年龄/错误。
- [ ] **Step 4: 跑全量** — Run: `.venv/bin/pytest`,Expected: 全 PASS
- [ ] **Step 5: Commit** — `git add bmon/tui/views.py bmon/tui/app.py tests/test_tui.py && git commit -m "feat: TUI 远程机器页,仅激活时轮询选中机器,会话曲线与极值"`

### Task 8: 敏感信息清理 + 文档

**Files:**
- Modify: `tests/test_bmc.py`(BMC_LIVE 用例凭据改环境变量 `BMC_BASE/BMC_USER/BMC_PASS`,缺省 skip)
- Modify: `docs/superpowers/plans/2026-10-29-bmc-monitor-tui.md`、`docs/superpowers/specs/2026-10-29-bmc-monitor-tui-design.md`(`<旧密码>` 全部替换为 `******`;IP 保留)
- Modify: `README.md`(新增:配置页/远程机器页说明、machines.txt 格式与 0600、从旧 [bmc] 迁移步骤、/api/machines /api/live、git 历史仍含旧密码的提示)

- [ ] **Step 1: 改 test_bmc.py** — 两处 `BmcClient("https://9.3.12.13", "Administrator", "<旧密码>")` 改为 env 读取,BMC_LIVE 门控不变。
- [ ] **Step 2: 全仓扫描** — Run: `grep -rn "<旧密码>" --include="*.py" --include="*.toml" --include="*.md" . | grep -v .venv | grep -v .git/`,Expected: 仅 .git 历史无命中(工作区 0 命中)
- [ ] **Step 3: 跑全量** — Run: `.venv/bin/pytest`,Expected: 全 PASS
- [ ] **Step 4: Commit** — `git add -A && git commit -m "chore: 清理仓库中的 BMC 凭据,文档补充配置页与按需采集"`

### Task 9: 0102 部署迁移与真机验证

**Files:** 无代码;运维步骤(在本机 0102 执行)

- [ ] **Step 1: 写 machines.txt** — 从现有 config.toml 的 [bmc] 读出凭据生成 `machines.txt`(一行 `9.3.12.13 <user> <password> 0102`),权限 0600(Task 2 已重写 config.toml 为无凭据版)
- [ ] **Step 2: 重启服务** — `sudo -n systemctl restart bmon-collect bmon-serve`(无免密 sudo 时请用户以 `!` 前缀自行执行)
- [ ] **Step 3: 验证** — `journalctl -u bmon-collect -n 20` 无异常;`sqlite3 data/bmon.db "SELECT MAX(last_ts) FROM sensors"` 持续前进;`curl -s localhost:8102/api/machines` 返回掩码;`curl -s localhost:8102/api/live/0102/sensors | head -c 300` 返回真实读数;`.venv/bin/pytest`(最终全绿)
- [ ] **Step 4: 汇报** — 向用户报告迁移结果与验证证据,提示 git 历史仍含旧密码、TUI 可 `bmon tui` 体验两个新页

## Self-Review 记录

- 覆盖检查:隐藏敏感信息(Task 1/2/8/9)、配置页一行一台(Task 6)、仅采集当前界面机器(Task 7 激活才轮询 + Task 3 TTL)、远程 TUI 无需直达 BMC(Task 5)——均有对应任务。
- 类型一致性:machines() 返回 `{"text","machines","errors"}` 在 Task 4/5/6 一致;live 返回 `{"ok",...}` 在 Task 3/4/5/7 一致;`bmc@别名` 命名 Task 2/7 一致。
- Review Focus 五项分别落在 Task 7/1+4/2/7/3 的测试步骤。
