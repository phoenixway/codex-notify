#!/usr/bin/python3
"""Install saved v1.0 locally; never install packages or start services."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import tomllib
import uuid

RELEASE = Path(__file__).resolve().parent
DEFAULT_DEVICE = 'CONFIGURE_DEVICE_ID'


def snapshot(path):
    if path.is_symlink():
        return {'kind':'link', 'target':os.readlink(path)}
    if path.exists():
        return {'kind':'file', 'data':base64.b64encode(path.read_bytes()).decode(),
                'mode':path.stat().st_mode & 0o777}
    return {'kind':'absent'}


def fingerprint(path):
    data = snapshot(path)
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def set_key(text, section, key, value):
    lines = text.splitlines(keepends=True)
    start, end = 0, len(lines)
    if section:
        headers = [i for i, line in enumerate(lines) if re.match(r'^\s*\['+re.escape(section)+r'\]\s*(?:#.*)?$', line)]
        if not headers:
            return text.rstrip()+'\n\n['+section+']\n'+key+' = '+value+'\n'
        start = headers[0]+1
    for i in range(start, len(lines)):
        if lines[i].lstrip().startswith('['):
            end = i
            break
    matches = [i for i in range(start, end) if re.match(r'^\s*'+re.escape(key)+r'\s*=', lines[i])]
    if matches:
        lines[matches[0]] = key+' = '+value+'\n'
    else:
        lines.insert(start, key+' = '+value+'\n')
    return ''.join(lines)


def restore(backup):
    manifest = json.loads((backup/'manifest.json').read_text())
    home = Path(manifest['home'])
    for rel, entry in manifest['files'].items():
        if fingerprint(home/rel) != entry['installed_hash']:
            raise ValueError('Changed after install; refusing rollback: '+str(home/rel))
    for rel, entry in manifest['files'].items():
        path = home/rel
        path.unlink(missing_ok=True)
        old = entry['before']
        if old['kind'] == 'file':
            path.write_bytes(base64.b64decode(old['data']));path.chmod(old['mode'])
        elif old['kind'] == 'link':
            path.symlink_to(old['target'])
    print('Previous files restored. Run: systemctl --user daemon-reload')


def install(home, device, target, check_tools=True):
    if check_tools:
        missing = [tool for tool in ['hypridle','hyprctl','wezterm','kdeconnect-cli','notify-send','systemctl'] if not shutil.which(tool)]
        if missing:
            raise ValueError('Missing prerequisites (no packages installed): '+', '.join(missing))
    target_paths = [home/'.config/systemd/user'/target, Path('/usr/lib/systemd/user')/target,
                    Path('/etc/systemd/user')/target]
    if not any(p.is_file() for p in target_paths):
        raise ValueError('Session target not found; audit Hyprland startup first: '+target)
    handler = home/'.local/bin/codex-notify.py'
    config = home/'.codex/config.toml'
    text = config.read_text() if config.exists() else ''
    old = tomllib.loads(text)
    command = ['/usr/bin/python3', str(handler)]
    if 'notify' in old and old['notify'] != command:
        raise ValueError('An existing notify handler requires manual integration.')
    new = set_key(set_key(text, '', 'notify', json.dumps(command)), 'tui', 'notifications', 'false')
    expected = dict(old, notify=command, tui=dict(old.get('tui', {}), notifications=False))
    if tomllib.loads(new) != expected:
        raise ValueError('Unusual TOML formatting; refusing automatic edit before any writes.')
    script = (RELEASE/'codex-notify.py').read_text().replace("DEVICE = '"+DEFAULT_DEVICE+"'", 'DEVICE = '+repr(device))
    callback = '/usr/bin/python3 '+shlex.quote(str(handler))
    idle = '''# Codex activity only. No lock, DPMS, or suspend actions.
general {
    ignore_dbus_inhibit = true
    ignore_systemd_inhibit = true
    ignore_wayland_inhibit = true
    inhibit_sleep = 0
}
listener {
    timeout = 120
    ignore_inhibit = true
    on-timeout = CALLBACK --activity away
    on-resume = CALLBACK --activity active
}
'''.replace('CALLBACK', callback)
    idle_path = home/'.config/hypr/hypridle.conf'
    if idle_path.exists() and idle_path.read_text() != idle:
        raise ValueError('Existing hypridle.conf differs; refusing to change idle/power policy.')
    drop = '[Unit]\nPartOf='+target+'\n\n[Service]\nExecStart=\nExecStart=/usr/bin/python3 "%h/.local/bin/codex-notify.py" --watch-idle\nRestartSec=2\n'
    files = {'.local/bin/codex-notify.py':script, '.codex/config.toml':new,
             '.config/hypr/hypridle.conf':idle,
             '.config/systemd/user/hypridle.service.d/codex-notify.conf':drop}
    link_rel = '.config/systemd/user/'+target+'.wants/hypridle.service'
    unit = Path('/usr/lib/systemd/user/hypridle.service')
    if not unit.is_file():
        raise ValueError('Packaged hypridle.service not found at '+str(unit))
    link = home/link_rel
    if (link.exists() or link.is_symlink()) and not (link.is_symlink() and link.resolve() == unit):
        raise ValueError('Unexpected existing autostart entry: '+str(link))
    for rel in files:
        path = home/rel
        if path.is_symlink():
            raise ValueError('Refusing to overwrite a symlink: '+str(path))
    backup = home/'.local/share/codex-notify/install-backups'/uuid.uuid4().hex
    backup.mkdir(parents=True, mode=0o700)
    manifest = {'version':'1.0.0','home':str(home),'files':{
        rel:{'before':snapshot(home/rel)} for rel in [*files, link_rel]}}
    # Persist originals before the first edit, even if installation is interrupted.
    (backup/'manifest.json').write_text(json.dumps(manifest, indent=2))
    for rel, contents in files.items():
        path = home/rel;path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
        if rel == '.local/bin/codex-notify.py':path.chmod(0o755)
    link.parent.mkdir(parents=True, exist_ok=True)
    if not link.is_symlink():link.symlink_to(unit)
    for rel, entry in manifest['files'].items():entry['installed_hash'] = fingerprint(home/rel)
    (backup/'manifest.json').write_text(json.dumps(manifest, indent=2))
    print('Installed v1.0.0; no service started. Backup:', backup)
    print('After checking the session environment: systemctl --user daemon-reload')
    print('Then: systemctl --user start hypridle.service')
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path.home(), help='destination HOME; supports offline restoration')
    parser.add_argument('--device-id', help='Paired KDE Connect device ID (kdeconnect-cli --list-devices)')
    parser.add_argument('--target', default='hyprland-session.target')
    parser.add_argument('--rollback', type=Path, help='restore an installation backup; stop Hypridle first')
    args = parser.parse_args()
    if args.rollback:
        restore(args.rollback.resolve());return
    if not re.fullmatch(r'[A-Za-z0-9_.-]+\.target', args.target):
        parser.error('invalid session target')
    if not args.device_id or not re.fullmatch(r'[A-Za-z0-9_-]+', args.device_id):
        parser.error('invalid KDE Connect device ID')
    install(args.home.expanduser().resolve(), args.device_id, args.target)


if __name__ == '__main__':
    try:main()
    except (OSError, ValueError, KeyError) as error:
        sys.exit(str(error))
