import copy
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import sys
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROUTER = Path(os.environ.get('CODEX_NOTIFY_SCRIPT', str(Path(__file__).with_name('codex-notify.py'))))
spec = importlib.util.spec_from_file_location('router', ROUTER)
n = importlib.util.module_from_spec(spec)
spec.loader.exec_module(n)


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, XDG_RUNTIME_DIR=self.temp.name, WEZTERM_PANE='0',
                              HYPRLAND_INSTANCE_SIGNATURE='test-session', WAYLAND_DISPLAY='wayland-test')
        self.env.start()
        self.addCleanup(self.env.stop)
        self.directory = n.runtime()
        self.event = {'type': 'agent-turn-complete', 'thread-id': 'thread', 'turn-id': 'one',
                      'cwd': '/work/My Project', 'last-assistant-message': 'SECRET', 'input-messages': ['SECRET']}
        self.active = {'class': 'org.wezfurlong.wezterm', 'address': '0x1', 'pid': 123,
                       'title': 'project', 'mapped': True}
        self.windows = [copy.deepcopy(self.active)]
        self.clients = [{'pid': 123, 'hostname': socket.gethostname(), 'focused_pane_id': 0}]
        self.panes = [{'pane_id': 0, 'window_id': 0, 'tab_id': 0, 'is_active': True, 'window_title': 'project'}]
        self.query_patch = patch.object(n, 'query', side_effect=self.query)
        self.query_patch.start(); self.addCleanup(self.query_patch.stop)
        self.send = patch.object(n, 'run', side_effect=lambda args, **kwargs:
            n.DEVICE+'\n' if '--list-available' in args else '').start()
        self.addCleanup(patch.stopall)

    def query(self, args):
        return {'activewindow': self.active, 'clients': self.windows,
                'list-clients': self.clients, 'list': self.panes}[next(x for x in args if x in
                ['activewindow', 'clients', 'list-clients', 'list'])]

    def away(self):
        n.write_json(self.directory/'activity.json', dict(pid=os.getpid(), identity=n.identity(os.getpid()),
            child_pid=os.getpid(), child_identity=n.identity(os.getpid()), session=n.session(),
            away=True, token='test', changed_monotonic=time.monotonic()-130))

    def test_active_codex_pane_zero(self):
        self.assertEqual(n.route(self.event), 'focused'); self.send.assert_not_called()

    def test_other_tab(self):
        self.clients[0]['focused_pane_id'] = 7
        self.assertEqual(n.route(self.event), 'unfocused'); self.assert_desktop()

    def test_other_split(self):
        self.clients[0]['focused_pane_id'] = 1
        self.assertEqual(n.route(self.event), 'unfocused'); self.assert_desktop()

    def test_other_application(self):
        self.active['class'] = 'firefox'
        self.assertEqual(n.route(self.event), 'unfocused'); self.assert_desktop()

    def test_away_over_120(self):
        self.away(); self.active['class'] = 'firefox'
        self.assertEqual(n.route(self.event), 'away'); self.assert_phone()

    def test_away_focused_codex(self):
        self.away()
        self.assertEqual(n.route(self.event), 'away'); self.assert_phone()

    def test_kde_unavailable(self):
        self.away(); self.send.side_effect = [n.DEVICE+'\n', None, '']
        self.assertEqual(n.route(self.event), 'desktop-fallback'); self.assert_desktop()

    def test_phone_not_reachable(self):
        self.away(); self.send.side_effect = ['0 devices found', '']
        self.assertEqual(n.route(self.event), 'desktop-fallback'); self.assert_desktop()
        self.assertFalse(any('--ping-msg' in c.args[0] for c in self.send.call_args_list))

    def test_user_resumes_during_reachability_query(self):
        self.away()
        def resume(args, **kwargs):
            if '--list-available' in args:
                with patch.dict(os.environ, CODEX_IDLE_TOKEN='test'):
                    n.activity('active')
                return n.DEVICE+'\n'
            return ''
        self.send.side_effect = resume
        self.assertEqual(n.route(self.event), 'desktop-fallback'); self.assert_desktop()
        self.assertFalse(any('--ping-msg' in c.args[0] for c in self.send.call_args_list))

    def test_unknown_focus(self):
        self.active = None
        self.assertEqual(n.route(self.event), 'unknown'); self.assert_desktop()

    def test_two_sequential_completions_and_replays(self):
        self.active['class'] = 'firefox'
        self.assertEqual(n.route(self.event), 'unfocused')
        self.assertEqual(n.route(self.event), 'duplicate')
        self.event['turn-id'] = 'two'
        self.assertEqual(n.route(self.event), 'unfocused')
        self.assertEqual(n.route(self.event), 'duplicate')
        self.assertEqual(self.send.call_count, 2)

    def test_ambiguous_windows(self):
        self.windows.append(dict(self.active, address='0x2'))
        self.panes.append(dict(self.panes[0], pane_id=8, window_id=1))
        self.assertEqual(n.route(self.event), 'unknown'); self.assert_desktop()

    def test_multiple_windows_unique_mapping(self):
        self.windows.append(dict(self.active, address='0x2', title='other'))
        self.panes.append(dict(self.panes[0], pane_id=8, window_id=1, window_title='other'))
        self.assertEqual(n.route(self.event), 'focused'); self.send.assert_not_called()

    def test_other_window_same_process(self):
        self.windows.append(dict(self.active, address='0x2', title='other'))
        self.active = self.windows[1]
        self.panes.append(dict(self.panes[0], pane_id=8, window_id=1, window_title='other'))
        self.assertEqual(n.route(self.event), 'unknown'); self.assert_desktop()

    def test_foreign_gui_process(self):
        self.clients[0]['pid'] = 999
        self.assertEqual(n.route(self.event), 'unknown'); self.assert_desktop()

    def test_missing_wezterm_pane(self):
        del os.environ['WEZTERM_PANE']
        self.assertEqual(n.route(self.event), 'unknown'); self.assert_desktop()

    def test_stale_owner_after_restart(self):
        self.away(); state=n.read_json(self.directory/'activity.json'); state['identity']=['old', '0']
        n.write_json(self.directory/'activity.json', state)
        self.assertFalse(n.is_away(self.directory))
        self.assertEqual(n.route(self.event), 'focused'); self.send.assert_not_called()

    def test_old_session(self):
        self.away(); os.environ['HYPRLAND_INSTANCE_SIGNATURE'] = 'new'
        self.assertFalse(n.is_away(self.directory))

    def test_dead_hypridle(self):
        self.away(); state=n.read_json(self.directory/'activity.json'); state['child_pid']=99999999
        n.write_json(self.directory/'activity.json', state)
        self.assertFalse(n.is_away(self.directory))

    def test_resume_callback_and_stale_callback(self):
        self.away()
        with patch.dict(os.environ, CODEX_IDLE_TOKEN='wrong'):
            n.activity('active')
        self.assertTrue(n.is_away(self.directory))
        with patch.dict(os.environ, CODEX_IDLE_TOKEN='test'):
            n.activity('active')
        self.assertFalse(n.is_away(self.directory))

    def test_dry_run_does_not_send_or_consume_event(self):
        self.active['class'] = 'firefox'
        n.route(self.event, dry_run=True); self.send.assert_not_called()
        self.assertFalse((self.directory/'seen.json').exists())
        self.assertEqual(n.route(self.event), 'unfocused')

    def test_invalid_event_shape(self):
        with self.assertRaises(ValueError): n.route([])
        self.assertEqual(n.route({'type':'other'}), 'ignored'); self.send.assert_not_called()

    def test_disable(self):
        (self.directory/'disabled').touch()
        self.assertEqual(n.route(self.event), 'disabled'); self.send.assert_not_called()

    def test_parallel_process_replays_and_two_distinct_turns(self):
        tools = Path(self.temp.name)/'tools'; tools.mkdir()
        marker = Path(self.temp.name)/'sent.jsonl'
        fake = '#!/usr/bin/python3\nimport json,os,sys\n'
        (tools/'hyprctl').write_text(fake+'print(json.dumps({"address":"0xf", "class":"firefox"}))\n')
        (tools/'notify-send').write_text(fake+
            'with open(os.environ["FAKE_SEND_LOG"], "a") as f: f.write(json.dumps(sys.argv)+"\\n")\n')
        for tool in tools.iterdir(): tool.chmod(0o755)
        env = dict(os.environ, PATH=str(tools)+':/usr/bin:/bin', FAKE_SEND_LOG=str(marker),
                   XDG_STATE_HOME=str(Path(self.temp.name)/'logs'))
        command = ['/usr/bin/python3', str(ROUTER)]
        processes = [subprocess.Popen(command+[json.dumps(self.event)], env=env) for _ in range(6)]
        for proc in processes: self.assertEqual(proc.wait(timeout=8), 0)
        self.assertEqual(len(marker.read_text().splitlines()), 1)
        self.event['turn-id'] = 'second'
        subprocess.run(command+[json.dumps(self.event)], env=env, check=True, timeout=8)
        self.assertEqual(len(marker.read_text().splitlines()), 2)
        self.assertNotIn('SECRET', marker.read_text())

    def test_command_timeout_missing_tool_and_bad_json(self):
        # Exercise real subprocess boundary, not the routing mock.
        with patch('subprocess.run') as proc:
            proc.side_effect = subprocess.TimeoutExpired('kdeconnect-cli', 3)
            self.assertIsNone(REAL_RUN(['kdeconnect-cli'], timeout=3))
            proc.side_effect = FileNotFoundError()
            self.assertIsNone(REAL_RUN(['missing']))
        with patch.object(n, 'run', return_value='not json'):
            self.assertIsNone(REAL_QUERY(['hyprctl']))

    def test_log_outcomes(self):
        with patch.object(n, 'log') as record:
            n.route(self.event)
            self.assertEqual(record.call_args.kwargs['outcome'], 'silenced')
            self.assertNotIn('delivered', record.call_args.kwargs)
            self.event['turn-id'] = 'desktop'
            self.active['class'] = 'firefox'
            n.route(self.event)
            self.assertEqual(record.call_args.kwargs['outcome'], 'desktop_sent')
            self.event['turn-id'] = 'phone'
            self.away(); n.route(self.event)
            self.assertEqual(record.call_args.kwargs['outcome'], 'phone_submitted')
            self.event['turn-id'] = 'failure'
            self.send.side_effect = lambda *a, **k: None
            n.route(self.event)
            self.assertEqual(record.call_args.kwargs['outcome'], 'send_failed')

    def assert_desktop(self):
        args=self.send.call_args.args[0]
        self.assertEqual(args[0], 'notify-send'); self.assertIn('--urgency=normal', args)
        self.assertIn('My Project', args[-1]); self.assertNotIn('SECRET', str(args))

    def assert_phone(self):
        self.assertEqual(self.send.call_count, 2)
        args=self.send.call_args.args[0]
        self.assertEqual(args[0], 'kdeconnect-cli'); self.assertIn(n.DEVICE, args)
        self.assertIn('My Project', args[-1]); self.assertNotIn('SECRET', str(args))
        self.assertEqual(self.send.call_args.kwargs['timeout'], 3)


REAL_RUN=n.run
REAL_QUERY=n.query
if __name__ == '__main__': unittest.main(verbosity=2)
