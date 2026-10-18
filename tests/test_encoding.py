"""Real HTTP/CLI encoding tests, including native Windows PowerShell byte replacement."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import urllib.error

import test_attachments as fixture

ROOT=Path(__file__).resolve().parents[1]


class EncodingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.AttachmentTest.setUpClass();cls.fx=fixture.AttachmentTest()

    @classmethod
    def tearDownClass(cls):fixture.AttachmentTest.tearDownClass()

    def cli(self,*args,raw=None,env=None):
        return subprocess.run([sys.executable,str(ROOT/'tg.py'),'--config',str(self.fx.config),*args],
                              input=raw,capture_output=True,env=env or self.fx.env,timeout=20)

    def stored(self,result):
        self.assertEqual(result.returncode,0,result.stderr.decode('utf-8','replace'))
        mid=int(result.stdout.decode('utf-8').split()[0][1:])
        return self.fx.api('GET',f'/api/messages/{mid}')['message']['body']

    def test_unicode_boms_are_automatic_for_files_and_stdin(self):
        text='Отчёт: всё готово. 日本語 中文 😀 — 12 ± 2\nВторая строка.'
        for encoding in ('utf-8','utf-8-sig','utf-16','utf-32'):
            for source in ('file','stdin'):
                with self.subTest(encoding=encoding,source=source):
                    raw=text.encode(encoding)
                    if source=='file':
                        path=self.fx.work/'message.txt';path.write_bytes(raw)
                        result=self.cli('send','-f',str(path))
                    else:result=self.cli('send','-f','-',raw=raw)
                    self.assertEqual(self.stored(result),text)
        for bom,encoding in ((b'\xfe\xff','utf-16-be'),(b'\xff\xfe','utf-16-le'),(b'\x00\x00\xfe\xff','utf-32-be'),(b'\xff\xfe\x00\x00','utf-32-le')):
            self.assertEqual(self.stored(self.cli('send','-f','-',raw=bom+text.encode(encoding))),text)

    def test_explicit_legacy_and_bomless_unicode_encodings(self):
        for encoding,text in [('cp1251','Русский текст: проверка файла'),('cp866','Русский текст: проверка stdin'),
                              ('koi8-r','Русский текст без потерь'),('cp1252','Résumé déjà vu — café'),
                              ('latin-1','Résumé café'),('shift_jis','日本語のメッセージ'),('gb18030','中文消息'),
                              ('utf-16-le','Отчёт готов 😀'),('utf-16-be','Отчёт готов 😀'),('utf-32-le','Отчёт готов 😀'),('utf-32-be','Отчёт готов 😀')]:
            with self.subTest(encoding=encoding):
                raw=text.encode(encoding)
                self.assertEqual(self.stored(self.cli('send','-f','-','--encoding',encoding,raw=raw)),text)
                path=self.fx.work/'legacy.txt';path.write_bytes(raw)
                self.assertEqual(self.stored(self.cli('send','-f',str(path),'--encoding',encoding)),text)

    def test_stdin_decoding_ignores_an_incorrect_python_console_encoding(self):
        text='Русский текст в UTF-8 😀';env=dict(self.fx.env,PYTHONIOENCODING='cp1251')
        self.assertEqual(self.stored(self.cli('send','-f','-',raw=text.encode(),env=env)),text)

    def test_unknown_or_ambiguous_encoding_fails_before_any_send(self):
        before=self.fx.api('GET','/api/messages')['last_id']
        for encoding,raw in [('auto','Русский'.encode('cp1251')),('utf-8',b'\xff'),('not-a-codec',b'fixture'),('base64_codec',b'YQ==')]:
            result=self.cli('send','-f','-','--encoding',encoding,raw=raw)
            self.assertEqual(result.returncode,2);self.assertIn('--encoding',result.stderr.decode('utf-8'))
        self.assertEqual(self.fx.api('GET','/api/messages')['last_id'],before)

    def test_damaged_text_is_refused_but_question_marks_and_unicode_are_valid(self):
        before=self.fx.api('GET','/api/messages')['last_id']
        for text in ('??????','?????? ???????? ???????? report 17','bad '+chr(0xfffd)*4,'null\x00text'):
            result=self.cli('send','-f','-',raw=text.encode())
            self.assertEqual(result.returncode,2,result.stderr)
        self.assertEqual(self.fx.api('GET','/api/messages')['last_id'],before)
        for text in ('Почему? Как? Серьёзно???','What??? Why???','a ? b : c; x ? y : z','Unicode: Привет 😀 日本語'):
            self.assertEqual(self.stored(self.cli('send','-f','-',raw=text.encode())),text)
        text='????????????';self.assertEqual(self.stored(self.cli('send',text,'--allow-lossy-text')),text)

    def test_old_agent_clients_are_protected_and_override_keeps_secret_filter(self):
        text='?????? ???????? ???????? report 17'
        with self.assertRaises(urllib.error.HTTPError) as raised:self.fx.api('POST','/api/messages',{'body':text})
        self.assertEqual(raised.exception.code,422);raised.exception.close()
        for flag in ('true',1):
            with self.assertRaises(urllib.error.HTTPError) as raised:self.fx.api('POST','/api/messages',{'body':text,'allow_lossy_text':flag})
            self.assertEqual(raised.exception.code,422);raised.exception.close()
        self.fx.api('POST','/api/messages',{'body':text,'allow_lossy_text':True})
        self.fx.api('POST','/api/messages',{'body':text},who='alice')
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.fx.api('POST','/api/messages',{'body':text+' ghp_'+'x'*36,'allow_lossy_text':True})
        self.assertEqual(raised.exception.code,422);raised.exception.close()

    def test_reply_and_direct_messages_use_the_same_decoding(self):
        mid=self.fx.api('POST','/api/messages',{'body':'fixture question','to':['bob-agent']},who='alice')['id']
        text='Личный ответ без потери кодировки'
        result=self.cli('reply',str(mid),'-f','-','--encoding','cp1251',raw=text.encode('cp1251'))
        self.assertEqual(self.stored(result),text)
        result=self.cli('dm','--to','alice','-f','-','--encoding','cp866',raw=text.encode('cp866'))
        self.assertEqual(self.stored(result),text)

    @unittest.skipUnless(os.name=='nt' and shutil.which('powershell.exe'),'Windows PowerShell is required')
    def test_actual_powershell_ascii_is_blocked_and_utf8_preserves_text(self):
        text='Русский отчёт: всё готово, проверка текста.'
        def run(encoding):
            quote=lambda x:"'"+str(x).replace("'","''")+"'"
            script='$OutputEncoding='+encoding+'; [Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false); '+quote(text)+' | & '+quote(sys.executable)+' '+quote(ROOT/'tg.py')+' --config '+quote(self.fx.config)+" send -f -; exit $LASTEXITCODE"
            encoded=base64.b64encode(script.encode('utf-16-le')).decode()
            return subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',encoded],capture_output=True,env=self.fx.env,timeout=25)
        before=self.fx.api('GET','/api/messages')['last_id']
        result=run('[Text.Encoding]::ASCII');self.assertEqual(result.returncode,2,result.stderr)
        self.assertEqual(self.fx.api('GET','/api/messages')['last_id'],before)
        result=run('(New-Object System.Text.UTF8Encoding($false))');self.assertEqual(self.stored(result).rstrip('\n'),text)

    def test_client_and_server_quality_checks_stay_identical(self):
        spec=importlib.util.spec_from_file_location('encoding_client',ROOT/'tg.py');tg=importlib.util.module_from_spec(spec);spec.loader.exec_module(tg)
        import inspect
        self.assertEqual(inspect.getsource(tg.text_encoding_problem),inspect.getsource(self.fx.s.text_encoding_problem))


if __name__=='__main__':unittest.main(verbosity=2)
