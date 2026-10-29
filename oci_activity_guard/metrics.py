"""读取 Linux 计数器；不把缓存或进程百分比冒充云端指标。"""
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import time


@dataclass(frozen=True)
class Point:
    wall: float
    mono: float
    boot: str
    counters: tuple
    cores: int
    memory_total: int
    memory_available: int
    disk_free: int
    psi_full: float


def cpu_percent(before, after):
    deltas = [right - left for left, right in zip(before.counters, after.counters)]
    if before.boot != after.boot or before.cores != after.cores or len(deltas) != 8 or min(deltas) < 0 or sum(deltas) <= 0:
        return None
    total = sum(deltas)
    return {'cpu': 100 * sum(deltas[index] for index in [0, 1, 2, 5, 6]) / total, 'steal': 100 * deltas[7] / total}


def read_point(proc=Path('/proc'), disk=Path('/')):
    counters = tuple(map(int, (proc / 'stat').read_text().splitlines()[0].split()[1:9]))
    memory = {}
    for line in (proc / 'meminfo').read_text().splitlines():
        key, value = line.split(':', 1)
        memory[key] = int(value.split()[0]) * 1024
    psi = (proc / 'pressure/memory').read_text().splitlines()
    full = next(line for line in psi if line.startswith('full '))
    avg = float(dict(item.split('=') for item in full.split()[1:])['avg10'])
    return Point(time.time(), time.monotonic(), (proc / 'sys/kernel/random/boot_id').read_text().strip(), counters,
                 os.cpu_count() or 1, memory['MemTotal'], memory['MemAvailable'], shutil.disk_usage(disk).free, avg)


def sample_between(before, after):
    elapsed = after.mono - before.mono
    wall_elapsed = after.wall - before.wall
    usage = cpu_percent(before, after)
    # 时钟跳变、重启、长间隔均是缺测，不能插值制造达标分钟。
    if usage is None or not 50 <= elapsed <= 90 or abs(elapsed - wall_elapsed) > 3:
        return None
    return {'start': after.wall - min(elapsed, 60), 'end': after.wall, **usage,
            'cores': after.cores, 'memory_available': after.memory_available}
