# 0102 BMC 监控 TUI 工具 — 设计文档

日期:2026-10-29
状态:已与用户确认
目标机器:0102(本机 `localhost0102`,内网 172.168.160.42)

## 1. 背景与目标

用户需要一套针对 0102 服务器(长江计算 R240K V2,4 路 Kunpeng 920,192 核,754Gi 内存,openEuler 24.03)的监控系统,对标现有 B0102 网页监控的维度,但形态为:

1. **秒级采集**:每秒从 BMC(9.3.12.13,iBMA)与 OS 采集全部传感器数据,走 Redfish API(实测 ipmitool 全量 SDR 需 31.6s,弃用)
2. **SQLite 存储**:历史数据落库,支持 1h/6h/12h/24h/7d/30d 查询范围
3. **TUI 展示**:Textual 终端界面,全部维度图表化
4. **对外服务**:本机为内网服务器,需监听端口向内网其他机器提供 REST API + 内置简版 Web 仪表盘

## 2. 已验证的环境事实

| 项目 | 结论(2026-10-29 实测) |
|---|---|
| BMC Redfish | `https://9.3.12.13/redfish/v1/`,Administrator / `******` |
| 模拟量传感器 | `/redfish/v1/Chassis/1/ThresholdSensors` → 76 个,~150ms,带上下限阈值 |
| 状态位传感器 | `/redfish/v1/Chassis/1/DiscreteSensors` → 137 个,~240ms |
| 功率/PSU | `/redfish/v1/Chassis/1/Power`:PowerControl 总功耗+min/max/avg;PowerSupplies PSU 电气(PSU1 缺位,PSU2 在役 2000W) |
| SEL | `/redfish/v1/Systems/1/LogServices/Log1`(HTTP 200);回退 `ipmitool sel list`(1.8s) |
| sudo | `sdc` 用户 NOPASSWD:ALL |
| 每核频率/占用 | BMC 不提供,从 OS `/proc/stat`、`/proc/cpuinfo` 采集(192 核) |
| SDC 压测日志 | 参考页路径 `/home/wangxu/sdc-stressng/sdc_stress_summary.log` 本机不存在 → 路径可配置,无数据时显示占位 |
| 网络 | 可直连 pypi,可建 venv 装 textual/plotext/fastapi |

## 3. 架构(方案 A:双守护进程 + 独立 TUI)

```
┌──────────────────┐  每1s: Redfish×3 + /proc 汇总
│ bmon-collect     │  每10s: 每核数据
│ (systemd 常驻)   │  每60s: SEL 增量、SDC 日志解析
│                  │  每5min: 1m 聚合   每1h: 过期清理
└───────┬──────────┘  唯一写入方 → data/bmon.db (WAL)
        │
   共享 SQLite(只读连接)
        │
┌───────▼──────────┐  ┌────────────────────────┐
│ bmon-serve       │  │ bmon tui                 │
│ (systemd 常驻)    │  │ (按需启动,Textual)       │
│ FastAPI :8102    │  │ 本机直读 DB;--remote 走  │
│ REST + 内置Web页 │  │ HTTP API,可远程跑        │
└──────────────────┘  └────────────────────────┘
```

- 采集与 Web 服务相互独立,任一崩溃不影响另一个(压测机 7×24 采集可靠性优先)
- 项目目录:`/home/sdc/wangxu/monitor/`,venv 隔离依赖
- 配置:`config.toml`(BMC 地址/账密、采集节奏、保留期、端口、SDC 日志路径、DB 路径)

## 4. 数据采集

| 数据源 | 方式 | 频率 | 内容 |
|---|---|---|---|
| BMC 模拟量 | GET ThresholdSensors | 1s | 76 个:4 路 CPU 温度、CPU/内存电压、12V 母线、风扇转速、总功率、进/出风口温度等,含阈值 |
| BMC 状态位 | GET DiscreteSensors | 1s | 137 个:Prochot、System Error、DIMM/磁盘/风扇在位、看门狗等 |
| PSU 电气 | GET Power | 1s | 总功耗+min/max/avg、PSU2 输入电压/电流、输出功率/电流/电压 |
| OS 汇总 | /proc/stat、/proc/meminfo、/proc/loadavg、statvfs | 1s | CPU 平均占用、CPU 最高核占用、内存 used/total/avail/usage、swap、load 1/5/15、磁盘占用率/已用/总量 |
| OS 每核 | /proc/stat 每核差分 | 10s(可配) | 192 核每核占用 |
| CPU 频率 | 不可采集 | — | 本机无 cpufreq sysfs、/proc/cpuinfo 无 MHz、BMC 无频率传感器;Redfish Processors 仅静态 FrequencyMHz=2600(Kunpeng920_5250),作为头部静态信息展示 |
| SEL 事件 | GET Log1(OEM HealthEvent 结构化事件)+ Entries 按 Id 增量拉取;失败回退 `ipmitool sel list`(1.8s 全量重同步) | 60s | 按记录 ID 去重增量入库 |
| SDC 压测日志 | mtime 变化才重解析 | 60s | stress-ng 汇总:总计/每用例/每核分布;无文件时占位 |

- 3 个 Redfish GET 用 `requests.Session` keep-alive,单 tick ≈ 0.5s
- 每核默认 10s:1s×384 序列=3300 万行/天价值极低;汇总值仍 1s。节奏均可配置
- BMC 失败:按 tick 重试,`sensors.last_ok` 标记,TUI 显示"过期 +x s"

## 5. SQLite 存储设计

数据库 `data/bmon.db`,WAL 模式,采集器唯一写入,服务与 TUI 只读连接。

```
sensors        传感器字典: id(PK), name, group, unit, source, 阈值列,
               last_value, last_ts, last_ok, 状态
raw            (sensor_id, ts, value) PK(sensor_id, ts) WITHOUT ROWID
               1s+10s 原始数据,保留 48h(覆盖 ≤24h 查询)
rollup_1m      (sensor_id, ts, min, avg, max) PK(sensor_id, ts)
               每分钟聚合,保留 35d(覆盖 7d/30d 查询)
sel_events     SEL: event_id(PK), ts, 类型, 描述, severity, raw
sdc_runs       压测会话: run_id(PK), 日志mtime, 总计 pass/fail/skip/interrupt/invalid/total
sdc_results    每用例×每核: (run_id, test, core, status)
meta           schema 版本、配置摘要
```

- 写入量约 2200 万行/天(235 个 1s 序列 + 192 个 10s 序列),稳态库容 ≈ 2GB(磁盘 14.8TB)
- 后台任务:5 分钟聚合最近窗口;每小时批量清理 raw>48h、rollup>35d
- 查询一律带 `max_points`,SQL 时间桶 GROUP BY:范围≤24h 读 raw、7d/30d 读 rollup_1m
- 写入用批量事务(每 tick 一批),失败整体回滚下一 tick 重试

## 6. TUI(Textual)

- 启动:`bmon tui`(直读 DB)或 `bmon tui --remote http://172.168.160.42:8102`
- 顶栏:标题 "B0102 SDC Monitor — R240K V2 / 192 cores / 754GB" + 实时时钟
- 控件:Range 1h/6h/12h/24h/7d/30d(键 1-6),Refresh 1s/5s/10s/30s/Manual(键 r 切换),q 退出
- KPI 行(卡片+sparkline):CPU Avg Usage、CPU Max Core Usage、Mem Usage、CPU1~4 Temp、Total PWR、PSU2 POut、Load 1min、Disk Usage
- 标签页:总览 / 温度 / 电压 / 风扇 / 电源PSU / 状态位 / 每核CPU / 负载 / 内存 / 磁盘 / SDC压测 / SEL
- 组页布局:传感器表格(名称/当前值/单位/状态/近范围极值)+ 选中传感器 plotext 大图
- 每核CPU页:192 核占用表 + 按占用着色核心网格
- SDC 页:总计面板 + 每用例明细表 + 每核 Pass/Fail 分布热力图(无数据显示"暂无数据")
- 值按阈值着色:正常绿 / 越非临界琥珀 / 越临界红;数据过期灰色标注
- Refresh 只控制 UI 重绘频率,与采集节奏解耦

## 7. HTTP 对外服务(FastAPI + uvicorn)

监听 `0.0.0.0:8102`(可配),内网直连,无鉴权,CORS 全开。

| 端点 | 内容 |
|---|---|
| `GET /api/overview` | 全部 KPI + 各组最新值 + 数据新鲜度 |
| `GET /api/sensors?group=temp` | 传感器清单(元数据+阈值+最新值) |
| `GET /api/history?sensor=CPU1_TEMP&range=1h&max_points=300` | 桶聚合时间序列 |
| `GET /api/sel?limit=100` | SEL 事件(倒序) |
| `GET /api/sdc` | SDC 汇总 + 每用例 + 每核分布 |
| `GET /` | 内置简版 HTML 仪表盘:KPI 卡 + 分组图表 + SEL/SDC 表 |

- Web 页图表库本地 vendored(不用 CDN,保证内网任意机器可渲染);定位为"能用即可",不对标现有监控站的视觉
- uvicorn 多 worker 无必要,单进程即可(读 DB 为轻操作)

## 8. 部署

- venv:`/home/sdc/wangxu/monitor/.venv`,依赖:textual、plotext、fastapi、uvicorn、requests
- systemd 系统单元(用 sudo 安装):`bmon-collect.service`、`bmon-serve.service`,开机自启、崩溃自动重启
- 命令入口:`bmon-collect`(守护)、`bmon-serve`(守护)、`bmon tui`(交互)

## 9. 错误处理

- **SDC 日志格式风险**:本机暂无该日志,解析器 v1 以参考页面展示的字段(总体 Pass/Fail/Skip/Interrupted/Invalid/Total、每用例计数、每核分布)为目标按常见 stress-ng 汇总格式实现并配 fixture 测试;真实日志出现后需适配一次
- BMC 请求失败:下一 tick 重试,传感器标记过期;连续失败写 journal
- SQLite:WAL + busy_timeout;采集器唯一写入方;读方用只读连接
- SDC 日志:文件不存在/写一半 → 解析器防御性处理,板块占位不报错
- SEL:记录 ID 去重,重复拉取无副作用
- 聚合/清理:分批删除、异常捕获、下轮重试

## 10. 测试

- pytest 单测:Redfish JSON 解析、/proc 解析、SEL 解析、SDC 日志解析(fixture 样例)、聚合与清理逻辑(临时 DB)
- 集成冒烟:采集器实跑 1 分钟验证行数;curl 全部端点;TUI 启动渲染检查
- Textual 测试框架做 TUI 冒烟(应用可挂载、数据可刷新)

## 11. 明确不做(YAGNI)

- 告警/通知(邮件、webhook)
- 鉴权与 HTTPS(内网实验机,与现有监控同策略)
- 多服务器聚合(仅本机 0102)
- Web 页深度美化(交给现有监控站,本工具 API 供其调用)
- ipmi 命令控制(风扇策略、电源控制等)——只读监控
