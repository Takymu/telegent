"""Direct delivery through the running Windows VS Code owner; private IPC protocol.
No queue, model subprocess or permission overrides. Unknown outcomes are held.
"""
import json, os, re, struct, subprocess, sys, time, uuid
from pathlib import Path


def write_receipt(path, receipt):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
    tmp.write_text(json.dumps(receipt), encoding='utf-8')
    os.replace(tmp, path)


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}


class RpcError(RuntimeError):
    pass


class Owner:
    def __init__(self, stream):
        self.stream = stream
        self.client = 'initializing-client'
        self.client = self.request('initialize', {'clientType': 'telegent-notifier'})['result']['clientId']

    def request(self, method, params, version=0, target=None):
        rid = str(uuid.uuid4())
        packet = {'type': 'request', 'requestId': rid, 'sourceClientId': self.client,
                  'version': version, 'method': method, 'params': params, 'timeoutMs': 20000}
        if target:
            packet['targetClientId'] = target
        raw = json.dumps(packet, ensure_ascii=False).encode('utf-8')
        self.stream.write(struct.pack('<I', len(raw))+raw)
        while True:
            size = struct.unpack('<I', self.read(4))[0]
            if size > 64*1024*1024 or not size:
                raise RpcError('invalid IPC frame')
            response = json.loads(self.read(size))
            if response.get('requestId') == rid:
                if response.get('resultType') != 'success':
                    error = str(response.get('error', ''))
                    # Never expose arbitrary app/server output in logs.
                    if ('SteerTurnInactiveError' in error or 'NoActiveTurn' in error
                            or error == 'no active turn to steer'
                            or re.search(r'Cannot steer conversation \S+ because its active turn already ended', error)
                            or re.search(r'Conversation \S+ is not being streamed\.', error)):
                        raise RpcError('inactive')
                    if error in ('no-client-found', 'client-disconnected', 'request-timeout'):
                        raise RpcError(error)
                    raise RpcError('submission-error')
                return response
            if response.get('type') == 'client-discovery-request':
                raw = json.dumps({'type': 'client-discovery-response',
                                  'requestId': response['requestId'], 'response': {'canHandle': False}}).encode()
                self.stream.write(struct.pack('<I', len(raw))+raw)

    def read(self, size):
        raw = bytearray()
        while len(raw) < size:
            chunk = self.stream.read(size-len(raw))
            if not chunk:
                raise RpcError('IPC disconnected')
            raw.extend(chunk)
        return bytes(raw)


def dispatch(owner, binding, notice, path):
    receipt = json.loads(path.read_text()) if path.exists() else {}
    if receipt and receipt.get('thread') != binding['thread']:
        raise RpcError('receipt thread mismatch')
    if receipt.get('status') == 'accepted':
        return receipt
    if receipt.get('status') == 'submitting':
        raise RpcError('submission outcome unknown; inspect before replay')
    target = owner.request('thread-owner-discovery',
                           {'hostId': 'local', 'conversationId': binding['thread']}, 1)['handledByClientId']
    if receipt.get('status') != 'accepted':
        qid, text = notice['id'], notice['text']
        restore = {'id': qid, 'text': text, 'cwd': binding['cwd'], 'createdAt': int(time.time()*1000),
                   'context': {'prompt': text, 'addedFiles': [], 'fileAttachments': [],
                               'imageAttachments': [], 'ideContext': None, 'workspaceRoots': [binding['cwd']]}}
        receipt = {'thread': binding['thread'], 'status': 'submitting', 'attempt_at': time.time()}
        write_receipt(path, receipt)
        receipt['mode'] = 'steer'
        try:
            response = owner.request('thread-follower-steer-turn',
                                     {'conversationId': binding['thread'], 'input': notice['input'],
                                      'restoreMessage': restore, 'clientUserMessageId': qid}, 1, target)
        except RpcError as error:
            if str(error) != 'inactive':
                if str(error) == 'no-client-found':
                    receipt['status'] = 'pending'
                    write_receipt(path, receipt)
                # A timeout/disconnect may follow a successful send: do not duplicate it.
                raise
            receipt['mode'] = 'start'
            context = {'inheritThreadSettings': True, 'restoreMessage': restore}
            response = owner.request('thread-follower-start-turn',
                                     {'conversationId': binding['thread'], 'turnStart': {
                                         'request': {'threadId': binding['thread'], 'input': notice['input'],
                                                     'clientUserMessageId': qid},
                                         'context': context}}, 2, target)
        result = response['result']['result']
        turn = result.get('turnId') or result.get('turn', {}).get('id')
        if not turn:
            raise RpcError('missing turn acknowledgement')
        receipt.update(status='accepted', turn_id=turn, accepted_at=time.time())
        write_receipt(path, receipt)
    return receipt


def deliver(binding, text, qid, path):
    # Isolate blocking Windows pipe I/O. A timeout leaves a durable ambiguous receipt.
    result = subprocess.run([sys.executable, '-X', 'utf8', str(Path(__file__).resolve())],
                            input=json.dumps({'binding': binding, 'notice': {'id': qid, 'text': text,
                                'input': [{'type': 'text', 'text': text, 'text_elements': []}]}, 'path': str(path)}),
                            capture_output=True, text=True, encoding='utf-8', timeout=50,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if result.returncode:
        raise RpcError('Owner delivery pending; inspect local receipt')
    return load_json(path)


def check_owner(binding):
    result = subprocess.run([sys.executable, '-X', 'utf8', str(Path(__file__).resolve())],
                            input=json.dumps({'binding': binding, 'probe': True}), capture_output=True,
                            text=True, encoding='utf-8', timeout=25,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if result.returncode:
        raise RpcError('session owner unavailable')


if __name__ == '__main__':
    try:
        request = json.load(sys.stdin)
        with open(r'\\.\pipe\codex-ipc', 'r+b', buffering=0) as pipe:
            owner = Owner(pipe)
            if request.get('probe'):
                owner.request('thread-owner-discovery', {'hostId': 'local',
                              'conversationId': request['binding']['thread']}, 1)
            else:
                dispatch(owner, request['binding'], request['notice'], Path(request['path']))
    except Exception as error:
        print(type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
