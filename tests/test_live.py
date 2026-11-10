from bmon.bmc import BmcError, SensorReading
from bmon.live import LivePoller
from bmon.machines import parse_machines


class FakeBmc:
    """离线假客户端:形态对齐 tests/test_collect.py(注入读数/错误)。

    额外计数 fetches:TTL 防抖是否真的少打 BMC,靠它验证而非只数工厂调用。
    """

    def __init__(self, readings=None, error=False):
        self.readings = readings or []
        self.error = error
        self.fetches = 0

    def get_all_readings(self):
        self.fetches += 1
        if self.error:
            raise BmcError("boom")
        return self.readings


class EvilBmc:
    """抛非 BmcError 原生异常的客户端,模拟 Redfish JSON 畸形
    (parse_* 里的 s["Name"]/float(...) 在 _get 的 try 之外执行)。"""

    def __init__(self):
        self.fetches = 0

    def get_all_readings(self):
        self.fetches += 1
        raise ValueError("malformed")


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


def test_success_payload_shape():
    """成功 payload 精确形状:三键;读数六键,thresholds 原样透传(可为 None)。"""
    thr = {"upper_crit": 85}
    def factory(m):
        return FakeBmc([SensorReading("bmc", "CPU1_TEMP", 48.0, "degrees C", "temp",
                                      "ok", thr)])
    ms, _ = parse_machines("9.3.12.13 u p 0102\n")
    p = LivePoller(ms, ttl_s=60, client_factory=factory)
    r = p.read("0102")
    assert set(r) == {"ok", "ts", "readings"} and isinstance(r["ts"], int)
    assert r["readings"] == [{"name": "CPU1_TEMP", "value": 48.0, "unit": "degrees C",
                              "grp": "temp", "status": "ok", "thresholds": thr}]


def test_ttl_expiry_refetches_with_same_client():
    """TTL 过期后重拉,但复用已建 client(连接不重建)。"""
    made = []
    def factory(m):
        c = FakeBmc([SensorReading("bmc", "CPU1_TEMP", 48.0, "degrees C", "temp", "ok")])
        made.append(c)
        return c
    ms, _ = parse_machines("9.3.12.13 u p 0102\n")
    p = LivePoller(ms, ttl_s=0, client_factory=factory)   # ttl=0:每次读都过期
    p.read("0102"); p.read("0102")
    assert len(made) == 1           # client 只建一次
    assert made[0].fetches == 2     # 过期后确实重新拉取


def test_error_cached_within_ttl():
    """BMC 失败结果同样缓存一个 TTL:不立刻重试,保持 ~1s 重试节奏。"""
    made = []
    def factory(m):
        c = FakeBmc(error=True)
        made.append(c)
        return c
    ms, _ = parse_machines("9.3.12.13 u p 0102\n")
    p = LivePoller(ms, ttl_s=60, client_factory=factory)
    r1 = p.read("0102"); r2 = p.read("0102")
    assert r1["code"] == r2["code"] == 502 and r1["error"] and r2["error"]
    assert made[0].fetches == 1     # 第二次读取命中错误缓存,未再打 BMC


def test_malformed_data_maps_to_502_not_exception():
    """畸形数据(非 BmcError 的 ValueError/KeyError 等)不得击穿 read():
    与 collect.py tick 同口径兜底,映射为 502 且同样缓存一个 TTL。"""
    evil = EvilBmc()
    ms, _ = parse_machines("9.3.12.13 u p 0102\n")
    p = LivePoller(ms, ttl_s=60, client_factory=lambda m: evil)
    r1 = p.read("0102")                    # 不得抛异常
    r2 = p.read("0102")
    assert r1 == {"ok": False, "error": "malformed", "code": 502}
    assert r2 == r1 and evil.fetches == 1  # 异常路径同样被 TTL 防抖


def test_set_machines_keeps_cache_for_unchanged_creds():
    """凭据 (ip, user, password) 未变的机器保留 client 与缓存读数;
    凭据变更/被删除的机器缓存作废。"""
    made = []
    def factory(m):
        c = FakeBmc([SensorReading("bmc", "CPU1_TEMP", 48.0, "degrees C", "temp", "ok")])
        made.append(c)
        return c
    ms, _ = parse_machines("9.3.12.13 u p 0102\n")
    p = LivePoller(ms, ttl_s=60, client_factory=factory)
    p.read("0102")
    assert made[0].fetches == 1
    # 清单例行重载、凭据未变(即使 Machine 对象是新的):不重连不重拉
    p.set_machines(parse_machines("9.3.12.13 u p 0102\n")[0])
    assert p.read("0102")["ok"]
    assert len(made) == 1 and made[0].fetches == 1
    # 别名重命名但凭据未变:缓存跟新别名走(按凭据匹配,而非别名)
    p.set_machines(parse_machines("9.3.12.13 u p 0102b\n")[0])
    assert p.read("0102b")["ok"]
    assert len(made) == 1 and made[0].fetches == 1
    # 密码变了:旧缓存作废,重建 client 重拉
    p.set_machines(parse_machines("9.3.12.13 u p2 0102\n")[0])
    p.read("0102")
    assert len(made) == 2 and made[1].fetches == 1
    # 机器被移除:别名变 404
    p.set_machines([])
    assert p.read("0102") == {"ok": False, "error": "未知机器: 0102", "code": 404}
