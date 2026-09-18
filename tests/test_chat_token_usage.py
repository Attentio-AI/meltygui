import json
import tempfile
import unittest
from pathlib import Path
from meltygui.chat.claude_code import ClaudeCodeChats
from meltygui.chat.codex_proxy import CodexChats
from meltygui.chat.codex_settings import ThreadUsageReader as UsageReader
from meltygui.model.chat_model import token_badge as badge, GREEN, YELLOW, RED, MUTED
from meltygui.chat.chat_proxy import Chat


class TokenUsageTests(unittest.TestCase):
    def test_context_thresholds_and_unknown(self):
        chat = Chat({'context_tokens': 499, 'context_window': 1000})
        self.assertEqual(badge(chat)[1], GREEN)
        chat['context_tokens'] = 500
        self.assertEqual(badge(chat)[1], YELLOW)
        chat['context_tokens'] = 800
        self.assertEqual(badge(chat)[1], RED)
        self.assertEqual(badge(Chat(remote_id='unloaded'))[1], MUTED)
        self.assertEqual(badge({'context_tokens': 160000})[1], RED)

    def test_saved_usage_is_latest_context_not_cumulative_total(self):
        def row(count):
            return json.dumps({'type': 'event_msg', 'payload': {'type': 'token_count',
                'info': {'last_token_usage': {'total_tokens': count},
                         'total_token_usage': {'total_tokens': 900000},
                         'model_context_window': 200000}}}).encode() + b'\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.jsonl'
            path.write_bytes(row(120000))
            reader = UsageReader()
            self.assertEqual(reader.read(path)['context_tokens'], 120000)
            with path.open('ab') as handle:
                handle.write(row(20000))  # compaction can reduce context
            self.assertEqual(reader.read(path)['context_tokens'], 20000)
            with path.open('ab') as handle:
                handle.write(b'{"partial":')
            self.assertEqual(reader.read(path)['context_tokens'], 20000)

    def test_live_codex_update_matches_only_its_thread(self):
        proxy = CodexChats.__new__(CodexChats)   # only `known` is read
        chat = Chat(remote_id='one')
        other = Chat(remote_id='two')
        proxy.known = {'a': chat, 'b': other}
        proxy._event({'method': 'thread/tokenUsage/updated', 'params': {
            'threadId': 'one', 'tokenUsage': {'last': {'totalTokens': 12345},
                'total': {'totalTokens': 500000}, 'modelContextWindow': 100000}}})
        self.assertEqual(chat['context_tokens'], 12345)
        self.assertNotIn('context_tokens', other)

    def test_claude_includes_cached_input(self):
        chat = {}
        ClaudeCodeChats._usage(chat, {'input_tokens': 100,
            'cache_read_input_tokens': 20000, 'cache_creation_input_tokens': 1000,
            'output_tokens': 500})
        self.assertEqual(chat['context_tokens'], 21600)


if __name__ == '__main__':
    unittest.main()
