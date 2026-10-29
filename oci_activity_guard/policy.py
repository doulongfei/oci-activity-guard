"""纯决策逻辑；计划时间不代替实测活跃时间。"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Decision:
    run: bool
    reason: str
    seconds: int = 0


def unsafe_reason(config, live):
    if live['memory_available'] < max(config.memory_available_min_bytes, live['memory_total'] * config.memory_available_ratio):
        return 'memory-headroom-low'
    if live['disk_free'] < config.disk_free_min_bytes:
        return 'disk-headroom-low'
    if live['psi_full'] >= config.memory_psi_full_limit:
        return 'memory-pressure'
    if live.get('steal', 0) >= config.steal_limit:
        return 'cpu-steal-high'
    return None


def decide(config, now, initialized_at, paused, live, day, week, budget_used, calibration=0, compensation=False, compensation_used=0):
    if paused:
        return Decision(False, 'owner-paused')
    if live is None or now-live.get('at', 0) > 90 or live.get('cpu') is None:
        return Decision(False, 'monitor-stale')
    unsafe = unsafe_reason(config, live)
    if unsafe:
        return Decision(False, unsafe)
    if not live['healthy']:
        return Decision(False, 'business-unhealthy')
    if live['cpu'] >= config.cpu_threshold:
        return Decision(False, 'natural-workload-sufficient')
    # 给停止与控制器收尾预留 30 秒，不能把日预算全部交给计算进程。
    remaining = int(config.daily_max_seconds-budget_used-30)
    if remaining < 60:
        return Decision(False, 'daily-budget-exhausted')
    if calibration:
        if type(calibration) is not int or not 60 <= calibration <= config.slot_seconds:
            raise ValueError('Calibration must be 60..slot_seconds')
        if calibration > remaining:
            return Decision(False, 'daily-budget-exhausted')
        return Decision(True, 'calibration', calibration)
    if now-initialized_at < config.warmup_hours*3600:
        return Decision(False, 'initial-observation')
    if day['eligible_seconds'] >= config.daily_target_seconds:
        return Decision(False, 'daily-target-met')
    if compensation:
        if week['coverage_ratio'] < .95:
            return Decision(False, 'week-history-incomplete')
        if day['eligible_seconds'] >= 7200 or week['eligible_ratio'] >= .08:
            return Decision(False, 'compensation-not-needed')
        remaining = min(remaining, int(2160-compensation_used))
        if remaining < 60:
            return Decision(False, 'compensation-budget-exhausted')
    return Decision(True, 'compensation' if compensation else 'scheduled-deficit', min(config.slot_seconds, remaining))


def resource_properties(config, cores):
    if type(cores) is not int or not 1 <= cores <= 256:
        raise ValueError('Invalid online core count')
    return {'CPUQuota': f'{cores*config.worker_host_cap}%', 'CPUWeight': '1', 'MemoryMax': '256M',
            'MemorySwapMax': '0', 'TasksMax': '16', 'Nice': '19', 'CPUSchedulingPolicy': 'idle',
            'DynamicUser': 'yes', 'NoNewPrivileges': 'yes', 'PrivateTmp': 'yes', 'PrivateNetwork': 'yes',
            'ProtectSystem': 'strict', 'ProtectHome': 'yes', 'ProtectKernelTunables': 'yes',
            'ProtectControlGroups': 'yes', 'RestrictSUIDSGID': 'yes', 'CapabilityBoundingSet': '',
            'BindsTo': 'oci-activity-monitor.service', 'After': 'oci-activity-monitor.service',
            'RuntimeMaxSec': f'{config.slot_seconds+10}s', 'TimeoutStopSec': '10s', 'KillMode': 'control-group'}
