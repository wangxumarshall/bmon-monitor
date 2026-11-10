# bmon — 0102 服务器 BMC 监控

长江计算 R240K V2(4×Kunpeng920_5250,192 核,754Gi)的 BMC+OS 秒级监控系统。
Redfish 采集 → SQLite(WAL)→ Textual TUI / FastAPI REST / 内置 Web 页。
BMC 凭据集中在机器清单 `machines.txt`(不进仓库),支持多机采集与远程按需查看。

## 组件
- `bmon-collect`(systemd):1s 采集 BMC 213 传感器 + PSU 电气 + OS 汇总;10s 每核占用;
  60s SEL/SDC 日志;5min 分钟级聚合;1h 清理(raw 48h / 聚合 35d)。
  凭据全部来自 `machines.txt`,清单变更热加载(改凭据免重启)
- `bmon-serve`(systemd):`0.0.0.0:8102` REST + Web 页
- `bmon tui`:本机 TUI;`bmon tui --remote http://172.168.160.42:8102` 远程 TUI

## 机器清单(`machines.txt`)
- 格式:一行一台 `ip 账号 密码 [别名]`,空格分隔;`#` 注释,空行忽略;别名缺省用 ip
- 凭据不进仓库:`machines.txt` 已被 `.gitignore` 忽略,示例见 `machines.example.txt`
- 文件权限 0600(保存时自动设置),防止同机其他用户读取
- `config.toml [collect] nodes` 指定要采集的机器别名列表:别名==node_id 的是本机
  (source 保持 `bmc`),其余为远端(source 打上 `bmc@别名` 命名空间)
- 文件缺失时仅采 OS,不采集 BMC;改凭据/加减机器保存后热加载生效,无需重启

### TUI「配置」页
- 一行一台直接编辑清单;已保存的密码显示 `****`,保存时密码列仍为 `****` 的行
  保留原密码(数据源按 ip/别名回填,界面全程不经手明文)
- 本机模式直接写 `machines.txt`;远程模式经 serve `POST /api/machines` 代理保存
- 清单有错(列数不对/别名重复等)整体拒绝保存,不落盘半新半旧的状态

### TUI「远程机器」页
- 仅本页激活时轮询当前选中的机器(看谁采谁),切走标签页即停采
- 曲线与极值为会话内数据(内存环,约 1 小时),不入库、切机器即清,无跨会话历史
- 远程 TUI 的清单与实时读数均经 serve 代理,无需直达各 BMC

## 常用命令
```bash
sudo systemctl status bmon-collect bmon-serve   # 服务状态
journalctl -u bmon-collect -f                   # 采集日志
.venv/bin/bmon tui                              # 本机 TUI(键 1-6 切范围,r 切刷新率,m 手动刷新,q 退出;配置页 ctrl+s 保存清单)
.venv/bin/pytest                                # 全部测试
BMC_LIVE=1 BMC_USER=... BMC_PASS=... .venv/bin/pytest -k live   # 含真实 BMC 的测试(凭据经环境变量,不入库)
```

## 部署(systemd)
```bash
sudo cp deploy/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bmon-collect bmon-serve
```

## REST API
| 端点 | 说明 |
|---|---|
| `/api/overview` | KPI(含近 1h 迷你序列)+ 分组概览 |
| `/api/sensors?group=temp` | 传感器清单(阈值/最新值/age) |
| `/api/history?sensor=<id>&range=1h&max_points=300` | 桶聚合序列(1h/6h/12h/24h/7d/30d) |
| `/api/extremes?sids=1,2&range=24h` | 一批传感器的范围极值 |
| `/api/sel` | BMC SEL 事件(倒序) |
| `/api/sdc` | SDC 压测结果(无日志为 null) |
| `/api/machines` | 机器清单读写(GET 掩码 `****` 展示;POST 保存,密码列仍为 `****` 的行保留原密码) |
| `/api/live/{alias}/sensors` | 按需实时读数(仅被查看的机器才发起 BMC 拉取,TTL ~1s 防抖) |
| `/`、`/group/{grp}`、`/sel`、`/sdc` | 内置 Web 页(10s 自刷新) |

内网直连,无鉴权(与现有监控同策略)。

## 配置(`config.toml`)
采集节奏、保留时长、端口、SDC 日志路径、监控磁盘路径;
机器清单位置(`[machines] file`)与采集节点(`[collect] nodes`)也在此配置。
BMC 凭据不在 config.toml(见上方机器清单)。
本机无 cpufreq/频率传感器,动态频率不采集(静态 2.6GHz 见 TUI 标题)。

### 从旧版 `[bmc]` 账密迁移
旧版把 BMC 账号/密码写在 `config.toml [bmc]`;现在凭据移入机器清单:
1. 把该机器写进 `machines.txt` 一行(`ip 账号 密码 [别名]`,可复制 `machines.example.txt` 修改);
2. 重启 `bmon-collect`。

## 已知限制
- SEL 同步仅走 Redfish(无 ipmitool 回退)。
- SDC 每核分布热力图未实现(per_core 数据已采集并可通过 `/api/sdc` 获取)。
- 状态位显示原始十六进制断言位掩码(本机正常态 0x8000,不做告警着色)。
- 部署采用 `SELinuxContext=unconfined_t`(单元文件内有注释说明)。
- git 历史仍含旧密码(内部仓库可接受;如需彻底清除须重写历史)。
