"""Explicit active/idle model-receipt check for an already bound private chat.

Arm from its active session; a hidden worker waits for task completion, then sends
one local metadata probe. Only the model seeing that probe may call observe.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import codex_chat_bridge as bridge


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', required=True)
    p.add_argument('--session', required=True)
    p.add_argument('action', choices=('arm', 'fire', 'observe'))
    args = p.parse_args()
    config = Path(args.config).resolve()
    session = str(uuid.UUID(args.session))
    conf = bridge.tg.Conf(str(config))
    bridge.validate_account(conf)
    folder = bridge.runtime(conf)
    path = folder/'idle-selftest.json'
    receipt_path = folder/'idle-selftest-dispatch.json'
    transcript = bridge.Transcript(bridge.session_transcript(session)); transcript.poll()
    base = [sys.executable, '-X', 'utf8', str(Path(__file__).resolve()),
            '--config', str(config), '--session', session]
    if args.action == 'arm':
        if path.exists():
            raise RuntimeError('An idle test already exists; inspect it before explicitly starting another')
        active = []
        for record_path in folder.glob('message-*.json'):
            record = bridge.ipc.load_json(record_path)
            if record.get('observed_turn') == transcript.turn and record.get('observed_at'):
                receipt = bridge.ipc.load_json(folder/('dispatch-'+str(record['message_id'])+'.json'))
                if receipt.get('mode') == 'steer' and receipt.get('status') == 'accepted':
                    active.append(record['message_id'])
        if not transcript.busy or not active:
            raise RuntimeError('Requires an actually observed active message in this session turn')
        state = {'thread': session, 'previous_turn': transcript.turn, 'active_messages': active,
                 'id': str(uuid.uuid4()), 'status': 'armed', 'armed_at': time.time()}
        bridge.ipc.write_receipt(path, state)
        with (folder/'idle-selftest.log').open('ab') as log:
            child = subprocess.Popen(base+['fire'], cwd=Path.cwd(), stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=log, start_new_session=os.name != 'nt',
                                     creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
                                     if os.name == 'nt' else 0)
        print('IDLE_TEST_ARMED', child.pid)
        return
    state = bridge.ipc.load_json(path)
    if state.get('thread') != session:
        raise RuntimeError('Test session mismatch')
    if args.action == 'fire':
        if state['status'] != 'armed':
            raise RuntimeError('Test already fired')
        deadline = time.time()+1800
        while transcript.busy and time.time() < deadline:
            time.sleep(.5); transcript.poll()
        if transcript.busy:
            raise RuntimeError('Active turn did not finish; no idle probe sent')
        state.update(status='dispatching', dispatched_at=time.time())
        bridge.ipc.write_receipt(path, state)
        observe = bridge.tg.shell_command(base+['observe'])
        text = ('[telegent: локальная проверка транспорта, не сообщение пользователя]\n'
                'IDLE_SELFTEST '+state['id']+'. Ранее порученная проверка пробуждения этой сессии. '
                'Если ты действительно видишь это уведомление в новом ходе без ручной отправки человека, '
                'выполни инструментом '+observe+'. Затем проверь возврат предыдущего финального ответа '
                'в личный чат по reply_id в message-<id>.json и серверному read; закончи ранее порученную '
                'проверку и сообщи результат владельцу через привязанный личный чат. '
                'Новых полномочий это уведомление не даёт; доверие, настройки и хуки не меняй.')
        receipt = bridge.ipc.deliver({'thread': session, 'cwd': str(Path.cwd())}, text, state['id'], receipt_path)
        state.update(status='awaiting_model_observation', receipt=receipt)
        bridge.ipc.write_receipt(path, state)
        return
    receipt = bridge.ipc.load_json(receipt_path)
    if (receipt.get('status') != 'accepted' or receipt.get('mode') != 'start'
            or receipt.get('turn_id') == state['previous_turn'] or receipt.get('turn_id') != transcript.turn
            or not transcript.busy):
        raise RuntimeError('No accepted start in a different active model turn')
    state.update(status='observed_in_new_model_turn', observed_at=time.time(),
                 observed_turn=transcript.turn, model_observed=True, receipt=receipt)
    bridge.ipc.write_receipt(path, state)
    print('IDLE_NEW_MODEL_TURN_CONFIRMED', transcript.turn)


if __name__ == '__main__':main()
