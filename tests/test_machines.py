import os

from bmon.machines import MASK, Machine, load_machines, mask_text, merge_masked, parse_machines, save_machines, to_text

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

def test_mask_text_preserves_comments_and_blanks():
    # 注释行与空行(含纯空白行)原样保留:手写清单的排版 round-trip 不丢,
    # 否则配置页第一次 ctrl+s 就会把它们从文件里删掉
    text = "# 生产机器\n\n9.3.12.13 admin s3cret 0102\n   \n"
    out = mask_text(text)
    assert out == "# 生产机器\n\n9.3.12.13 admin **** 0102\n   \n"
    assert "s3cret" not in out

def test_mask_text_masks_3col_and_4col_lines():
    # 3 列(无别名)与 4 列(带别名)机器行:第 3 列密码换 ****,别名不动;
    # 行内多余空白按单空格重排(split 口径与解析一致)
    out = mask_text("9.3.12.13 admin s3cret\n9.3.12.15  admin\tpw2  0103\n")
    assert out == "9.3.12.13 admin ****\n9.3.12.15 admin **** 0103\n"

def test_mask_text_short_line_untouched():
    # 不足 3 列的行不可能是机器:原样保留,解析错误由 errors 单独呈现
    assert mask_text("badline\na b\n") == "badline\na b\n"

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

def test_load_machines_non_utf8(tmp_path):
    # GBK 编码的中文注释不是合法 UTF-8:UnicodeDecodeError 是 ValueError 而非
    # OSError,漏接会让 load_machines 向上抛、击穿 serve 的 POST 合并路径
    p = tmp_path / "machines.txt"
    p.write_bytes("# 中文注释\n9.3.12.13 Administrator Secret1 0102\n".encode("gbk"))
    assert load_machines(str(p)) == []
