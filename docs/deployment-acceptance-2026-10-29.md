# 首次部署验收

2026-10-29，在已有业务运行的 OCI Ampere A1 ARM64 节点部署。节点为 4 OCPU / 24GB；本次不修改实例规格、账户类型、公开服务入口、现有代理或流量上限。

## 代码与环境

- 实际负载验收代码：`c1ab753e850c55abea1c02d8c4faebb73707ff17`；本报告提交只增加文档，运行代码保持一致。
- 节点 Python 3.14.4、systemd 259，发行版 `stress-ng 0.20.01-1`。
- 本机与 ARM64 节点均通过 33 项测试；对应 [GitHub Actions](https://github.com/doulongfei/oci-activity-guard/actions/runs/40850576120) 通过。
- systemd unit 验证通过，监测器和 timer 已启用开机启动。

## 实际运行结果

| 验证 | 结果 |
| --- | --- |
| 60 秒校准 | worker 正常到期，控制器正确记录完成；资源与隔离属性符合配置 |
| 完整 12 分钟窗口 | 控制器记录运行约 720.3 秒，正常到期，非提前中断 |
| 后台 CPU | 整机口径平均 29.998%，20 秒采集区间峰值约 30.12% |
| 整机 CPU | 同期监测样本平均约 30.79%，中位数约 31.21% |
| 业务健康 | 完整窗口 36 次独立采集全部健康 |
| 翻译业务 | 负载期间真实英译中请求 HTTP 200，约 0.604 秒完成 |
| 并发调度 | 负载期间真实启动 timer 对应 service，正确跳过重复控制器，不终止现有合法 worker |
| 手动暂停与恢复 | 实际运行中暂停后 worker 停止并记为 interrupted；暂停期间校准被阻止，恢复后回到原观察期 |
| 网络隔离 | 内核 network namespace 与宿主不同，worker 路由表为空 |
| 资源属性 | DynamicUser=yes、PrivateNetwork=yes、CPUQuota=140%、MemoryMax=256MB、MemorySwapMax=0 |
| 原业务配置 | Caddy、流量保护和小游戏配置的 SHA-256 与部署前一致；核心服务持续运行 |
| 预算与观察期 | 多次安装升级保留已有预算、暂停状态、采样和原观察期，不重新计时 |

一次早期校准发现只读工作目录不满足 stress-ng 的临时路径要求，已通过显式 private `/tmp` 修复。验证过程中还修正了“停止后退出码为 0 即被记为完成”的问题：现在同时核对运行时长、暂停和保护事件。最初失败及中断记录保留，没有清空数据库或假计为有效 CPU 时间。

## 自动运行状态

首个观察期结束时间为北京时间 **2026-10-30 17:17:38**。之后基础窗口每两小时按需执行，观察期结束后的首个基础窗口约为当天 18:00；若自然业务已达目标或业务保护条件触发，则跳过。奇数 UTC 小时仅检查有界补偿。

常用命令：

```sh
sudo oci-activity-guard status
sudo oci-activity-guard pause
sudo oci-activity-guard resume
sudo systemctl list-timers oci-activity-guard.timer --no-pager
```

## 验证边界

尚未经过完整 24 小时和七天运行周期；这些结果必须随真实时间累计。当前为本机采样，未完成 Oracle 云端指标对照，状态中仍明确显示 `cloud_metrics_verified=false`。没有重启整台服务器做物理重启测试；持久化恢复、核数换算、预算、缺测等由自动测试和重复部署验证。

本项目不保证永不回收，账户权益和业务异机备份独立管理。
