#!/usr/bin/python3
"""Local Codex completion router; no response/prompt contents are logged or sent."""
import argparse
import contextlib
import fcntl
import hashlib
import html
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid

DEVICE = 'CONFIGURE_DEVICE_ID'
VERSION = '1.0.0'
CLASSES = {'org.wezfurlong.wezterm', 'wezterm'}
LOG = logging.getLogger('codex-notify')


def setup_log():
    directory = Path(os.environ.get('XDG_STATE_HOME', str(Path.home()/'.local/state')))/'codex-notify'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = RotatingFileHandler(directory/'diagnostic.jsonl', maxBytes=524288, backupCount=2)
    handler.setFormatter(logging.Formatter('%(message)s'))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)


def log(event, **fields):
    LOG.info(json.dumps(dict(time=time.time(), event=event, **fields), ensure_ascii=False))


def run(args, timeout=1.5):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                                stdin=subprocess.DEVNULL)
        if result.returncode:
            log('command_failed', tool=args[0], code=result.returncode)
            return None
        return result.stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        log('command_failed', tool=args[0], error=type(exc).__name__)
        return None


def query(args):
    result = run(args)
    try:
        return json.loads(result) if result is not None else None
    except (ValueError, TypeError):
        log('invalid_tool_json', tool=args[0])
        return None


def runtime():
    base = os.environ.get('XDG_RUNTIME_DIR')
    if not base:
        raise RuntimeError('XDG_RUNTIME_DIR is missing')
    directory = Path(base)/'codex-notify'
    directory.mkdir(mode=0o700, exist_ok=True)
    return directory


def read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def write_json(path, data):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.tmp-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextlib.contextmanager
def locked(path):
    with path.open('a') as stream:
        deadline = time.monotonic()+1
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise RuntimeError('state lock timeout')
                time.sleep(.01)
        yield


def identity(pid):
    # Linux starttime guards against PID reuse, boot_id against reboot.
    fields = Path(f'/proc/{int(pid)}/stat').read_text().rsplit(')', 1)[1].split()
    if fields[0] == 'Z':
        raise ValueError('dead process')
    return [Path('/proc/sys/kernel/random/boot_id').read_text().strip(), fields[19]]


def session():
    return [os.environ.get('HYPRLAND_INSTANCE_SIGNATURE'), os.environ.get('WAYLAND_DISPLAY')]


def valid_owner(state):
    try:
        return (isinstance(state, dict) and all(session()) and state['session'] == session()
                and state['identity'] == identity(state['pid'])
                and state['child_identity'] == identity(state['child_pid']))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def is_away(directory):
    state = read_json(directory/'activity.json')
    return bool(valid_owner(state) and state.get('away') is True)


def activity(value):
    directory = runtime()
    with locked(directory/'activity.lock'):
        state = read_json(directory/'activity.json')
        if not valid_owner(state) or state.get('token') != os.environ.get('CODEX_IDLE_TOKEN'):
            log('ignored_stale_callback')
            return
        state.update(away=value == 'away', changed_monotonic=time.monotonic())
        write_json(directory/'activity.json', state)
    log('activity', away=state['away'])


def watch_idle():
    directory = runtime()
    # One supervisor per session; kernel releases this lock even after SIGKILL.
    with locked(directory/'watch.lock'):
        token = uuid.uuid4().hex
        env = dict(os.environ, CODEX_IDLE_TOKEN=token)
        child = subprocess.Popen(['/usr/bin/hypridle', '-c', str(Path.home()/'.config/hypr/hypridle.conf')], env=env)
        state = dict(pid=os.getpid(), identity=identity(os.getpid()), child_pid=child.pid,
                     child_identity=identity(child.pid), session=session(), token=token, away=False)
        write_json(directory/'activity.json', state)
        def stop(_signum, _frame):
            child.terminate()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        log('watch_started')
        try:
            return child.wait()
        finally:
            if child.poll() is None:
                child.terminate()
            with locked(directory/'activity.lock'):
                current = read_json(directory/'activity.json', {})
                if current.get('token') == token:
                    (directory/'activity.json').unlink(missing_ok=True)
            log('watch_stopped')


def focus():
    pane = os.environ.get('WEZTERM_PANE')
    if pane is None or not pane.isdecimal():
        return 'unknown'
    active = query(['hyprctl', 'activewindow', '-j'])
    if not isinstance(active, dict) or not active.get('address') or not active.get('class'):
        return 'unknown'
    if active['class'].lower() not in CLASSES:
        return 'unfocused'
    clients = query(['wezterm', 'cli', '--no-auto-start', 'list-clients', '--format', 'json'])
    panes = query(['wezterm', 'cli', '--no-auto-start', 'list', '--format', 'json'])
    windows = query(['hyprctl', 'clients', '-j'])
    if not all(isinstance(v, list) and all(isinstance(x, dict) for x in v) for v in [clients, panes, windows]):
        return 'unknown'
    local = [c for c in clients if c.get('pid') == active.get('pid')
             and c.get('hostname') == socket.gethostname()]
    if len(local) != 1 or local[0].get('focused_pane_id') is None:
        return 'unknown'
    if str(local[0]['focused_pane_id']) != pane:
        return 'unfocused'
    target = [p for p in panes if str(p.get('pane_id')) == pane]
    if len(target) != 1 or target[0].get('is_active') is not True:
        return 'unknown'
    target = target[0]
    related = [w for w in windows if w.get('pid') == active.get('pid')
               and w.get('class', '').lower() in CLASSES and w.get('mapped') is True]
    ids = {p.get('window_id') for p in panes}
    if None in ids or not any(w.get('address') == active['address'] for w in related):
        return 'unknown'
    if len(related) == 1 and len(ids) == 1:
        matched = True
    else:
        # Titles are only usable if unique on BOTH sides. Never infer from PID alone.
        title = active.get('title')
        title_ids = {p.get('window_id') for p in panes if p.get('window_title') == title}
        matched = (bool(title) and title_ids == {target.get('window_id')}
                   and sum(w.get('title') == title for w in related) == 1)
    if not matched:
        return 'unknown'
    again = query(['hyprctl', 'activewindow', '-j'])
    if not isinstance(again, dict) or any(again.get(k) != active.get(k) for k in ['address', 'pid', 'title']):
        return 'unknown'
    return 'focused'


def event_key(event):
    thread, turn = event.get('thread-id'), event.get('turn-id')
    if isinstance(thread, str) and thread and isinstance(turn, str) and turn:
        return hashlib.sha256(json.dumps([thread, turn]).encode()).hexdigest(), 86400
    # Older payloads lacking turn-id: short best-effort replay window only.
    return hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest(), 2


def route(event, dry_run=False):
    if not isinstance(event, dict):
        raise ValueError('event must be an object')
    if event.get('type') != 'agent-turn-complete':
        return 'ignored'
    directory = runtime()
    if (directory/'disabled').exists():
        return 'disabled'
    key, ttl = event_key(event)
    if not dry_run:
        with locked(directory/'dedupe.lock'):
            now = time.time()
            seen = read_json(directory/'seen.json', {})
            if not isinstance(seen, dict):
                seen = {}
            seen = {k: v for k, v in seen.items() if isinstance(v, (int, float)) and v > now}
            if key in seen:
                log('duplicate', event_id=key[:12])
                return 'duplicate'
            seen[key] = now+ttl
            # Bound runtime storage, keeping the newest expirations.
            write_json(directory/'seen.json', dict(sorted(seen.items(), key=lambda x: x[1])[-4096:]))
    decision = 'away' if is_away(directory) else focus()
    if dry_run:
        print(json.dumps(dict(decision=decision, dry_run=True)))
        return decision
    cwd = event.get('cwd')
    project = Path(cwd).name if isinstance(cwd, str) and cwd else 'проєкт'
    project = ''.join(c for c in project if c.isprintable())[:100] or 'проєкт'
    message = f'Codex завершив завдання: {project}'
    sent = None
    if decision == 'away':
        available = run(['kdeconnect-cli', '--list-available', '--id-only'], timeout=1.5)
        # Recheck after querying reachability; user activity during that query wins.
        if available is not None and DEVICE in available.split() and is_away(directory):
            sent = run(['kdeconnect-cli', '-d', DEVICE, '--ping-msg', message], timeout=3)
        if sent is None:
            decision = 'desktop-fallback'
    if decision not in ('focused', 'away'):
        sent = run(['notify-send', '--urgency=normal', '--app-name=Codex',
                    'Codex', html.escape(message)], timeout=2)
    outcome = ('silenced' if decision == 'focused' else
               'phone_submitted' if decision == 'away' and sent is not None else
               'desktop_sent' if sent is not None else 'send_failed')
    log('completion', event_id=key[:12], decision=decision, outcome=outcome, version=VERSION)
    return decision


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version=VERSION)
    parser.add_argument('--dry-run', action='store_true', help='live routing check; no sends or dedupe changes')
    parser.add_argument('--watch-idle', action='store_true')
    parser.add_argument('--activity', choices=['away', 'active'])
    parser.add_argument('payload', nargs='?')
    args = parser.parse_args()
    setup_log()
    try:
        if args.watch_idle:
            return watch_idle()
        if args.activity:
            activity(args.activity)
        else:
            route(json.loads(args.payload or '{}'), args.dry_run)
    except (ValueError, TypeError, OSError, RuntimeError) as exc:
        log('handler_error', error=type(exc).__name__)
    return 0


if __name__ == '__main__':
    sys.exit(main())
