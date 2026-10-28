#!/usr/bin/env python3
"""Bind the administrator's development project to its VS Code Codex owner (Windows).

External content is read explicitly by the model. No CLI resume or settings overrides.
"""
import argparse
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import codex_owner_transport as ipc
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tg


class Transcript:
    def __init__(self, path):
        self.path, self.offset = Path(path), 0
        self.turn, self.busy, self.completed, self.closed = None, True, {}, set()
        self.updates = []

    def poll(self):
        if self.path.stat().st_size < self.offset:
            raise RuntimeError('session journal was replaced')
        with self.path.open('rb') as stream:
            stream.seek(self.offset)
            while True:
                start = stream.tell(); line = stream.readline()
                if not line or not line.endswith(b'\n'):
                    self.offset = start
                    break
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                payload = event.get('payload') or {}
                # Only explicit user-facing commentary. Never read reasoning or tool payloads.
                if (event.get('type') == 'response_item' and payload.get('type') == 'message'
                        and payload.get('role') == 'assistant' and payload.get('phase') == 'commentary'):
                    text = ''.join(item.get('text','') for item in payload.get('content',[])
                                   if item.get('type') == 'output_text')
                    if self.turn and text.strip():
                        self.updates.append((self.turn,start,text[:4000]))
                        self.updates = self.updates[-200:]
                if event.get('type') != 'event_msg':
                    continue
                if payload.get('type') == 'task_started':
                    self.turn, self.busy = payload.get('turn_id'), True
                elif payload.get('type') == 'task_complete':
                    self.busy = False
                    self.completed[payload.get('turn_id')] = payload.get('last_agent_message') or ''
                    self.closed.add(payload.get('turn_id'))
                elif payload.get('type') == 'turn_aborted':
                    self.busy = False
                    self.closed.add(payload.get('turn_id') or self.turn)


def session_transcript(session):
    root = Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex')/'sessions'
    paths = list(root.rglob('*'+session+'*.jsonl'))
    if len(paths) != 1:
        raise RuntimeError('Expected one journal for the explicitly selected Codex session')
    return paths[0]


def notice(message, config, session):
    read = tg.shell_command([sys.executable, '-X', 'utf8', str(Path(tg.__file__).resolve()),
                             '--config', str(config), 'read', str(int(message['id']))])
    observe = tg.shell_command([sys.executable, '-X', 'utf8', str(Path(__file__).resolve()),
                                '--config', str(config), '--session', session,
                                '--observe', str(int(message['id']))])
    return (
        f"[telegent] Новое сообщение #{int(message['id'])} (HUMAN).\n"
        f'{tg.message_times(message)}\n'
        f'Прочитай: {read}\n'
        f'Подтверди получение: {observe}\n'
        'Финальный ответ автоматически вернётся в этот чат.'
    )


def validate_account(conf):
    conf.agent_client='codex'
    me = tg.api_retry(conf, 'GET', '/api/me')
    if (me['kind'] != 'agent' or (me.get('project') or {}).get('kind') != 'development'
            or not me['project'].get('is_development_session')):
        raise RuntimeError('The dedicated agent bound to the development session is required')
    return me


def runtime(conf):
    folder = Path(conf.state_path+'.session-bridge')
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    return folder


def command_authorized(conf, me, message):
    # Roles and membership come from the API, never from the body or display name.
    humans = {u['id'] for u in me.get('users', []) if u['kind'] == 'human'}
    return (message.get('author_kind') == 'human' and message.get('author_id') in humans
            and message.get('project_id') == me['project']['id'])


def observe(conf, session, mid):
    me = validate_account(conf)
    message = tg.api_retry(conf, 'GET', '/api/messages/'+str(mid))['message']
    if not command_authorized(conf, me, message):
        raise RuntimeError('A human member of the development project is required')
    path = runtime(conf)/('message-'+str(mid)+'.json')
    record = ipc.load_json(path)
    receipt = ipc.load_json(runtime(conf)/('dispatch-'+str(mid)+'.json'))
    transcript = Transcript(session_transcript(session)); transcript.poll()
    if (record.get('thread') != session or receipt.get('status') != 'accepted'
            or receipt.get('turn_id') != transcript.turn or not transcript.busy):
        raise RuntimeError('No matching accepted dispatch in this active session turn')
    record.update(observed_at=time.time(), observed_turn=transcript.turn, observed_offset=transcript.offset)
    ipc.write_receipt(path, record)
    print('MODEL_RECEIPT_CONFIRMED', mid, transcript.turn)


def forward_progress(conf, session, transcript, folder, messages):
    if not getattr(conf,'share_progress',False):return
    ledger_path = folder/'progress-receipts.json'
    ledger = ipc.load_json(ledger_path)
    groups = {}
    for message in messages:
        record = ipc.load_json(folder/('message-'+str(message['id'])+'.json'))
        if (record.get('thread') == session and record.get('observed_at')
                and type(record.get('observed_offset')) is int):
            groups.setdefault(record['observed_turn'],[]).append((message,record))
    for turn, items in groups.items():
        # Mixed scopes fail closed. The fixed source survives new messages in the same turn.
        if len({(m['project_id'],m.get('conversation_id')) for m,_ in items}) != 1:continue
        plan_path = folder/('progress-turn-'+str(uuid.uuid5(uuid.UUID(session),turn))+'.json')
        plan = ipc.load_json(plan_path)
        if not plan:
            source,record = min(items,key=lambda row:row[0]['id'])
            plan = {'message_id':source['id'],'project_id':source['project_id'],
                    'conversation_id':source.get('conversation_id'),'offset':record['observed_offset']}
            ipc.write_receipt(plan_path,plan)
        if (plan['project_id'],plan.get('conversation_id')) != (items[0][0]['project_id'],items[0][0].get('conversation_id')):continue
        updates = [('started','started','Сообщение прочитано. Агент приступил к работе.')]
        updates += [(str(offset),'working',text) for event_turn,offset,text in transcript.updates
                    if event_turn==turn and offset>=plan['offset']]
        if turn in transcript.completed:
            updates.append(('completed','completed','Ход завершён. Это не означает завершение всей задачи.'))
        elif turn in transcript.closed:
            updates.append(('aborted','waiting','Ход прерван. Задача может быть не завершена.'))
        for suffix,state,body in updates:
            key = 'codex-progress-'+str(uuid.uuid5(uuid.UUID(session),turn+':'+suffix))
            if key not in ledger.get('sent',[]):
                ledger.setdefault('pending',{})[key]={'message_id':plan['message_id'],'key':key,'state':state,'body':body}
    # Persist before attempting HTTP. Acknowledged messages may disappear from the next inbox.
    ipc.write_receipt(ledger_path,ledger)
    if ledger.get('retry_after',0)>time.time():return
    if not ledger.get('pending'):return
    try:
        if not tg.api_retry(conf,'GET','/api/info',tries=1).get('agent_progress'):
            ledger['retry_after']=time.time()+60;ipc.write_receipt(ledger_path,ledger);return
    except (SystemExit,tg.NetError,OSError):
        ledger['retry_after']=time.time()+30;ipc.write_receipt(ledger_path,ledger);return
    for key,payload in list(ledger.get('pending',{}).items())[:10]:
        try:
            tg.api_retry(conf,'POST','/api/progress',payload,tries=1)
        except (SystemExit,tg.NetError,OSError) as error:
            # Optional reporting must not stop delivery, receipts or final replies on old/offline servers.
            ledger['retry_after']=time.time()+30
            # Rotate a refused source so a deleted task cannot block other tasks' reports.
            ledger['pending'].pop(key,None);ledger['pending'][key]=payload
            ipc.write_receipt(ledger_path,ledger)
            if isinstance(error,SystemExit) and error.code==2:continue
            return
        ledger['pending'].pop(key,None)
        ledger['sent']=(ledger.get('sent',[])+[key])[-2000:]
        ledger.pop('retry_after',None);ipc.write_receipt(ledger_path,ledger)


def forward_completed(conf, session, transcript, folder, me, messages):
    groups = {}
    for message in messages:
        path = folder/('message-'+str(message['id'])+'.json')
        record = ipc.load_json(path)
        turn = record.get('observed_turn') if record.get('observed_at') else None
        if turn and transcript.completed.get(turn):
            groups.setdefault((turn, message.get('conversation_id')), []).append((message, path, record))
    for (turn, conversation), items in groups.items():
        # One model final answers the batch, not each notification that steered that turn.
        key = str(uuid.uuid5(uuid.UUID(session), 'reply:'+turn+':'+str(conversation)))
        path = folder/('turn-reply-'+key+'.json')
        plan = ipc.load_json(path)
        if plan and (plan.get('thread') != session or plan.get('owner') != me['owner_id']
                     or plan.get('turn') != turn or plan.get('conversation_id') != conversation):
            raise RuntimeError('Turn reply binding mismatch')
        if not plan:
            latest = max((m for m, _, _ in items), key=lambda m: m['id'])
            plan = {'thread': session, 'owner': me['owner_id'], 'turn': turn,
                    'conversation_id': conversation, 'reply_to': latest['id'],
                    'source_ids': sorted(m['id'] for m, _, _ in items),
                    'to_ids': sorted({m['author_id'] for m, _, _ in items}),
                    'topic': latest.get('topic') or ''}
            # Recover receipts written by older bridges without sending another copy.
            previous = [r['reply_id'] for _, _, r in items if r.get('reply_id')]
            if previous:
                plan['reply_id'] = min(previous)
            ipc.write_receipt(path, plan)
        if not plan.get('reply_id'):
            answer = transcript.completed[turn]
            response = tg.api_retry(conf, 'POST', '/api/messages', {
                'body': answer, 'type': 'info', 'reply_to': plan['reply_to'], 'topic': plan['topic'],
                'to_ids': plan['to_ids'], 'idem_key': 'codex-turn-'+key})
            plan.update(reply_id=response['id'], replied_at=time.time())
            ipc.write_receipt(path, plan)
        for message, record_path, record in items:
            record.update(reply_id=plan['reply_id'], replied_at=plan.get('replied_at', time.time()))
            ipc.write_receipt(record_path, record)
        tg.api_retry(conf, 'POST', '/api/receipts', {'kind': 'ack', 'ids': [m['id'] for m, _, _ in items]})


def step(conf, config, session, transcript, folder, deliver=ipc.deliver):
    me = validate_account(conf)
    transcript.poll()
    tg.codex_report_activity(conf, {'session_id':session}, bool(transcript.turn and transcript.busy), str(folder/'activity.json'))
    messages = tg.api_retry(conf, 'GET', '/api/messages?for_me=1&unacked=1&limit=1000')['messages']
    wanted = [m for m in messages if command_authorized(conf, me, m)
              and m['type'] not in ('status', 'approval')]
    held = False
    for message in wanted:
        mid = message['id']
        path, dispatch_path = folder/('message-'+str(mid)+'.json'), folder/('dispatch-'+str(mid)+'.json')
        record = ipc.load_json(path)
        if record and (record.get('thread') != session or record.get('owner') != me['owner_id']):
            raise RuntimeError('Message binding mismatch')
        if not record:
            record = {'thread': session, 'owner': me['owner_id'], 'message_id': mid,
                      'id': str(uuid.uuid5(uuid.UUID(session), 'telegent-message-'+str(mid)))}
            ipc.write_receipt(path, record)
        receipt = ipc.load_json(dispatch_path)
        if receipt.get('status') != 'accepted':
            scope = ipc.load_json(folder/'turn-scope.json')
            if (scope.get('turn') and scope['turn'] not in transcript.closed
                    and scope.get('conversation_id') != message.get('conversation_id')):
                held = True
                continue
            try:
                receipt = deliver({'thread': session, 'cwd': str(Path.cwd())},
                                  notice(message, config, session), record['id'], dispatch_path)
            except (ipc.RpcError, OSError, subprocess.TimeoutExpired):
                held = True
                continue
            if receipt.get('status') == 'accepted':
                ipc.write_receipt(folder/'turn-scope.json', {'turn': receipt.get('turn_id'),
                                  'conversation_id': message.get('conversation_id')})
    # IPC acceptance is not a model read. Only explicit observation enables forwarding.
    forward_completed(conf, session, transcript, folder, me, wanted)
    forward_progress(conf, session, transcript, folder, wanted)
    ipc.write_receipt(folder/'status.json', {'status': 'delivery_pending' if held else 'listening',
                      'pid': os.getpid(), 'thread': session, 'heartbeat': time.time(),
                      'pending': [m['id'] for m in wanted]})
    # Keep presence alive during active turns and while awaiting actual model observation.
    tg.api_retry(conf, 'GET', '/api/wait?after='+str(me['last_id'])+'&for_me=1&full=0&timeout=4', timeout=10)


@contextlib.contextmanager
def session_lock(session):
    lock = Path.home()/'.telegent'/('codex-session-'+session+'.lock')
    lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock.open('a+b') as handle:
        if not handle.tell():
            handle.write(b'0'); handle.flush()
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--session', required=True)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--observe', type=int)
    args = parser.parse_args()
    session = str(uuid.UUID(args.session))
    config = Path(args.config).resolve()
    conf = tg.Conf(str(config))
    folder = runtime(conf)
    if args.observe:
        observe(conf, session, args.observe)
        return 0
    transcript = Transcript(session_transcript(session))
    with session_lock(session):
        while not (folder/'stop').exists():
            try:
                # Do not advertise a listening agent while its VS Code session is unavailable.
                ipc.check_owner({'thread': session})
                step(conf, config, session, transcript, folder)
            except (tg.NetError, OSError, ipc.RpcError, subprocess.TimeoutExpired):
                ipc.write_receipt(folder/'status.json', {'status': 'offline', 'pid': os.getpid(), 'heartbeat': time.time()})
                time.sleep(5)
            except SystemExit as error:
                # tg.py is a CLI: exhausted retries and HTTP refusals exit.
                # Keep the listener alive, without acknowledging or replaying a model turn.
                ipc.write_receipt(folder/'status.json', {'status': 'offline' if error.code == 3 else 'needs_attention',
                                  'pid': os.getpid(), 'client_exit': error.code, 'heartbeat': time.time()})
                time.sleep(5)
            except Exception as error:
                # Auth/protocol errors remain visible without discarding/acknowledging messages.
                ipc.write_receipt(folder/'status.json', {'status': 'needs_attention', 'pid': os.getpid(),
                                  'error': type(error).__name__, 'heartbeat': time.time()})
                print(type(error).__name__, file=sys.stderr, flush=True)
                time.sleep(5)
            if args.once:
                return 0


if __name__ == '__main__':
    sys.exit(main())
