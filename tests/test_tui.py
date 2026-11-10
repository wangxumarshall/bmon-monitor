"""Task 11:TUI 视图与应用的测试。

与 brief 的差异(版本漂移,textual 8.2.8):textual 8.x 的 Static 移除了
`renderable` 属性,断言改用其后继者 `visual`(str() 后同为渲染出的纯文本)。
"""

import time

from textual.widgets import DataTable, Select, Static, TabbedContent, TextArea
from textual_plotext import PlotextPlot

from bmon.tui.app import BmonApp
from bmon.tui.views import (ConfigView, CoreView, GroupView, KpiBar,
                            RemoteView, core_grid_text, threshold_style)


class FakeDS:
    def __init__(self):
        # Task 6 配置页:save_machines 的响应可配置(默认成功一台),
        # saved_text 记录回传全文供断言(掩码 **** 必须原样送回)
        self.saved_text = None
        self.save_resp = {"ok": True, "errors": [],
                          "machines": [{"ip": "9.3.12.13", "user": "u",
                                        "password": "****", "alias": "0102"}]}
        # Task 7 远程机器页:live 有状态 —— 逐拍变值、live_error 可切错误,
        # live_calls 计数供"仅活动页轮询"的核心保证断言
        self.live_error = False
        self.live_calls = 0
        self._live_tick = 0
        # ts 基准 + 递增计数:真实 LivePoller 的 TTL≥1s 保证两次新鲜拉取的
        # int 秒 ts 必不同;假数据源用递增 ts 模拟该不变量 —— 若直接取
        # int(time.time()),同一秒内的两拍会被 ts 去重逻辑误判为缓存命中
        self._ts_base = int(time.time())
        self.machines_extra = []   # 测试注入:会话中途变更机器清单

    def overview(self):
        return {"node_ts": 1, "groups": {"temp": {"count": 1, "last_ts": 1}},
                # 终版评审修复 2:头部机型/核数/内存来自 overview 的 static
                # 载荷(服务器侧 static_info 采集),而非 TUI 本机
                "static": {"hostname": "localhost0102", "cores": 192,
                           "mem_total_gb": 754,
                           "cpu_model": "Kunpeng920_5250 ×4 @2.6GHz"},
                "kpis": [{"id": 1, "name": "cpu_avg_usage", "label": "CPU Avg Usage",
                          "value": 42.0, "unit": "%", "ts": 1, "age_s": 0,
                          "history": [(1, 42.0)]},
                         # 覆盖 age_s=None / value=None 的 KPI(评审修复3):
                         {"id": 9, "name": "psu_pout", "label": "PSU POut",
                          "value": None, "unit": "W", "ts": None, "age_s": None,
                          "history": []}]}

    def sensors(self, group=None):
        if group == "cpu":
            # 图示缺陷 1:cpu_avg/max 组需要独立标签页,FakeDS 提供两个传感器
            return [{"id": 11, "name": "cpu_avg_usage", "grp": "cpu",
                     "unit": "%", "last_value": 42.0, "last_ts": 1,
                     "age_s": 0},
                    {"id": 12, "name": "cpu_max_core_usage", "grp": "cpu",
                     "unit": "%", "last_value": 88.0, "last_ts": 1,
                     "age_s": 0}]
        if group == "temp":
            return [{"id": 2, "name": "CPU1_TEMP", "grp": "temp",
                     "unit": "degrees C", "last_value": 48.0, "last_ts": 1,
                     "age_s": 0, "thr_upper_nc": 78, "thr_upper_crit": 85},
                    {"id": 4, "name": "CPU2_TEMP", "grp": "temp",
                     "unit": "degrees C", "last_value": 49.0, "last_ts": 1,
                     "age_s": 0},
                    # 无读数传感器:age_s=None 应显示"过期"(评审修复3)
                    {"id": 5, "name": "CPU3_TEMP", "grp": "temp",
                     "unit": "degrees C", "last_value": None, "last_ts": None,
                     "age_s": None}]
        if group == "status":
            # 终版评审修复 1:状态位传感器(固件标称断言态非 0,
            # 本机正常 0x8000,数值恒非 0)
            return [{"id": 7, "name": "DIMM1_STATUS", "grp": "status",
                     "unit": "status", "last_value": 32768.0, "last_ts": 1,
                     "age_s": 0, "last_status": "0x8000"},
                    {"id": 8, "name": "CPU1_STATUS", "grp": "status",
                     "unit": "status", "last_value": 32896.0, "last_ts": 1,
                     "age_s": 0, "last_status": "0x8080"},
                    # last_status 缺失:应显示 N/A,而非回退到十进制数值
                    {"id": 10, "name": "MISSING_STATUS", "grp": "status",
                     "unit": "status", "last_value": 32768.0, "last_ts": 1,
                     "age_s": 0, "last_status": None}]
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

    def machines(self):
        # 真实明文密码("Secret" 一类的角色)只存在于文件/BMC 侧;
        # 发给 TUI 的一律掩码 **** —— test_config_view_masked_load 守卫
        # 明文永不漏到界面。Task 7:两台机器,供远程页切换测试
        ms = [{"ip": "9.3.12.13", "user": "u",
               "password": "****", "alias": "0102"},
              {"ip": "9.3.12.15", "user": "u",
               "password": "****", "alias": "0103"}]
        return {"text": "9.3.12.13 u **** 0102\n9.3.12.15 u **** 0103\n",
                "machines": ms + list(self.machines_extra),
                "errors": []}

    def save_machines(self, text):
        self.saved_text = text
        return self.save_resp

    def live(self, alias):
        """有状态 live:同一机器逐拍返回不同值;live_error 切换为错误响应。

        读数形态与 LocalDataSource/RemoteDataSource 完全一致(同一
        LivePoller 序列化):状态位的 status 字段即原始十六进制掩码
        (同 bmc.parse_discrete_sensors)。
        """
        self.live_calls += 1
        if alias not in ("0102", "0103"):
            return {"ok": False, "error": f"未知机器: {alias}"}
        if self.live_error:
            return {"ok": False, "error": "连接失败"}
        self._live_tick += 1
        t = self._live_tick
        if alias == "0102":
            readings = [
                {"name": "CPU1_TEMP", "value": 48.0 + t, "unit": "degrees C",
                 "grp": "temp", "status": "ok",
                 "thresholds": {"upper_nc": 78, "upper_crit": 85}},
                {"name": "DIMM1_STATUS", "value": 32768.0, "unit": "status",
                 "grp": "status", "status": "0x8000", "thresholds": None},
            ]
        else:
            readings = [{"name": "FAN1", "value": 3000.0 + t, "unit": "RPM",
                         "grp": "fan", "status": "ok", "thresholds": None}]
        return {"ok": True, "ts": self._ts_base + t, "readings": readings}


def test_threshold_style():
    th = {"upper_nc": 78, "upper_crit": 85}
    assert threshold_style(48.0, th, "degrees C") == "green"
    assert threshold_style(80.0, th, "degrees C") == "yellow"
    assert threshold_style(90.0, th, "degrees C") == "red"
    # 终版评审修复 1:着色逻辑不再含状态位分支 —— 固件标称断言态非 0
    # (本机正常 0x8000),按数值着色必为假告警;状态位由 GroupView
    # 单独渲染(见 test_status_bit_renders_hex_mask_neutral)
    assert threshold_style(32768.0, None, "status") == "green"
    assert threshold_style(None, th, "C") == "dim"


def test_core_grid_text():
    latest = {f"cpu{i}_usage": 100.0 if i < 5 else 50.0 for i in range(192)}
    text = core_grid_text(latest)
    assert text.count("██") == 192          # 每块=1核
    assert text.count("\n") == 6           # 6 行网格 + 1 行图例
    assert "c000-031" in text and "c160-191" in text   # 行首核号段
    assert "每块=1核" in text               # 图例说明
    assert "[red]" in text                  # 100% 的核着红


def test_core_grid_near_idle_dim():
    """近空闲(<10%)核显示暗灰而非绿色,空闲机器不再是一面绿墙。"""
    latest = {f"cpu{i}_usage": 3.0 for i in range(192)}
    text = core_grid_text(latest)
    assert "[dim]██" in text
    assert "[green]██" not in text


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
        assert "CPU Avg Usage" in str(bar.query_one("#kpi-clock", Static).visual)
        await pilot.press("q")


# ---------- 评审修复 1:选中行大图跟随 + 刷新后选中/滚动保持 ----------

async def test_selection_persists_and_plot_follows():
    app = BmonApp(FakeDS())
    async with app.run_test() as pilot:
        # CPU 现为第一个标签页:激活温度页使其成为刷新目标
        app.query_one(TabbedContent).active = "pane-temp"
        await pilot.pause()
        await app.refresh_all()
        await pilot.pause()
        tv = next(v for v in app.query(GroupView) if v.grp == "temp")
        tbl = tv.query_one(DataTable)
        plot = tv.query_one(PlotextPlot)
        assert tbl.cursor_row == 0
        assert "CPU1_TEMP" in plot.plt.build()  # 初始画第 0 行
        tbl.focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert tbl.cursor_row == 1
        # 光标移动后大图立即跟随到新选中的传感器(而非永远画第 0 行)
        assert "CPU2_TEMP" in plot.plt.build()
        await pilot.press("m")          # 触发一次完整重灌刷新
        for _ in range(3):
            await pilot.pause()
        # 刷新(clear+重灌)后:选中行不回弹到第 0 行,大图画的是选中行
        assert tbl.cursor_row == 1
        assert "CPU2_TEMP" in plot.plt.build()


async def test_core_cursor_and_scroll_persist():
    app = BmonApp(FakeDS())
    async with app.run_test() as pilot:
        app.query_one(TabbedContent).active = "pane-core"
        await pilot.pause()
        await app.refresh_all()
        await pilot.pause()
        core = app.query_one(CoreView)
        tbl = core.query_one(DataTable)
        assert tbl.row_count == 192
        tbl.move_cursor(row=100)        # 滚到深处
        await pilot.pause()
        saved_scroll = tbl.scroll_y
        assert saved_scroll > 50         # 确认确实滚下去了
        await app.refresh_all()          # 1s 周期刷新同款路径:clear+重灌
        for _ in range(3):
            await pilot.pause()
        assert tbl.cursor_row == 100     # 选中行保持
        assert tbl.scroll_y >= 50        # 滚动位置保持,不再每拍跳回顶部


# ---------- 评审修复 2:refresh 失败可诊断且指示不叠加 ----------

class BoomDS(FakeDS):
    def __init__(self):
        self.boom = True

    def overview(self):
        if self.boom:
            raise RuntimeError("boom")
        return super().overview()


async def test_refresh_error_indicator_not_accumulating():
    app = BmonApp(BoomDS())
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.refresh_all()          # 失败 → 指示出现
        await app.refresh_all()          # 再失败 → 指示不得叠加
        assert app.sub_title.count("(refresh error)") == 1
        app.ds.boom = False
        await app.refresh_all()          # 恢复 → 指示清除
        assert "refresh error" not in app.sub_title
        await pilot.press("q")


# ---------- 评审修复 3:age_s 语义(0=新鲜,None=过期) ----------

async def test_age_semantics():
    app = BmonApp(FakeDS())
    async with app.run_test() as pilot:
        # CPU 现为第一个标签页:激活温度页使其成为刷新目标
        app.query_one(TabbedContent).active = "pane-temp"
        await pilot.pause()
        await app.refresh_all()
        await pilot.pause()
        tv = next(v for v in app.query(GroupView) if v.grp == "temp")
        tbl = tv.query_one(DataTable)
        assert tbl.row_count == 3
        # age_s=0:同秒数据,必须是 ok(原 (0 or 9999) 误判过期)
        assert str(tbl.get_row_at(0)[3]) == "ok"
        # age_s=None:从无读数,必须显示"过期"
        assert str(tbl.get_row_at(2)[3]) == "过期"
        # KPI age_s=None:标 ⚠ 而不是抛 TypeError;value=None 显示 N/A
        bar = app.query_one(KpiBar)
        text = str(bar.query_one("#kpi-clock", Static).visual)
        assert "⚠" in text
        assert "N/A" in text
        # 终版评审修复 2:头部机型/核数/内存渲染自 overview 的 static 载荷
        # (服务器侧采集),远程 TUI 不再显示客户端本机规格
        assert "Kunpeng920_5250" in text
        assert "192 cores" in text


# ---------- 终版评审修复 1:状态位显示原始十六进制掩码,不做告警着色 ----------

async def test_status_bit_renders_hex_mask_neutral():
    app = BmonApp(FakeDS())
    async with app.run_test() as pilot:
        app.query_one(TabbedContent).active = "pane-status"
        await app.refresh_all()
        await pilot.pause()
        sv = next(v for v in app.query(GroupView) if v.grp == "status")
        tbl = sv.query_one(DataTable)
        # 值列显示采集的十六进制掩码(而非十进制数值),且中性 dim、非红
        row0 = [str(c) for c in tbl.get_row_at(0)]
        assert "0x8000" in row0[1]
        assert "32768" not in row0[1]
        assert "[red]" not in row0[1] and "[dim]" in row0[1]
        row1 = [str(c) for c in tbl.get_row_at(1)]
        assert "0x8080" in row1[1] and "[red]" not in row1[1]
        # last_status 缺失 → N/A,绝不回退显示十进制数值
        row2 = [str(c) for c in tbl.get_row_at(2)]
        assert "N/A" in row2[1] and "32768" not in row2[1]
        # 非状态位传感器仍按阈值着色(48℃ < upper_nc 78 → green)
        tview = next(v for v in app.query(GroupView) if v.grp == "temp")
        await tview.refresh_data()
        trow0 = [str(c) for c in tview.query_one(DataTable).get_row_at(0)]
        assert "[green]" in trow0[1]


async def test_app_theme_is_dracula():
    """程序员经典配色守卫:主题名漂移(Textual 升级/改名)时立刻报警。"""
    app = BmonApp(FakeDS())
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.theme == "dracula"



def test_group_labels_include_cpu_first():
    """图示缺陷 1:cpu_avg/max 传感器组必须有 TUI 标签页(此前无任何视图)。"""
    from bmon.tui.views import GROUP_LABELS
    assert GROUP_LABELS[0] == ("cpu", "CPU")


def test_chart_points_relative_minutes():
    """图示缺陷 2:图表 x 轴用相对分钟(0=最新)——epoch 秒会渲染天文数字刻度。"""
    from bmon.tui.views import _chart_points
    xs, ys = _chart_points([(1000, 1.0), (1060, 2.0), (1120, 3.0)])
    assert xs == [-2, -1, 0]
    assert ys == [1.0, 2.0, 3.0]
    assert _chart_points([]) == ([], [])


async def test_core_view_plots_selected_core():
    """图示缺陷 3:每核CPU 页必须有选中核的 history 大图(此前无图)。"""
    app = BmonApp(FakeDS())
    async with app.run_test() as pilot:
        app.query_one(TabbedContent).active = "pane-core"
        await pilot.pause()
        await app.refresh_all()
        await pilot.pause()
        core = app.query_one(CoreView)
        tbl = core.query_one(DataTable)
        plot = core.query_one(PlotextPlot)   # 每核页应有图;无则此处即失败
        assert "cpu0_usage" in plot.plt.build()      # 初始画选中行
        tbl.focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert tbl.cursor_row == 1
        assert "cpu1_usage" in plot.plt.build()      # 光标移动,图跟随


async def test_range_keys_reach_core_view():
    """1-6 切范围必须同步到每核页的图表。"""
    app = BmonApp(FakeDS())
    async with app.run_test() as pilot:
        await pilot.press("3")
        assert app.query_one(CoreView).range_key == "12h"
        await pilot.press("6")
        assert app.query_one(CoreView).range_key == "30d"


# ---------- Task 6:配置页(掩码编辑、ctrl+s 保存、不被自动刷新) ----------


async def _open_config(app, pilot):
    """激活配置页并等 on_mount worker 完成掩码清单载入。"""
    app.query_one(TabbedContent).active = "pane-config"
    for _ in range(3):
        await pilot.pause()


async def test_config_view_masked_load():
    """配置页载入的是掩码清单:密码列只见 ****,明文密码永不出现。"""
    app = BmonApp(FakeDS())
    async with app.run_test() as pilot:
        await _open_config(app, pilot)
        ta = app.query_one("#machines-text", TextArea)
        assert "9.3.12.13" in ta.text    # 机器行已载入编辑器
        assert "****" in ta.text         # 密码列是掩码
        assert "Secret" not in ta.text   # 明文密码泄漏守卫


async def test_config_view_save_via_ctrl_s():
    """编辑后 ctrl+s:编辑器全文回传数据源(**** 原样),状态行报成功。

    TextArea 焦点下 ctrl+s 不被吞(实测 textual 8.2.8 冒泡到视图绑定),
    走真实按键路径而非直接调 action。
    """
    app = BmonApp(FakeDS())
    async with app.run_test() as pilot:
        await _open_config(app, pilot)
        ta = app.query_one("#machines-text", TextArea)
        ta.focus()                        # 真实用户流:先点进编辑器再按键
        await pilot.pause()
        ta.text = "9.3.12.13 u **** 0102\n9.3.12.15 u NewPw 0103\n"
        await pilot.press("ctrl+s")
        for _ in range(3):
            await pilot.pause()
        assert "9.3.12.15" in app.ds.saved_text
        assert "****" in app.ds.saved_text   # 掩码原样回传,回填在数据源侧
        status = str(app.query_one("#config-status", Static).visual)
        assert "已保存" in status
        assert "1 台" in status and "0102" in status


async def test_config_view_save_error_shown():
    """保存被拒:状态行显示全部错误(含行号),绝不显示成功字样。"""
    app = BmonApp(FakeDS())
    app.ds.save_resp = {"ok": False,
                        "errors": ["第1行: 密码为 **** 但无对应已有机器 (ip=9.9.9.9)"]}
    async with app.run_test() as pilot:
        await _open_config(app, pilot)
        ta = app.query_one("#machines-text", TextArea)
        ta.focus()
        await pilot.pause()
        await pilot.press("ctrl+s")
        for _ in range(3):
            await pilot.pause()
        status = str(app.query_one("#config-status", Static).visual)
        assert "第1行" in status
        assert "已保存" not in status


async def test_config_view_not_autorefreshed():
    """周期刷新不得重灌配置页:编辑到一半的内容不能被冲掉。"""
    app = BmonApp(FakeDS())
    async with app.run_test() as pilot:
        await _open_config(app, pilot)
        cv = app.query_one(ConfigView)
        # 结构性守卫:配置页根本不提供 refresh_data,refresh_all 只刷新
        # 有该方法的活动视图 —— 这是"编辑不被自动刷新重灌"的机制本身
        assert not hasattr(cv, "refresh_data")
        ta = app.query_one("#machines-text", TextArea)
        edited = "1.2.3.4 u 手工编辑的密码 x\n"
        ta.text = edited
        await app.refresh_all()           # 周期刷新同款路径
        for _ in range(3):
            await pilot.pause()
        assert ta.text == edited           # 编辑器内容原样保留


async def test_config_view_load_errors_shown():
    """载入的清单有坏行:坏行被丢弃(编辑器只有可解析行),去向显示在状态行。"""
    class ConfigErrDS(FakeDS):
        def machines(self):
            return {"text": "9.3.12.13 u **** 0102\n",
                    "machines": [{"ip": "9.3.12.13", "user": "u",
                                  "password": "****", "alias": "0102"}],
                    "errors": ["第2行: 至少需要 ip 账号 密码 三列"]}

    app = BmonApp(ConfigErrDS())
    async with app.run_test() as pilot:
        await _open_config(app, pilot)
        ta = app.query_one("#machines-text", TextArea)
        assert "9.3.12.13" in ta.text     # 可解析的好行保留
        status = str(app.query_one("#config-status", Static).visual)
        assert "第2行" in status           # 被丢弃的坏行有交代


# ---------- Task 7:远程机器页(仅活动页轮询选中机器 + 会话曲线) ----------


async def _manual_flush(app, pilot):
    """切 Manual 刷新并排空挂起的初始刷新。

    远程页测试要对 live 调用数做精确断言(== 1 / == 2):先按 4 次 r
    走真实按键路径切到 Manual(REFRESH_STEPS 末位),排除 1s 定时器在
    测试中途补刀;再排空 on_mount 的首次刷新(打在默认 CPU 页,与远程页
    无关),之后测试里的 refresh_all 就是唯一的刷新来源。
    """
    for _ in range(4):
        await pilot.press("r")
    for _ in range(3):
        await pilot.pause()


async def test_remote_view_polls_and_builds_table():
    """激活即轮询选中机器:表格按组排序、真实单元格内容、buffer 逐拍累积。"""
    app = BmonApp(FakeDS())
    async with app.run_test() as pilot:
        await _manual_flush(app, pilot)
        app.query_one(TabbedContent).active = "pane-remote"
        await pilot.pause()
        await app.refresh_all()
        for _ in range(3):
            await pilot.pause()
        rv = app.query_one(RemoteView)
        tbl = rv.query_one(DataTable)
        assert rv.query_one(Select).value == "0102"   # 默认选第一台
        assert tbl.row_count == 2                     # CPU1_TEMP + DIMM1_STATUS
        # 排序:temp 组在 GROUP_LABELS 中先于 status → CPU1_TEMP 是第 0 行。
        # 第 1 拍值 = 48 + 1 = 49.00(48℃ < upper_nc 78 → green)
        row0 = [str(c) for c in tbl.get_row_at(0)]
        assert row0[0] == "温度"                       # 组列:GROUP_LABELS 中文
        assert "CPU1_TEMP" in row0[1]
        assert row0[2] == "[green]49.00[/green]"       # 值列:读数按阈值着色
        assert row0[3] == "degrees C"                  # 单位列
        assert row0[4] == "ok"                         # 状态列
        assert row0[5] == "49.00~49.00"                # 会话极值:单样本 min~max
        # 状态位行:值列显示原始十六进制掩码、中性 dim(沿分组页规则),
        # 绝不显示十进制数值
        row1 = [str(c) for c in tbl.get_row_at(1)]
        assert row1[0] == "状态位"
        assert "DIMM1_STATUS" in row1[1]
        assert "0x8000" in row1[2] and "[dim]" in row1[2]
        assert "32768" not in row1[2]
        status = str(rv.query_one("#remote-status", Static).visual)
        assert "0102" in status and "2 传感器" in status
        assert len(rv._buf["CPU1_TEMP"]) == 1
        # 第二拍:值变化、buffer 增长到 2 点、极值更新为两样本 min~max
        await app.refresh_all()
        for _ in range(3):
            await pilot.pause()
        assert len(rv._buf["CPU1_TEMP"]) == 2
        row0b = [str(c) for c in tbl.get_row_at(0)]
        assert row0b[2] == "[green]50.00[/green]"      # 48 + 第2拍
        assert row0b[5] == "49.00~50.00"               # 两样本极值


async def test_remote_view_ttl_cache_hit_dedups_sample():
    """TTL 缓存命中:同 ts 的重复样本不重复进 buffer(不挤占 3600 槽位)。

    serve 侧 LivePoller 的 1s TTL 命中时,两次刷新拿到的是同一份
    (ts, value) 缓存载荷 —— 逐拍无条件追加会让重复样本占据会话 buffer,
    有效会话窗口缩水到不足标称的 ~1h。
    """
    class SameTsDS(FakeDS):
        # 模拟 TTL 命中:两次 live 返回同一 (ts, value) 样本
        def live(self, alias):
            self.live_calls += 1
            return {"ok": True, "ts": 1700000000,
                    "readings": [{"name": "CPU1_TEMP", "value": 48.0,
                                  "unit": "degrees C", "grp": "temp",
                                  "status": "ok", "thresholds": None}]}

    app = BmonApp(SameTsDS())
    async with app.run_test() as pilot:
        await _manual_flush(app, pilot)
        app.query_one(TabbedContent).active = "pane-remote"
        await pilot.pause()
        await app.refresh_all()
        for _ in range(3):
            await pilot.pause()
        rv = app.query_one(RemoteView)
        assert len(rv._buf["CPU1_TEMP"]) == 1
        await app.refresh_all()                  # 同 ts:缓存命中的重复样本
        for _ in range(3):
            await pilot.pause()
        assert app.ds.live_calls == 2            # 确实刷新了两拍
        assert len(rv._buf["CPU1_TEMP"]) == 1    # 但 buffer 不重复追加


async def test_remote_view_plot_follows_selection():
    """选中行大图跟随:光标移动后画的是新选中的传感器(会话曲线)。"""
    app = BmonApp(FakeDS())
    async with app.run_test() as pilot:
        await _manual_flush(app, pilot)
        app.query_one(TabbedContent).active = "pane-remote"
        await pilot.pause()
        await app.refresh_all()
        await pilot.pause()
        rv = app.query_one(RemoteView)
        tbl = rv.query_one(DataTable)
        plot = rv.query_one(PlotextPlot)
        assert "CPU1_TEMP" in plot.plt.build()         # 初始画选中行(第 0 行)
        tbl.focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert tbl.cursor_row == 1
        assert "DIMM1_STATUS" in plot.plt.build()      # 光标移动,大图跟随


async def test_remote_view_error_keeps_table():
    """live 失败:状态行红字报错,表格与会话曲线保留上一拍,恢复后续接。"""
    app = BmonApp(FakeDS())
    async with app.run_test() as pilot:
        await _manual_flush(app, pilot)
        app.query_one(TabbedContent).active = "pane-remote"
        await pilot.pause()
        await app.refresh_all()
        await pilot.pause()
        rv = app.query_one(RemoteView)
        tbl = rv.query_one(DataTable)
        assert tbl.row_count == 2
        app.ds.live_error = True                       # BMC 拉取失败
        await app.refresh_all()
        for _ in range(3):
            await pilot.pause()                        # 不抛异常即通过本行
        status = str(rv.query_one("#remote-status", Static).visual)
        assert "连接失败" in status                    # 状态行含错误文本
        assert tbl.row_count == 2                      # 表格不清空
        assert len(rv._buf["CPU1_TEMP"]) == 1          # buffer 保留上一拍
        app.ds.live_error = False                      # 恢复后下一拍续接
        await app.refresh_all()
        for _ in range(3):
            await pilot.pause()
        assert len(rv._buf["CPU1_TEMP"]) == 2
        assert "连接失败" not in str(
            rv.query_one("#remote-status", Static).visual)


async def test_remote_view_switch_clears_buffer():
    """切机器清会话数据;重选同台不清;新机器下一拍重填表格。"""
    app = BmonApp(FakeDS())
    async with app.run_test() as pilot:
        await _manual_flush(app, pilot)
        app.query_one(TabbedContent).active = "pane-remote"
        await pilot.pause()
        await app.refresh_all()
        await pilot.pause()
        rv = app.query_one(RemoteView)
        tbl = rv.query_one(DataTable)
        assert len(rv._buf["CPU1_TEMP"]) == 1
        rv.query_one(Select).value = "0102"            # 重选同台
        await pilot.pause()
        assert len(rv._buf["CPU1_TEMP"]) == 1          # 不清
        rv.query_one(Select).value = "0103"            # 切到第二台
        await pilot.pause()
        assert rv._buf == {}                           # 会话曲线清空
        assert rv._sensors == [] and rv._plotted_row is None
        await app.refresh_all()                        # 下一拍按新机器重填
        for _ in range(3):
            await pilot.pause()
        assert tbl.row_count == 1                      # 0103 只有 FAN1
        row0 = [str(c) for c in tbl.get_row_at(0)]
        assert row0[0] == "风扇"                       # GROUP_LABELS 中文
        assert "FAN1" in row0[1]
        assert row0[2] == "[green]3002.00[/green]"     # 3000 + 第2次 live
        assert "CPU1_TEMP" not in rv._buf              # 旧机器 buffer 不复活


async def test_remote_view_inactive_pane_does_not_poll():
    """核心保证"仅采集当前界面中的机器":远程页非活动时零 live 调用。"""
    app = BmonApp(FakeDS())
    async with app.run_test() as pilot:
        await _manual_flush(app, pilot)
        await app.refresh_all()                        # 活动页是默认 CPU 页
        await pilot.pause()
        assert app.ds.live_calls == 0                  # 非活动 → 零轮询
        app.query_one(TabbedContent).active = "pane-remote"
        await pilot.pause()
        await app.refresh_all()
        await pilot.pause()
        assert app.ds.live_calls == 1                  # 激活后才开始轮询
        assert app.query_one(RemoteView)._alias == "0102"


async def test_remote_view_machine_added_preserves_session():
    """清单中途变更(他处新添机器)触发选项重建:选中与会话曲线不得被误清。

    set_options 会连发程序性 Changed(NULL)+Changed(恢复值),若不过滤
    过期事件,正在观看机器的会话曲线会被清零重来。
    """
    app = BmonApp(FakeDS())
    async with app.run_test() as pilot:
        await _manual_flush(app, pilot)
        app.query_one(TabbedContent).active = "pane-remote"
        await pilot.pause()
        await app.refresh_all()
        await pilot.pause()
        rv = app.query_one(RemoteView)
        assert len(rv._buf["CPU1_TEMP"]) == 1
        app.ds.machines_extra = [{"ip": "9.3.12.16", "user": "u",
                                  "password": "****", "alias": "0104"}]
        await app.refresh_all()
        for _ in range(3):
            await pilot.pause()
        # 选项重建后:选中仍是 0102,曲线连续追加(49→50)而非清零重来
        assert rv.query_one(Select).value == "0102"
        assert [p[1] for p in rv._buf["CPU1_TEMP"]] == [49.0, 50.0]


async def test_remote_view_ds_exception_does_not_escape():
    """数据源抛异常(远程 HTTP 传输错误等):不逃逸 refresh_data。

    异常若逃到 app.refresh_all 会被记成整体 refresh error —— 单页的
    数据抖动不该连累其它页的刷新指示,本页降级为状态行红字。
    """
    class BoomMachinesDS(FakeDS):
        def machines(self):
            raise RuntimeError("network down")

    app = BmonApp(BoomMachinesDS())
    async with app.run_test() as pilot:
        await _manual_flush(app, pilot)
        app.query_one(TabbedContent).active = "pane-remote"
        await pilot.pause()
        await app.refresh_all()          # 异常被本页兜住
        await pilot.pause()
        status = str(app.query_one(RemoteView)
                     .query_one("#remote-status", Static).visual)
        assert "network down" in status  # 状态行红字可诊断
        assert "(refresh error)" not in app.sub_title   # 未击穿全局指示


async def test_remote_view_selected_machine_vanishes():
    """选中机器被删:清会话数据并自动改选第一台;机器删光则提示并零轮询。"""
    class DropDS(FakeDS):
        def __init__(self):
            super().__init__()
            self.drop_0102 = False
            self.drop_all = False

        def machines(self):
            resp = super().machines()
            if self.drop_all:
                resp["machines"] = []
            elif self.drop_0102:
                resp["machines"] = [m for m in resp["machines"]
                                    if m["alias"] != "0102"]
            return resp

    app = BmonApp(DropDS())
    async with app.run_test() as pilot:
        await _manual_flush(app, pilot)
        app.query_one(TabbedContent).active = "pane-remote"
        await pilot.pause()
        await app.refresh_all()
        await pilot.pause()
        rv = app.query_one(RemoteView)
        tbl = rv.query_one(DataTable)
        assert rv._alias == "0102" and len(rv._buf["CPU1_TEMP"]) == 1
        app.ds.drop_0102 = True             # 正在观看的机器被从清单删除
        await app.refresh_all()
        for _ in range(3):
            await pilot.pause()
        assert rv._alias == "0103"          # 自动改选第一台可用机器
        assert "CPU1_TEMP" not in rv._buf   # 旧机器会话数据已清
        assert rv.query_one(Select).value == "0103"
        assert tbl.row_count == 1 and "FAN1" in str(tbl.get_row_at(0)[1])
        app.ds.drop_all = True              # 机器删光:提示去配置页,不再轮询
        calls = app.ds.live_calls
        await app.refresh_all()
        for _ in range(3):
            await pilot.pause()
        status = str(rv.query_one("#remote-status", Static).visual)
        assert "无机器" in status
        assert tbl.row_count == 0           # 已删机器的表格数据不残留
        assert app.ds.live_calls == calls   # 无机器可看即零轮询
