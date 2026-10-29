"""本机管理命令；不开放 HTTP 管理端口。"""
import argparse
from datetime import datetime, timezone
import json
import logging
import os
import signal
import threading
import time

from .config import Config
from .runtime import cleanup, monitor, run, status, stop_worker
from .store import Store


def main():
    parser = argparse.ArgumentParser(description='Bounded OCI activity maintenance')
    parser.add_argument('--config', default='/etc/oci-activity-guard/config.json')
    parser.add_argument('--state-dir', default='/var/lib/oci-activity-guard')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ['monitor', 'status', 'pause', 'resume', 'cleanup']:
        commands.add_parser(name)
    commands.add_parser('run').add_argument('--compensation', action='store_true')
    commands.add_parser('calibrate').add_argument('--seconds', type=int, default=60)
    arguments = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run with sudo; configuration and status are private to the owner')
    os.umask(0o027)
    config = Config.load(arguments.config)
    if arguments.command == 'calibrate' and not 60 <= arguments.seconds <= config.slot_seconds:
        parser.error('Calibration must be 60..slot_seconds')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    stop = threading.Event()
    for name in [signal.SIGTERM, signal.SIGINT]:
        signal.signal(name, lambda *_: stop.set())
    if arguments.command == 'monitor':
        return monitor(config, arguments.state_dir, stop)
    if arguments.command == 'cleanup':
        return cleanup(arguments.state_dir)
    if arguments.command in ('run', 'calibrate'):
        compensation = getattr(arguments, 'compensation', False)
        if arguments.command == 'run' and not compensation:
            compensation = datetime.now(timezone.utc).hour % 2 == 1
        return run(config, arguments.state_dir, stop,
                   calibration=arguments.seconds if arguments.command == 'calibrate' else 0,
                   compensation=compensation)
    store = Store(arguments.state_dir)
    try:
        store.initialize(time.time())
        if arguments.command in ('pause', 'resume'):
            store.set('paused', arguments.command == 'pause')
            if arguments.command == 'pause':
                stop_worker()
            store.event(time.time(), 'owner-control', arguments.command)
        print(json.dumps(status(config, store, time.time()), ensure_ascii=False, indent=2))
    finally:
        store.close()
    return 0
