from bmon.serve import create_app
from bmon.webpage import svg_chart
from fastapi.testclient import TestClient


def test_svg_chart_basic_and_empty():
    svg = svg_chart([(1, 10.0), (2, 20.0), (3, 15.0)])
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert "polyline" in svg and "10.00 ~ 20.00" in svg
    assert "no data" in svg_chart([])


def test_index_page(seeded_db):
    client = TestClient(create_app(seeded_db[0]))
    html = client.get("/").text
    assert "0102 Monitor" in html
    assert "Mem Usage" in html
    assert "polyline" in html            # KPI sparkline
    assert 'href="/group/temp"' in html  # 分组导航


def test_group_page(seeded_db):
    client = TestClient(create_app(seeded_db[0]))
    html = client.get("/group/temp").text
    assert "CPU1_TEMP" in html and "48.00" in html
    html_core = client.get("/group/core").text  # core 组无折线
    assert "polyline" not in html_core


def test_sel_and_sdc_pages(seeded_db):
    client = TestClient(create_app(seeded_db[0]))
    assert "test event" in client.get("/sel").text
    assert "暂无数据" in client.get("/sdc").text


def test_group_page_xss_escaped(seeded_db):
    # 注:%2F 会被 ASGI 层解码为真实 "/",导致路径参数无法匹配(404),
    # 故用不含斜杠的载荷;断言不变——转义后出现 &lt;script&gt;、不出现 <script>。
    client = TestClient(create_app(seeded_db[0]))
    r = client.get("/group/%3Cscript%3Ealert(1)")
    assert r.status_code == 200
    assert "&lt;script&gt;" in r.text
    assert "<script>" not in r.text


def test_group_status_page_shows_hex_mask(db_path):
    # 终版评审修复 1:状态位显示采集的原始十六进制断言位掩码(本机正常态
    # 0x8000),不显示十进制数值 —— 固件标称断言态非 0,按数值渲染必成假告警
    import time

    from bmon.db import connect, init_db, update_latest, upsert_sensor

    conn = connect(db_path)
    init_db(conn)
    sid = upsert_sensor(conn, "bmc", "DIMM1_STATUS", "status", "status")
    update_latest(conn, int(time.time()), [(sid, 32768.0, "0x8000")])
    sid2 = upsert_sensor(conn, "bmc", "DIMM2_STATUS", "status", "status")
    update_latest(conn, int(time.time()), [(sid2, 32768.0, None)])
    conn.close()
    client = TestClient(create_app(db_path))
    page = client.get("/group/status").text
    assert "0x8000" in page
    assert "32768.00" not in page
    assert "N/A" in page            # last_status 缺失回退 N/A,不显示数值
    assert "polyline" not in page    # 状态位分支不画折线


def test_unit_xss_escaped(db_path):
    # 终版评审修复 3:固件可控的 unit 插值进 HTML 必须转义
    # (KPI val 与分组页 latest 两处)
    import time

    from bmon.db import connect, init_db, update_latest, upsert_sensor

    conn = connect(db_path)
    init_db(conn)
    sid = upsert_sensor(conn, "os", "mem_usage_pct", "mem",
                        "<script>alert(1)</script>")
    update_latest(conn, int(time.time()), [(sid, 51.0, "ok")])
    tsid = upsert_sensor(conn, "bmc", "EVIL_TEMP", "temp", "<x>W")
    update_latest(conn, int(time.time()), [(tsid, 48.0, "ok")])
    conn.close()
    client = TestClient(create_app(db_path))
    index = client.get("/").text
    assert "<script>alert(1)" not in index
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in index
    grp = client.get("/group/temp").text
    assert "<x>" not in grp
    assert "&lt;x&gt;W" in grp


def test_bad_range_400(seeded_db):
    client = TestClient(create_app(seeded_db[0]))
    assert client.get("/group/temp", params={"range": "bad"}).status_code == 400
    assert client.get("/", params={"range": "bad"}).status_code == 400
