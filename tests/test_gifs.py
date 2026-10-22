"""GIF originals stay ordinary attachments for agents; no automatic media reads."""
import hashlib
import json
import re
import struct
import unittest
from unittest import mock

import test_attachments as fixture


def animated_gif():
    width, height = 40, 24
    data = b'GIF89a' + struct.pack('<HH', width, height) + b'\x80\0\0\xff\0\0\0\0\xff'
    data += b'\x21\xff\x0bNETSCAPE2.0\x03\x01\0\0\0'
    for color in (0, 1):
        # Frequent clear codes keep the test encoder at three bits without dependencies.
        codes = [code for _ in range(width * height) for code in (4, color)] + [5]
        packed = bytearray((len(codes) * 3 + 7) // 8)
        for index, code in enumerate(codes):
            for bit in range(3):
                if code & (1 << bit):
                    pos = index * 3 + bit
                    packed[pos // 8] |= 1 << (pos % 8)
        data += b'\x21\xf9\x04\x04\x14\0\0\0\x2c\0\0\0\0' + struct.pack('<HH', width, height) + b'\0\x02'
        for offset in range(0, len(packed), 255):
            block = packed[offset:offset + 255]
            data += bytes([len(block)]) + block
        data += b'\0'
    return data + b'\x3b'


class GifClientTest(unittest.TestCase):
    def test_cli_labels_but_never_downloads_or_decodes_until_requested(self):
        fixture.AttachmentTest.setUpClass()
        fx = fixture.AttachmentTest()
        try:
            config = json.loads(fx.config.read_text())
            config['auto_update'] = False
            fx.config.write_text(json.dumps(config))
            raw = animated_gif()
            path = fx.work / 'animation.GIF'
            path.write_bytes(raw)
            sent = fx.cli('send', 'Caption remains a normal instruction', '-a', str(path))
            self.assertEqual(sent.returncode, 0, sent.stderr)
            mid = int(re.search(r'#(\d+)', sent.stdout)[1])
            # Address a second animation to the agent so inbox uses the same marker.
            upload = fx.upload(raw, name='reaction.gif', who='alice')
            incoming = fx.send(upload, who='alice')['id']
            with mock.patch.object(fx.s, 'stream_attachment', wraps=fx.s.stream_attachment) as stream:
                for command in (('inbox',), ('read', str(incoming)), ('read', str(mid))):
                    result = fx.cli(*command)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('GIF (просмотр необязателен)', result.stdout)
                    if command[0] == 'read':
                        self.assertIn('GIF для общения людей', result.stdout)
                self.assertEqual(stream.call_count, 0, 'Reading metadata must not fetch the binary')
                downloaded = fx.cli('get', str(incoming), '--out', str(fx.work / 'downloads'))
                self.assertEqual(downloaded.returncode, 0, downloaded.stderr)
                self.assertEqual(stream.call_count, 1)
            self.assertEqual((fx.work / 'downloads/reaction.gif').read_bytes(), raw)
            message = fx.api('GET', f'/api/messages/{incoming}')['message']
            self.assertEqual(message['attachments'][0]['md5'], hashlib.md5(raw).hexdigest())
            fx.error(404, 'GET', f"/api/attachments/{message['attachments'][0]['id']}", who='carol')
        finally:
            fixture.AttachmentTest.tearDownClass()


if __name__ == '__main__':
    unittest.main(verbosity=2)
