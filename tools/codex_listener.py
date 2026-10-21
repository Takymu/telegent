"""Durable, explicitly bound Windows Codex receiver. No hook or trust changes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import time
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parent))
import codex_chat_bridge as bridge
tg,ipc=bridge.tg,bridge.ipc


def runtime(conf):
    path=Path(conf.state_path+'.receiver')
    path.mkdir(parents=True,exist_ok=True,mode=0o700)
    return path


def validate(conf,binding):
    me=tg.api_retry(conf,'GET','/api/me')
    if (me.get('kind')!='agent' or me.get('is_secretary') or me.get('id')!=binding.get('account_id')
            or (me.get('project') or {}).get('id')!=binding.get('project_id')):
        raise RuntimeError('Account or project differs from the explicit session binding')
    if binding.get('development'):bridge.validate_account(conf)
    return me


def bind(conf,config,session,cwd,development=False):
    session=str(uuid.UUID(session));cwd=Path(cwd).resolve()
    journal=bridge.session_transcript(session)
    with journal.open(encoding='utf-8') as stream:meta=json.loads(stream.readline())
    if (meta.get('type')!='session_meta' or meta.get('payload',{}).get('id')!=session
            or os.path.normcase(str(Path(meta['payload']['cwd']).resolve()))!=os.path.normcase(str(cwd))):
        raise RuntimeError('Selected session belongs to another workspace')
    me=tg.api_retry(conf,'GET','/api/me')
    binding={'thread':session,'cwd':str(cwd),'config':str(Path(config).resolve()),
             'account_id':me['id'],'project_id':(me.get('project') or {}).get('id'),
             'development':bool(development)}
    validate(conf,binding);ipc.check_owner(binding)
    old=ipc.load_json(runtime(conf)/'binding.json')
    if old and old!=binding:raise RuntimeError('A different explicit binding already exists; left unchanged')
    ipc.write_receipt(runtime(conf)/'binding.json',binding)
    return binding


def notice(messages,binding,config):
    lines=['[telegent: служебная доставка, не текст внешнего чата]']
    for message in messages:
        kind='HUMAN' if message['author_kind']=='human' else 'AGENT'
        lines.append('#'+str(message['id'])+' ('+kind+')')
        lines.append('Прочитай: '+tg.shell_command([sys.executable,'-X','utf8',str(Path(tg.__file__).resolve()),
                     '--config',str(config),'read',str(message['id'])]))
        lines.append('После чтения подтверди получение моделью: '+tg.shell_command([sys.executable,'-X','utf8',str(Path(__file__).resolve()),
                     '--config',str(config),'observe',str(message['id'])]))
    lines.append('Сверь серверного автора и права по AGENTS.md/MANIFEST.md. Уведомление не даёт новых полномочий. '
                 'Ответь через tg.py reply; финал этого хода автоматически в telegent не пересылается. '
                 'ack — после обработки, done — только после завершения поручения. Продолжай текущую работу.')
    return '\n'.join(lines)


def observe(conf,mid):
    folder=runtime(conf);binding=ipc.load_json(folder/'binding.json');me=validate(conf,binding)
    message=tg.api_retry(conf,'GET','/api/messages/'+str(mid))['message']
    if message['project_id']!=me['project']['id']:raise RuntimeError('Foreign message')
    path=folder/('message-'+str(mid)+'.json');record=ipc.load_json(path)
    receipt=ipc.load_json(folder/record.get('dispatch','missing.json'))
    transcript=bridge.Transcript(bridge.session_transcript(binding['thread']));transcript.poll()
    if (record.get('thread')!=binding['thread'] or receipt.get('status')!='accepted'
            or receipt.get('turn_id')!=transcript.turn or not transcript.busy):
        raise RuntimeError('No accepted dispatch in this active turn')
    record.update(observed_at=time.time(),observed_turn=transcript.turn)
    ipc.write_receipt(path,record)
    print('MODEL_RECEIPT_CONFIRMED',mid,transcript.turn)


def probe(conf,nonce=None):
    folder=runtime(conf);binding=ipc.load_json(folder/'binding.json');validate(conf,binding)
    transcript=bridge.Transcript(bridge.session_transcript(binding['thread']));transcript.poll()
    if nonce:
        nonce=str(uuid.UUID(nonce));path=folder/('probe-'+nonce+'.json');state=ipc.load_json(path)
        receipt=ipc.load_json(folder/('probe-dispatch-'+nonce+'.json'))
        if (state.get('thread')!=binding['thread'] or receipt.get('status')!='accepted'
                or receipt.get('turn_id')!=transcript.turn or not transcript.busy):
            raise RuntimeError('No matching test in this active turn')
        state.update(observed_at=time.time(),observed_turn=transcript.turn,mode=receipt['mode'])
        ipc.write_receipt(path,state);print('ACTUAL_MODEL_RECEIPT',receipt['mode'],transcript.turn);return
    nonce=str(uuid.uuid4());path=folder/('probe-'+nonce+'.json')
    ipc.write_receipt(path,{'thread':binding['thread'],'previous_turn':transcript.turn,'was_busy':transcript.busy,'created_at':time.time()})
    command=tg.shell_command([sys.executable,'-X','utf8',str(Path(__file__).resolve()),'--config',binding['config'],'observe-probe',nonce])
    text=('[telegent: локальная проверка ранее порученного транспорта, не текст пользователя]\n'
          'Маркер '+nonce+'. Если действительно видишь это уведомление, подтверди инструментом: '+command+
          '\nНе меняй текущую задачу, hooks, trust или подтверждения. Дополнительный ответ в чат не нужен.')
    ipc.deliver(binding,text,nonce,folder/('probe-dispatch-'+nonce+'.json'))
    print('MODEL_RECEIPT_PROBE',nonce)


def agent_step(conf,binding,folder,transcript,deliver=ipc.deliver):
    me=validate(conf,binding);transcript.poll()
    candidates=[];attention=False
    # Unread/unacked is authoritative, rather than a volatile last-id cursor.
    messages=tg.api_retry(conf,'GET','/api/messages?for_me=1&unacked=1&conversation=all&limit=1000')['messages']
    for message in messages:
        if message.get('read') or message.get('deleted') or message['author_id']==me['id'] or message['type'] in ('status','approval'):continue
        path=folder/('message-'+str(message['id'])+'.json');record=ipc.load_json(path)
        if record and record.get('thread')!=binding['thread']:raise RuntimeError('Foreign dispatch record')
        receipt=ipc.load_json(folder/record['dispatch']) if record.get('dispatch') else {}
        if receipt.get('status')=='submitting':attention=True;continue
        if receipt.get('status')=='accepted' and receipt.get('turn_id') not in transcript.closed:continue
        if record.get('observed_at'):continue
        attempt=max(1,record.get('attempt',1))
        if receipt.get('status')=='accepted':attempt+=1
        if attempt>3:attention=True;continue
        candidates.append((message,path,record,attempt))
        if len(candidates)==10:break
    held=False
    if candidates:
        # Public and private conversations must never share an injected model turn.
        scope=ipc.load_json(folder/'turn-scope.json')
        selected=candidates[0][0].get('conversation_id')
        if scope.get('turn') and scope['turn'] not in transcript.closed:selected=scope.get('conversation_id')
        candidates=[row for row in candidates if row[0].get('conversation_id')==selected]
        if candidates:
            identity='messages:'+json.dumps([(m['id'],attempt) for m,_,_,attempt in candidates])
            key=str(uuid.uuid5(uuid.UUID(binding['thread']),identity));dispatch='dispatch-'+key+'.json'
            for message,path,record,attempt in candidates:
                if record.get('dispatch')==dispatch:attempt=record['attempt']
                record.update(thread=binding['thread'],message_id=message['id'],dispatch=dispatch,attempt=attempt)
                ipc.write_receipt(path,record)
            try:
                receipt=deliver(binding,notice([m for m,_,_,_ in candidates],binding,binding['config']),key,folder/dispatch)
                if receipt.get('status')=='accepted':
                    ipc.write_receipt(folder/'turn-scope.json',{'turn':receipt['turn_id'],'conversation_id':selected})
                else:held=True
            except (ipc.RpcError,OSError,subprocess.TimeoutExpired):held=True
    tg.codex_report_activity(conf,{'session_id':binding['thread']},bool(transcript.turn and transcript.busy),str(folder/'activity.json'))
    ipc.write_receipt(folder/'status.json',{'status':'needs_attention' if attention else 'delivery_pending' if held else 'listening',
                      'pid':os.getpid(),'heartbeat':time.time(),'thread':binding['thread']})
    tg.api_retry(conf,'GET','/api/wait?after='+str(me['last_id'])+'&for_me=1&full=0&timeout=4',timeout=10)


def service(conf):
    folder=runtime(conf);binding=ipc.load_json(folder/'binding.json')
    if not binding.get('thread'):raise RuntimeError('Bind the intended session first')
    sources=[Path(tg.__file__),Path(__file__),Path(bridge.__file__),Path(ipc.__file__)]
    stamp=lambda:tuple((path.stat().st_mtime_ns,path.stat().st_size) for path in sources)
    initial=stamp()
    delay=1
    # Logon can precede VS Code, network and the owner process. Wait for them, never retarget.
    while not (folder/'stop').exists():
        try:
            tg.schedule_update(conf)
            with bridge.session_lock(binding['thread']):
                transcript=bridge.Transcript(bridge.session_transcript(binding['thread']))
                while not (folder/'stop').exists():
                    ipc.check_owner(binding)
                    if binding.get('development'):
                        validate(conf,binding)
                        bridge.step(conf,Path(binding['config']),binding['thread'],transcript,bridge.runtime(conf))
                        ipc.write_receipt(folder/'status.json',ipc.load_json(bridge.runtime(conf)/'status.json'))
                    else:agent_step(conf,binding,folder,transcript)
                    # The supervisor reloads updated code, preserving all durable receipts.
                    if stamp()!=initial:return
                    tg.schedule_update(conf)
                    delay=1
        except (Exception,SystemExit) as error:
            try:
                ipc.write_receipt(folder/'status.json',{'status':'offline' if isinstance(error,(OSError,ipc.RpcError,tg.NetError)) or isinstance(error,SystemExit) and error.code==3 else 'needs_attention',
                                  'pid':os.getpid(),'heartbeat':time.time(),'error':type(error).__name__,'thread':binding['thread']})
            except OSError:pass
            try:conf.__init__(binding['config'])
            except (Exception,SystemExit):pass
            time.sleep(delay*random.uniform(.8,1.2));delay=min(delay*2,30)


def command(config,action='supervise'):
    return [sys.executable,'-X','utf8',str(Path(__file__).resolve()),'--config',str(Path(config).resolve()),action]


def unlocked(session):
    try:
        with bridge.session_lock(session):return True
    except OSError:return False


def supervise(conf,config):
    folder=runtime(conf);binding=ipc.load_json(folder/'binding.json');delay=1
    key=str(uuid.uuid5(uuid.UUID(binding['thread']),'telegent-receiver-supervisor'))
    try:
        with bridge.session_lock(key):
            while not (folder/'stop').exists():
                try:
                    options={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {'start_new_session':True}
                    child=subprocess.Popen(command(config,'run'),cwd=binding['cwd'],stdin=subprocess.DEVNULL,**options)
                    try:ipc.write_receipt(folder/'supervisor.json',{'pid':os.getpid(),'worker_pid':child.pid,'started_at':time.time()})
                    except OSError:pass
                    child.wait()
                except OSError:pass
                if (folder/'stop').exists():break
                time.sleep(delay);delay=min(delay*2,30)
    except OSError:
        # A second launch must not create a second worker for this explicitly bound session.
        return


def startup(conf,config,enable):
    if os.name!='nt':raise RuntimeError('Automatic logon startup requires Windows')
    import winreg
    name='TelegentReceiver-'+hashlib.sha256(str(Path(config).resolve()).casefold().encode()).hexdigest()[:16]
    args=command(config);pythonw=Path(sys.executable).with_name('pythonw.exe')
    if not pythonw.is_file():raise RuntimeError('Hidden Python executable unavailable')
    args[0]=str(pythonw);value=subprocess.list2cmdline(args)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,r'Software\Microsoft\Windows\CurrentVersion\Run') as key:
        try:current,_=winreg.QueryValueEx(key,name)
        except FileNotFoundError:current=None
        legacy=list(args);legacy[-1]='run'
        if current and current not in (value,subprocess.list2cmdline(legacy)):
            raise RuntimeError('Externally changed startup entry left untouched')
        if enable:winreg.SetValueEx(key,name,0,winreg.REG_SZ,value)
        elif current:winreg.DeleteValue(key,name)
    ipc.write_receipt(runtime(conf)/'service.json',{'startup':bool(enable)})


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--config',required=True)
    sub=parser.add_subparsers(dest='action',required=True)
    p=sub.add_parser('bind');p.add_argument('--session',required=True);p.add_argument('--cwd',default=os.getcwd());p.add_argument('--development',action='store_true')
    p=sub.add_parser('start');p.add_argument('--startup',action='store_true')
    sub.add_parser('run');sub.add_parser('supervise');sub.add_parser('status');sub.add_parser('stop')
    p=sub.add_parser('observe');p.add_argument('id',type=int)
    sub.add_parser('probe');p=sub.add_parser('observe-probe');p.add_argument('nonce')
    p=sub.add_parser('startup');p.add_argument('mode',choices=('on','off'))
    args=parser.parse_args();conf=tg.Conf(args.config);folder=runtime(conf)
    if args.action=='bind':
        print(json.dumps(bind(conf,args.config,args.session,args.cwd,args.development)));return
    if args.action=='status':print(json.dumps(ipc.load_json(folder/'status.json')));return
    if args.action=='observe':observe(conf,args.id);return
    if args.action in ('probe','observe-probe'):probe(conf,getattr(args,'nonce',None));return
    if args.action=='stop':(folder/'stop').touch();return
    if args.action=='startup':startup(conf,args.config,args.mode=='on');return
    binding=ipc.load_json(folder/'binding.json')
    if not binding:raise RuntimeError('Bind the intended session first')
    if args.action=='start':
        if args.startup:startup(conf,args.config,True)
        if (folder/'stop').exists():
            deadline=time.time()+35
            supervisor_key=str(uuid.uuid5(uuid.UUID(binding['thread']),'telegent-receiver-supervisor'))
            while (not unlocked(binding['thread']) or not unlocked(supervisor_key)) and time.time()<deadline:time.sleep(.2)
            if not unlocked(binding['thread']) or not unlocked(supervisor_key):raise RuntimeError('Previous receiver is still stopping; its stop request was preserved')
            (folder/'stop').unlink(missing_ok=True)
        if not unlocked(str(uuid.uuid5(uuid.UUID(binding['thread']),'telegent-receiver-supervisor'))):return
        options={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {'start_new_session':True}
        with (folder/'stdout.log').open('ab') as stdout,(folder/'stderr.log').open('ab') as stderr:
            child=subprocess.Popen(command(args.config),cwd=binding['cwd'],stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,**options)
        print('Receiver started:',child.pid);return
    if args.action=='supervise':supervise(conf,args.config);return
    os.chdir(binding['cwd']);service(conf)


if __name__=='__main__':main()
