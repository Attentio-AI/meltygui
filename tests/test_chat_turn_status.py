import unittest
from meltygui.model.chat_model import is_active as is_working, turn_status
from meltygui.chat.chat_proxy import Chat


class TurnStatusTests(unittest.TestCase):
    def test_completion_does_not_wait_for_recent_activity_timeout(self):
        chat = Chat({'running': True, 'updated': 10**12,
                     'messages': {'reply': {'role': 'assistant', 'status': 'completed'}}})
        self.assertEqual(turn_status(chat)[0], 'Working')
        chat['running'] = False
        self.assertFalse(is_working(chat))
        self.assertEqual(turn_status(chat)[1], 'Your turn · Response finished')

    def test_request_overrides_running_and_clears_when_answered(self):
        chat = Chat({'running': True, 'requests': {'q': {'kind': 'input'}}})
        self.assertEqual(turn_status(chat)[0], 'Reply needed')
        chat['requests']['q']['answer'] = {}
        self.assertEqual(turn_status(chat)[0], 'Working')

    def test_approval_is_distinct_from_question(self):
        chat = Chat({'requests': {'a': {'kind': 'approval'}}})
        self.assertEqual(turn_status(chat)[0], 'Approval needed')
        chat['requests']['q'] = {'kind': 'input'}
        self.assertEqual(turn_status(chat)[0], 'Reply needed')

    def test_idle_is_not_always_success(self):
        chat = Chat({'messages': {'reply': {'role': 'assistant', 'status': 'running'}}})
        self.assertNotIn('finished', turn_status(chat)[1])
        chat.error = 'Connection lost'
        self.assertEqual(turn_status(chat)[0], 'Error')

    def test_inflight_external_and_queued_work(self):
        chat = Chat()
        chat.inflight.add('send')
        self.assertTrue(is_working(chat))
        chat.inflight.clear()
        chat.turn_id = 'turn'
        self.assertTrue(is_working(chat))
        chat.turn_id = None
        chat['external_busy'] = True
        self.assertIn('another session', turn_status(chat)[1])
        chat['external_busy'] = False
        chat['queued_messages'] = {'q': {}}
        self.assertEqual(turn_status(chat)[0], 'Queued')

    def test_unloaded_and_empty_chats_do_not_claim_completion(self):
        chat = Chat(remote_id='remote')
        self.assertEqual(turn_status(chat)[0], '')
        chat.loaded = True
        self.assertIn('Send a message', turn_status(chat)[1])


class ChatProjectTests(unittest.TestCase):
    def test_path_gives_its_project_root_and_models_ask_the_application(self):
        import tempfile
        from pathlib import Path
        from meltygui.core.runtime import extensions
        from meltygui.model.chat_model import chat_project
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / 'pyproject.toml').write_text('')
            (root / 'pkg').mkdir()
            self.assertEqual(chat_project(str(root / 'pkg' / 'module.py')), str(root))
        self.assertIsNone(chat_project(None))
        previous = extensions.get('chat_project')
        extensions.register('chat_project', lambda value: value.get('project'))
        try:
            self.assertEqual(chat_project({'project': '/work'}), '/work')
            self.assertIsNone(chat_project({}))
        finally:
            extensions.register('chat_project', previous)


if __name__ == '__main__':
    unittest.main()
