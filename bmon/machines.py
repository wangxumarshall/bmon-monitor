import os
from dataclasses import dataclass
from pathlib import Path

MASK = "****"


@dataclass
class Machine:
    ip: str
    user: str
    password: str
    alias: str

    @property
    def base_url(self) -> str:
        # BMC Redfish 常规走 https;ip 已带 scheme(如 http://ip:443)时原样返回
        return self.ip if "://" in self.ip else f"https://{self.ip}"


def _data_lines(text: str):
    """产出 (行号, 有效行);跳过空行与 # 注释,行号从 1 起,报错要指明行号。"""
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if line and not line.startswith("#"):
            yield lineno, line


def _parse_pairs(text: str) -> tuple[list[tuple[int, Machine]], list[str]]:
    """核心解析:返回 [(行号, Machine)] 与错误列表;坏行跳过,继续看后续行。"""
    pairs: list[tuple[int, Machine]] = []
    errs: list[str] = []
    seen_ip: set[str] = set()
    seen_alias: set[str] = set()
    for lineno, line in _data_lines(text):
        cols = line.split()
        if len(cols) < 3:
            errs.append(f"第{lineno}行: 至少需要 ip 账号 密码 三列")
            continue
        if len(cols) > 4:
            errs.append(f"第{lineno}行: 最多 4 列(ip 账号 密码 [别名])")
            continue
        ip, user, password = cols[0], cols[1], cols[2]
        alias = cols[3] if len(cols) == 4 else ip  # 别名缺省用 ip
        # 别名/ip 各用一个 set 去重,重复即报错,防止采集端键冲突
        if alias in seen_alias:
            errs.append(f"第{lineno}行: 别名重复 ({alias})")
            continue
        if ip in seen_ip:
            errs.append(f"第{lineno}行: ip 重复 ({ip})")
            continue
        seen_alias.add(alias)
        seen_ip.add(ip)
        pairs.append((lineno, Machine(ip=ip, user=user, password=password,
                                      alias=alias)))
    return pairs, errs


def parse_machines(text: str) -> tuple[list[Machine], list[str]]:
    """解析清单文本为 (机器列表, 错误消息列表)。"""
    pairs, errs = _parse_pairs(text)
    return [m for _, m in pairs], errs


def to_text(machines: list[Machine], mask: bool = True) -> str:
    """序列化回清单文本;mask=True 时密码列输出 ****,供界面展示/编辑。"""
    lines = [f"{m.ip} {m.user} {MASK if mask else m.password} {m.alias}"
             for m in machines]
    return "".join(f"{line}\n" for line in lines)


def mask_text(text: str) -> str:
    """整篇掩码:只把机器行(≥3 列)的第 3 列密码换成 ****,其余行原样。

    与 to_text(parse_machines(text)) 的关键差异:后者只重排合法机器行,
    注释/空行/坏行会静默消失 —— 配置页加载后第一次 ctrl+s 就会把用户
    手写的这些内容从文件里删掉(machines.example.txt 与 README 均允许
    写注释)。本函数逐行处理,保证编辑器文本往返不丢内容:
    - 注释行(# 开头)与空行(含纯空白)原样保留;
    - ≥3 列的行视为机器行,第 3 列(密码列)换 MASK 后按单空格重排;
    - <3 列的行不可能是机器,原样保留,解析错误由 errors 单独呈现。
    """
    out: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            out.append(raw)          # 注释/空行:用户格式,一字不动
            continue
        cols = stripped.split()
        if len(cols) < 3:
            out.append(raw)          # 坏行:留给解析错误呈现,不代用户删
            continue
        cols[2] = MASK
        out.append(" ".join(cols))
    # splitlines() 不携带末尾换行:原文以 \n 结尾时补回,保持文件形态
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def merge_masked(text: str, old: list[Machine]) -> tuple[list[Machine], list[str]]:
    """合并编辑后的清单:密码列仍是 **** 视为未修改,从 old 回填真实密码。

    回填按 ip 优先、别名次之(允许用户改 ip 但保留别名);清单有任何错误时
    整体拒绝(返回空列表),避免半新半旧的状态落盘。
    """
    pairs, errs = _parse_pairs(text)
    if errs:
        return [], errs
    by_ip = {m.ip: m for m in old}
    by_alias = {m.alias: m for m in old}
    machines: list[Machine] = []
    for lineno, m in pairs:
        if m.password != MASK:
            machines.append(m)
            continue
        src = by_ip.get(m.ip)
        if src is None:
            src = by_alias.get(m.alias)
        if src is None:
            errs.append(f"第{lineno}行: 密码为 {MASK} 但无对应已有机器 (ip={m.ip})")
            continue
        m.password = src.password
        machines.append(m)
    if errs:
        return [], errs
    return machines, []


def load_machines(path: str) -> list[Machine]:
    """读清单文件;缺失或解析出错返回 [],由调用方决定回退策略。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # UnicodeDecodeError(如 GBK 存的中文注释)是 ValueError 而非 OSError,
        # 漏接会向上抛、击穿 serve POST 合并等调用方;归一到"不可读=空清单"。
        return []
    machines, errs = parse_machines(text)
    return machines if not errs else []


def save_machines(path: str, machines: list[Machine]) -> None:
    """明文密码落盘,chmod 0600 防止同机其他用户读取。"""
    p = Path(path)
    p.write_text(to_text(machines, mask=False), encoding="utf-8")
    os.chmod(p, 0o600)
