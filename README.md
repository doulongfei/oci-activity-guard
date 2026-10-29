# OCI Activity Guard

面向 OCI Linux 节点的轻量活跃度维护服务：先统计真实业务负载，缺少有效 CPU 时间时才运行短时、低优先级的计算任务，业务压力出现时自动停止。

它降低空闲回收风险，不承诺永久保留实例。账户额度、试用到期、账户状态和数据备份仍需独立管理。当前实现记录本机 `/proc` 指标，不把本地结果冒称为 Oracle 云端指标。

## 机制与默认值

| 项目 | 默认配置 |
| --- | --- |
| 启用过程 | 首 24 小时观察，之后自动按需补足；安装、重启、升级不会清零观察期或预算 |
| 采样 | 每 10 秒探测，每约 60 秒保存一条整机 CPU 样本，保留 14 天 |
| 有效时间 | 整机 CPU 至少 25% 才计入；缺测、时钟跳变与重启不插值 |
| 日目标 | 滚动 24 小时有效时间 144 分钟，包含真实业务 |
| 基础窗口 | 每两小时最多补 12 分钟；自然负载足够时跳过 |
| 补偿 | 奇数 UTC 小时在覆盖充分的日或周指标不足时补偿，最多 36 分钟/24 小时；修补采样边界损失 |
| CPU | 目标整机 30%，硬上限 35%；4 核 `CPUQuota=140%`，2 核 `70%`，每次按实际核数计算 |
| 内存与网络 | worker 256MB、无 swap、无外网；动态低权限用户、只读系统 |
| 硬边界 | worker 单次最长 12 分钟，独立 systemd 超时与整组进程清理；每日全部运行含校准最多 180 分钟 |
| 保护 | CPU 连续繁忙、业务连续失败、内存不足、PSI 异常、CPU steal、磁盘不足或监测失联时停止/拒绝启动 |

CPU、内存、预算和核数均采用主机口径；不通过占满内存或制造外网流量维持利用率。仅使用系统 Python 3、SQLite、systemd 和发行版 `stress-ng`，无 pip 依赖和 Docker 容器。

## 安装

支持 Python 3.12+、cgroup v2 和 systemd 的 Debian/Ubuntu Linux。先确认账户实际额度和已有业务健康；配置示例是本机回环探测，不含凭据、实例 OCID 或公开管理接口。

```sh
git clone https://github.com/doulongfei/oci-activity-guard.git
cd oci-activity-guard
python3 -m unittest discover -s tests -v
apt-cache policy stress-ng
# 用发行版当前提供且审核过的具体版本替换 VERSION。
sudo python3 scripts/install.py --install-dependencies --stress-version VERSION
sudo oci-activity-guard status
```

安装器从已提交 Git 版本导出不可变目录，运行配置首次由 `config.example.json` 创建；再次部署保留已有配置与数据库。依赖安装只在部署阶段执行，运行时不调用 apt。

```text
/opt/oci-activity-guard/releases/<git-sha>/   # 不可变代码与安装材料
/opt/oci-activity-guard/current              # 当前版本指针
/etc/oci-activity-guard/config.json          # root 管理，含本机业务探测
/var/lib/oci-activity-guard/metrics.db        # 私有样本、预算、暂停状态
/var/lib/oci-activity-guard/status.json      # 私有只读状态快照
/var/lib/oci-activity-guard/deployment.json  # 部署提交与依赖版本
```

常驻 `oci-activity-monitor.service`，每小时由 `oci-activity-guard.timer` 检查策略。实际负载使用动态 `oci-activity-worker.service`，每次启动时设置核数对应的 cgroup 上限，避免降配后沿用旧 CPUQuota。

## 校准和运维

```sh
sudo oci-activity-guard calibrate --seconds 60
sudo oci-activity-guard calibrate --seconds 720
sudo oci-activity-guard status
sudo systemctl list-timers oci-activity-guard.timer --no-pager
sudo journalctl -u oci-activity-monitor -u oci-activity-guard -u oci-activity-worker --since today --no-pager

sudo oci-activity-guard pause     # 持久暂停，并立即停止 worker
sudo oci-activity-guard resume    # 恢复策略；保留观察期、历史和预算
```

校准只绕过观察期与日目标，不绕过暂停、业务健康、内存和每日硬预算。校准仍计入当日执行预算；是否实际达标由独立采样决定。

`status` 的 `mode` 是 `observing`、`armed` 或 `paused`。`day/week` 提供实际覆盖率、有效秒数和仅已观测数据的加权 P95；数据不足不能宣称七天达标。`cloud_metrics_verified` 固定为 false，必须在 OCI 控制台对照 `oci_computeagent` 的实际数据。

健康探测限数字回环 HTTP，禁止域名、凭据、云元数据地址和跳转。没有外部控制端口、通知收件人或自动付费/规格修改功能。日志使用英文，控制文件与样本只供管理员读取。

升级前暂停并备份 `/etc/oci-activity-guard` 和 `/var/lib/oci-activity-guard`。更新源码后运行相同安装器，保留之前不可变版本。紧急停止：

```sh
sudo oci-activity-guard pause
sudo systemctl disable --now oci-activity-guard.timer
sudo systemctl stop oci-activity-guard.service oci-activity-monitor.service
```

只停 timer 不会停止已经运行的任务，所以先使用 `pause`。回滚时保持暂停，恢复 `current` 到上一部署目录，并恢复对应 systemd unit；数据库与配置不要清空。备份和恢复业务数据与本项目分别管理。

## 验证与范围

测试包含滚动预算、缺测、CPU 口径、暂停与重启、观察期、补偿上限、4→2 核配额、SSRF/跳转边界、并发控制器和运行中内存保护。CI 使用固定摘要的 GitHub Action。

生产启用还需短时和完整窗口校准、业务响应检查；持续 24 小时与 7 天的观测只能随实际时间完成。程序不会重启或继续已经结束的其他 Agent，也不变更现有代理/流量限额。

依据与参数说明见 [设计说明](docs/design.md)。本项目原创控制代码使用 MIT；外部 `stress-ng` 按其自身许可证独立分发，不内嵌其源码。
