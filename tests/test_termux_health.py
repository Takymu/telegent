"""Exercise the actual shell health policy without a phone or network access."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import os
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which('bash') if __import__('os').name != 'nt' else shutil.which('bash', path='C:/Program Files/Git/bin')


@unittest.skipUnless(BASH, 'bash unavailable')
class HealthTest(unittest.TestCase):
    def shell(self, body):
        script = (ROOT/'termux/server_health.sh').read_text(encoding='utf-8')
        with tempfile.TemporaryDirectory() as log:
            result = subprocess.run([BASH, '-c', script+'\n'+body], text=True, capture_output=True,
                                    env={**os.environ, 'LOG': Path(log).as_posix()})
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)

    def test_transient_failure_and_recovery_reset_counter(self):
        self.shell('''
restarts=0
restart_server() { restarts=$((restarts+1)); }
curl() { return 1; }
check_server; check_server
[ "$server_fails" = 2 ] && [ "$restarts" = 0 ] || exit 1
curl() { return 0; }
check_server || exit 2
[ "$server_fails" = 0 ] || exit 3
curl() { return 1; }
check_server
[ "$restarts" = 0 ] && [ "$server_fails" = 1 ]
''')

    def test_local_failure_restarts_at_threshold_and_skips_tunnel(self):
        self.shell('''
restarts=0
restart_server() { restarts=$((restarts+1)); }
curl() { return 1; }
for i in 1 2 3; do
  fails=2
  if ! check_server; then fails=0; continue; fi
  exit 1
done
[ "$restarts" = 1 ] && [ "$server_fails" = 0 ] && [ "$fails" = 0 ]
''')

    def test_term_then_kill_only_original_managed_process(self):
        self.shell('''
SERVER_PIDFILE=unused
cat() { printf '42\\n'; }
date() { echo now; }
managed=1 term=0 forced=0
is_managed_server() { [ "$managed" = 1 ] && [ "$1" = 42 ]; }
kill() { case "$1" in -TERM) term=$((term+1));; -KILL) forced=$((forced+1));; esac; }
sleep() { managed=0; }
restart_server 2>/dev/null
[ "$term" = 1 ] && [ "$forced" = 0 ] || exit 1
managed=1
sleep() { :; }
restart_server 2>/dev/null
[ "$term" = 2 ] && [ "$forced" = 1 ] || exit 2
managed=0
restart_server 2>/dev/null
[ "$term" = 2 ] && [ "$forced" = 1 ]
''')


if __name__ == '__main__':
    unittest.main()
