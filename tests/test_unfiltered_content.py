"""Transport accepts literal message/file contents, including legacy deny-list configurations."""
import base64
import json
import unittest

import test_attachments as fixture


class UnfilteredContentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.AttachmentTest.setUpClass();cls.fx=fixture.AttachmentTest();cls.s=cls.fx.s

    @classmethod
    def tearDownClass(cls):fixture.AttachmentTest.tearDownClass()

    def test_existing_filter_configuration_is_ignored_without_reading_files(self):
        server_config=self.fx.work/'legacy-server.json'
        server_config.write_text(json.dumps({'deny_file':str(self.fx.work/'missing-deny.txt')}))
        self.s.load_config(str(server_config))
        config=json.loads(self.fx.config.read_text())
        config['secret_files']=[{'path':str(self.fx.work/'missing-private.txt')}]
        self.fx.config.write_text(json.dumps(config))
        body='token=fixture_value; password=fixture_password'
        result=self.fx.cli('send',body)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertNotIn('secret_files',result.stderr)
        mid=int(result.stdout.split()[0][1:])
        self.assertEqual(self.fx.api('GET',f'/api/messages/{mid}')['message']['body'],body)

    def test_agent_file_upload_and_download_preserve_pattern_like_content(self):
        raw=b'-----BEGIN PRIVATE KEY-----\n' + b'fixture, not a credential\npassword=example_value\n'
        source=self.fx.work/'token=example_value.txt';source.write_bytes(raw)
        result=self.fx.cli('send','-a',str(source))
        self.assertEqual(result.returncode,0,result.stderr)
        mid=int(result.stdout.split()[0][1:]);out=self.fx.work/'unfiltered-download'
        result=self.fx.cli('get',str(mid),'--out',str(out))
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual((out/source.name).read_bytes(),raw)

    def test_inline_files_links_and_structured_message_fields_are_not_filtered(self):
        body='password=example_value';raw=b'api_key=fixture_value'
        result=self.fx.api('POST','/api/messages',{'body':body,'topic':'token=example',
             'result':{'note':'password=example_value'},'attachments':[
                 {'name':'password=example.txt','content_b64':base64.b64encode(raw).decode()},
                 {'name':'link.txt','link':'https://example.invalid/?token=example_value'}]})
        message=self.fx.api('GET',f"/api/messages/{result['id']}")['message']
        self.assertEqual(message['body'],body);self.assertEqual(message['topic'],'token=example')
        with self.fx.request('GET',f"/api/attachments/{message['attachments'][0]['id']}") as response:
            self.assertEqual(response.read(),raw)
        self.assertEqual(message['attachments'][1]['link'],'https://example.invalid/?token=example_value')


if __name__=='__main__':unittest.main(verbosity=2)
