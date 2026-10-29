"""systemd 控制和业务探测；固定程序参数，不开放网络控制接口。"""
from dataclasses import asdict
from datetime import datetime, timezone
import fcntl
import json
import logging
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request

from .metrics import cpu_percent, read_point, sample_between
from .policy import decide, resource_properties, unsafe_reason
from .store import Store

WORKER = 'oci-activity-worker.service'
LOG = logging.getLogger('oci-activity-guard')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def health(config):
    failures = []
    try:
        result = subprocess.run(['/usr/bin/systemctl', 'is-active', *config.critical_units], capture_output=True, text=True, timeout=4)
        states = result.stdout.splitlines()
        if len(states) != len(config.critical_units) or any(value != 'active' for value in states):
            failures.append('critical-unit-inactive')
    except (OSError, subprocess.TimeoutExpired):
        failures.append('critical-unit-check-failed')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    for index, url in enumerate(config.health_urls):
        try:
            with opener.open(url, timeout=2) as response:
                if response.status != 200:
                    failures.append(f'http-probe-{index}-status')
        except urllib.error.HTTPError as error:
            error.close()
            failures.append(f'http-probe-{index}-failed')
        except (OSError, urllib.error.URLError, ValueError):
            failures.append(f'http-probe-{index}-failed')
    return failures


def systemctl(*args, check=False):
    return subprocess.run(['/usr/bin/systemctl', *args], capture_output=True, text=True, timeout=20, check=check)


def worker_active():
    return systemctl('is-active', '--quiet', WORKER).returncode == 0


def stop_worker():
    systemctl('stop', WORKER)


def worker_command(config, cores, seconds):
    if type(seconds) is not int or not 60 <= seconds <= config.slot_seconds:
        raise ValueError('Unsafe worker duration')
    properties = resource_properties(config, cores)
    # stress-ng 自行到期后仍需回收子进程；独立保险留十秒，日预算已预留收尾。
    properties['RuntimeMaxSec'] = f'{seconds+10}s'
    command = ['/usr/bin/systemd-run', '--quiet', '--wait', '--collect', '--unit='+WORKER, '--service-type=exec']
    for key, value in properties.items():
        command.append(f'--property={key}={value}')
    return command + ['/usr/bin/stress-ng', '--cpu', str(cores), '--cpu-load', str(config.worker_cpu_load),
                      '--timeout', f'{seconds}s', '--metrics-brief']


def write_status(directory, data):
    target = Path(directory) / 'status.json'
    temporary = target.with_suffix('.new')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')
    temporary.chmod(0o640)
    temporary.replace(target)


def status(config, store, now):
    initialized = store.get('initialized_at', now)
    day = store.summary(now, 86400, config.cpu_threshold)
    week = store.summary(now, 604800, config.cpu_threshold)
    live = store.get('live')
    alerts = []
    age = now-initialized
    if live is None or now-live.get('at', 0) > 90:
        alerts.append('monitor-stale')
    elif not live['healthy']:
        alerts.append('business-unhealthy')
    if live and live['cores'] != config.expected_cores:
        alerts.append('shape-cores-changed')
    expected_memory = config.expected_memory_gib*1024**3
    if live and abs(live['memory_total']-expected_memory) > expected_memory*.1:
        alerts.append('shape-memory-changed')
    if age >= 86400 and day['eligible_seconds'] < 7200:
        alerts.append('daily-effective-time-low')
    if age >= 604800 and (week['coverage_ratio'] < .95 or week['eligible_ratio'] < .08 or (week['observed_p95'] or 0) < 25):
        alerts.append('weekly-local-target-unmet')
    recent = store.recent_runs()
    if len(recent) >= 3 and all(row['result'] == 'failed' for row in recent[:3]):
        alerts.append('worker-repeated-failure')
    return {'version': '1.0.0', 'at': now, 'at_utc': datetime.fromtimestamp(now, timezone.utc).isoformat(),
            'mode': 'paused' if store.get('paused', False) else 'observing' if age < config.warmup_hours*3600 else 'armed',
            'observe_until_utc': datetime.fromtimestamp(initialized+config.warmup_hours*3600, timezone.utc).isoformat(),
            'live': live, 'day': day, 'week': week, 'worker_seconds_24h': round(store.budget_used(now), 1),
            'compensation_seconds_24h': round(store.budget_used(now, 'compensation'), 1),
            'cloud_metrics_verified': False, 'cloud_note': 'Local /proc samples; compare with OCI Monitoring separately.',
            'last_decision': store.get('last_decision'), 'recent_runs': recent, 'alerts': alerts}


def monitor(config, directory, stop):
    store = Store(directory)
    store.initialize(time.time())
    previous = baseline = read_point()
    last_alerts = None
    last_cleanup = 0
    unhealthy = hot = 0
    try:
        while not stop.is_set():
            started = time.monotonic()
            point = read_point()
            usage = cpu_percent(previous, point)
            failures = health(config)
            unhealthy = unhealthy+1 if failures else 0
            hot = hot+1 if usage and usage['cpu'] >= config.cpu_stop_threshold else 0
            live = {'at': point.wall, 'cpu': round(usage['cpu'], 3) if usage else None,
                    'steal': round(usage['steal'], 3) if usage else 0, 'cores': point.cores,
                    'memory_total': point.memory_total, 'memory_available': point.memory_available,
                    'disk_free': point.disk_free, 'psi_full': point.psi_full,
                    'healthy': not failures, 'health_failures': failures, 'unhealthy_streak': unhealthy, 'hot_streak': hot}
            store.set('live', live)
            reason = unsafe_reason(config, live)
            if store.get('paused', False):
                reason = 'owner-paused'
            elif unhealthy >= 3:
                reason = 'business-unhealthy'
            elif hot >= 3:
                reason = 'business-cpu-busy'
            if reason and worker_active():
                store.set('last_emergency', {'at': point.wall, 'reason': reason})
                LOG.warning('Worker emergency stop: %s', reason)
                stop_worker()
            elapsed = point.mono-baseline.mono
            if elapsed >= config.sample_seconds:
                sample = sample_between(baseline, point)
                if sample:
                    store.add_sample(sample)
                else:
                    store.event(point.wall, 'sample-gap', 'clock-boot-or-collection-gap')
                baseline = point
            previous = point
            snapshot = status(config, store, point.wall)
            write_status(directory, snapshot)
            if snapshot['alerts'] != last_alerts:
                LOG.info('Monitor alerts: %s', ','.join(snapshot['alerts']) or 'none')
                last_alerts = snapshot['alerts']
            if point.wall-last_cleanup >= 3600:
                store.cleanup(point.wall, config.retention_days)
                last_cleanup = point.wall
            stop.wait(max(.1, config.tick_seconds-(time.monotonic()-started)))
    finally:
        stop_worker()
        store.close()


def run(config, directory, stop, calibration=0, compensation=False):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    lock = (directory/'controller.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        LOG.info('Worker skipped: controller-already-running')
        return 0
    store = Store(directory)
    process = None
    run_id = None
    result, reason = 'failed', 'controller-error'
    try:
        now = time.time()
        store.initialize(now)
        # 持锁后才清理遗留 worker，并保守恢复未结算的执行预算。
        if worker_active():
            stop_worker()
        store.reconcile(now)
        snapshot = status(config, store, now)
        decision = decide(config, now, store.get('initialized_at'), store.get('paused'), store.get('live'),
                          snapshot['day'], snapshot['week'], store.budget_used(now), calibration, compensation,
                          store.budget_used(now, 'compensation'))
        store.set('last_decision', {'at': now, **asdict(decision)})
        store.event(now, 'decision', decision.reason)
        LOG.info('Worker decision: %s duration=%s', decision.reason, decision.seconds)
        if not decision.run:
            return 0
        if health(config):
            store.set('last_decision', {'at': time.time(), 'run': False, 'seconds': 0, 'reason': 'business-changed-before-start'})
            LOG.warning('Worker skipped: business-changed-before-start')
            return 0
        cores = read_point().cores
        run_id = store.run_start(now, decision.seconds, bool(calibration), decision.reason)
        command = worker_command(config, cores, decision.seconds)
        LOG.info('Worker starting: cores=%s target_host_cpu=%s cap=%s', cores, config.worker_cpu_load, resource_properties(config, cores)['CPUQuota'])
        process = subprocess.Popen(command)
        deadline = time.monotonic()+decision.seconds+15
        while process.poll() is None and not stop.is_set():
            now = time.time()
            store.heartbeat(run_id, now)
            live = store.get('live')
            if live is None or now-live['at'] > 90:
                reason = 'monitor-stale'
                break
            if store.get('paused'):
                reason = 'owner-paused'
                break
            unsafe = unsafe_reason(config, live)
            if unsafe:
                reason = unsafe
                break
            if live['cores'] != cores:
                reason = 'online-cores-changed'
                break
            if live['unhealthy_streak'] >= 3 or live['hot_streak'] >= 3:
                reason = 'business-protection'
                break
            if time.monotonic() >= deadline:
                reason = 'worker-wallclock-limit'
                break
            stop.wait(config.tick_seconds)
        else:
            if stop.is_set():
                result, reason = 'interrupted', 'controller-stopped'
            elif process.returncode == 0:
                result, reason = 'completed', 'worker-exited-zero'
            else:
                result, reason = 'failed', 'worker-exited-nonzero'
                emergency = store.get('last_emergency', {})
                if store.get('paused'):
                    result, reason = 'interrupted', 'owner-paused'
                elif emergency.get('at', 0) >= now:
                    result, reason = 'interrupted', emergency['reason']
        if process.poll() is None:
            result = 'interrupted'
            stop_worker()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        store.finish(run_id, time.time(), result, reason)
        LOG.info('Worker finished: result=%s reason=%s', result, reason)
        return 1 if result == 'failed' else 0
    finally:
        if run_id:
            if process is None or process.poll() is None:
                stop_worker()
                if process is not None:
                    process.kill()
                    process.wait(timeout=5)
            store.finish(run_id, time.time(), result, reason)
        store.close()
        lock.close()


def cleanup(directory):
    """仅清理无控制器持锁的孤儿任务，不能打断另一个合法校准任务。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory/'controller.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        stop_worker()
        store = Store(directory)
        try:
            store.reconcile(time.time())
        finally:
            store.close()
