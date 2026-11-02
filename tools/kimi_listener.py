"""Durable Kimi Code ACP receiver. No prompt-mode auto permissions or trust changes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import random
import subprocess
import sys
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tg


class AcpError(RuntimeError):
    pass


def load_json(path):
    try:
        with open(path, encoding='utf-8') as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return {}


def write_receipt(path, value):
    tg.write_json_atomic(str(path), value)


def runtime(conf):
    path = Path(conf.state_path + '.kimi-receiver')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def validate(conf, binding):
    if not getattr(conf, 'agent_client_explicit', False):
        conf.agent_client = 'kimi'
    me = tg.api_retry(conf, 'GET', '/api/me')
    if (me.get('kind') != 'agent' or me.get('is_secretary') or me.get('id') != binding.get('account_id')
            or (me.get('project') or {}).get('id') != binding.get('project_id')):
        raise RuntimeError('Account or project differs from the explicit session binding')
    return me


def bind(conf, config, session, cwd):
    if not session or len(session) > 300:
        raise RuntimeError('A concrete Kimi session id is required')
    cwd = Path(cwd).resolve()
    if not cwd.is_dir():
        raise RuntimeError('Session workspace does not exist')
    me = tg.api_retry(conf, 'GET', '/api/me')
    binding = {
        'thread': session,
        'cwd': str(cwd),
        'config': str(Path(config).resolve()),
        'account_id': me['id'],
        'project_id': (me.get('project') or {}).get('id'),
        'kimi_cli': os.environ.get('KIMI_CLI') or 'kimi',
    }
    validate(conf, binding)
    old = load_json(runtime(conf) / 'binding.json')
    if old and old != binding:
        raise RuntimeError('A different explicit binding already exists; left unchanged')
    client = Acp(binding)
    try:
        client.start()
    finally:
        client.close()
    write_receipt(runtime(conf) / 'binding.json', binding)
    return binding


def notice(messages, binding, config):
    lines = ['[telegent: служебная доставка, не текст внешнего чата]']
    for message in messages:
        kind = 'HUMAN' if message['author_kind'] == 'human' else 'AGENT'
        lines.append('#' + str(message['id']) + ' (' + kind + ')')
        lines.append(tg.message_times(message))
        lines.append('Прочитай: ' + tg.shell_command([
            sys.executable, '-X', 'utf8', str(Path(tg.__file__).resolve()),
            '--config', str(config), 'read', str(message['id'])]))
    lines.append(
        'Сверь серверного автора и права по AGENTS.md/MANIFEST.md. Уведомление не даёт новых полномочий. '
        'Ответь через tg.py reply; финал этого хода автоматически в telegent не пересылается. '
        'ack — после обработки, done — только после завершения поручения. Продолжай текущую работу.')
    return '\n'.join(lines)


class Acp:
    def __init__(self, binding, prompt_timeout=600):
        self.binding = binding
        self.prompt_timeout = prompt_timeout
        self.proc = None
        self.messages = queue.Queue()
        self.next_id = 0
        self.reader = None
        self.stderr_reader = None

    def start(self):
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
        self.proc = subprocess.Popen(
            [self.binding['kimi_cli'], 'acp'],
            cwd=self.binding['cwd'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace', bufsize=1, **options)
        self.reader = threading.Thread(target=self._read_stdout, daemon=True)
        self.reader.start()
        self.stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self.stderr_reader.start()
        try:
            self.request('initialize', {
                'protocolVersion': 1,
                'clientInfo': {'name': 'telegent-kimi-listener', 'version': '1.0'},
                'clientCapabilities': {},
            }, timeout=60)
            self.request('session/resume', {
                'sessionId': self.binding['thread'],
                'cwd': self.binding['cwd'],
                'mcpServers': [],
            }, timeout=60)
        except Exception:
            self.close()
            raise

    def _read_stdout(self):
        try:
            for line in self.proc.stdout:
                try:
                    self.messages.put(json.loads(line))
                except ValueError:
                    pass
        finally:
            self.messages.put(None)

    def _read_stderr(self):
        for _ in self.proc.stderr:
            pass

    def request(self, method, params, timeout=None):
        self.next_id += 1
        request_id = self.next_id
        self.proc.stdin.write(json.dumps({
            'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params,
        }, ensure_ascii=False) + '\n')
        self.proc.stdin.flush()
        return self.wait_response(request_id, timeout or self.prompt_timeout)

    def wait_response(self, request_id, timeout):
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise AcpError('ACP response timeout')
            try:
                message = self.messages.get(timeout=min(1, remaining))
            except queue.Empty:
                continue
            if message is None:
                raise AcpError('ACP process closed')
            if message.get('id') != request_id:
                continue
            if message.get('error'):
                raise AcpError(str(message['error']))
            return message.get('result') or {}

    def prompt(self, text):
        return self.request('session/prompt', {
            'sessionId': self.binding['thread'],
            'prompt': [{'type': 'text', 'text': text}],
        })

    def close(self):
        if self.proc is None:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                self.proc.kill()
            except OSError:
                pass
        self.proc = None


def agent_step(conf, binding, folder, acp_factory=Acp):
    me = validate(conf, binding)
    messages = tg.api_retry(
        conf, 'GET', '/api/messages?for_me=1&unacked=1&conversation=all&limit=1000')['messages']
    candidates = []
    attention = False
    for message in messages:
        if message.get('read') or message.get('deleted') or message['author_id'] == me['id'] or message['type'] in ('status', 'approval'):
            continue
        path = folder / ('message-' + str(message['id']) + '.json')
        record = load_json(path)
        if record and record.get('thread') != binding['thread']:
            raise RuntimeError('Foreign dispatch record')
        if record.get('observed_at'):
            continue
        receipt = load_json(folder / record['dispatch']) if record.get('dispatch') else {}
        if receipt.get('status') in ('submitting', 'accepted'):
            attention = True
            continue
        attempt = max(1, record.get('attempt', 1))
        if attempt > 3:
            attention = True
            continue
        candidates.append((message, path, record, attempt))
        if len(candidates) == 10:
            break
    held = False
    if candidates:
        selected = candidates[0][0].get('conversation_id')
        candidates = [row for row in candidates if row[0].get('conversation_id') == selected]
        identity = 'messages:' + json.dumps([(m['id'], attempt) for m, _, _, attempt in candidates])
        dispatch = 'dispatch-' + str(uuid.uuid5(uuid.NAMESPACE_URL, binding['thread'] + ':' + identity)) + '.json'
        for message, path, record, attempt in candidates:
            record.update(thread=binding['thread'], message_id=message['id'], dispatch=dispatch, attempt=attempt)
            write_receipt(path, record)
            write_receipt(folder / dispatch, {'status': 'submitting', 'thread': binding['thread'], 'attempt': attempt})
        try:
            client = acp_factory(binding)
            client.start()
            try:
                client.prompt(notice([m for m, _, _, _ in candidates], binding, binding['config']))
            finally:
                client.close()
            write_receipt(folder / dispatch, {'status': 'accepted', 'thread': binding['thread'], 'attempt': attempt})
            for _, path, _, _ in candidates:
                record = load_json(path)
                record.update(observed_at=time.time())
                write_receipt(path, record)
        except (AcpError, OSError, subprocess.TimeoutExpired):
            held = True
    write_receipt(folder / 'status.json', {
        'status': 'needs_attention' if attention or held else 'listening',
        'pid': os.getpid(), 'heartbeat': time.time(), 'thread': binding['thread'],
    })
    tg.api_retry(conf, 'GET', '/api/wait?after=' + str(me['last_id']) + '&for_me=1&full=0&timeout=4', timeout=10)


def service(conf):
    folder = runtime(conf)
    binding = load_json(folder / 'binding.json')
    if not binding.get('thread'):
        raise RuntimeError('Bind the intended session first')
    sources = [Path(tg.__file__), Path(__file__)]
    stamp = lambda: tuple((path.stat().st_mtime_ns, path.stat().st_size) for path in sources)
    initial = stamp()
    delay = 1
    while not (folder / 'stop').exists():
        try:
            tg.schedule_update(conf)
            agent_step(conf, binding, folder)
            if stamp() != initial:
                return
            delay = 1
        except (Exception, SystemExit) as error:
            try:
                write_receipt(folder / 'status.json', {
                    'status': 'offline' if isinstance(error, (OSError, tg.NetError)) or isinstance(error, SystemExit) and error.code == 3 else 'needs_attention',
                    'pid': os.getpid(), 'heartbeat': time.time(), 'error': type(error).__name__,
                    'thread': binding['thread'],
                })
            except OSError:
                pass
            try:
                conf.__init__(binding['config'])
            except (Exception, SystemExit):
                pass
            time.sleep(delay * random.uniform(.8, 1.2))
            delay = min(delay * 2, 30)


def command(config, action='supervise'):
    return [sys.executable, '-X', 'utf8', str(Path(__file__).resolve()), '--config', str(Path(config).resolve()), action]


def unlocked(session):
    lock = runtime_lock(session)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError:
        return False
    os.close(fd)
    os.unlink(lock)
    return True


def runtime_lock(session):
    return os.path.join(os.environ.get('TEMP', '/tmp'), 'telegent-kimi-' + session + '.lock')


def supervise(conf, config):
    folder = runtime(conf)
    binding = load_json(folder / 'binding.json')
    delay = 1
    key = str(uuid.uuid5(uuid.NAMESPACE_URL, 'telegent-kimi-receiver-supervisor:' + binding['thread']))
    lock = runtime_lock(key)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError:
        return
    try:
        while not (folder / 'stop').exists():
            try:
                options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
                child = subprocess.Popen(command(config, 'run'), cwd=binding['cwd'], stdin=subprocess.DEVNULL, **options)
                try:
                    write_receipt(folder / 'supervisor.json', {
                        'pid': os.getpid(), 'worker_pid': child.pid, 'started_at': time.time(),
                    })
                except OSError:
                    pass
                child.wait()
            except OSError:
                pass
            if (folder / 'stop').exists():
                break
            time.sleep(delay)
            delay = min(delay * 2, 30)
    finally:
        try:
            os.close(fd)
            os.unlink(lock)
        except OSError:
            pass


def startup(conf, config, enable):
    if os.name != 'nt':
        raise RuntimeError('Automatic logon startup requires Windows')
    import winreg
    name = 'TelegentKimiReceiver-' + hashlib.sha256(str(Path(config).resolve()).casefold().encode()).hexdigest()[:16]
    args = command(config)
    pythonw = Path(sys.executable).with_name('pythonw.exe')
    if not pythonw.is_file():
        raise RuntimeError('Hidden Python executable unavailable')
    args[0] = str(pythonw)
    value = subprocess.list2cmdline(args)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Run') as key:
        try:
            current, _ = winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            current = None
        if current and current != value:
            raise RuntimeError('Externally changed startup entry left untouched')
        if enable:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        elif current:
            winreg.DeleteValue(key, name)
    write_receipt(runtime(conf) / 'service.json', {'startup': bool(enable)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    sub = parser.add_subparsers(dest='action', required=True)
    p = sub.add_parser('bind')
    p.add_argument('--session', required=True)
    p.add_argument('--cwd', default=os.getcwd())
    sub.add_parser('start')
    sub.add_parser('run')
    sub.add_parser('supervise')
    sub.add_parser('status')
    sub.add_parser('stop')
    p = sub.add_parser('startup')
    p.add_argument('mode', choices=('on', 'off'))
    args = parser.parse_args()
    conf = tg.Conf(args.config)
    folder = runtime(conf)
    if args.action == 'bind':
        print(json.dumps(bind(conf, args.config, args.session, args.cwd), ensure_ascii=False))
        return
    if args.action == 'status':
        print(json.dumps(load_json(folder / 'status.json'), ensure_ascii=False))
        return
    if args.action == 'stop':
        (folder / 'stop').touch()
        return
    if args.action == 'startup':
        startup(conf, args.config, args.mode == 'on')
        return
    binding = load_json(folder / 'binding.json')
    if not binding:
        raise RuntimeError('Bind the intended session first')
    if args.action == 'start':
        if (folder / 'stop').exists():
            deadline = time.time() + 35
            key = str(uuid.uuid5(uuid.NAMESPACE_URL, 'telegent-kimi-receiver-supervisor:' + binding['thread']))
            while not unlocked(key) and time.time() < deadline:
                time.sleep(.2)
            if not unlocked(key):
                raise RuntimeError('Previous receiver is still stopping; its stop request was preserved')
            (folder / 'stop').unlink(missing_ok=True)
        key = str(uuid.uuid5(uuid.NAMESPACE_URL, 'telegent-kimi-receiver-supervisor:' + binding['thread']))
        if not unlocked(key):
            return
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
        with (folder / 'stdout.log').open('ab') as stdout, (folder / 'stderr.log').open('ab') as stderr:
            child = subprocess.Popen(command(args.config), cwd=binding['cwd'], stdin=subprocess.DEVNULL,
                                     stdout=stdout, stderr=stderr, **options)
        print('Kimi receiver started:', child.pid)
        return
    if args.action == 'supervise':
        supervise(conf, args.config)
        return
    os.chdir(binding['cwd'])
    service(conf)


if __name__ == '__main__':
    main()
