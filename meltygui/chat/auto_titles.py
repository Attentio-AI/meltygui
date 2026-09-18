"""Background conversation names matching ~/bin/claude-d-title."""
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path


TITLE_PROMPT = '''Name a chat session for the request below. Reply with ONLY a title of the form: file name, function or class name, short bug/task name (two-three lowercase words) — comma-separated, e.g. "text_editor.py, draw_text, missing labels". Use only file/function/class names actually mentioned in the request; OMIT any part you cannot identify (keep the others, no placeholders) — worst case just the bug name. No explanation. If the request is too vague to name (e.g. "continue", "yes", "fix it"), reply NONE.

Request: '''


def generate_title(prompt):
    executable = shutil.which('claude') or str(Path.home() / '.local/bin/claude')
    env = {k: v for k, v in os.environ.items()
           if k not in ('TMUX', 'TMUX_PANE', 'CLAUDECODE', 'CLAUDE_CODE_ENTRYPOINT')}
    env['CLAUDE_CODE_DISABLE_TERMINAL_TITLE'] = '1'
    try:
        result = subprocess.run(
            [executable, '-p', '--model', 'haiku', '--output-format', 'text',
             '--no-session-persistence', '--tools', '', '--strict-mcp-config',
             '--settings', '{"disableAllHooks":true}'],
            input=TITLE_PROMPT + prompt[:400], cwd='/', env=env,
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = result.stdout.strip().splitlines()
    name = ' '.join(lines[-1].split()) if lines else ''
    if (result.returncode == 0 and name != 'NONE' and len(name) <= 64
            and re.fullmatch(r'[A-Za-z0-9_./-]+(?:,? [A-Za-z0-9_./-]+){0,6}', name)):
        return name
    return None


class AutoTitles:
    def __init__(self, lock, generate=generate_title):
        self.lock = lock
        self.generate = generate
        self.pending = set()

    def install(self, proxy):
        submit = proxy.submit

        def with_title(operation, *args):
            submit(operation, *args)
            if operation == 'send':
                key, _, _, prompt, *_ = args
                self.request(proxy, key, prompt)

        proxy.submit = with_title

    def request(self, proxy, key, prompt):
        chat = dict.get(proxy, key)
        if chat is None or not prompt.strip():
            return
        state = chat.metadata.get('auto_title')
        if state is None and chat['title'] == 'New conversation':
            state = chat.metadata['auto_title'] = 'retry'
        if state != 'retry' or id(chat) in self.pending:
            return
        self.pending.add(id(chat))
        threading.Thread(target=self.finish, args=(proxy, key, chat, prompt),
                         name='melty-auto-title', daemon=True).start()

    def finish(self, proxy, key, chat, prompt):
        try:
            name = self.generate(prompt)
            with self.lock:
                if (name and dict.get(proxy, key) is chat
                        and chat.metadata.get('auto_title') == 'retry'):
                    chat['title'] = name
                    chat.metadata['auto_title'] = 'done'
                    proxy.revision += 1
                    proxy.reconcile()  # The backend persists it as a normal rename.
        finally:
            with self.lock:
                self.pending.discard(id(chat))
