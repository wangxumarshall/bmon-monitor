from bmon.config import load_config


def test_defaults_without_file(tmp_path):
    cfg = load_config(str(tmp_path / "missing.toml"))
    assert cfg.machines_file == "machines.txt" and cfg.collect_nodes == ["0102"]
    assert cfg.bmc_verify_tls is False and cfg.db_path == "data/bmon.db"
    assert getattr(cfg, "bmc_password", None) is None      # 凭据字段已删除


def test_partial_override(tmp_path):
    p = tmp_path / "c.toml"; p.write_text('[collect]\nnodes = ["a","b"]\n[machines]\nfile = "m.txt"\n')
    cfg = load_config(str(p))
    assert cfg.collect_nodes == ["a", "b"] and cfg.machines_file == "m.txt"


def test_load_project_config():
    cfg = load_config("config.toml")  # 项目根的真实配置
    assert cfg.node_id == "0102"
    assert cfg.bmc_verify_tls is False
    assert cfg.disk_path == "/"
