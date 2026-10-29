from dataclasses import replace
import fcntl
import http.server
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from oci_activity_guard.config import Config
from oci_activity_guard.metrics import Point, cpu_percent, sample_between
from oci_activity_guard.policy import decide, resource_properties, unsafe_reason
from oci_activity_guard.runtime import cleanup, health, run, status, worker_command
from oci_activity_guard.store import Store


NOW = 1_800_000_000


def live(**changes):
    return {'at': NOW, 'cpu': 1, 'steal': 0, 'cores': 4, 'memory_total': 24*1024**3,
            'memory_available': 20*1024**3, 'disk_free': 60*1024**3, 'psi_full': 0,
            'healthy': True, 'unhealthy_streak': 0, 'hot_streak': 0, **changes}


def sample(start, end, cpu):
    return {'start': start, 'end': end, 'cpu': cpu, 'steal': 0, 'cores': 4, 'memory_available': 20*1024**3}


class ConfigurationTests(unittest.TestCase):
    def test_defaults_are_bounded(self):
        self.assertIs(Config().validate().__class__, Config)

    def test_rejects_unbounded_and_nonfinite_values(self):
        for changes in [{'worker_host_cap': 100}, {'worker_cpu_load': 100}, {'slot_seconds': 86400},
                        {'daily_max_seconds': 86400}, {'warmup_hours': 0}, {'cpu_threshold': float('nan')},
                        {'cpu_threshold': True}, {'worker_cpu_load': 25}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(Config(), **changes).validate()

    def test_rejects_metadata_dns_credentials_and_nonlocal_probes(self):
        for url in ['http://169.254.169.254:80/', 'http://localhost:80/', 'https://127.0.0.1:443/',
                    'http://user:pass@127.0.0.1:80/', 'http://8.8.8.8:80/', 'http://127.0.0.1/']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                replace(Config(), health_urls=[url]).validate()

    def test_accepts_ipv4_and_ipv6_loopback(self):
        replace(Config(), health_urls=['http://127.0.0.1:8080/', 'http://[::1]:8080/health']).validate()

    def test_unknown_fields_and_unit_injection_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'config.json'
            path.write_text('{"command":"rm"}')
            with self.assertRaises(ValueError): Config.load(path)
        with self.assertRaises(ValueError): replace(Config(), critical_units=['a.service; reboot']).validate()


class MetricsTests(unittest.TestCase):
    def point(self, seconds, counters, **changes):
        return Point(wall=NOW+seconds, mono=1000+seconds, boot='boot1', counters=counters, cores=changes.get('cores', 4),
                     memory_total=24*1024**3, memory_available=20*1024**3, disk_free=50*1024**3, psi_full=0)

    def test_cpu_is_whole_host_and_excludes_io_wait_and_steal(self):
        before = self.point(0, (0,)*8)
        after = self.point(60, (20, 0, 10, 50, 10, 0, 0, 10))
        self.assertEqual(cpu_percent(before, after), {'cpu': 30, 'steal': 10})

    def test_boot_change_and_counter_reset_are_not_samples(self):
        before = self.point(0, (100,)*8)
        after = self.point(60, (200,)*8)
        self.assertIsNone(cpu_percent(before, replace(after, boot='boot2')))
        self.assertIsNone(cpu_percent(before, self.point(60, (0,)*8)))
        self.assertIsNone(cpu_percent(before, replace(after, cores=2)))

    def test_long_gaps_and_wall_clock_jumps_do_not_fabricate_minutes(self):
        before = self.point(0, (0,)*8)
        self.assertIsNone(sample_between(before, self.point(600, (100,)*8)))
        self.assertIsNone(sample_between(before, replace(self.point(60, (100,)*8), wall=NOW+120)))

    def test_normal_interval_has_at_most_one_minute_credit(self):
        result = sample_between(self.point(0, (0,)*8), self.point(65, (100,)*8))
        self.assertEqual(result['end']-result['start'], 60)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(self.directory.name)
        self.store.initialize(NOW-86400)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_missing_samples_do_not_inflate_active_ratio(self):
        self.store.add_sample(sample(NOW-60, NOW, 30))
        day = self.store.summary(NOW, 86400, 25)
        self.assertEqual(day['eligible_seconds'], 60)
        self.assertLess(day['eligible_ratio'], .001)
        self.assertEqual(day['observed_p95'], 30)

    def test_window_clips_intervals_at_both_boundaries(self):
        self.store.add_sample(sample(NOW-100, NOW-40, 30))
        self.store.add_sample(sample(NOW-40, NOW+20, 10))
        result = self.store.summary(NOW, 60, 25)
        self.assertEqual(result['observed_seconds'], 60)
        self.assertEqual(result['eligible_seconds'], 20)

    def test_duplicates_and_overlap_are_rejected(self):
        self.store.add_sample(sample(NOW-60, NOW, 30))
        for record in [sample(NOW-60, NOW, 30), sample(NOW-30, NOW+30, 30)]:
            with self.assertRaises(ValueError): self.store.add_sample(record)

    def test_budget_and_pause_survive_reopening_and_initialization(self):
        self.store.set('paused', True)
        run_id = self.store.run_start(NOW-720, 720, False)
        self.store.finish(run_id, NOW, 'completed', 'worker-exited-zero')
        self.store.close()
        self.store = Store(self.directory.name)
        self.store.initialize(NOW)
        self.assertTrue(self.store.get('paused'))
        self.assertEqual(self.store.get('initialized_at'), NOW-86400)
        self.assertEqual(self.store.budget_used(NOW), 720)

    def test_crash_budget_is_conservative_and_not_effective_cpu(self):
        self.store.run_start(NOW-1000, 720, False)
        self.store.reconcile(NOW)
        self.assertEqual(self.store.budget_used(NOW), 735)
        self.assertEqual(self.store.summary(NOW, 86400, 25)['eligible_seconds'], 0)
        self.assertEqual(self.store.recent_runs()[0]['result'], 'interrupted')

    def test_budget_is_rolling_not_reset_at_midnight(self):
        run_id = self.store.run_start(NOW-86500, 720, False)
        self.store.finish(run_id, NOW-85780, 'completed', 'exit')
        self.assertEqual(self.store.budget_used(NOW), 620)

    def test_compensation_has_separate_accounting(self):
        for index, kind in enumerate(['scheduled', 'compensation', 'calibration']):
            run_id = self.store.run_start(NOW-(index+1)*1000, 720, kind=='calibration', kind)
            self.store.finish(run_id, NOW-(index+1)*1000+720, 'completed', 'exit')
        self.assertEqual(self.store.budget_used(NOW), 2160)
        self.assertEqual(self.store.budget_used(NOW, 'compensation'), 720)

    def test_retention_preserves_current_run_and_removes_old_samples(self):
        self.store.add_sample(sample(NOW-20*86400-60, NOW-20*86400, 30))
        self.store.add_sample(sample(NOW-60, NOW, 30))
        self.store.run_start(NOW-10, 720, False)
        self.store.cleanup(NOW, 14)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM samples').fetchone()[0], 1)
        self.assertEqual(len(self.store.recent_runs()), 1)


class PolicyTests(unittest.TestCase):
    def decision(self, **changes):
        args = {'config': Config(), 'now': NOW, 'initialized_at': NOW-86400, 'paused': False, 'live': live(),
                'day': {'eligible_seconds': 0, 'coverage_ratio': 1}, 'week': {'eligible_ratio': 0, 'coverage_ratio': 1}, 'budget_used': 0}
        args.update(changes)
        return decide(**args)

    def test_default_slot_and_four_to_two_core_scaling(self):
        self.assertEqual(self.decision().seconds, 720)
        self.assertEqual(resource_properties(Config(), 4)['CPUQuota'], '140%')
        self.assertEqual(resource_properties(Config(), 2)['CPUQuota'], '70%')

    def test_warmup_and_owner_pause(self):
        self.assertEqual(self.decision(initialized_at=NOW-1).reason, 'initial-observation')
        self.assertEqual(self.decision(paused=True).reason, 'owner-paused')

    def test_natural_cpu_and_daily_target_skip_background_work(self):
        self.assertFalse(self.decision(live=live(cpu=30)).run)
        self.assertEqual(self.decision(day={'eligible_seconds': 8640}).reason, 'daily-target-met')

    def test_stale_monitor_and_unhealthy_business_block_start(self):
        self.assertEqual(self.decision(live=live(at=NOW-100)).reason, 'monitor-stale')
        self.assertEqual(self.decision(live=live(healthy=False)).reason, 'business-unhealthy')

    def test_memory_pressure_steal_and_disk_protection(self):
        for changes, reason in [({'memory_available': 4*1024**3}, 'memory-headroom-low'),
                                ({'psi_full': 2}, 'memory-pressure'), ({'steal': 15}, 'cpu-steal-high'),
                                ({'disk_free': 1024**3}, 'disk-headroom-low')]:
            self.assertEqual(self.decision(live=live(**changes)).reason, reason)

    def test_remaining_daily_budget_limits_run_and_calibration(self):
        self.assertEqual(self.decision(budget_used=10670).seconds, 100)
        self.assertFalse(self.decision(budget_used=10750).run)
        self.assertFalse(self.decision(budget_used=10700, calibration=720).run)

    def test_calibration_bypasses_warmup_but_not_safety_or_pause(self):
        self.assertTrue(self.decision(initialized_at=NOW, calibration=60).run)
        self.assertFalse(self.decision(initialized_at=NOW, calibration=60, paused=True).run)
        self.assertFalse(self.decision(calibration=60, live=live(memory_available=1)).run)
        with self.assertRaises(ValueError): self.decision(calibration=999999)

    def test_compensation_requires_real_history_and_is_capped(self):
        self.assertFalse(self.decision(compensation=True, day={'eligible_seconds': 0, 'coverage_ratio': .1}, week={'eligible_ratio': 0, 'coverage_ratio': .1}).run)
        self.assertFalse(self.decision(compensation=True, day={'eligible_seconds': 8640, 'coverage_ratio': 1}, week={'eligible_ratio': .1, 'coverage_ratio': 1}).run)
        self.assertFalse(self.decision(compensation=True, compensation_used=2160).run)
        self.assertEqual(self.decision(compensation=True, compensation_used=2000).seconds, 160)

    def test_compensation_repairs_sample_boundary_loss_even_with_healthy_week(self):
        self.assertTrue(self.decision(compensation=True, day={'eligible_seconds': 7920, 'coverage_ratio': 1}, week={'eligible_ratio': .1, 'coverage_ratio': 1}).run)
        self.assertTrue(self.decision(compensation=True, day={'eligible_seconds': 7920, 'coverage_ratio': 1}, week={'eligible_ratio': 0, 'coverage_ratio': .2}).run)

    def test_worker_is_unprivileged_memory_bounded_and_network_isolated(self):
        command = worker_command(Config(), 4, 60)
        for value in ['--property=DynamicUser=yes', '--property=PrivateNetwork=yes', '--property=MemoryMax=256M',
                      '--property=MemorySwapMax=0', '--property=CPUQuota=140%', '--property=KillMode=control-group',
                      '--property=RuntimeMaxSec=70s']:
            self.assertIn(value, command)
        self.assertEqual(command[-3:], ['--timeout', '60s', '--metrics-brief'])


class RuntimeBoundaryTests(unittest.TestCase):
    def test_cleanup_does_not_kill_another_controller_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            with (Path(directory)/'controller.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                with patch('oci_activity_guard.runtime.stop_worker') as stop:
                    cleanup(directory)
                    stop.assert_not_called()

    def test_active_worker_is_stopped_on_new_memory_pressure(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            store.initialize(NOW-86400)
            store.set('live', live())
            class Process:
                returncode = None
                def poll(self): return self.returncode
                def wait(self, timeout=None): self.returncode = -15; return self.returncode
                def kill(self): self.returncode = -9
            process = Process()
            def spawn(command):
                store.set('live', live(memory_available=1))
                return process
            try:
                with patch('oci_activity_guard.runtime.time.time', return_value=NOW), \
                     patch('oci_activity_guard.runtime.worker_active', return_value=False), \
                     patch('oci_activity_guard.runtime.health', return_value=[]), \
                     patch('oci_activity_guard.runtime.read_point') as point, \
                     patch('oci_activity_guard.runtime.subprocess.Popen', side_effect=spawn), \
                     patch('oci_activity_guard.runtime.stop_worker') as stop:
                    point.return_value.cores = 4
                    self.assertEqual(run(Config(), directory, threading.Event(), calibration=60), 0)
                    stop.assert_called_once()
                self.assertEqual(store.recent_runs()[0]['result'], 'interrupted')
                self.assertEqual(store.recent_runs()[0]['reason'], 'memory-headroom-low')
            finally: store.close()

    def test_second_controller_does_not_touch_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            with (Path(directory)/'controller.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                with patch('oci_activity_guard.runtime.worker_active') as active:
                    self.assertEqual(run(Config(), directory, threading.Event()), 0)
                    active.assert_not_called()

    def test_status_never_claims_cloud_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                store.initialize(NOW)
                store.set('live', live())
                result = status(Config(), store, NOW)
                self.assertFalse(result['cloud_metrics_verified'])
                self.assertEqual(result['mode'], 'observing')
                self.assertEqual(result['week']['coverage_ratio'], 0)
            finally: store.close()

    def test_loopback_redirect_is_not_followed_to_metadata(self):
        seen = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                self.send_response(302)
                self.send_header('Location', 'http://169.254.169.254/opc/v2/instance/')
                self.end_headers()
            def log_message(self, *args): pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = replace(Config(), critical_units=['caddy.service'], health_urls=[f'http://127.0.0.1:{server.server_port}/'])
            with patch('oci_activity_guard.runtime.subprocess.run') as command:
                command.return_value.stdout = 'active\n'
                self.assertEqual(health(config), ['http-probe-0-failed'])
            self.assertEqual(seen, ['/'])
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
