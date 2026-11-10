"""Task 11:Textual 主应用。绑定 1-6 切范围、r 循环刷新率、m 手动刷新、q 退出。"""

import logging

from textual.app import App, ComposeResult
from textual.widgets import Footer, Header, TabbedContent, TabPane

from .datasource import LocalDataSource, RemoteDataSource  # noqa: F401(导出便利)
from .views import (GROUP_LABELS, ConfigView, CoreView, GroupView, KpiBar,
                    RemoteView, SdcView, SelView)

log = logging.getLogger("bmon.tui.app")

REFRESH_STEPS = [1, 5, 10, 30, 0]  # 0 = Manual


class BmonApp(App):
    # 程序员经典配色:Dracula(高对比暗色),标题/头部不再难以辨认
    THEME = "dracula"

    CSS = """
    Screen { layout: vertical; }
    #kpibar { height: 3; padding: 0 1; }
    DataTable { height: 1fr; }
    PlotextPlot { height: 40%; border: round #444; }
    #core-grid { padding: 0 1; }
    #sdc-summary { padding: 0 1; color: $text; }
    #remote-status { height: auto; padding: 0 1; }
    #config-status { height: auto; padding: 0 1; }
    Header { background: $surface; color: $text; }
    """

    BINDINGS = [
        ("1", "set_range('1h')", "1h"), ("2", "set_range('6h')", "6h"),
        ("3", "set_range('12h')", "12h"), ("4", "set_range('24h')", "24h"),
        ("5", "set_range('7d')", "7d"), ("6", "set_range('30d')", "30d"),
        ("r", "cycle_refresh", "刷新率"), ("m", "manual_refresh", "手动刷新"),
        ("q", "quit", "退出"),
    ]

    def __init__(self, ds):
        super().__init__()
        self.theme = self.THEME  # 程序员经典配色(可改为 textual 内置主题名)
        self.ds = ds
        self.range_key = "1h"
        self.refresh_idx = 0
        self._interval = None
        self._refresh_error = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield KpiBar(self.ds)
        with TabbedContent():
            for grp, label in GROUP_LABELS:
                with TabPane(label, id=f"pane-{grp}"):
                    yield GroupView(grp, self.ds, label)
            with TabPane("每核CPU", id="pane-core"):
                yield CoreView(self.ds)
            with TabPane("SDC压测", id="pane-sdc"):
                yield SdcView(self.ds)
            with TabPane("SEL事件", id="pane-sel"):
                yield SelView(self.ds)
            # 远程机器页:仅本页活动时轮询当前选中机器(方案甲"看谁采谁"
            # 的界面入口);放在配置页之前,配置页保持最后
            with TabPane("远程机器", id="pane-remote"):
                yield RemoteView(self.ds)
            with TabPane("配置", id="pane-config"):
                yield ConfigView(self.ds)
        yield Footer()

    def on_mount(self) -> None:
        self._apply_interval()
        self.call_after_refresh(self.refresh_all)

    def _refresh_subtitle(self) -> None:
        """每次整体重建 sub_title,刷新失败标记只出现一次、恢复即清除。"""
        sec = REFRESH_STEPS[self.refresh_idx]
        self.sub_title = (f"range={self.range_key} "
                          f"refresh={'Manual' if not sec else f'{sec}s'}"
                          + (" (refresh error)" if self._refresh_error else ""))

    def _apply_interval(self) -> None:
        if self._interval is not None:
            self._interval.stop()
            self._interval = None
        sec = REFRESH_STEPS[self.refresh_idx]
        if sec:
            self._interval = self.set_interval(sec, self._tick_refresh)
        self._refresh_subtitle()

    def _tick_refresh(self) -> None:
        self.run_worker(self.refresh_all(), exclusive=False)

    async def refresh_all(self) -> None:
        try:
            await self.query_one(KpiBar).refresh_data(self.range_key)
            pane = self.query_one(TabbedContent).active_pane
            view = pane.children[0] if pane and pane.children else None
            if view is not None and hasattr(view, "refresh_data"):
                await view.refresh_data()
        except Exception:
            # 不静默:记录堆栈;指示用标志位整体重建,不叠加(textual 8.2.8
            # 的 self.log 无 .exception(),故用标准库 logging 记完整回溯)
            log.exception("TUI 刷新失败")
            self._refresh_error = True
        else:
            self._refresh_error = False
        finally:
            self._refresh_subtitle()

    def action_set_range(self, key: str) -> None:
        self.range_key = key
        for v in self.query(GroupView):
            v.range_key = key
        for v in self.query(CoreView):   # 每核页大图同样随范围切换
            v.range_key = key
        self._apply_interval()
        self.run_worker(self.refresh_all(), exclusive=True)

    def action_cycle_refresh(self) -> None:
        self.refresh_idx = (self.refresh_idx + 1) % len(REFRESH_STEPS)
        self._apply_interval()

    def action_manual_refresh(self) -> None:
        self.run_worker(self.refresh_all(), exclusive=True)
