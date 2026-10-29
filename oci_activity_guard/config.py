"""只接受明确、有界的配置，定时执行时不安装依赖。"""
from dataclasses import asdict, dataclass, field
import ipaddress
import json
import math
from pathlib import Path
import re
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Config:
    tick_seconds: int = 10
    sample_seconds: int = 60
    warmup_hours: int = 24
    cpu_threshold: float = 25
    worker_cpu_load: int = 30
    worker_host_cap: int = 35
    slot_seconds: int = 720
    daily_target_seconds: int = 8640
    daily_max_seconds: int = 10800
    cpu_stop_threshold: int = 60
    memory_available_ratio: float = .20
    memory_available_min_bytes: int = 2147483648
    disk_free_min_bytes: int = 12884901888
    memory_psi_full_limit: float = 1
    steal_limit: float = 10
    retention_days: int = 14
    critical_units: list = field(default_factory=lambda: ['caddy.service', 'sing-box.service', 'frps.service', 'proxy-quota.service'])
    health_urls: list = field(default_factory=lambda: ['http://127.0.0.1:18101/', 'http://127.0.0.1:18102/', 'http://127.0.0.1:18103/health'])
    expected_cores: int = 4
    expected_memory_gib: int = 24
    account_type: str = 'Free Tier'

    def validate(self):
        integer_bounds = {
            'tick_seconds': (5, 30), 'sample_seconds': (60, 60), 'warmup_hours': (24, 168),
            'worker_cpu_load': (25, 35), 'worker_host_cap': (30, 40), 'slot_seconds': (60, 720),
            'daily_target_seconds': (7200, 9000), 'daily_max_seconds': (9000, 10800),
            'cpu_stop_threshold': (50, 80), 'memory_available_min_bytes': (1073741824, 8589934592),
            'disk_free_min_bytes': (1073741824, 107374182400), 'retention_days': (7, 30),
            'expected_cores': (1, 64), 'expected_memory_gib': (1, 1024),
        }
        for key, (low, high) in integer_bounds.items():
            value = getattr(self, key)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f'Invalid configuration: {key}')
        for key, low, high in [('cpu_threshold', 20, 30), ('memory_available_ratio', .15, .5), ('memory_psi_full_limit', .1, 5), ('steal_limit', 5, 25)]:
            value = getattr(self, key)
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f'Invalid configuration: {key}')
        if not self.cpu_threshold < self.worker_cpu_load <= self.worker_host_cap < self.cpu_stop_threshold:
            raise ValueError('CPU thresholds must preserve working headroom')
        if self.daily_target_seconds >= self.daily_max_seconds:
            raise ValueError('Daily maximum must exceed target')
        if not isinstance(self.critical_units, list) or not 1 <= len(self.critical_units) <= 12:
            raise ValueError('Invalid critical unit list')
        if any(not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,80}\.service', name) for name in self.critical_units):
            raise ValueError('Invalid systemd unit name')
        if not isinstance(self.health_urls, list) or len(self.health_urls) > 4:
            raise ValueError('Invalid health URL list')
        for url in self.health_urls:
            if not isinstance(url, str) or len(url) > 300:
                raise ValueError('Invalid health URL')
            parsed = urlsplit(url)
            if parsed.scheme != 'http' or parsed.username or parsed.password or parsed.fragment:
                raise ValueError('Health probes must use plain loopback HTTP without credentials')
            try:
                if not ipaddress.ip_address(parsed.hostname).is_loopback or not 1 <= parsed.port <= 65535:
                    raise ValueError('Health probes must use numeric loopback addresses and a port')
            except (ValueError, TypeError) as error:
                raise ValueError('Invalid loopback health URL') from error
        if self.account_type not in ('Free Tier', 'Pay As You Go'):
            raise ValueError('Unsupported account record')
        return self

    @classmethod
    def load(cls, path):
        data = json.loads(Path(path).read_text())
        if not isinstance(data, dict) or set(data) - set(cls.__dataclass_fields__):
            raise ValueError('Unknown configuration fields')
        return cls(**data).validate()

    def as_dict(self):
        return asdict(self)
