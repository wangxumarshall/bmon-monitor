import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = "config.toml"


@dataclass
class Config:
    node_id: str = "0102"
    machines_file: str = "machines.txt"
    # 可变默认值必须用 default_factory,避免所有实例共享同一个 list
    collect_nodes: list = field(default_factory=lambda: ["0102"])
    bmc_verify_tls: bool = False
    db_path: str = "data/bmon.db"
    raw_keep_s: int = 172800
    rollup_keep_s: int = 3024000
    interval_core_s: int = 10
    interval_slow_s: int = 60
    interval_rollup_s: int = 300
    interval_prune_s: int = 3600
    serve_host: str = "0.0.0.0"
    serve_port: int = 8102
    sdc_log_path: str = "/home/wangxu/sdc-stressng/sdc_stress_summary.log"
    disk_path: str = "/"


def load_config(path: str | None = None) -> Config:
    """加载 config.toml;文件不存在/缺键时使用默认值。

    BMC 凭据不在此处:已移入 machines.txt(见 bmon/machines.py),
    这里只保留连接参数 verify_tls 与清单位置 machines_file。
    """
    cfg = Config()
    p = Path(path or DEFAULT_CONFIG_PATH)
    if not p.exists():
        return cfg
    raw = tomllib.loads(p.read_text())

    def take(section: str, key: str, field: str) -> None:
        v = raw.get(section, {}).get(key)
        if v is not None:
            setattr(cfg, field, v)

    take("server", "node_id", "node_id")
    take("machines", "file", "machines_file")
    take("collect", "nodes", "collect_nodes")
    take("bmc", "verify_tls", "bmc_verify_tls")
    take("storage", "db_path", "db_path")
    take("storage", "raw_keep_s", "raw_keep_s")
    take("storage", "rollup_keep_s", "rollup_keep_s")
    take("collect", "interval_core_s", "interval_core_s")
    take("collect", "interval_slow_s", "interval_slow_s")
    take("collect", "interval_rollup_s", "interval_rollup_s")
    take("collect", "interval_prune_s", "interval_prune_s")
    take("serve", "host", "serve_host")
    take("serve", "port", "serve_port")
    take("sdc", "log_path", "sdc_log_path")
    take("os", "disk_path", "disk_path")
    return cfg
