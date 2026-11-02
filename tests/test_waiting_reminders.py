"""Time-based waiting reminders: threshold, throttle and metadata only."""
import importlib.util
import time
import types
import unittest
from unittest import mock

import test_attachments as fixture

spec = importlib.util.spec_from_file_location('waiting_reminders_tg', fixture.ROOT / 'tg.py')
tg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tg)


class ReminderTest(unittest.TestCase):
    def conf(self):
        return types.SimpleNamespace(
            remind_after_minutes=60,
            remind_interval_minutes=60,
            save_state=lambda state: self.saved.append(dict(state)),
        )

    def summary(self):
        return {
            'waiting_me': {
                'count': 1, 'oldest_age': 3600,
                'items': [{'id': 42, 'type': 'question', 'author': 'alice-agent'}],
            },
            'taken_open': {'count': 0, 'ids': []},
        }

    def test_threshold_throttle_and_no_message_text(self):
        conf = self.conf()
        state = {}
        self.saved = []
        with mock.patch.object(tg, 'api_retry', return_value=self.summary()) as api:
            lines = tg.reminder_lines(conf, state)
            self.assertEqual(len(lines), 1)
            self.assertIn('#42 question от alice-agent', lines[0])
            self.assertIn('напоминание:', lines[0])
            self.assertNotIn('PEER_TEXT', lines[0])
            tg.mark_reminded(conf, state)
            self.assertEqual(tg.reminder_lines(conf, state), [])
            self.assertEqual(api.call_count, 1)
        self.assertTrue(state['waiting_reminder_at'] > 0)
        self.assertEqual(self.saved, [state])

    def test_disabled_and_before_threshold(self):
        conf = self.conf()
        conf.remind_after_minutes = 0
        with mock.patch.object(tg, 'api_retry') as api:
            self.assertEqual(tg.reminder_lines(conf, {}), [])
            api.assert_not_called()
        conf.remind_after_minutes = 120
        with mock.patch.object(tg, 'api_retry', return_value=self.summary()) as api:
            self.assertEqual(tg.reminder_lines(conf, {}), [])
            self.assertEqual(api.call_count, 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
