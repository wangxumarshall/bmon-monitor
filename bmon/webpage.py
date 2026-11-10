import html
import time

from . import queries
from .db import query_series

PAGE_HEAD = """<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="10">
<title>bmon — 0102 Monitor</title>
<style>
 body{font-family:sans-serif;background:#0f1115;color:#ddd;margin:0;padding:12px}
 h1{font-size:18px} .kpis{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}
 .kpi{background:#1a1e26;border-radius:6px;padding:8px 10px}
 .kpi .v{font-size:20px;font-weight:700} .kpi .l{color:#888;font-size:12px}
 a{color:#6ab0f3} table{border-collapse:collapse;font-size:13px;margin:8px 0}
 td,th{border:1px solid #2a2f3a;padding:3px 8px} .nav a{margin-right:10px}
 .grid{display:flex;flex-wrap:wrap;gap:10px}
 .card{background:#1a1e26;border-radius:6px;padding:8px;margin:4px}
 .warn{color:#f66} .ok{color:#6c6}
</style></head><body>"""

GROUP_LABELS = [("temp", "温度"), ("volt", "电压"), ("fan", "风扇"),
                ("power", "功率"), ("psu", "电源"), ("status", "状态位"),
                ("load", "负载"), ("mem", "内存"), ("disk", "磁盘")]


def svg_chart(points, w=560, h=90, color="#4fc3f7") -> str:
    if not points:
        return (f'<svg width="{w}" height="{h}">'
                f'<text x="8" y="{h // 2}" fill="#555">no data</text></svg>')
    ts0, ts1 = points[0][0], points[-1][0]
    vs = [v for _, v in points]
    vmin, vmax = min(vs), max(vs)
    if vmax - vmin < 1e-9:
        vmax = vmin + 1.0
    coords = []
    for ts, v in points:
        x = (ts - ts0) / max(1, ts1 - ts0) * (w - 8) + 4
        y = h - 8 - (v - vmin) / (vmax - vmin) * (h - 16)
        coords.append(f"{x:.1f},{y:.1f}")
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
            f'<polyline fill="none" stroke="{color}" stroke-width="1.5" '
            f'points="{" ".join(coords)}"/>'
            f'<text x="4" y="12" fill="#888" font-size="10">'
            f'{vmin:.2f} ~ {vmax:.2f}</text></svg>')


def _nav() -> str:
    links = ['<a href="/">总览</a>']
    links += [f'<a href="/group/{g}">{label}</a>' for g, label in GROUP_LABELS]
    links += ['<a href="/sel">SEL</a><a href="/sdc">SDC</a>']
    return '<div class="nav">' + "".join(links) + "</div>"


def _fmt_ts(ts: int) -> str:
    return time.strftime("%m-%d %H:%M:%S", time.localtime(ts))


def render_index(conn, range_key: str = "24h") -> str:
    ov = queries.get_overview(conn)
    cards = []
    for k in ov["kpis"]:
        # unit 由固件提供,插值前转义(终版评审修复 3)
        val = (f'{k["value"]:.2f}{html.escape(k["unit"])}'
               if k["value"] is not None else "N/A")
        age = (f'<div class="l warn">过期 {k["age_s"]}s</div>'
               if k["age_s"] > 10 else "")
        cards.append(
            f'<div class="kpi"><div class="l">{html.escape(k["label"])}</div>'
            f'<div class="v">{val}</div>{age}'
            f'{svg_chart(k["history"], 150, 36)}</div>')
    groups_line = " ".join(
        f'<span class="card">{g}: {info["count"]} 个, 更新于 '
        f'{_fmt_ts(info["last_ts"]) if info["last_ts"] else "-"}</span>'
        for g, info in sorted(ov["groups"].items()))
    return (PAGE_HEAD
            + f"<h1>bmon — 0102 Monitor ({time.strftime('%Y-%m-%d %H:%M:%S')})</h1>"
            + _nav() + '<div class="kpis">' + "".join(cards) + "</div>"
            + f'<div class="grid">{groups_line}</div></body></html>')


def render_group(conn, grp: str, range_key: str = "24h") -> str:
    now = int(time.time())
    span = queries.RANGES[range_key]
    parts = []
    for s in queries.get_sensors(conn, grp):
        if s["last_value"] is None:
            continue
        stale = ' class="warn"' if (s["age_s"] or 999) > 10 else ' class="ok"'
        if grp == "status":
            # 终版评审修复 1:状态位显示采集的原始十六进制断言位掩码
            # (last_status,本机正常态 0x8000),缺失回退 N/A,不显示十进制
            # 数值 —— 固件标称断言态非 0,按数值渲染必成假告警
            parts.append(f'<div class="card"><b>{html.escape(s["name"])}</b>'
                         f'<span{stale}>: '
                         f'{html.escape(s.get("last_status") or "N/A")}'
                         '</span></div>')
            continue
        # unit 由固件提供,插值前转义(终版评审修复 3)
        latest = f'{s["last_value"]:.2f} {html.escape(s["unit"])}'.strip()
        if grp == "core":
            parts.append(f'<div class="card"><b>{html.escape(s["name"])}</b>'
                         f'<span{stale}>: {latest}</span></div>')
        else:
            hist = query_series(conn, s["id"], now - span, now, 200)
            parts.append(f'<div class="card"><b>{html.escape(s["name"])}</b>'
                         f'<span{stale}> {latest}</span>{svg_chart(hist)}</div>')
    return (PAGE_HEAD + f"<h1>{html.escape(grp)} — {range_key}</h1>" + _nav()
            + '<div class="grid">' + "".join(parts) + "</div></body></html>")


def render_sel(conn) -> str:
    events = queries.get_sel(conn, 200)
    rows = "".join(
        f"<tr><td>{_fmt_ts(e['ts'])}</td><td>{html.escape(e['severity'] or '')}</td>"
        f"<td>{html.escape(e['event_type'] or '')}</td>"
        f"<td>{html.escape(e['message'] or '')}</td></tr>" for e in events)
    return (PAGE_HEAD + "<h1>SEL 事件</h1>" + _nav()
            + "<table><tr><th>时间</th><th>级别</th><th>类型</th><th>消息</th></tr>"
            + rows + "</table></body></html>")


def render_sdc(conn) -> str:
    s = queries.get_sdc(conn)
    if not s:
        return (PAGE_HEAD + "<h1>SDC 压测结果</h1>" + _nav()
                + "<p>暂无数据(未配置日志或日志不存在)</p></body></html>")
    t = s["totals"]
    head = ("<h1>SDC 压测结果</h1>" + _nav()
            + f"<p>Pass <b>{t.get('pass', 0)}</b> / "
            f"Fail <b class='warn'>{t.get('fail', 0)}</b> / "
            f"Skip {t.get('skip', 0)} / Interrupted {t.get('interrupt', 0)} / "
            f"Invalid {t.get('invalid', 0)} / Total {t.get('total', 0)}</p>")
    tbl = ("<table><tr><th>套件</th><th>用例</th><th>P</th><th>F</th>"
           "<th>S</th><th>I</th><th>V</th><th>T</th></tr>")
    for suite in s["suites"]:
        for te in suite.get("tests", []):
            tbl += (f"<tr><td>{html.escape(str(suite.get('name', '')))}</td>"
                    f"<td>{html.escape(str(te.get('name', '')))}</td>"
                    f"<td>{te.get('pass', 0)}</td>"
                    f"<td class='warn'>{te.get('fail', 0)}</td>"
                    f"<td>{te.get('skip', 0)}</td>"
                    f"<td>{te.get('interrupt', 0)}</td>"
                    f"<td>{te.get('invalid', 0)}</td>"
                    f"<td>{te.get('total', 0)}</td></tr>")
    tbl += "</table>"
    return PAGE_HEAD + head + tbl + "</body></html>"
