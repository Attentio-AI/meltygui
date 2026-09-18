import subprocess
import threading
import unittest
from unittest.mock import Mock, patch

from meltygui.chat.auto_titles import AutoTitles, generate_title
from meltygui.chat.chat_service import apply_edit
import test_chat_service as test_persistent_chats
from meltygui.chat.chat_proxy import Chat


class TitleTests(unittest.TestCase):
    def setUp(self):
        self.lock = threading.RLock()
        self.chat = Chat()
        self.chat.metadata = {}
        self.rows = {'chat': self.chat}
        # AutoTitles deliberately uses dict.get to avoid hydration on the worker.
        class Proxy(dict):
            revision = 0
            reconcile = Mock()
        self.proxy = Proxy(self.rows)
        self.namer = AutoTitles(self.lock, lambda prompt: 'editor.py, missing labels')

    def finish(self):
        self.chat.metadata['auto_title'] = 'retry'
        self.namer.finish(self.proxy, 'chat', self.chat, 'Fix editor.py labels')

    def test_names_and_reconciles(self):
        self.finish()
        self.assertEqual(self.chat['title'], 'editor.py, missing labels')
        self.assertEqual(self.chat.metadata['auto_title'], 'done')
        self.assertEqual(self.proxy.revision, 1)

    def test_manual_rename_wins_during_generation(self):
        def generate(prompt):
            apply_edit(self.proxy, ('title', 'chat', 'My title'))
            return 'generated title'
        self.namer.generate = generate
        self.finish()
        self.assertEqual(self.chat['title'], 'My title')
        self.assertEqual(self.chat.metadata['auto_title'], 'manual')

    def test_deleted_chat_is_not_renamed(self):
        self.proxy.clear()
        self.finish()
        self.assertEqual(self.chat['title'], 'New conversation')

    def test_failed_generation_retries_only_on_next_send(self):
        self.namer.generate = lambda prompt: None
        self.finish()
        self.assertEqual(self.chat.metadata['auto_title'], 'retry')
        self.assertFalse(self.namer.pending)
        with patch('meltygui.chat.auto_titles.threading.Thread') as worker:
            self.namer.request(self.proxy, 'chat', 'Fix labels')
            self.namer.request(self.proxy, 'chat', 'Fix labels again')
            self.assertEqual(worker.call_count, 1)

    def test_existing_titles_are_preserved(self):
        self.chat['title'] = 'Existing title'
        with patch('meltygui.chat.auto_titles.threading.Thread') as worker:
            self.namer.request(self.proxy, 'chat', 'Fix labels')
            worker.assert_not_called()

    @patch('meltygui.chat.auto_titles.subprocess.run')
    def test_cli_validation_and_isolation(self, run):
        for output, expected in [('editor.py, draw, missing labels\n', 'editor.py, draw, missing labels'),
                                 ('NONE\n', None), ('', None), ('invalid: title', None), ('x' * 65, None)]:
            run.return_value = subprocess.CompletedProcess([], 0, output, '')
            self.assertEqual(generate_title('x' * 500), expected)
        args, kwargs = run.call_args
        self.assertIn('--no-session-persistence', args[0])
        self.assertEqual(kwargs['cwd'], '/')
        self.assertNotIn('TMUX_PANE', kwargs['env'])
        self.assertTrue(kwargs['input'].endswith('Request: ' + 'x' * 400))
        run.side_effect = subprocess.TimeoutExpired('claude', 60)
        self.assertIsNone(generate_title('Fix labels'))


class TitleServiceTests(unittest.TestCase):
    setUp = test_persistent_chats.ServiceTests.setUp
    exchange = test_persistent_chats.ServiceTests.exchange

    def test_send_starts_naming_once_and_reconnect_receives_title(self):
        self.chat['title'] = self.chat.saved_title = 'New conversation'
        from meltygui.chat.messages import user_message
        commands = [(1, ('user', 'chat', ('prompt', user_message('Fix labels'))))]
        with patch('meltygui.chat.auto_titles.threading.Thread') as worker:
            self.exchange(commands)
            self.exchange(commands)
            self.assertEqual(worker.call_count, 1)
            self.assertEqual(len([op for op, _ in self.proxy.operations if op == 'send']), 1)
        self.service.auto_titles.generate = lambda prompt: 'missing labels'
        self.service.auto_titles.finish(self.proxy, 'chat', self.chat, 'Fix labels')
        self.assertIn(('rename', ('chat', 'remote', 'missing labels')), self.proxy.operations)
        snapshot = self.exchange(client='reopened', seen={})
        self.assertEqual(snapshot['chats']['chat'][0]['title'], 'missing labels')
