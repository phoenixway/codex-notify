import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location('installer', Path(__file__).with_name('install.py'))
i = importlib.util.module_from_spec(spec);spec.loader.exec_module(i)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        target = self.home/'.config/systemd/user/hyprland-session.target'
        target.parent.mkdir(parents=True);target.write_text('[Unit]\nDescription=Test\n')
        self.output = contextlib.redirect_stdout(io.StringIO());self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def install(self):
        return i.install(self.home, i.DEFAULT_DEVICE, 'hyprland-session.target', check_tools=False)

    def test_fresh_install_and_restore(self):
        backup = self.install()
        config = self.home/'.codex/config.toml'
        parsed = tomllib.loads(config.read_text())
        self.assertEqual(parsed['notify'][1], str(self.home/'.local/bin/codex-notify.py'))
        self.assertFalse(parsed['tui']['notifications'])
        self.assertIn(str(self.home), (self.home/'.config/hypr/hypridle.conf').read_text())
        i.restore(backup)
        self.assertFalse(config.exists())
        self.assertFalse((self.home/'.local/bin/codex-notify.py').exists())

    def test_preserves_settings_and_original_bytes_on_restore(self):
        config = self.home/'.codex/config.toml';config.parent.mkdir()
        original = '# keep this\nmodel = "example"\n[tui]\nnotifications = true\nnotification_condition = "always"\n[features]\napps = false\n'
        config.write_text(original);backup=self.install()
        parsed=tomllib.loads(config.read_text())
        self.assertEqual(parsed['model'], 'example')
        self.assertEqual(parsed['tui']['notification_condition'], 'always')
        self.assertFalse(parsed['features']['apps'])
        self.assertEqual(config.read_text().count('[tui]'), 1)
        i.restore(backup);self.assertEqual(config.read_text(), original)

    def test_existing_idle_policy_refused_before_writes(self):
        config=self.home/'.config/hypr/hypridle.conf';config.parent.mkdir(parents=True)
        config.write_text('listener {\n timeout = 600\n on-timeout = lock\n}\n')
        with self.assertRaises(ValueError):self.install()
        self.assertFalse((self.home/'.codex/config.toml').exists())
        self.assertIn('lock', config.read_text())

    def test_existing_notify_refused_before_writes(self):
        config=self.home/'.codex/config.toml';config.parent.mkdir();config.write_text('notify = ["existing"]\n')
        with self.assertRaises(ValueError):self.install()
        self.assertFalse((self.home/'.local/bin/codex-notify.py').exists())

    def test_rollback_refuses_later_edits(self):
        backup=self.install();config=self.home/'.codex/config.toml'
        config.write_text(config.read_text()+'\n# later edit\n')
        with self.assertRaises(ValueError):i.restore(backup)
        self.assertTrue((self.home/'.local/bin/codex-notify.py').exists())

    def test_repeat_install_is_valid(self):
        self.install();before=(self.home/'.codex/config.toml').read_text()
        self.install();self.assertEqual((self.home/'.codex/config.toml').read_text(),before)


if __name__ == '__main__':unittest.main(verbosity=2)
