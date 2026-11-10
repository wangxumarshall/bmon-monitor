from bmon.sdclog import parse_sdc_log

FIXTURE = """=== SDC STRESS SUMMARY 2026-10-29 15:31:18 ===
overall pass=183 fail=0 skip=59 interrupted=88 invalid=88 total=330
suite=eigen
test=eigen_gemm_cdouble_dynamic_square pass=6 fail=0 skip=3 interrupt=3 invalid=4 total=16
test=eigen_svd pass=5 fail=0 skip=10 interrupt=10 invalid=15 total=40
suite=ldr
test=neon_rot_ldr_at_top pass=19 fail=0 skip=1 interrupt=0 invalid=0 total=20
core=0 test=eigen_gemm_cdouble_dynamic_square pass=6 fail=0 interrupt=3 invalid=4
core=0 test=eigen_svd pass=5 fail=0 interrupt=10 invalid=15
core=30 test=eigen_svd pass=0 fail=0 interrupt=9 invalid=1
"""


def test_parse_full(tmp_path):
    p = tmp_path / "sdc.log"
    p.write_text(FIXTURE)
    s = parse_sdc_log(str(p))
    assert s["totals"] == {"pass": 183, "fail": 0, "skip": 59, "interrupt": 88,
                           "invalid": 88, "total": 330}
    by_name = {x["name"]: x for x in s["suites"]}
    assert by_name["eigen"]["pass"] == 11          # 6+5 由 test 累加
    assert by_name["eigen"]["total"] == 56
    assert by_name["ldr"]["tests"][0]["name"] == "neon_rot_ldr_at_top"
    assert s["per_core"]["0"]["eigen_svd"]["invalid"] == 15
    assert s["per_core"]["30"]["eigen_svd"]["pass"] == 0
    assert s["log_mtime"] > 0


def test_missing_file_returns_none(tmp_path):
    assert parse_sdc_log(str(tmp_path / "nope.log")) is None


def test_unrecognized_content_returns_none(tmp_path):
    p = tmp_path / "g.log"
    p.write_text("hello world\nnothing parseable\n")
    assert parse_sdc_log(str(p)) is None


def test_unknown_prefix_lines_ignored(tmp_path):
    # 仅前缀相似、首 token 不等于关键字的行都不是可识别行
    p = tmp_path / "only_unknown.log"
    p.write_text("testing pass=3\ntest_ended pass=2\noverallx pass=1\n")
    assert parse_sdc_log(str(p)) is None

    # 混入有效日志时被忽略,不产生幻影 suite ""
    p2 = tmp_path / "with_noise.log"
    p2.write_text(FIXTURE + "testing pass=3\n")
    s = parse_sdc_log(str(p2))
    assert {x["name"] for x in s["suites"]} == {"eigen", "ldr"}
    assert s["totals"]["pass"] == 183
    assert [t["name"] for t in s["suites"][0]["tests"]] == [
        "eigen_gemm_cdouble_dynamic_square", "eigen_svd"]


def test_core_only_log_not_none(tmp_path):
    p = tmp_path / "core.log"
    p.write_text("core=3 test=eigen_svd pass=1\n")
    s = parse_sdc_log(str(p))
    assert s is not None
    assert s["totals"] == {}
    assert s["suites"] == []
    assert s["per_core"]["3"]["eigen_svd"]["pass"] == 1


def test_suite_explicit_zero_not_overwritten(tmp_path):
    # suite 行已给出 pass=0,不属于"缺计数",不能被 test 累加覆盖
    p = tmp_path / "suite_zero.log"
    p.write_text("suite=s1 pass=0\ntest=t pass=7 total=7\n")
    s = parse_sdc_log(str(p))
    suite = next(x for x in s["suites"] if x["name"] == "s1")
    assert suite["pass"] == 0
    assert suite["total"] == 7  # 缺失的 total 仍按 test 累加
