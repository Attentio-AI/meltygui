import json
import tempfile
import unittest
from pathlib import Path

from meltygui.chat.chat_service import ChatService
from test_chat_service import FakeBackend, Mirror
from meltygui.chat.chat_proxy import Chat
from meltygui.chat.codex_proxy import CodexChats
from meltygui.chat.codex_settings import (
    ThreadSettingsReader, effective_settings, fast_service_tier, model_service_tiers)
from meltygui.chat.metadata import ChatMetadata


class OfflineCodex(CodexChats):
    def _work(self):
        pass


class FastTests(unittest.TestCase):
    def setUp(self):
        self.proxy = OfflineCodex('test', ChatMetadata())
        self.proxy.receive('models', [
            {'model': 'future-model', 'serviceTiers': [{'id': 'priority', 'name': 'Fast'}]},
            {'model': 'standard-only', 'serviceTiers': []},
            {'model': 'legacy', 'additionalSpeedTiers': ['fast']}])
        self.proxy['chat'] = Chat({'project': '/tmp'}, remote_id='remote')
        self.chat = dict.get(self.proxy, 'chat')
        self.proxy.known['chat'] = self.chat
        self.chat.loaded = True

    def test_catalog_controls_availability(self):
        self.assertEqual(fast_service_tier(self.proxy, 'future-model'), 'priority')
        self.assertEqual(fast_service_tier(self.proxy, 'legacy'), 'fast')
        self.assertIsNone(fast_service_tier(self.proxy, 'standard-only'))
        self.assertIsNone(fast_service_tier(self.proxy, 'unknown'))
        self.assertEqual(model_service_tiers({'serviceTiers': [], 'additionalSpeedTiers': ['fast']}), ())

    def test_unset_config_inherits_catalog_but_explicit_off_does_not(self):
        settings = effective_settings(self.chat, {'service_tier': None})
        self.assertNotIn('service_tier', settings)
        self.chat.metadata['service_tier'] = None
        self.assertIn('service_tier', effective_settings(self.chat, {'service_tier': None}))

    def test_saved_settings_and_newer_selection(self):
        self.chat.update(codex_settings={'model': 'future-model', 'service_tier': 'priority'}, codex_settings_at=20)
        self.chat.metadata.update(model='standard-only', model_explicit=True, model_selected_at=10,
                                  service_tier=None, service_tier_selected_at=10)
        settings = effective_settings(self.chat, {'model': 'standard-only'})
        self.assertEqual(settings['model'], 'future-model')
        self.assertEqual(settings['service_tier'], 'priority')
        self.chat.metadata['service_tier_selected_at'] = 30
        self.assertIsNone(effective_settings(self.chat, {'service_tier': 'priority'})['service_tier'])

    def test_send_forwards_tier_and_explicit_off(self):
        class Transport:
            def __init__(self):
                self.calls = []
            def request(self, method, params):
                self.calls.append((method, params))
                if method == 'config/read':
                    return {'config': {'model': 'future-model', 'service_tier': 'priority'}}
                if method == 'turn/start':
                    return {'turn': {'id': 'turn'}}
                return {}
        server = Transport()
        self.proxy.writers['remote'] = server
        for tier in ('priority', 'fast', None):
            self.chat.metadata['service_tier'] = tier
            self.proxy.execute('send', 'chat', 'remote', 'message', 'hello', False)
            for method, params in server.calls[-3:]:
                if method in ('thread/resume', 'turn/start'):
                    self.assertIn('serviceTier', params)
                    self.assertEqual(params['serviceTier'], 'priority' if tier == 'fast' else tier)
        self.chat.metadata.update(model='standard-only', model_explicit=True, service_tier='priority')
        self.assertIsNone(self.proxy._thread_options(server, 'chat', '/tmp')['serviceTier'])

    def test_log_initial_incremental_partial_and_rewrite(self):
        def event(tier, timestamp):
            return json.dumps({'type': 'event_msg', 'timestamp': timestamp, 'payload': {
                'type': 'thread_settings_applied', 'thread_settings': {
                    'model': 'future-model', 'service_tier': tier, 'reasoning_effort': 'high'}}}).encode() + b'\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.jsonl'
            reader = ThreadSettingsReader()
            path.write_bytes(event('priority', 10) + json.dumps({'large': 'x' * 140000}).encode() + b'\n')
            state = reader.read(path)
            self.assertEqual(state['codex_settings']['service_tier'], 'priority')
            self.assertIs(reader.read(path), state)
            line = event(None, 20)
            with path.open('ab') as handle:
                handle.write(line[:-1])
            self.assertEqual(reader.read(path), state)
            with path.open('ab') as handle:
                handle.write(b'\n')
            self.assertIsNone(reader.read(path)['codex_settings']['service_tier'])
            path.write_bytes(event('priority', 30))
            self.assertEqual(reader.read(path)['codex_settings_at'], 30)
            history = self.proxy._history({'path': str(path)}, '/tmp')
            self.proxy.receive('hydrated', ('chat', history))
            self.assertEqual(effective_settings(self.chat, {})['service_tier'], 'priority')

    def test_delayed_settings_reply_keeps_newer_user_selection(self):
        self.chat.metadata.update(service_tier=None, service_tier_selected_at=30)
        self.proxy.receive('settings', ('chat', {'model': 'future-model', 'serviceTier': 'priority'}, 20))
        self.assertIsNone(effective_settings(self.chat, {})['service_tier'])
        self.proxy.receive('settings', ('chat', {'serviceTier': None}, 40))
        self.proxy.receive('settings', ('chat', {'serviceTier': 'priority'}, 20))
        self.assertIsNone(effective_settings(self.chat, {})['service_tier'])

    def test_live_settings_notification(self):
        self.proxy.receive('event', {'method': 'thread/settings/updated', 'params': {
            'threadId': 'remote', 'threadSettings': {'model': 'future-model', 'serviceTier': 'priority'}}})
        self.assertEqual(effective_settings(self.chat, {})['service_tier'], 'priority')
        self.proxy.receive('event', {'method': 'thread/settings/updated', 'params': {
            'threadId': 'remote', 'threadSettings': {'serviceTier': None}}})
        self.assertIsNone(effective_settings(self.chat, {})['service_tier'])

    def test_reconnected_running_chat_retains_tier_and_capabilities(self):
        service = ChatService(lambda kind, account, metadata: FakeBackend(account, metadata))
        service.stopped.set()
        service.thread.join()
        hello = {'kind': 'codex', 'account': {'id': 'test'}, 'metadata': {'projects': {}}, 'client': 'one'}
        backend = service.attach(hello)
        backend.model_service_tiers = self.proxy.model_service_tiers
        backend['chat'] = Chat({'project': '/tmp', 'running': True,
            'codex_settings': {'model': 'future-model', 'service_tier': 'priority'}, 'codex_settings_at': 20}, remote_id='remote')
        chat = dict.get(backend, 'chat')
        chat.loaded = True
        chat.turn_id = 'running-turn'
        backend.known['chat'] = chat
        mirror = Mirror('codex', {'id': 'test'}, ChatMetadata())
        mirror.receive('snapshot', service.exchange(backend, 'reopened', [], {}))
        loaded = dict.get(mirror, 'chat')
        self.assertTrue(loaded['running'])
        self.assertEqual(loaded.turn_id, 'running-turn')
        settings = effective_settings(loaded, {})
        self.assertEqual(settings['service_tier'], fast_service_tier(mirror, settings['model']))
        mirror.close()
