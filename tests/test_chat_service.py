import unittest

from meltygui.chat.chat_service import ChatService, PersistentChats, chat_value
from meltygui.chat.chat_proxy import Chat, ChatProxy
from meltygui.chat.messages import user_message, AssistantMessage
from meltygui.chat.metadata import ChatMetadata


class FakeBackend(ChatProxy):
    def __init__(self, account, metadata):
        self.operations = []
        super().__init__(account['id'], metadata)

    def _work(self):
        pass

    def submit(self, operation, *args):
        self.operations.append((operation, args))

    def connect(self):
        pass


class Mirror(PersistentChats):
    def _work(self):
        pass


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = ChatService(lambda kind, account, meta: FakeBackend(account, meta))
        self.service.stopped.set()
        self.service.thread.join()
        self.hello = {'kind': 'codex', 'account': {'id': 'test'},
                      'metadata': {'projects': {}}, 'client': 'one'}
        self.proxy = self.service.attach(self.hello)
        self.proxy['chat'] = Chat({'title': 'Test', 'project': '/tmp'}, remote_id='remote')
        self.chat = dict.get(self.proxy, 'chat')
        self.chat.loaded = True
        self.proxy.known['chat'] = self.chat
        self.seen = {}

    def exchange(self, commands=(), client='one', seen=None):
        return self.service.exchange(self.proxy, client, commands, self.seen if seen is None else seen)

    def test_reconnect_preserves_running_turn_and_typed_messages(self):
        self.chat['running'] = True
        self.chat.turn_id = 'turn-1'
        self.chat['messages']['assistant'] = AssistantMessage(content={'text': 'still working'})
        self.exchange()
        other = self.service.attach({**self.hello, 'client': 'two'})
        self.assertIs(other, self.proxy)
        snapshot = self.exchange(client='two', seen={})
        data, attrs, _ = snapshot['chats']['chat']
        self.assertTrue(data['running'])
        self.assertEqual(attrs['turn_id'], 'turn-1')
        self.assertIsInstance(data['messages']['assistant'], AssistantMessage)
        self.assertFalse(any(op == 'interrupt' for op, args in self.proxy.operations))

    def test_reconnect_recovers_missing_empty_archive_without_stopping_turn(self):
        error = 'no rollout found for thread id remote'
        self.chat.error = self.proxy.error = error
        self.chat.inflight.add('archive')
        self.proxy['active'] = Chat({'project': '/tmp'}, remote_id='active-remote')
        active = dict.get(self.proxy, 'active')
        active.loaded = True
        active['running'] = True
        active.turn_id = 'active-turn'
        active['requests']['approval'] = {'kind': 'approval', 'text': 'May I?'}
        self.proxy.known['active'] = active
        self.exchange()  # Populate the snapshot cache before recovery.

        self.service.attach({**self.hello, 'client': 'reopened'})
        snapshot = self.exchange(seen={})
        self.assertNotIn('chat', snapshot['keys'])
        self.assertNotIn('chat', snapshot['chats'])
        self.assertNotIn('chat', self.proxy.known)
        self.assertIsNone(snapshot['fields']['error'])
        self.assertTrue(active['running'])
        self.assertEqual(active.turn_id, 'active-turn')
        self.assertIn('approval', active['requests'])
        self.assertEqual(self.proxy.operations, [('release', ('remote',))])

    def test_reconnect_does_not_discard_history_or_pending_work(self):
        for field in ('history', 'queue', 'approval', 'running', 'turn', 'unloaded', 'send'):
            with self.subTest(field=field):
                chat = Chat({'project': '/tmp'}, remote_id='missing')
                chat.loaded = field != 'unloaded'
                chat.error = self.proxy.error = 'no rollout found for thread id missing'
                chat.inflight = {'send'} if field == 'send' else {'archive'}
                if field == 'history':
                    chat['messages']['user'] = user_message('Keep this')
                if field == 'queue':
                    chat['queued_messages'] = {'user': user_message('Pending')}
                if field == 'approval':
                    chat['requests']['request'] = {'kind': 'approval'}
                chat['running'] = field == 'running'
                chat.turn_id = 'turn' if field == 'turn' else None
                self.proxy['chat'] = chat
                self.proxy.known['chat'] = chat
                self.service.attach(self.hello)
                self.assertIs(dict.get(self.proxy, 'chat'), chat)
                self.assertEqual(self.proxy.error, chat.error)
        self.assertEqual(self.proxy.operations, [])

    def test_empty_archive_recovery_keeps_unrelated_account_error(self):
        self.chat.error = 'no rollout found for thread id remote'
        self.chat.inflight.add('archive')
        self.proxy.error = 'Connection lost'
        self.service.attach(self.hello)
        self.assertNotIn('chat', self.proxy)
        self.assertEqual(self.proxy.error, 'Connection lost')

    def test_reopened_ui_unblocks_account_with_older_service(self):
        self.chat.error = self.proxy.error = 'no rollout found for thread id remote'
        self.chat.inflight.add('archive')
        mirror = self.mirror()
        self.assertIsNone(mirror.error)
        self.assertEqual(dict.get(mirror, 'chat').error, self.chat.error)
        # An unchanged chat is omitted from subsequent snapshots.
        mirror.receive('snapshot', self.exchange())
        mirror.receive('snapshot', self.exchange())
        self.assertIsNone(mirror.error)
        self.proxy.error = 'Connection lost'
        mirror.receive('snapshot', self.exchange())
        self.assertEqual(mirror.error, 'Connection lost')
        self.assertEqual(mirror.pending, [])

    def test_retried_send_is_not_duplicated(self):
        commands = [(1, ('user', 'chat', ('prompt-id', user_message('hello'))))]
        self.exchange(commands)
        self.exchange(commands)
        sends = [args for op, args in self.proxy.operations if op == 'send']
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][2], 'prompt-id')
        self.assertTrue(self.chat['running'])

    def test_explicit_stop_interrupts_after_reconnect(self):
        self.chat['running'] = True
        self.chat.turn_id = 'turn-1'
        self.exchange(client='two', seen={})
        self.exchange([(1, ('stop', 'chat', None))], client='two')
        self.assertIn(('interrupt', ('chat', 'remote', 'turn-1')), self.proxy.operations)

    def test_approval_survives_disconnect_and_answer_is_sent_once(self):
        self.chat['running'] = True
        self.chat['requests']['approval'] = {'kind': 'approval', 'text': 'May I?'}
        snapshot = self.exchange(client='two', seen={})
        self.assertNotIn('answer', snapshot['chats']['chat'][0]['requests']['approval'])
        commands = [(1, ('answer', 'chat', ('approval', {'decision': 'accept'})))]
        self.exchange(commands, client='two')
        self.exchange(commands, client='two')
        self.assertEqual(len([op for op, args in self.proxy.operations if op == 'answer']), 1)

    def mirror(self):
        mirror = Mirror('codex', {'id': 'test'}, ChatMetadata())
        mirror.receive('snapshot', self.exchange(seen={}))
        return mirror

    def test_snapshot_does_not_lose_unacknowledged_user_input(self):
        mirror = self.mirror()
        local = dict.get(mirror, 'chat')
        local['messages']['new'] = user_message('unsent edit')
        mirror.reconcile()
        self.chat['messages']['assistant'] = AssistantMessage(content={'text': 'stream update'})
        self.proxy.revision += 1  # As ChatProxy.drain does for a provider event.
        mirror.receive('snapshot', self.exchange())
        self.assertIn('new', local['messages'])
        self.assertIn('assistant', local['messages'])
        self.assertEqual(len([edit for _, edit in mirror.pending if edit[0] == 'user']), 1)

    def test_close_does_not_send_stop_or_release(self):
        self.chat['running'] = True
        mirror = self.mirror()
        mirror.close()
        self.assertTrue(mirror.closed)
        self.assertEqual(mirror.pending, [])
        self.assertTrue(self.chat['running'])

    def test_directory_switch_recreates_session_through_service(self):
        from types import SimpleNamespace
        from meltygui.chat.chat_interface import switch_new_chat_project
        mirror = self.mirror()
        mirror['chat'].metadata.update(model='chosen', model_explicit=True, permissions='full', effort='high')
        state = SimpleNamespace(account='test', selected={'test': 'chat'},
                                drafts={'test:chat': 'Keep my prompt'}, projects={})
        self.assertTrue(switch_new_chat_project(state, {'test': mirror}, '/new/work'))
        key = state.selected['test']
        self.assertEqual(state.drafts, {'test:' + key: 'Keep my prompt'})
        mirror.reconcile()
        mirror.receive('snapshot', self.exchange(mirror.pending, client=mirror.client_id))
        self.assertNotIn('chat', self.proxy)
        self.assertEqual(self.proxy[key]['project'], '/new/work')
        self.assertEqual(self.proxy[key].metadata['model'], 'chosen')
        self.assertTrue(self.proxy[key].metadata['model_explicit'])
        self.assertEqual(self.proxy[key].metadata['permissions'], 'full')
        self.assertEqual(self.proxy[key].metadata['effort'], 'high')
        self.assertIn(('create', (key, '/new/work', 'Test')), self.proxy.operations)
        self.assertFalse(any(op == 'send' for op, args in self.proxy.operations))
        mirror[key]['messages']['prompt'] = user_message('Already sent')
        self.assertFalse(switch_new_chat_project(state, {'test': mirror}, '/another/work'))

    def test_codex_empty_session_cleanup_without_rollout(self):
        from unittest.mock import Mock
        from meltygui.chat.codex_proxy import CodexChats
        proxy = CodexChats.__new__(CodexChats)
        writer = Mock()
        writer.request.side_effect = RuntimeError('no rollout found for thread id remote')
        proxy.transport = writer
        proxy.writers = {'remote': writer}
        proxy.known = {'chat': self.chat}
        proxy._close_transport = Mock()
        proxy.publish = Mock()
        proxy.execute('archive', 'chat', 'remote')
        proxy._close_transport.assert_called_once_with(writer)
        proxy.publish.assert_called_once_with('archived', 'chat')
        self.chat['messages']['prompt'] = user_message('Existing history')
        with self.assertRaises(RuntimeError):
            proxy.execute('archive', 'chat', 'remote')

    def test_queue_waits_and_sends_in_order_after_ui_disconnects(self):
        self.chat['running'] = True
        self.chat.turn_id = 'active'
        mirror = self.mirror()
        mirror.queue_message('chat', user_message('first'))
        mirror.queue_message('chat', user_message('second'))
        snapshot = self.exchange(mirror.pending, client=mirror.client_id)
        self.exchange(mirror.pending, client=mirror.client_id)  # retry before ack
        self.assertEqual(len(self.chat['queued_messages']), 2)
        self.assertFalse(any(op in ('send', 'interrupt') for op, _ in self.proxy.operations))
        mirror.receive('snapshot', snapshot)
        mirror.close()
        for expected in ('first', 'second'):
            self.chat['running'] = False
            self.chat.turn_id = None
            self.chat.inflight.discard('send')
            self.proxy.reconcile()
            sends = [args for op, args in self.proxy.operations if op == 'send']
            self.assertEqual(sends[-1][3], expected)
            self.chat.sent.add(sends[-1][2])
        self.assertEqual(len([op for op, _ in self.proxy.operations if op == 'send']), 2)
        self.assertFalse(self.chat['queued_messages'])

    def test_idle_send_submits_directly_before_a_paused_queue(self):
        self.chat['queued_messages'] = {'later': user_message('later')}
        self.chat['queue_paused'] = True
        mirror = self.mirror()
        self.assertTrue(mirror.queue_message('chat', user_message('now'), interrupt=True))
        self.assertEqual(list(mirror['chat']['queued_messages']), ['later'])
        mirror.reconcile()
        mirror.receive('snapshot', self.exchange(mirror.pending, client=mirror.client_id))
        sends = [args for op, args in self.proxy.operations if op == 'send']
        self.assertEqual([args[3] for args in sends], ['now'])
        self.chat.sent.add(sends[0][2])
        self.chat.inflight.clear()
        self.chat['running'] = False
        self.proxy.reconcile()
        self.assertEqual([args[3] for op, args in self.proxy.operations if op == 'send'], ['now', 'later'])

    def test_send_queued_now_interrupts_and_is_not_duplicated_on_retry(self):
        self.chat['running'] = True
        self.chat.turn_id = 'active'
        self.chat['queued_messages'] = {'one': user_message('one'), 'two': user_message('two')}
        mirror = self.mirror()
        mirror.send_queued('chat', 'two')
        snapshot = self.exchange(mirror.pending, client=mirror.client_id)
        self.exchange(mirror.pending, client=mirror.client_id)
        self.assertEqual(list(self.chat['queued_messages']), ['two', 'one'])
        self.assertTrue(any(op == 'interrupt' for op, _ in self.proxy.operations))
        mirror.receive('snapshot', snapshot)
        self.chat.turn_id = None
        self.chat.inflight.clear()
        self.proxy.reconcile()
        self.assertEqual([args[3] for op, args in self.proxy.operations if op == 'send'], ['two'])

    def test_old_service_keeps_draft_instead_of_acknowledging_unknown_queue_edit(self):
        mirror = self.mirror()
        mirror.queue_protocol = 0
        self.assertFalse(mirror.queue_message('chat', user_message('keep me'), interrupt=True))
        self.assertFalse(mirror.pending)
        self.assertFalse(mirror['chat']['messages'])
        self.assertIn('outdated', mirror.error)

    def test_send_interrupts_and_takes_priority_stop_pauses_queue(self):
        self.chat['running'] = True
        self.chat.turn_id = 'active'
        mirror = self.mirror()
        mirror.queue_message('chat', user_message('later'))
        mirror.queue_message('chat', user_message('now'), interrupt=True)
        mirror.receive('snapshot', self.exchange(mirror.pending, client=mirror.client_id))
        self.assertTrue(any(op == 'interrupt' for op, _ in self.proxy.operations))
        self.assertFalse(any(op == 'send' for op, _ in self.proxy.operations))
        mirror.stop_chat('chat')
        self.exchange(mirror.pending, client=mirror.client_id)
        self.chat.turn_id = None
        self.chat.inflight.clear()
        self.proxy.reconcile()
        self.assertFalse(any(op == 'send' for op, _ in self.proxy.operations))
        self.proxy.resume_queue('chat')
        self.proxy.reconcile()
        sends = [args for op, args in self.proxy.operations if op == 'send']
        self.assertEqual(sends[-1][3], 'now')
        self.assertEqual(len(self.chat['queued_messages']), 1)

    def test_queue_edit_survives_stale_snapshot_and_can_be_removed(self):
        self.chat['running'] = True
        mirror = self.mirror()
        stale = self.exchange(seen={})
        mirror.queue_message('chat', user_message('pending'))
        mirror.receive('snapshot', stale)
        identifier = next(iter(mirror['chat']['queued_messages']))
        mirror.cancel_queued('chat', identifier)
        mirror.receive('snapshot', self.exchange(mirror.pending, client=mirror.client_id))
        self.assertFalse(self.chat['queued_messages'])
        self.assertFalse(mirror['chat']['queued_messages'])

    def test_idle_refresh_and_reconnect_never_resend_history(self):
        self.chat['messages']['old-prompt'] = user_message('Already answered')
        self.chat.sent.add('old-prompt')
        mirror = self.mirror()
        for _ in range(100):
            mirror.reconcile()
            mirror.refresh()
            snapshot = self.exchange(mirror.pending, client=mirror.client_id)
            mirror.receive('snapshot', snapshot)
        mirror.close()
        reopened = self.mirror()
        reopened.reconcile()
        self.exchange(reopened.pending, client=reopened.client_id, seen={})
        self.assertFalse(any(op in ('send', 'create', 'interrupt')
                             for op, args in self.proxy.operations))

    def test_reconnect_removes_rows_deleted_while_disconnected(self):
        mirror = self.mirror()
        dict.pop(self.proxy, 'chat')
        self.proxy.revision += 1
        mirror.receive('snapshot', self.exchange(seen={}))
        self.assertNotIn('chat', mirror)

    def test_fork_transmits_placeholder_before_fork_without_extra_create(self):
        mirror = self.mirror()
        new_key = mirror.fork('chat')
        self.exchange(mirror.pending, client=mirror.client_id)
        operations = [op for op, args in self.proxy.operations]
        self.assertEqual(operations.count('fork'), 1)
        self.assertNotIn('create', operations)
        self.assertIn(new_key, self.proxy)


if __name__ == '__main__':
    unittest.main()
