import os
import re

_STATUS_KEYS = {"pass": "pass", "fail": "fail", "skip": "skip",
                "interrupt": "interrupt", "interrupted": "interrupt",
                "invalid": "invalid", "v": "invalid", "total": "total"}


def _counts(kv: dict) -> dict:
    c = {}
    for k, v in kv.items():
        key = _STATUS_KEYS.get(k.lower())
        if key:
            try:
                c[key] = int(v)
            except ValueError:
                pass
    return c


def parse_sdc_log(path: str) -> dict | None:
    """解析 stress-ng 汇总日志;缺失/不可读/无有效行返回 None。

    v1 格式:overall / suite= / test= / core= 行,key=value 计数。
    真实日志出现后需按实际格式适配一次(见设计文档风险说明)。
    """
    try:
        mtime = os.path.getmtime(path)
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None

    totals: dict = {}
    suites: dict = {}
    per_core: dict = {}
    cur_suite = None
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        kv = dict(re.findall(r"([A-Za-z_]+)=(\S+)", s))
        first = s.split(None, 1)[0]
        keyword = first.split("=", 1)[0]
        if keyword == "overall":
            totals = _counts(kv)
        elif keyword == "suite":
            name = kv.get("suite", "")
            cur_suite = {"name": name, "tests": [], **_counts(kv)}
            suites[name] = cur_suite
        elif keyword == "test":
            if cur_suite is None:
                cur_suite = {"name": "", "tests": []}
                suites[""] = cur_suite
            cur_suite["tests"].append({"name": kv.get("test", ""), **_counts(kv)})
        elif keyword == "core":
            core, test = kv.get("core"), kv.get("test")
            if core is None or test is None:
                continue
            per_core.setdefault(core, {})[test] = _counts(kv)

    if not totals and not suites and not per_core:
        return None
    for suite in suites.values():
        for key in ("pass", "fail", "skip", "interrupt", "invalid", "total"):
            if suite.get(key) is None:
                suite[key] = sum(t.get(key, 0) for t in suite["tests"])
    return {"log_mtime": mtime, "totals": totals,
            "suites": list(suites.values()), "per_core": per_core}
