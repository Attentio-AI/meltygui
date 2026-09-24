"""The detached chat service. UI disconnects never own provider lifetimes.

Conversations run in one service process per user (`python -m
meltygui.chat.chat_service --serve`, started on the first connection), so
closing a window leaves accepted turns running and every app that draws the
chat shows the same live state. `PersistentChats` is the render-thread mirror
the chat window edits (the factory registered for the "codex" and "anthropic"
account kinds); `ChatService` owns the real backends (`CodexChats`,
`ClaudeCodeChats`).

The Unix socket and its containing directory are private to this user. Pickle
preserves Melty's typed message values; it is never accepted over a network.
The frames carry this package's classes, so the directory name is this
package's own: a service of another implementation (the latent-descent
melty_claude pickles `src.lsd...` values) is never shared.
"""
import copy
import fcntl
import os
import pickle
import signal
import socket
import struct
import sys
import threading
import time
import uuid
from pathlib import Path

from meltygui.chat.auto_titles import AutoTitles
from meltygui.chat.chat_proxy import Chat, ChatProxy, apply_queue_edit
from meltygui.chat.metadata import ChatMetadata

PROXY_FIELDS = ('loading', 'error', 'models', 'models_error', 'default_model',
                'model_efforts', 'model_default_efforts', 'model_service_tiers',
                'model_default_service_tiers', 'inherits_defaults',
                'config_defaults', 'effort_defaults', 'resolved_models')
CHAT_FIELDS = ('remote_id', 'loaded', 'loading', 'refreshing', 'error', 'turn_id',
               'resumed', 'saved_title', 'inflight', 'sent')


def service_dir():
    root = Path(os.environ.get('XDG_RUNTIME_DIR', '/tmp')) / f'meltygui-chat-{os.getuid()}'
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise RuntimeError(f'Chat service directory must be private: {root}')
    # A separate namespace is useful for integration tests; normal app instances
    # all reconnect to the same service, independent of their window socket.
    namespace = os.environ.get('MELTY_CHAT_SERVICE', 'default')
    if not namespace.replace('-', '').replace('_', '').isalnum():
        raise ValueError('Invalid MELTY_CHAT_SERVICE name')
    return root / namespace


def send_frame(sock, value):
    payload = pickle.dumps(value, protocol=5)
    sock.sendall(struct.pack('!I', len(payload)) + payload)


def read_exact(sock, count):
    data = bytearray()
    while len(data) < count:
        part = sock.recv(count - len(data))
        if not part:
            raise EOFError('Chat service disconnected')
        data.extend(part)
    return data


def recv_frame(sock):
    size, = struct.unpack('!I', read_exact(sock, 4))
    if size > 256 * 1024 * 1024:
        raise ValueError('Chat service message too large')
    return pickle.loads(read_exact(sock, size))


def start_service(log_path):
    """Start the detached service: a session of its own, so it outlives the UI
    that started it. `os.posix_spawn` (vfork), never a fork of the app's
    GL/CUDA address space; Python's fds are not inheritable, so only the
    three opened here reach the child."""
    actions = [(os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0),
               (os.POSIX_SPAWN_OPEN, 1, log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600),
               (os.POSIX_SPAWN_DUP2, 1, 2)]
    return os.posix_spawn(sys.executable, [sys.executable, '-m', 'meltygui.chat.chat_service', '--serve'],
                          os.environ, file_actions=actions, setsid=True, setsigmask=(),
                          setsigdef=(signal.SIGPIPE, signal.SIGXFSZ))


def connect_service():
    base = service_dir()
    with open(str(base) + '.launch-lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for attempt in range(100):
            sock = socket.socket(socket.AF_UNIX)
            sock.settimeout(10)
            try:
                sock.connect(str(base) + '.sock')
                return sock
            except (FileNotFoundError, ConnectionRefusedError):
                sock.close()
                if attempt == 0:
                    start_service(str(base) + '.log')
                time.sleep(0.1)
    raise RuntimeError(f'Chat service did not start; see {base}.log')


def editable(chat, previous=None):
    """Only UI-owned edits cross back; streamed messages are never overwritten."""
    answers = {k: r['answer'] for k, r in chat['requests'].items() if 'answer' in r}
    # Keep detached values when they are equal. Nested edits still compare
    # against a snapshot, but ordinary viewport input needs no deep copies.
    metadata = (previous['metadata'] if previous is not None and chat.metadata == previous['metadata']
                else copy.deepcopy(chat.metadata))
    answers = (previous['answers'] if previous is not None and answers == previous['answers']
               else copy.deepcopy(answers))
    return {'title': chat['title'], 'running': chat['running'], 'project': chat['project'],
            'users': {k for k, m in chat['messages'].items() if m.get('role') == 'user'},
            'answers': answers, 'metadata': metadata}


def chat_value(chat):
    return (dict(chat), {k: getattr(chat, k) for k in CHAT_FIELDS if hasattr(chat, k)},
            dict(chat.metadata))


def missing_empty_archive(chat):
    return (chat.loaded and not chat['messages'] and not chat['running']
            and not chat.turn_id and not chat['requests'] and not chat.get('queued_messages')
            and chat.inflight == {'archive'}
            and 'no rollout found for thread id' in str(chat.error).lower())


def apply_edit(proxy, edit):
    op, key, value = edit
    if op == 'create':
        if key not in proxy:
            data, attributes, metadata = value
            proxy[key] = data
            dict.get(proxy, key).__dict__.update(attributes)
            dict.get(proxy, key).metadata.update(metadata)
            proxy.known[key] = dict.get(proxy, key)
        return
    if op == 'delete':
        dict.pop(proxy, key, None)
        return
    chat = dict.get(proxy, key)
    if chat is None:
        return
    if op in ('queue_message', 'stop_queue', 'cancel_queued', 'resume_queue', 'send_queued'):
        apply_queue_edit(chat, op, value)
    elif op == 'title':
        chat['title'] = value
        chat.metadata['auto_title'] = 'manual'
    elif op == 'stop':
        chat['running'] = False
    elif op == 'user':
        message_id, message = value
        if message_id not in chat['messages']:
            chat['messages'][message_id] = message
            if chat.get('retryable_error'):
                chat.error = None
                chat['retryable_error'] = False
    elif op == 'answer':
        request_id, answer = value
        if request_id in chat['requests']:
            chat['requests'][request_id]['answer'] = answer
    elif op == 'metadata':
        updates, removed = value
        chat.metadata.update(updates)
        for field in removed:
            chat.metadata.pop(field, None)


class PersistentChats(ChatProxy):
    """A render-thread mirror; its worker only exchanges service messages."""
    def _queue_edit(self, key, operation, value=None):
        if self.connected and not getattr(self, 'queue_protocol', 0):
            self.error = 'The connected chat service is outdated. Your draft has been kept; update the service before submitting.'
            self.revision += 1
            return False
        self.reconcile()  # A new conversation must reach the service before its queue edits.
        self.enqueue((operation, key, value))
        apply_queue_edit(self[key], operation, value)
        self.revision += 1
        return True

    def __init__(self, kind, account, metadata=None, wake=None):
        self.kind = kind
        self.account = {k: v for k, v in account.items() if not k.startswith('_')}
        self.client_id = str(uuid.uuid4())
        self.pending = []
        self.pending_lock = threading.Lock()
        self.sequence = 0
        self.baseline = {}
        self.config_defaults = {}
        self.config_pending = set()
        self.effort_defaults = {}
        self.effort_requested = {}
        self.inherits_defaults = kind == 'codex'
        self.connected = False
        self.connection = None
        super().__init__(account['id'], metadata, wake)

    def enqueue(self, edit):
        with self.pending_lock:
            self.sequence += 1
            self.pending.append((self.sequence, copy.deepcopy(edit)))

    def _work(self):
        while not self.closed:
            try:
                with connect_service() as sock:
                    self.connection = sock
                    send_frame(sock, {'kind': self.kind, 'account': self.account,
                                      'metadata': self.metadata.account(self.account_id),
                                      'client': self.client_id})
                    # Negotiate before sending edits: older daemons acknowledge
                    # unknown operations, which otherwise silently loses drafts.
                    send_frame(sock, [])
                    initial = recv_frame(sock)
                    if initial.get('queue_protocol', 0) < 1:
                        raise RuntimeError('Chat service is outdated; waiting for the updated service. Unsent messages are retained.')
                    self.publish('snapshot', initial)
                    previous = None
                    while not self.closed:
                        with self.pending_lock:
                            pending = list(self.pending)
                        send_frame(sock, pending)
                        snapshot = recv_frame(sock)
                        status = (snapshot['ack'], snapshot['fields'], snapshot['keys'])
                        if snapshot['chats'] or snapshot['removed'] or status != previous:
                            self.publish('snapshot', snapshot)
                        previous = status
                        time.sleep(0.1)
            except Exception as error:
                # Every failure is shown and retried: a worker that dies on an
                # unexpected one (a frame this package cannot unpickle) leaves
                # an empty window with no error.
                if not self.closed:
                    self.publish('connection_error', f'Chat service: {error}' if not isinstance(
                        error, (OSError, EOFError, ValueError, RuntimeError)) else str(error))
                time.sleep(0.5)
            finally:
                self.connection = None

    def reconcile(self):
        if not self.connected:
            return
        baseline = {}
        metadata_changed = False
        for key in self.baseline.keys() - self.keys():
            self.enqueue(('delete', key, None))
        for key, chat in list(self.items()):
            if not isinstance(chat, Chat):
                self[key] = chat
                chat = dict.get(self, key)
            before = self.baseline.get(key)
            now = editable(chat, before)
            metadata_changed |= (before is None or now['metadata'] != before['metadata']
                                 or now['project'] != before.get('project'))
            # This detached snapshot is also the next baseline. Taking it again
            # doubles the history scan and metadata copies on every scroll frame.
            baseline[key] = now
            if before is None:
                self.enqueue(('create', key, chat_value(chat)))
                before = {'title': now['title'], 'running': False, 'users': set(), 'answers': {}, 'metadata': {}}
            if now['title'] != before['title']:
                self.enqueue(('title', key, now['title']))
            if before['running'] and not now['running']:
                self.enqueue(('stop', key, None))
            if now['metadata'] != before['metadata']:
                updates = {k: v for k, v in now['metadata'].items() if k not in before['metadata'] or v != before['metadata'][k]}
                self.enqueue(('metadata', key, (updates, before['metadata'].keys() - now['metadata'].keys())))
            for message_id in now['users'] - before['users']:
                self.enqueue(('user', key, (message_id, chat['messages'][message_id])))
            for request_id, answer in now['answers'].items():
                if request_id not in before['answers'] or answer != before['answers'][request_id]:
                    self.enqueue(('answer', key, (request_id, answer)))
        self.baseline = baseline
        # Match the base proxy's metadata boundary: scrolling does not change
        # conversation order or tints. Direct dictionary edits still count.
        if (metadata_changed or list(self) != self.applied_order or self.revision != self._applied_revision
                or self._tints_changed()):
            self.metadata.collect(self.account_id, self)
            self.metadata.apply(self.account_id, self)
            self._applied_revision = self.revision

    def drain(self):
        # With no snapshot to replace values, the caller's normal reconcile
        # boundary suffices. A later arrival remains queued for the next frame.
        if self.events.empty():
            return False
        # Capture edits made since the last frame before replacing any snapshots.
        self.reconcile()
        return super().drain()

    def receive(self, kind, value):
        if kind == 'connection_error':
            self.error = value
            return
        with self.pending_lock:
            self.pending = [(seq, edit) for seq, edit in self.pending if seq > value['ack']]
            pending = list(self.pending)
        for key in self.keys() - set(value['keys']):
            dict.pop(self, key, None)
        for key, (data, attributes, metadata) in value['chats'].items():
            chat = dict.get(self, key)
            if chat is None:
                self[key] = data
                chat = dict.get(self, key)
            else:
                chat.clear()
                chat.update(data)
            chat.__dict__.update(attributes)
            chat.metadata.clear()
            chat.metadata.update(metadata)
            self.known[key] = chat
        self.__dict__.update(value['fields'])
        self.queue_protocol = value.get('queue_protocol', 0)
        self.connected = True
        for _, edit in pending:
            if edit[0] not in ('call',):
                apply_edit(self, edit)
        self.baseline = {key: editable(chat) for key, chat in self.items()}
        self.metadata.apply(self.account_id, self)
        # Services started before empty-archive recovery may still be running
        # real turns. Keep their stale deletion error local to that empty row,
        # so reopening the UI unblocks the account without killing its worker.
        if self.error and any(chat.error == self.error and missing_empty_archive(chat)
                              for chat in self.values()):
            self.error = None

    def _load_history(self, key, chat):
        if not chat.loaded and not chat.loading:
            chat.loading = True
            self.enqueue(('call', key, ('hydrate', (key,))))

    def submit(self, operation, *args):
        if operation == 'fork':
            self.reconcile()
        self.enqueue(('call', None, (operation, args)))

    def refresh(self):
        self.submit('refresh')

    def defaults_for(self, project):
        if project not in self.config_defaults and project not in self.config_pending:
            self.config_pending.add(project)
            self.submit('defaults', project)
        return self.config_defaults.get(project, self.config_defaults.get('', {}))

    def default_effort_for(self, project, model):
        model = getattr(self, 'resolved_models', {}).get(model, model)
        key = (project, model)
        if time.monotonic() - self.effort_requested.get(key, 0) > 5:
            self.effort_requested[key] = time.monotonic()
            self.submit('effort_default', project, model)
        return self.effort_defaults.get(key)

    def fork(self, key):
        # Reuse the shared fork setup, flushing its new row before the RPC.
        self.reconcile()
        return super().fork(key)

    def close(self):
        self.reconcile()
        self.closed = True  # Never stop, release, or close the service's backend.
        if self.connection is not None:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class ChatService:
    def __init__(self, factory=None):
        self.factory = factory or self.backend
        self.proxies = {}
        self.acks = {}
        self.payloads = {}
        self.lock = threading.RLock()
        self.auto_titles = AutoTitles(self.lock)
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.pump, daemon=True)
        self.thread.start()

    @staticmethod
    def backend(kind, account, metadata):
        if kind == 'codex':
            from meltygui.chat.codex_proxy import CodexChats
            return CodexChats(account['id'], metadata)
        if kind == 'anthropic':
            from meltygui.chat.claude_code import ClaudeCodeChats
            return ClaudeCodeChats(account['id'], metadata, account=account)
        raise ValueError(f'Unknown chat provider: {kind}')

    def attach(self, hello):
        identity = (hello['kind'], hello['account']['id'])
        with self.lock:
            if identity not in self.proxies:
                metadata = ChatMetadata()
                metadata.accounts[identity[1]] = hello['metadata']
                self.proxies[identity] = self.factory(identity[0], hello['account'], metadata)
                self.auto_titles.install(self.proxies[identity])
            proxy = self.proxies[identity]
            self.recover_empty_archives(proxy)
            return proxy

    @staticmethod
    def recover_empty_archives(proxy):
        """Finish failed deletion of unsent Codex threads on UI reconnect.

        Older workers restored these rows and left an account-wide error.
        They have no persisted rollout to archive. Never reset the backend:
        other conversations may still own turns and pending approvals.
        """
        recovered = set()
        for key, chat in list(proxy.known.items()):
            if not missing_empty_archive(chat):
                continue
            recovered.add(chat.error)
            if chat.remote_id:
                proxy.submit('release', chat.remote_id)
            dict.pop(proxy, key, None)
            proxy.known.pop(key, None)
        if recovered:
            if proxy.error in recovered:
                proxy.error = None
            proxy.revision += 1

    def pump(self):
        while not self.stopped.wait(0.05):
            with self.lock:
                for proxy in self.proxies.values():
                    try:
                        if proxy.drain():
                            proxy.reconcile()
                    except Exception:
                        import traceback
                        traceback.print_exc()

    def exchange(self, proxy, client, commands, seen):
        with self.lock:
            ack = self.acks.get(client, 0)
            edited = False
            for seq, edit in commands:
                if seq <= ack:
                    continue  # Reconnect/retry cannot duplicate a send or fork.
                op, key, value = edit
                if op == 'call':
                    operation, args = value
                    if operation == 'hydrate':
                        proxy[args[0]]
                    else:
                        proxy.reconcile()
                        proxy.submit(operation, *args)
                else:
                    apply_edit(proxy, edit)
                ack = seq
                self.acks[client] = ack
                edited = True
            if proxy.drain() or edited:
                proxy.reconcile()
            if edited:
                proxy.revision += 1
            changed = {}
            removed = seen.keys() - proxy.keys()
            for key in removed:
                seen.pop(key)
            cached_revision, payloads = self.payloads.get(id(proxy), (None, {}))
            if cached_revision != proxy.revision:
                payloads = {key: pickle.dumps(chat_value(chat), protocol=5)
                            for key, chat in proxy.items()}
                self.payloads[id(proxy)] = (proxy.revision, payloads)
            for key, payload in payloads.items():
                if payload != seen.get(key):
                    changed[key] = pickle.loads(payload)
                    seen[key] = payload
            return {'ack': ack, 'chats': changed, 'removed': list(removed), 'keys': list(proxy), 'queue_protocol': 1,
                    'fields': {k: copy.deepcopy(getattr(proxy, k)) for k in PROXY_FIELDS if hasattr(proxy, k)}}

    def handle(self, sock):
        try:
            with sock:
                hello = recv_frame(sock)
                proxy = self.attach(hello)
                seen = {}
                while True:
                    commands = recv_frame(sock)
                    send_frame(sock, self.exchange(proxy, hello['client'], commands, seen))
        except (EOFError, OSError):
            pass  # An absent UI changes nothing about active work or approvals.


def serve():
    os.umask(0o077)
    base = service_dir()
    with open(str(base) + '.service-lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        path = str(base) + '.sock'
        Path(path).unlink(missing_ok=True)
        service = ChatService()
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(path)
            listener.listen()
            print(f'Chat service pid={os.getpid()} socket={path}', flush=True)
            while True:
                sock, _ = listener.accept()
                threading.Thread(target=service.handle, args=(sock,), daemon=True).start()


if __name__ == '__main__':
    serve()
