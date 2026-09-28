"""Hermetic regression tests for #1591; never touch live dept trees."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
REL = 'scripts/lib/dispatch_helpers.py'


class VendorIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fw = self.root / 'framework'
        self.dept = self.root / 'agents' / 'alpha'
        self.env = os.environ.copy()
        for key in ('TELEGRAM_STATE_DIR', 'TELEGRAM_BOT_TOKEN', 'GH_TOKEN', 'GITHUB_TOKEN'):
            self.env.pop(key, None)
        self.env['BUBBLE_FRAMEWORK_ROOT'] = str(self.fw)
        for repo in (self.fw, self.dept):
            repo.mkdir(parents=True)
            self.git(repo, 'init', '-q')
            self.git(repo, 'config', 'user.email', 'fixture@test')
            self.git(repo, 'config', 'user.name', 'fixture')
        self.write(self.fw / REL, 'old canonical\n')
        self.commit(self.fw)
        self.write(self.dept / REL, 'old canonical\n')
        self.write(self.fw / REL, 'new canonical\n')
        self.commit(self.fw)

    def git(self, repo, *args):
        return subprocess.check_output(['git', '-C', str(repo), *args], env=self.env, text=True).strip()

    def commit(self, repo):
        self.git(repo, 'add', '.')
        self.git(repo, 'commit', '-qm', 'fixture')

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def vendor(self):
        return subprocess.run(['bash', str(ROOT / 'scripts/vendor-dept-libs.sh'), str(self.dept)],
                              env=self.env, capture_output=True, text=True, check=True)

    def test_past_canonical_bootstraps_and_replaces_readonly_inode(self):
        dst = self.dept / REL
        old = self.root / 'old-hardlink'
        os.link(dst, old)
        dst.chmod(0o444)
        result = self.vendor()
        self.assertIn('bootstrapped trusted baseline', result.stderr)
        self.assertEqual(dst.read_text(), 'new canonical\n')
        self.assertEqual(old.read_text(), 'old canonical\n')
        self.assertEqual(dst.stat().st_uid, os.getuid())
        self.assertEqual((self.dept / '.git/vendor-dept-libs' / REL).read_text(), dst.read_text())
        self.assertIn('0 file(s) refreshed', self.vendor().stderr)

    def test_blob_at_other_path_is_not_a_baseline(self):
        self.write(self.fw / 'unrelated.py', 'local fork\n')
        self.commit(self.fw)
        self.write(self.dept / REL, 'local fork\n')
        self.assertIn('DEFERRED', self.vendor().stderr)
        self.assertEqual((self.dept / REL).read_text(), 'local fork\n')
        self.assertFalse((self.dept / '.git/vendor-dept-libs' / REL).exists())

    def test_existing_baseline_does_not_authorize_changed_fork(self):
        self.write(self.dept / '.git/vendor-dept-libs' / REL, 'different baseline\n')
        self.assertIn('changed since last vendor', self.vendor().stderr)
        self.assertEqual((self.dept / REL).read_text(), 'old canonical\n')

    def test_fail_open_uses_existing_deduplicating_emitter(self):
        target = self.root / 'untouched'
        self.write(target, 'private\n')
        (self.dept / REL).unlink()
        (self.dept / REL).symlink_to(target)
        self.env['ALERT_LOG'] = str(self.root / 'alert')
        self.write(self.fw / 'tools/kanban/emit_kanban_item.sh',
                   '#!/bin/bash\nprintf "%s\\n" "$@" >> "$ALERT_LOG"\n')
        self.vendor()
        alert = (self.root / 'alert').read_text()
        self.assertIn('task=vendor-dept-libs', alert)
        self.assertIn('title=Vendor refresh failed: alpha', alert)
        self.assertIn('dest is a symlink', alert)
        self.assertIn('budget=1', alert)
        self.assertEqual(target.read_text(), 'private\n')

    def test_copy_failure_preserves_live_bytes_and_reports(self):
        bins = self.root / 'bin'
        self.write(bins / 'cp', '#!/bin/bash\nexit 1\n')
        (bins / 'cp').chmod(0o755)
        self.env['PATH'] = str(bins) + ':' + self.env['PATH']
        self.env['ALERT_LOG'] = str(self.root / 'alert')
        self.write(self.dept / '.git/vendor-dept-libs' / REL, 'old canonical\n')
        self.write(self.fw / 'tools/kanban/emit_kanban_item.sh',
                   '#!/bin/bash\nprintf "%s\\n" "$@" >> "$ALERT_LOG"\n')
        self.vendor()
        self.assertEqual((self.dept / REL).read_text(), 'old canonical\n')
        self.assertIn('could not copy', (self.root / 'alert').read_text())
        self.assertEqual(list((self.dept / 'scripts/lib').glob('.vendor.*')), [])

    def test_live_layout_runs_as_isolated_account_and_skips_local(self):
        scripts = self.root / 'scripts'
        scripts.mkdir()
        shutil.copy(ROOT / 'scripts/revendor-all-depts.sh', scripts)
        self.write(scripts / 'vendor-dept-libs.sh', '#!/bin/bash\necho "$1" >> "$VENDOR_LOG"\n')
        bins = self.root / 'bin'
        self.write(bins / 'id', '#!/bin/bash\necho 0\n')
        self.write(bins / 'runuser', '#!/bin/bash\nprintf "%s\\n" "$@" >> "$RUNUSER_LOG"\nshift 3\nexec "$@"\n')
        for path in bins.iterdir():
            path.chmod(0o755)
        self.env.update(PATH=str(bins) + ':' + self.env['PATH'],
                        VENDOR_LOG=str(self.root / 'vendor-log'),
                        RUNUSER_LOG=str(self.root / 'runuser-log'))
        self.write(self.dept.parent / 'mac/onboarding/STATE.yaml', 'host: local\n')
        cmd = ['bash', str(scripts / 'revendor-all-depts.sh'), '--agents-root', str(self.dept.parent)]
        subprocess.run(cmd + ['--dry-run'], env=self.env, capture_output=True, check=True)
        self.assertFalse((self.root / 'runuser-log').exists())
        subprocess.run(cmd, env=self.env, capture_output=True, check=True)
        self.assertEqual((self.root / 'vendor-log').read_text().strip(), str(self.dept))
        self.assertIn('-u\nagent-alpha\n--\nenv\n', (self.root / 'runuser-log').read_text())
        self.assertIn(':-/srv/agents}', (scripts / 'revendor-all-depts.sh').read_text())

        # A failed account switch must alert, never fall back to root, and
        # must not prevent a later department from receiving its refresh.
        (self.dept.parent / 'beta').mkdir()
        (self.root / 'vendor-log').unlink()
        self.write(bins / 'runuser', '#!/bin/bash\n[[ "$2" == agent-alpha ]] && exit 1\nshift 3\nexec "$@"\n')
        self.env['ALERT_LOG'] = str(self.root / 'alert')
        self.write(self.fw / 'tools/kanban/emit_kanban_item.sh',
                   '#!/bin/bash\nprintf "%s\\n" "$@" >> "$ALERT_LOG"\n')
        subprocess.run(cmd, env=self.env, capture_output=True, check=True)
        self.assertEqual((self.root / 'vendor-log').read_text().strip(), str(self.dept.parent / 'beta'))
        self.assertIn('runuser vendor failed', (self.root / 'alert').read_text())


if __name__ == '__main__':
    unittest.main()
