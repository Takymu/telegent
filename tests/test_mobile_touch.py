"""Chromium compositor touch input, overflowing chat content and nested code scrolling."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import time
import unittest
import urllib.parse
import urllib.request

import test_attachments as fixture


def chromium_path():
    candidates = [os.environ.get('TELEGENT_TEST_CHROMIUM'), shutil.which('chromium'),
                  shutil.which('google-chrome'), shutil.which('msedge')]
    for base in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
        root = Path(os.environ.get(base, '.'))
        candidates += [str(root/'Google/Chrome/Application/chrome.exe'),
                       str(root/'Microsoft/Edge/Application/msedge.exe')]
        candidates += [str(p) for p in (root/'Microsoft/EdgeCore').glob('*/msedge.exe')]
    return next((p for p in candidates if p and Path(p).is_file()), None)


class DevTools:
    """Minimal local-only CDP WebSocket transport, avoiding a browser-driver dependency."""
    def __init__(self, url):
        parsed = urllib.parse.urlsplit(url)
        assert parsed.scheme == 'ws' and parsed.hostname in ('127.0.0.1', 'localhost')
        self.sock = socket.create_connection((parsed.hostname, parsed.port), timeout=15)
        self.stream = self.sock.makefile('rb')
        self.sequence = 0
        self.events = []
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(('GET '+parsed.path+' HTTP/1.1\r\nHost: '+parsed.netloc+
                           '\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: '+key+
                           '\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
        assert b' 101 ' in self.stream.readline(), 'CDP handshake rejected'
        headers = {}
        while True:
            line = self.stream.readline().strip()
            if not line: break
            name, value = line.split(b':', 1); headers[name.lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
        assert headers[b'sec-websocket-accept'] == expected

    def call(self, method, **params):
        self.sequence += 1
        raw = json.dumps({'id': self.sequence, 'method': method, 'params': params}).encode()
        n = len(raw); mask = os.urandom(4)
        length = bytes([0x80 | n]) if n < 126 else b'\xfe'+struct.pack('!H', n) if n < 65536 else b'\xff'+struct.pack('!Q', n)
        self.sock.sendall(b'\x81'+length+mask+bytes(b ^ mask[i % 4] for i, b in enumerate(raw)))
        while True:
            head = self.stream.read(2)
            if len(head) != 2: raise RuntimeError('CDP disconnected')
            n = head[1] & 127
            if n == 126: n = struct.unpack('!H', self.stream.read(2))[0]
            elif n == 127: n = struct.unpack('!Q', self.stream.read(8))[0]
            data = self.stream.read(n)
            if head[0] & 15 == 8: raise RuntimeError('CDP closed')
            reply = json.loads(data)
            if 'method' in reply:
                self.events.append(reply)
            if reply.get('id') != self.sequence: continue
            if 'error' in reply: raise RuntimeError(reply['error'])
            return reply.get('result', {})

    def evaluate(self, expression):
        result = self.call('Runtime.evaluate', expression=expression, returnByValue=True, awaitPromise=True)
        if result.get('exceptionDetails'): raise AssertionError(result['exceptionDetails'])
        return result['result'].get('value')

    def close(self):
        self.stream.close(); self.sock.close()


@unittest.skipUnless(chromium_path(), 'Chromium is required for native touch checks')
class MobileTouchTest(unittest.TestCase):
    def test_native_swipes_and_scrolling_with_long_messages(self):
        fixture.AttachmentTest.setUpClass()
        fx = fixture.AttachmentTest()
        browser = dev = None
        try:
            fx.s.HERE = str(fx.work)
            source = Path(os.environ.get('TELEGENT_TEST_WEB', str(fixture.ROOT/'web.html')))
            (fx.work/'web.html').write_text(source.read_text(encoding='utf-8').replace('<script>', '<script>localStorage.setItem("tg_language","ru");</script><script>', 1),encoding='utf-8')
            with fx.s.DB_LOCK:
                fx.s.db().execute("UPDATE users SET display_name=? WHERE name='bob-agent'", ('agent-'+'W'*26,))
            body = '\n\n'.join(['Plain fixture message for native touch navigation. '*7]*4)
            body += '\n\nhttps://example.org/'+'segment'*100+'\n\n`'+'inline'*80+'`'
            body += '\n\n```text\n'+'wide_code_'*100+'\n```'
            mid = fx.api('POST', '/api/messages?project=main', {'body': body, 'topic': 'fixture-'+'W'*52}, who='alice')['id']
            fx.api('POST','/api/projects',{'title':'Second fixture','slug':'second'},who='alice')
            fx.api('POST','/api/projects',{'title':'Third fixture','slug':'third'},who='alice')
            profile = fx.work/'chromium-profile'
            browser = subprocess.Popen([chromium_path(), '--headless', '--disable-gpu', '--no-first-run',
                                        '--no-default-browser-check', '--remote-debugging-port=0',
                                        '--user-data-dir='+str(profile), 'about:blank'], env=fx.env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            active = profile/'DevToolsActivePort'
            for _ in range(100):
                if active.exists(): break
                if browser.poll() is not None: self.fail('headless Chromium exited')
                time.sleep(.1)
            self.assertTrue(active.exists(), 'Chromium debugging endpoint unavailable')
            port = active.read_text().splitlines()[0]
            with urllib.request.urlopen('http://127.0.0.1:'+port+'/json/list', timeout=5) as response:
                page = next(p for p in json.load(response) if p['type'] == 'page')
            dev = DevTools(page['webSocketDebuggerUrl'])
            dev.call('Emulation.setDeviceMetricsOverride', width=390, height=844, deviceScaleFactor=1, mobile=True)
            dev.call('Emulation.setTouchEmulationEnabled', enabled=True, maxTouchPoints=5)
            dev.call('Page.enable')
            dev.call('Page.navigate', url=fx.url+'/#token='+fx.tokens['alice']+'&p=main')

            def wait(expression):
                for _ in range(100):
                    if dev.evaluate(expression): return
                    time.sleep(.05)
                state=dev.evaluate('({tab:TAB,selection:getSelection().toString(),target:document.elementFromPoint(100,260)?.outerHTML?.slice(0,180),events:window.fixtureTouches})')
                self.fail('Browser condition timed out: '+expression+' '+json.dumps(state))

            def swipe(x, y, dx, dy=0, steps=8):
                dev.call('Input.dispatchTouchEvent', type='touchStart', touchPoints=[{'x': x, 'y': y, 'id': 1}])
                for step in range(1, steps+1):
                    dev.call('Input.dispatchTouchEvent', type='touchMove', touchPoints=[{'x': x+dx*step/steps, 'y': y+dy*step/steps, 'id': 1}])
                    time.sleep(.025)
                dev.call('Input.dispatchTouchEvent', type='touchEnd', touchPoints=[])
                time.sleep(.15)

            wait('typeof ME!=="undefined" && ME?.project && MSGS.has('+str(mid)+')')
            dev.evaluate('window.fixtureTouches=[];for(const name of ["touchstart","touchmove","touchend","touchcancel"])document.addEventListener(name,e=>{fixtureTouches.push({type:name,cancelable:e.cancelable,target:e.target.id||e.target.className});fixtureTouches=fixtureTouches.slice(-15)},true)')
            dev.evaluate('EXPANDED.add('+str(mid)+');render(true);$("scroller").scrollTop=0;$("cBody").blur()')
            sizes = dev.evaluate('({width:$("scroller").clientWidth,scroll:$("scroller").scrollWidth,body:document.documentElement.scrollWidth})')
            self.assertLessEqual(sizes['scroll'], sizes['width']+1, sizes)
            self.assertLessEqual(sizes['body'], 390, sizes)
            order=dev.evaluate('[...$("mobileNav").querySelectorAll("[data-mobile]")].map(b=>b.dataset.mobile)')
            self.assertEqual(order, ['projects', 'feed', 'users', 'direct', 'more'])

            def section(expected):
                wait('$("mobileNav").querySelector("[aria-current=page]")?.dataset.mobile==='+json.dumps(expected))
                if expected=='users':
                    self.assertTrue(dev.evaluate('$("aside").classList.contains("open")'))
                else:
                    self.assertEqual(dev.evaluate('TAB'), expected)
                    self.assertFalse(dev.evaluate('$("aside").classList.contains("open")'))
                    if expected=='more':
                        self.assertTrue(dev.evaluate('!$("moreView").classList.contains("hidden") && $("menu").classList.contains("hidden")'))

            def feed_swipe(dx):
                point=dev.evaluate('(()=>{$("scroller").scrollTop=$("scroller").scrollHeight;const r=$("scroller").getBoundingClientRect();return {y:r.bottom-8}})()')
                swipe(280 if dx<0 else 100,point['y'],dx)

            for _ in range(3):
                feed_swipe(-85)
                section(order[2])
                swipe(280, 600, -85, 10)
                section(order[3])
                swipe(280, 260, -85, 10)
                section(order[4])
                swipe(100, 260, 85, 10)
                section(order[3])
                swipe(100, 260, 85, 10)
                section(order[2])
                swipe(100, 600, 85, 10)
                section(order[1])
                wait('MSGS.has('+str(mid)+')')
            point = dev.evaluate('(()=>{const link=$("m'+str(mid)+'").querySelector(".mtext a");link.scrollIntoView({block:"center"});const r=link.getClientRects()[0];return {x:Math.min(280,r.left+180),y:r.top+8}})()')
            swipe(point['x'], point['y'], -85)
            wait('REPLY?.id==='+str(mid))
            self.assertEqual(dev.evaluate('TAB'),'feed','message swipe navigated instead of replying')
            self.assertTrue(dev.evaluate('document.activeElement===$("cBody")'),'reply swipe did not focus composer')
            self.assertEqual(dev.evaluate('$("m'+str(mid)+'").style.transform'),'','reply swipe left message displaced')
            dev.evaluate('$("cancelReply").click();$("cBody").blur()')
            feed_swipe(-85)
            section('users')
            swipe(280, 600, -85)
            section('direct')
            # A tap still opens the row, while a swipe over that same button navigates.
            point = dev.evaluate('(()=>{const b=$("dmList").querySelector("button");const r=b.getBoundingClientRect();return {x:r.left+80,y:r.top+r.height/2}})()')
            dev.call('Input.dispatchTouchEvent', type='touchStart', touchPoints=[dict(point, id=1)])
            dev.call('Input.dispatchTouchEvent', type='touchEnd', touchPoints=[])
            wait('!!ACTIVE_DM')
            dm_mid=dev.evaluate('(async()=>{const id=(await api("POST","/api/messages",{body:"Private swipe fixture",conversation_id:ACTIVE_DM.id})).id;await refresh();return id})()')
            point=dev.evaluate('(()=>{const r=$("m'+str(dm_mid)+'").querySelector(".mtext").getBoundingClientRect();return {x:r.left+120,y:r.top+r.height/2}})()')
            swipe(point['x'],point['y'],-85)
            wait('REPLY?.id==='+str(dm_mid))
            self.assertTrue(dev.evaluate('!!ACTIVE_DM'),'private reply swipe left its conversation')
            dev.evaluate('$("cancelReply").click();$("cBody").blur()')
            dev.call('Input.dispatchTouchEvent',type='touchStart',touchPoints=[dict(point,id=1)])
            dev.call('Input.dispatchTouchEvent',type='touchMove',touchPoints=[{'x':point['x']-30,'y':point['y'],'id':1}])
            dev.call('Input.dispatchTouchEvent',type='touchCancel',touchPoints=[])
            self.assertTrue(dev.evaluate('!REPLY && !NAV_TOUCH && !$("m'+str(dm_mid)+'").style.transform'),'cancelled swipe started a reply or left movement behind')
            swipe(100, 490, 85)
            wait('TAB==="direct" && !ACTIVE_DM')
            swipe(100, 260, 85)
            section('users')
            swipe(100, 600, 85)
            wait('TAB==="feed" && MSGS.has('+str(mid)+')')
            dev.evaluate('$("scroller").scrollTop=0')
            feed_swipe(85)
            wait('$("projects").classList.contains("open")')
            point = dev.evaluate('(()=>{const r=$("projects").querySelector(".pselect").getBoundingClientRect();return {x:280,y:r.top+r.height/2}})()')
            self.assertGreater(point['y'], 844/2, 'project choices are outside the lower thumb area')
            artifacts = os.environ.get('TELEGENT_TEST_ARTIFACTS')
            if artifacts:
                dest = Path(artifacts); dest.mkdir(parents=True, exist_ok=True)
                shot = dev.call('Page.captureScreenshot', format='png')['data']
                (dest/'chromium-projects-sheet.png').write_bytes(base64.b64decode(shot))
            swipe(point['x'], point['y'], -85)
            wait('!$("projects").classList.contains("open")')
            feed_swipe(85)
            wait('$("projects").classList.contains("open")')
            point=dev.evaluate('(()=>{const r=$("projectGrab").getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2}})()')
            swipe(point['x'],point['y'],0,85)
            wait('!$("projects").classList.contains("open")')
            self.assertEqual(dev.evaluate('$("projects").style.transform'), '')
            feed_swipe(85)
            wait('$("projects").classList.contains("open")')
            point=dev.evaluate('(()=>{const r=$("projects").querySelector(".pselect").getBoundingClientRect();return {x:r.left+100,y:r.top+r.height/2}})()')
            swipe(point['x'],point['y'],0,20)
            self.assertTrue(dev.evaluate('$("projects").classList.contains("open")'), 'short pull became a project tap')
            swipe(point['x'],point['y'],0,70)
            wait('!$("projects").classList.contains("open")')
            # Hold and drag uses native touch, without dismissing or selecting the sheet.
            feed_swipe(85);wait('$("projects").classList.contains("open")')
            first=dev.evaluate('PROJECTS[0].id')
            point=dev.evaluate('(()=>{const r=document.querySelector("#projList .pselect").getBoundingClientRect();return {x:r.left+85,y:r.top+r.height/2}})()')
            target=dev.evaluate('(()=>{const rows=$("projList").querySelectorAll(".pitem");return rows[rows.length-1].getBoundingClientRect().bottom-2})()')
            dev.call('Input.dispatchTouchEvent',type='touchStart',touchPoints=[dict(point,id=1)])
            time.sleep(.55)
            self.assertTrue(dev.evaluate('!!document.querySelector(".project-insert")'),'hold did not show insertion line')
            for step in range(1,9):
                dev.call('Input.dispatchTouchEvent',type='touchMove',touchPoints=[{'x':point['x'],'y':point['y']+(target-point['y'])*step/8,'id':1}]);time.sleep(.025)
            dev.call('Input.dispatchTouchEvent',type='touchEnd',touchPoints=[])
            wait('!PROJECT_ORDER_SAVING && PROJECTS.at(-1).id==='+str(first))
            self.assertTrue(dev.evaluate('$("projects").classList.contains("open") && PROJECT==="main"'))
            self.assertEqual(dev.evaluate('(async()=> (await api("GET","/api/projects")).projects.at(-1).id)()'),first)
            saved=dev.evaluate('PROJECTS.map(p=>p.id)')
            point=dev.evaluate('(()=>{const r=document.querySelector("#projList .pselect").getBoundingClientRect();return {x:r.left+85,y:r.top+r.height/2}})()')
            dev.call('Input.dispatchTouchEvent',type='touchStart',touchPoints=[dict(point,id=1)]);time.sleep(.55)
            self.assertTrue(dev.evaluate('!!PROJECT_DRAG?.active'))
            dev.call('Input.dispatchTouchEvent',type='touchCancel',touchPoints=[])
            wait('!PROJECT_DRAG')
            self.assertEqual(dev.evaluate('PROJECTS.map(p=>p.id)'),saved,'cancelled drag changed order')
            dev.evaluate('closeMobilePanels()')
            dev.evaluate('$("scroller").scrollTop=0')
            swipe(200, 490, 8, -230)
            self.assertGreater(dev.evaluate('$("scroller").scrollTop'), 30, 'native vertical scrolling stopped '+json.dumps(dev.evaluate('({height:$("scroller").clientHeight,scroll:$("scroller").scrollHeight,target:document.elementFromPoint(200,490)?.outerHTML?.slice(0,100),events:fixtureTouches})')))
            self.assertEqual(dev.evaluate('TAB'), 'feed')
            self.assertEqual(dev.evaluate('$("scroller").scrollLeft'), 0, 'feed moved horizontally')
            # A nested code scroller must retain native horizontal movement.
            point = dev.evaluate('(()=>{const pre=$("m'+str(mid)+'").querySelector("pre");pre.scrollIntoView({block:"center"});const r=pre.getBoundingClientRect();return {x:r.left+160,y:r.top+r.height/2}})()')
            swipe(point['x'], point['y'], -100)
            self.assertGreater(dev.evaluate('$("m'+str(mid)+'").querySelector("pre").scrollLeft'), 20, 'native code scrolling stopped')
            self.assertEqual(dev.evaluate('TAB'), 'feed', 'code gesture navigated away')
            self.assertEqual(dev.evaluate('$("scroller").scrollLeft'), 0)
            def tap(selector):
                point=dev.evaluate('(()=>{const r=document.querySelector('+json.dumps(selector)+').getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2}})()')
                dev.call('Input.dispatchTouchEvent',type='touchStart',touchPoints=[dict(point,id=1)])
                dev.call('Input.dispatchTouchEvent',type='touchEnd',touchPoints=[])

            # Native buttons must activate before a keyboard blur can move their target.
            dev.evaluate('$("cBody").focus();window.fixtureButtonBlur=0;$("cBody").addEventListener("blur",()=>fixtureButtonBlur++)')
            tap('#cOptsBtn');wait('!$("cOpts").classList.contains("hidden")')
            self.assertEqual(dev.evaluate('fixtureButtonBlur'),0)
            tap('#cOptsBtn');wait('$("cOpts").classList.contains("hidden")')
            dev.call('Page.setInterceptFileChooserDialog',enabled=True)
            tap('#attachBtn')
            dev.evaluate('true')
            self.assertTrue(any(e['method']=='Page.fileChooserOpened' for e in dev.events),'first touch did not open native file chooser')
            self.assertEqual(dev.evaluate('fixtureButtonBlur'),0)
            dev.call('Page.setInterceptFileChooserDialog',enabled=False)
            dev.evaluate('startReply(MSGS.get('+str(mid)+'))')
            tap('#cancelReply');wait('!REPLY')
            self.assertEqual(dev.evaluate('fixtureButtonBlur'),0)
            dev.evaluate('(async()=>{await addFiles([new File(["fixture"],"fixture.txt")]);$("cBody").focus()})()')
            tap('#cPending .file-remove');wait('ATTACHED.length===0')
            self.assertEqual(dev.evaluate('fixtureButtonBlur'),0)
            dev.evaluate('$("cTopic").value=""')
            # Native tap with a focused composer must send before losing keyboard focus.
            dev.evaluate('window.fixtureApi=api;api=async(...args)=>{if(args[0]==="POST"&&args[1]==="/api/messages")await new Promise(r=>setTimeout(r,700));return fixtureApi(...args)};$("cBody").value="single tap fixture";$("cBody").focus();window.fixtureBlur=[];$("cBody").addEventListener("blur",()=>fixtureBlur.push(SENDING))')
            point=dev.evaluate('(()=>{const r=$("sendBtn").getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2}})()')
            dev.call('Input.dispatchTouchEvent', type='touchStart', touchPoints=[dict(point,id=1)])
            dev.call('Input.dispatchTouchEvent', type='touchEnd', touchPoints=[])
            wait('!!$("pendingOutgoing").querySelector(".receipt-spinner")')
            self.assertEqual(dev.evaluate('$("pendingOutgoing").querySelectorAll("svg path").length'),0,'sending showed a delivery tick')
            wait('!SENDING && [...MSGS.values()].some(m=>m.body==="single tap fixture")')
            self.assertEqual(dev.evaluate('$("pendingOutgoing").childElementCount'),0,'pending copy remained after delivery')
            self.assertEqual(dev.evaluate('[...MSGS.values()].filter(m=>m.body==="single tap fixture").length'),1)
            self.assertFalse(dev.evaluate('fixtureBlur.includes(false)'), 'tap blurred the keyboard before sending')
            artifacts = os.environ.get('TELEGENT_TEST_ARTIFACTS')
            if artifacts:
                dest = Path(artifacts); dest.mkdir(parents=True, exist_ok=True)
                shot = dev.call('Page.captureScreenshot', format='png')['data']
                (dest/'chromium-mobile-touch.png').write_bytes(base64.b64decode(shot))
        finally:
            if dev: dev.close()
            if browser:
                browser.terminate(); browser.wait(timeout=10)
            fixture.AttachmentTest.tearDownClass()


if __name__ == '__main__': unittest.main(verbosity=2)
