import argparse

from .config import load_config


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bmon", description="0102 监控 TUI")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("tui", help="启动 TUI")
    p.add_argument("--remote",
                   help="远程 bmon-serve 地址,如 http://172.168.160.42:8102")
    p.add_argument("--config", default="config.toml")
    args = ap.parse_args(argv)
    if args.cmd != "tui":
        ap.print_help()
        return 2
    from .tui.app import BmonApp
    from .tui.datasource import LocalDataSource, RemoteDataSource
    if args.remote:
        ds = RemoteDataSource(args.remote)
    else:
        # 本机模式:清单路径与 verify_tls 一并交给数据源,机器页由 TUI 进程直连 BMC
        cfg = load_config(args.config)
        ds = LocalDataSource(cfg.db_path, cfg.machines_file, cfg.bmc_verify_tls)
    BmonApp(ds).run()
    return 0
