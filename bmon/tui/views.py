"""Task 11:TUI 视图层。

与 brief 的差异(版本漂移,均已验证并记录于 task-11-report.md):
1. textual 8.2.8 已移除内置 `textual.widgets.PlotextPlot`,改用独立包
   `textual-plotext`(`from textual_plotext import PlotextPlot`),其随附
   plotext 5.3.2 与 brief 假设的 5.x API 一致。
2. `PlotextPlot` 渲染的是部件自有的 Plot 对象(经 `.plt` 属性暴露),
   而非 plotext 模块级单例 —— 因此 `_plot` 中改用 `plot_widget.plt`
   调用 clear_figure/title/plot,再 `plot_widget.refresh()` 触发重绘。
"""

import time
from collections import deque

from textual.containers import Horizontal, Vertical
from textual.markup import escape
from textual.widgets import DataTable, Select, Static, TextArea
from textual_plotext import PlotextPlot

GROUP_LABELS = [("cpu", "CPU"), ("temp", "温度"), ("volt", "电压"),
               ("fan", "风扇"), ("power", "功率"), ("psu", "电源PSU"),
               ("status", "状态位"), ("load", "负载"), ("mem", "内存"),
               ("disk", "磁盘")]


def threshold_style(value, thresholds, unit) -> str:
    """值→终端样式:dim(无值)/红(越临界)/琥珀(越非临界)/绿。

    终版评审修复 1:移除状态位分支 —— 本固件的标称断言态非 0(本机正常
    0x8000),按数值着色必然全红假告警。状态位传感器不经此函数,由
    GroupView.refresh_data 改显原始十六进制掩码并固定 dim。
    unit 仅保留调用签名兼容,不再参与判断。
    """
    if value is None:
        return "dim"
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


def _grid_style(v) -> str:
    """网格块专用分档:近空闲(<10%)与无数据显示暗灰,忙碌核一目了然。

    与 _usage_style(表格行用)分开:表格里空闲核的数值仍绿色可读;
    网格里空闲块降灰 —— 否则空闲机器呈现一面无信息量的绿墙,
    真正忙碌的核反而淹没其中。
    """
    if v is None or v < 10:
        return "dim"
    return "green" if v < 70 else "yellow" if v < 90 else "red"


def core_grid_text(latest: dict) -> str:
    """192 核占用网格:每行 32 核,行首标核号段,尾行图例。

    每块 ██ 代表 1 个核;灰=近空闲(<10% 或无数据)、绿<70%、
    黄<90%、红≥90%。行首 c000-031 即该行覆盖的核号范围。
    """
    lines = []
    for row in range(0, 192, 32):
        blocks = []
        for i in range(row, min(row + 32, 192)):
            v = latest.get(f"cpu{i}_usage")
            blocks.append(f"[{_grid_style(v)}]██[/{_grid_style(v)}]")
        lines.append(f"[dim]c{row:03d}-{min(row + 31, 191):03d}[/dim] │"
                     + "".join(blocks))
    lines.append("[dim]每块=1核 · 灰<10% 绿<70% 黄<90% 红≥90%[/dim]")
    return "\n".join(lines)


def _stale(age_s) -> bool:
    """读数是否过期:无 age(None)视为过期;age 0 是同秒新数据、不过期。"""
    return age_s is None or age_s > 10


def _chart_points(pts) -> tuple[list, list]:
    """history 点 → (x, y):x 用相对分钟(0=最新,负值=过去)。

    epoch 秒直接作 x 轴会把刻度渲染成 1.79e9 级天文数字,不可读;
    points 同时兼容本地元组与远端 JSON 列表两种形态(按位取值)。
    """
    if not pts:
        return [], []
    t_last = pts[-1][0]
    xs = [round((p[0] - t_last) / 60, 2) for p in pts]
    return xs, [p[1] for p in pts]


def _restore_table(tbl: DataTable, row: int, scroll_x: float, scroll_y: float) -> None:
    """重灌表格后恢复选中行与滚动位置。

    textual 8.x 的 DataTable.clear() 会把光标重置到 (0,0)、滚动归零,
    周期刷新若不恢复,选中行会每拍弹回顶部、长表无法下翻。行号夹紧到
    新行数;滚动位置等布局尺寸稳定后(call_after_refresh)再恢复。
    """
    if tbl.row_count:
        tbl.move_cursor(row=min(row, tbl.row_count - 1), animate=False,
                        scroll=False)
    tbl.call_after_refresh(tbl.scroll_to, x=scroll_x, y=scroll_y,
                           animate=False)


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
            flag = "[red]⚠[/red]" if _stale(k.get("age_s")) else ""
            parts.append(f"{k['label']} [b]{val}[/b]{flag}")
        # 终版评审修复 2:机型/核数/内存取自 overview 的 static 载荷
        # (static_info 在 bmon-serve 端运行,即服务器本机),远程 TUI
        # 不再显示 TUI 客户端的规格;旧版 serve 无此载荷时降级显示占位
        st = ov.get("static") or {}
        info = (f"{st.get('cpu_model') or 'N/A'} / "
                f"{st.get('cores', '?')} cores / {st.get('mem_total_gb', '?')}GB")
        # 标题用 Dracula 荧光绿加粗(程序员经典配色),分隔符降灰提升可读性
        self.query_one("#kpi-clock", Static).update(
            f"[b #50FA7B]B0102 SDC Monitor — {info}[/b #50FA7B]   "
            + "[dim]│[/dim]".join(parts))


class _SelectionPlotMixin:
    """DataTable 选中行 → 该行传感器 history 大图(相对分钟轴)。

    GroupView 与 CoreView 共用;宿主需提供 ds、range_key、_sensors、
    _plotted_row,且 compose 中各含一个 DataTable 与一个 PlotextPlot。

    - 版本漂移:画在部件自有的 Plot 对象上(textual-plotext 1.x),
      而非 plotext 模块级单例;history 点按 p[0]/p[1] 取,兼容本地
      元组与远端 JSON 列表两种形态。
    - x 轴用相对分钟而非 epoch 秒(见 _chart_points)。
    - textual 8.x 光标移动信号是 DataTable.RowHighlighted;clear() 触发
      的 (0,0) 事件是重灌过程的过期事件,按 event.cursor_row != 当前
      光标过滤;同一行不重复取数重画。
    - 曲线数据源经 _history_points 钩子取得:分组页/每核页走 DB
      history(默认实现),远程机器页覆写为会话 buffer —— 取数路径
      不同,画图/去重逻辑完全共用。
    """

    def _history_points(self, sensor: dict) -> list[tuple[float, float]]:
        """曲线数据源钩子:默认走 DB history(分组页/每核页)。"""
        return self.ds.history(sensor["id"], self.range_key, 200)

    async def _plot(self, sensor: dict) -> None:
        pts = self._history_points(sensor)
        plot_widget = self.query_one(PlotextPlot)
        plot = plot_widget.plt
        plot.clear_figure()
        plot.title(f"{sensor['name']} ({self.range_key})")
        xs, ys = _chart_points(pts)
        plot.plot(xs, ys, color="cyan")
        plot.xlabel("分钟 (0=现在)")
        plot_widget.refresh()

    async def on_data_table_row_highlighted(
            self, event: DataTable.RowHighlighted) -> None:
        tbl = self.query_one(DataTable)
        if event.data_table is not tbl:
            return
        row = event.cursor_row
        if row != tbl.cursor_row or row == self._plotted_row:
            return
        if 0 <= row < len(self._sensors):
            await self._plot(self._sensors[row])
            self._plotted_row = row


class GroupView(_SelectionPlotMixin, Vertical):
    def __init__(self, grp: str, ds, label: str):
        super().__init__()
        self.grp, self.ds, self.label = grp, ds, label
        self.range_key = "1h"
        self._sensors: list[dict] = []   # 供光标事件取行数据
        self._plotted_row: int | None = None  # 当前大图已画的行,去重

    def compose(self):
        yield DataTable(id=f"tbl-{self.grp}")
        yield PlotextPlot(id=f"plot-{self.grp}")

    def on_mount(self) -> None:
        tbl = self.query_one(DataTable)
        tbl.add_columns("传感器", "当前值", "单位", "状态", "范围极值")
        tbl.cursor_type = "row"

    async def refresh_data(self) -> None:
        sensors = self.ds.sensors(self.grp)
        self._sensors = sensors
        extremes = (self.ds.extremes([s["id"] for s in sensors
                                      if s["last_ts"] is not None],
                                     self.range_key)
                    if any(s["last_ts"] is not None for s in sensors) else {})
        tbl = self.query_one(DataTable)
        # clear() 会把光标/滚动清零,先保存再重灌,随后恢复,选中行不回弹
        saved_row, saved_x, saved_y = tbl.cursor_row, tbl.scroll_x, tbl.scroll_y
        tbl.clear()
        for s in sensors:
            ex = extremes.get(str(s["id"]))
            extxt = f"{ex['min']:.2f}~{ex['max']:.2f}" if ex else "—"
            if s.get("grp") == "status" or s.get("unit") == "status":
                # 终版评审修复 1:状态位显示采集的原始十六进制断言位掩码
                # (last_status,本机正常态 0x8000),缺失回退 N/A;中性 dim,
                # 不做告警着色 —— 固件标称断言态本就非 0,按数值着色会永久
                # 全红,真实状态变化反而不可见
                style, val = "dim", s.get("last_status") or "N/A"
            else:
                v = s["last_value"]
                style = threshold_style(v, _th(s), s["unit"])
                val = f"{v:.2f}" if v is not None else "N/A"
            state = "过期" if _stale(s.get("age_s")) else "ok"
            tbl.add_row(f"[{style}]{s['name']}[/{style}]",
                        f"[{style}]{val}[/{style}]", s["unit"], state, extxt,
                        key=str(s["id"]))
        if sensors:
            row = min(saved_row, len(sensors) - 1)
            _restore_table(tbl, row, saved_x, saved_y)
            await self._plot(sensors[row])   # 重画当前选中行(而非第 0 行)
            self._plotted_row = row
        else:
            self._plotted_row = None


class CoreView(_SelectionPlotMixin, Vertical):
    """每核CPU 页:192 核占用网格 + 长表 + 选中核的 history 大图。

    图示缺陷 3 修复:此前本页只有网格和表格、没有图(选中核看不到
    历史曲线);现与分组页同构 —— 选中行即出该核的大图。
    """

    def __init__(self, ds):
        super().__init__()
        self.ds = ds
        self.range_key = "1h"
        self._sensors: list[dict] = []
        self._plotted_row: int | None = None

    def compose(self):
        yield Static("", id="core-grid")
        yield DataTable(id="core-tbl")
        yield PlotextPlot(id="core-plot")

    def on_mount(self) -> None:
        tbl = self.query_one(DataTable)
        tbl.add_columns("核", "占用 %")
        # 行光标模式:光标移动才发布 RowHighlighted(大图跟随依赖此事件),
        # 与 GroupView 一致;默认 cell 模式不发该事件
        tbl.cursor_type = "row"

    async def refresh_data(self) -> None:
        sensors = self.ds.sensors("core")
        self._sensors = sensors
        latest = {s["name"]: s["last_value"] for s in sensors}
        self.query_one("#core-grid", Static).update(core_grid_text(latest))
        tbl = self.query_one(DataTable)
        # 192 行长表:clear() 清零滚动会让每拍跳回顶部,先保存后恢复
        saved_row, saved_x, saved_y = tbl.cursor_row, tbl.scroll_x, tbl.scroll_y
        tbl.clear()
        for s in sensors:
            v = s["last_value"]
            st = _usage_style(v)
            tbl.add_row(s["name"], f"[{st}]{v:.1f}[/{st}]" if v is not None
                        else "N/A")
        if sensors:
            row = min(saved_row, len(sensors) - 1)
            _restore_table(tbl, row, saved_x, saved_y)
            await self._plot(sensors[row])   # 大图画当前选中的核
            self._plotted_row = row


class RemoteView(_SelectionPlotMixin, Vertical):
    """远程机器页:机器下拉 + 读数表 + 会话曲线(方案甲"看谁采谁")。

    - 轮询时机:refresh_data 只被 app 刷新循环在本页活动时调用 ——
      "仅采集当前界面中的机器"由既有活动页机制保证,本页不另设
      定时器/后台线程;切走标签页即停采。
    - 会话曲线:dict[name] → deque(maxlen=3600) 内存环(1s 采样约
      1 小时),不入库 —— 远程机器无 DB 存储,切机器即清。
    - 与分组页同构:排序按 GROUP_LABELS 组序、状态位显示原始十六进制
      掩码中性 dim、重灌后恢复光标滚动、选中行大图跟随。
    """

    def __init__(self, ds):
        super().__init__()
        self.ds = ds
        # 本页曲线是会话级 buffer,与全局 1h/6h 范围键无关(切范围
        # 只同步分组页/每核页,见 app.action_set_range)
        self.range_key = "会话"
        self._machines: list[dict] = []   # 上一拍的机器清单(别名集合比对用)
        self._alias: str | None = None    # 当前选中机器
        self._sensors: list[dict] = []    # 当前表格数据,供光标事件取行
        self._plotted_row: int | None = None
        self._buf: dict[str, deque] = {}  # name → deque[(ts, value)],maxlen=3600

    def compose(self):
        yield Select([], id="remote-select", prompt="选择机器")
        yield Static("", id="remote-status")
        yield DataTable(id="remote-tbl")
        yield PlotextPlot(id="remote-plot")

    def on_mount(self) -> None:
        tbl = self.query_one(DataTable)
        tbl.add_columns("组", "传感器", "当前值", "单位", "状态", "会话极值")
        # 行光标模式:光标移动才发布 RowHighlighted(大图跟随依赖此事件)
        tbl.cursor_type = "row"

    def _history_points(self, sensor: dict) -> list[tuple[float, float]]:
        """曲线数据源覆写:取会话 buffer 而非 DB history(远程机器不入库)。"""
        return list(self._buf.get(sensor.get("name"), ()))

    def _reset_session(self) -> None:
        """清会话级状态:曲线 buffer、表格数据缓存、已画行号。"""
        self._buf.clear()
        self._sensors = []
        self._plotted_row = None

    def _status(self, text: str, style: str) -> None:
        """状态行统一出口:动态内容(别名/错误消息)先转义再包 markup,
        与 ConfigView._status 同法 —— 远端数据含 [ ] 时不会 MarkupError。"""
        self.query_one("#remote-status", Static).update(
            f"[{style}]{escape(text)}[/{style}]")

    def on_select_changed(self, event: Select.Changed) -> None:
        """切机器:清会话数据,下一拍 refresh_data 按新机器重填。

        - 重选同台:Select 值未变不发事件,兜底再按别名相等去重,不清;
        - 机器清单重建(增删机器)会先发程序性 Changed(NULL) 再恢复
          选中值,消息处理时控件值已恢复 —— 按"消息值 ≠ 控件现值"过滤
          过期事件(与分组页过滤 clear() 触发的 (0,0) 光标事件同思路),
          否则清单一变就会误清正在观看机器的会话曲线。
        """
        if event.value != event.select.value:
            return
        alias = None if event.select.is_blank() else event.value
        if alias == self._alias:
            return
        self._alias = alias
        self._reset_session()

    async def refresh_data(self) -> None:
        # 全程兜底:数据侧任何异常都不许逃逸 —— refresh_all 会把异常记成
        # 整体 refresh error,单页数据抖动不该连累其它页的刷新指示
        try:
            resp = self.ds.machines()
            ms = list(resp.get("machines") or [])
            aliases = [m.get("alias") or "" for m in ms]
            sel = self.query_one(Select)
            if aliases != [m.get("alias") or "" for m in self._machines]:
                # 机器集合变化才重建选项:set_options 会重置选中并连发
                # Changed,每拍重建既制造事件噪声又让会话曲线被误清
                self._machines = ms
                sel.set_options([(a, a) for a in aliases])
                if self._alias in aliases:
                    sel.value = self._alias    # 选中机器仍在:恢复,会话数据不动
                else:
                    # 选中机器已从清单消失:清会话数据与表格,回到未选状态
                    self._alias = None
                    self._reset_session()
                    self.query_one(DataTable).clear()
            if not aliases:
                self._status("无机器:请在配置页添加", "yellow")
                return
            if self._alias is None:
                # 未选机器(首次进入/选中机器消失后):默认选第一台
                self._alias = aliases[0]
                sel.value = self._alias
            live = self.ds.live(self._alias)
            if not live.get("ok"):
                # 拉取失败:红字报错;表格与会话曲线保留上一拍,下一拍重试
                self._status(f"{self._alias}: {live.get('error') or '未知错误'}",
                             "red")
                return
            readings = list(live.get("readings") or [])
            ts = live.get("ts") or int(time.time())
            for r in readings:
                v = r.get("value")
                if v is None:
                    # 值缺失(None)只进表格 N/A、不进 buffer
                    continue
                buf = self._buf.setdefault(r.get("name") or "",
                                           deque(maxlen=3600))
                # TTL 缓存命中时本拍与上一拍是同一份载荷(同 ts 同值):
                # 跳过追加 —— 否则重复样本白白挤占 maxlen 槽位,有效会话
                # 窗口缩水到不足标称的 ~1h
                if buf and ts == buf[-1][0]:
                    continue
                buf.append((ts, v))
            order = {g: i for i, (g, _) in enumerate(GROUP_LABELS)}
            labels = dict(GROUP_LABELS)
            # 组序按 GROUP_LABELS、组内按名排:与分组页的浏览习惯一致;
            # 未知组(如固件的 other)排在已知组之后
            readings.sort(key=lambda r: (order.get(r.get("grp"), len(order)),
                                         r.get("name") or ""))
            self._sensors = readings
            tbl = self.query_one(DataTable)
            # clear() 清零光标/滚动:先保存,重灌后恢复(同分组页)
            saved_row, saved_x, saved_y = tbl.cursor_row, tbl.scroll_x, tbl.scroll_y
            tbl.clear()
            for r in readings:
                name, unit = r.get("name") or "", r.get("unit") or ""
                grp = r.get("grp") or ""
                state = r.get("status") or "N/A"
                if grp == "status" or unit == "status":
                    # 状态位:值列显示原始十六进制掩码(status 即 Redfish
                    # 原文),中性 dim 不告警 —— 固件标称断言态非 0,按数值
                    # 着色必全红(规则同分组页)
                    style, val = "dim", state
                else:
                    v = r.get("value")
                    style = threshold_style(v, r.get("thresholds"), unit)
                    val = f"{v:.2f}" if v is not None else "N/A"
                buf = self._buf.get(name)
                # 会话极值:本会话 buffer 的 min~max;尚无样本显示 —
                extxt = (f"{min(p[1] for p in buf):.2f}~"
                         f"{max(p[1] for p in buf):.2f}" if buf else "—")
                # 单元格内容是远端数据,先转义再包样式 markup(同状态行)
                tbl.add_row(escape(labels.get(grp, grp)),
                            f"[{style}]{escape(name)}[/{style}]",
                            f"[{style}]{escape(val)}[/{style}]",
                            escape(unit), escape(state), extxt)
            if readings:
                row = min(saved_row, len(readings) - 1)
                _restore_table(tbl, row, saved_x, saved_y)
                await self._plot(readings[row])   # 重画当前选中行的会话曲线
                self._plotted_row = row
            else:
                self._plotted_row = None
            # 状态行:别名 / 传感器数 / 刷新时刻(数据略陈旧时附年龄)
            when = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else ""
            age = max(0, int(time.time()) - int(ts)) if ts else 0
            age_txt = f" · {age}s 前" if age > 0 else ""
            self._status(f"{self._alias} · {len(readings)} 传感器 · "
                         f"刷新于 {when}{age_txt}", "green")
        except Exception as e:
            self._status(f"刷新失败: {e}", "red")


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
        saved_row, saved_x, saved_y = tbl.cursor_row, tbl.scroll_x, tbl.scroll_y
        tbl.clear()
        events = self.ds.sel(100)
        for e in events:
            ts = _time.strftime("%m-%d %H:%M:%S", _time.localtime(e["ts"]))
            sev = e.get("severity") or ""
            style = "red" if sev not in ("OK", "Informational", "") else "green"
            tbl.add_row(ts, f"[{style}]{sev}[/{style}]",
                        e.get("event_type", ""), e.get("message", ""))
        if events:
            _restore_table(tbl, saved_row, saved_x, saved_y)


class ConfigView(Vertical):
    """配置页:机器清单编辑器,一行一台(ip 账号 密码 [别名])。

    - 敏感信息:载入与回存的密码列都是数据源掩码后的 ****,真实密码由
      数据源侧按 ip/别名回填 —— 界面全程不经手明文。
    - 故意不定义 refresh_data:app.refresh_all 只刷新带该方法的活动视图;
      配置页若参与周期刷新,用户编辑到一半的内容会被掩码文本直接冲掉。
    """

    BINDINGS = [("ctrl+s", "save", "保存")]

    def __init__(self, ds):
        super().__init__()
        self.ds = ds

    def compose(self):
        yield TextArea(id="machines-text")
        yield Static("", id="config-status")

    def on_mount(self) -> None:
        # 掩码清单可能来自远程 HTTP,经 worker 载入,挂载流程不等网络
        self.run_worker(self._load())

    async def _load(self) -> None:
        try:
            resp = self.ds.machines()
            self.query_one("#machines-text", TextArea).text = resp["text"]
            if resp.get("errors"):
                # 坏行由 mask_text 原样留在编辑器里(不代用户删),
                # 错误消息指明行号与原因,引导用户自行修正
                self._status("\n".join(resp["errors"]), "yellow")
        except Exception as e:
            # 兜住远程传输层异常:run_worker 默认 exit_on_error=True,
            # 裸抛会带崩整个应用;降级为状态行提示
            self._status(f"载入失败: {e}", "red")

    async def action_save(self) -> None:
        try:
            resp = self.ds.save_machines(
                self.query_one("#machines-text", TextArea).text)
        except Exception as e:
            self._status(f"保存失败: {e}", "red")
            return
        if resp.get("ok"):
            ms = resp.get("machines") or []
            aliases = " ".join(m.get("alias", "") for m in ms)
            self._status(f"✓ 已保存 {len(ms)} 台: {aliases}", "green")
        else:
            self._status("\n".join(resp.get("errors") or ["保存失败"]), "red")

    def _status(self, text: str, style: str) -> None:
        """状态行统一出口:动态内容(别名/错误消息)先转义再包 markup,
        清单里出现 [ ] 之类字符时不会 MarkupError 击穿渲染。"""
        self.query_one("#config-status", Static).update(
            f"[{style}]{escape(text)}[/{style}]")
