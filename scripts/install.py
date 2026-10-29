#!/usr/bin/env python3
"""安装固定 Git 版本；保留运行配置、暂停状态、采样和预算。"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import time


def command(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--install-dependencies', action='store_true')
    parser.add_argument('--stress-version')
    arguments = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run installer with sudo')
    source = Path(__file__).resolve().parents[1]
    revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain', '--untracked-files=no'], text=True).strip():
        raise RuntimeError('Install only a committed clean source revision')
    if not shutil.which('stress-ng'):
        if not arguments.install_dependencies or not arguments.stress_version:
            raise RuntimeError('Install stress-ng first or pass --install-dependencies --stress-version VERSION')
        command('apt-get', 'install', '-y', '--no-install-recommends', 'stress-ng='+arguments.stress_version,
                env={**os.environ, 'DEBIAN_FRONTEND': 'noninteractive', 'NEEDRESTART_MODE': 'l'})
    installed = subprocess.check_output(['dpkg-query', '-W', '-f=${Version}', 'stress-ng'], text=True).strip()
    if arguments.stress_version and installed != arguments.stress_version:
        raise RuntimeError('Installed stress-ng version does not match requested version')
    root = Path('/opt/oci-activity-guard')
    release = root/'releases'/revision
    if not release.exists():
        release.mkdir(parents=True)
        archive = subprocess.check_output(['git', '-C', str(source), 'archive', revision])
        with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
            stream.extractall(release, filter='data')
    config_directory = Path('/etc/oci-activity-guard')
    config_directory.mkdir(mode=0o755, exist_ok=True)
    config = config_directory/'config.json'
    if not config.exists():
        shutil.copyfile(release/'config.example.json', config)
        config.chmod(0o644)
    import sys
    sys.path.insert(0, str(release))
    from oci_activity_guard.config import Config
    Config.load(config)
    state = Path('/var/lib/oci-activity-guard')
    state.mkdir(mode=0o750, exist_ok=True)
    previous = (root/'current').resolve() if (root/'current').exists() else None
    # 只停止本项目服务，避免升级期间新旧控制器同时运行。
    subprocess.run(['systemctl', 'stop', 'oci-activity-guard.timer', 'oci-activity-guard.service', 'oci-activity-monitor.service'], capture_output=True)
    link = root/'current.new'
    link.unlink(missing_ok=True)
    link.symlink_to(Path('releases')/revision, target_is_directory=True)
    link.replace(root/'current')
    try:
        units = list((release/'systemd').glob('*.service'))+list((release/'systemd').glob('*.timer'))
        command('systemd-analyze', 'verify', *map(str, units))
        for unit in units:
            shutil.copyfile(unit, Path('/etc/systemd/system')/unit.name)
        shutil.copyfile(release/'bin/oci-activity-guard', '/usr/local/bin/oci-activity-guard')
        Path('/usr/local/bin/oci-activity-guard').chmod(0o755)
        command('systemctl', 'daemon-reload')
        command('systemctl', 'enable', '--now', 'oci-activity-monitor.service', 'oci-activity-guard.timer')
    except BaseException:
        if previous:
            link.symlink_to(previous, target_is_directory=True)
            link.replace(root/'current')
        raise
    manifest = {'revision': revision, 'previous_release': str(previous) if previous else None,
                'installed_at': time.time(), 'stress_ng_version': installed,
                'config_sha256': hashlib.sha256(config.read_bytes()).hexdigest()}
    manifest_path = state/'deployment.json'
    manifest_path.write_text(json.dumps(manifest, indent=2)+'\n')
    manifest_path.chmod(0o640)
    print('INSTALL_COMPLETE', revision, 'stress-ng='+installed)


if __name__ == '__main__':
    main()
