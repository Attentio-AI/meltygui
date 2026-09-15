"""Typed history conversion and streaming, with no app-server or filesystem I/O."""
import sys
from pathlib import Path

from meltygui.chat.messages import ITEM_TYPES
from meltygui.chat.messages import AssistantMessage
from meltygui.chat.messages import UserMessage
from meltygui.chat.messages import UnknownMessage
from meltygui.chat.messages import CommandExecution
from meltygui.chat.messages import FileChange
from meltygui.chat.messages import PythonString
from meltygui.chat.messages import CodeString
from meltygui.chat.messages import MarkdownString
from meltygui.chat.messages import ShellString
from meltygui.chat.messages import ToolOutput
from meltygui.chat.messages import ImageReference
from meltygui.chat.messages import AudioReference
from meltygui.chat.messages import FileReference
from meltygui.chat.messages import JsonData
from meltygui.chat.messages import from_codex
from meltygui.chat.messages import input_text
from meltygui.chat.messages import set_text
from meltygui.chat.messages import text_blocks
from meltygui.chat.messages import upsert
from meltygui.chat.messages import user_message


def test_python_fences_preserve_indentation_and_other_languages_stay_distinct():
    blocks = text_blocks('Before\n```python\ndef f():\n    return 1\n```\nAfter\n```js\nlet x = 1;\n```\n')
    assert isinstance(blocks['0'], MarkdownString)
    assert type(blocks['1']) is PythonString
    assert blocks['1'] == 'def f():\n    return 1\n'
    assert blocks['2'] == 'After\n'
    assert type(blocks['3']) is CodeString and blocks['3'].language == 'js'


def test_streamed_open_fence_and_completed_item_keep_stable_message_and_prose():
    message = AssistantMessage('agentMessage')
    set_text(message, 'Before\n```python\nx =')
    prose = message['content']['0']
    assert isinstance(message['content']['1'], PythonString)
    set_text(message, message.source_text + ' 1\n```\n')
    assert message['content']['0'] is prose
    messages = {'id': message}
    upsert(messages, 'id', from_codex({'type': 'agentMessage', 'text': message.source_text}))
    assert messages['id'] is message and message['content']['0'] is prose
    assert message['content']['1'] == 'x = 1\n'


def test_all_schema_item_kinds_have_distinct_message_types():
    assert len(ITEM_TYPES) == 19
    assert len(set(ITEM_TYPES.values())) == len(ITEM_TYPES)
    for kind, cls in ITEM_TYPES.items():
        message = from_codex({'id': 'item', 'type': kind})
        assert type(message) is cls
        assert isinstance(message['content'], dict)
    unknown = from_codex({'type': 'future', 'payload': {'enabled': True, 'count': 3}})
    assert isinstance(unknown, UnknownMessage)
    assert unknown['details']['payload'] == {'enabled': True, 'count': 3}


def test_commands_files_and_nested_tool_data_are_not_flattened():
    command = from_codex({'type': 'commandExecution', 'command': 'pytest -q',
                         'aggregatedOutput': '3 passed', 'exitCode': 0, 'cwd': '/repo'})
    assert isinstance(command, CommandExecution)
    assert isinstance(command['content']['command'], ShellString)
    assert isinstance(command['content']['output'], ToolOutput)
    assert command['details']['exitCode'] == 0
    files = from_codex({'type': 'fileChange', 'changes': [{'path': '/repo/a.py', 'diff': '+x = 1', 'kind': {'type': 'add'}}]})
    assert isinstance(files, FileChange)
    assert isinstance(files['content']['0']['file'], FileReference)
    tool = from_codex({'type': 'mcpToolCall', 'server': 'test', 'tool': 'run',
                       'arguments': {'language': 'python', 'code': 'print(1)', 'limit': 4},
                       'result': {'content': [{'type': 'image', 'data': 'encoded', 'mimeType': 'image/png'}]}})
    assert isinstance(tool['content']['arguments'], JsonData)
    assert isinstance(tool['content']['arguments']['code'], PythonString)
    assert isinstance(tool['content']['result']['content'][0], ImageReference)
    assert tool['content']['arguments']['limit'] == 4


def test_media_is_preserved_as_inert_references():
    message = from_codex({'type': 'userMessage', 'content': [
        {'type': 'text', 'text': 'Look at this'},
        {'type': 'localImage', 'path': '/does/not/exist.png'},
        {'type': 'audio', 'url': 'data:audio/wav;base64,AAAA'},
    ]})
    assert isinstance(message, UserMessage)
    assert isinstance(message['content']['1'], ImageReference)
    assert isinstance(message['content']['2'], AudioReference)
    assert message['content']['1']['path'] == '/does/not/exist.png'


def test_outbound_prompt_comes_from_mutated_content():
    message = user_message('Hello')
    message['content']['0'] = MarkdownString('Edited')
    assert input_text(message) == 'Edited'


def test_changed_file_summary_counts_hunks_and_keeps_file_identity():
    from meltygui.chat.messages import changed_files
    from meltygui.chat.messages import DiffString
    message = from_codex({'type': 'fileChange', 'changes': [
        {'path': '/a.py', 'diff': '--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n-old\n+new\n+more\n context\n'},
        {'path': '/b.py', 'diff': '+new file\n'},
    ]})
    files = changed_files(message)
    assert (files['/a.py']['added'], files['/a.py']['removed']) == (2, 1)
    assert files['/b.py']['added'] == 1
    assert changed_files(message) is files
    message['content']['1']['diff'] = DiffString('+a\n+b\n', 'diff')
    assert changed_files(message)['/b.py']['added'] == 2
    assert changed_files(from_codex({'type': 'commandExecution', 'command': 'echo hello'})) == {}


def test_bash_commands_and_fences_are_typed_and_file_intent_is_parsed():
    import shlex
    from meltygui.chat.messages import BashString
    from meltygui.chat.messages import FileTags
    source = "python - <<'PYTHON'\nfrom pathlib import Path\np = Path('/repo') / 'first.py'\ns = p.read_text()\np.write_text(s.replace('old', 'new'))\nPath('/repo/second.py').write_text('hello')\nPYTHON\nsed -n '1,20p' /repo/read.py"
    message = from_codex({'type': 'commandExecution', 'command': '/bin/bash -lc ' + shlex.quote(source)})
    assert type(message['content']['command']) is BashString
    assert message['content']['command'] == source
    assert isinstance(message['summary'], FileTags)
    assert {p: v['access'] for p, v in message['summary'].items()} == {
        '/repo/first.py': 'write', '/repo/second.py': 'write', '/repo/read.py': 'read'}
    assert all(v['added'] is None and v['removed'] is None for v in message['summary'].values())
    assert isinstance(text_blocks('```bash\necho hello\n```')['0'], BashString)


def test_captured_python_script_references_follow_history_without_file_reads():
    scripts = {}
    from_codex({'type': 'commandExecution', 'command': "cat > /tmp/edit.py <<'PY'\nfrom pathlib import Path\nfor name in ['a.py', 'b.py']:\n    p = Path('/repo') / name\n    p.write_text('new')\nPY\n"}, scripts=scripts)
    message = from_codex({'type': 'commandExecution', 'command': 'python /tmp/edit.py'}, scripts=scripts)
    assert message['summary']['/repo/a.py']['access'] == 'write'
    assert message['summary']['/repo/b.py']['access'] == 'write'
    unknown = from_codex({'type': 'commandExecution', 'command': 'python /tmp/not-captured.py'})
    assert list(unknown['summary']) == ['/tmp/not-captured.py']


def test_command_parser_does_not_resolve_dynamic_paths_or_unused_functions():
    from meltygui.chat.command_parser import parse_command
    source = "python - <<'PY'\nfrom pathlib import Path\ndef unused():\n    Path('/never.py').write_text('x')\nPath(get_path()).write_text('x')\nPY\necho ok > '$DYNAMIC/file.py'"
    _, files = parse_command(source)
    assert files == {}


def test_script_capture_with_redirect_after_heredoc_and_shell_line_separators():
    from meltygui.chat.command_parser import parse_command
    _, files = parse_command("cat <<'PY' > /tmp/edit.py\nfrom pathlib import Path\nPath('/repo/edit.py').write_text('new')\nPY\npython /tmp/edit.py;\ntouch /repo/new.py")
    assert files == {'/tmp/edit.py': 'write', '/repo/edit.py': 'write', '/repo/new.py': 'write'}


def test_command_file_references_deduplicate_provider_aliases():
    message = from_codex({'type': 'commandExecution', 'cwd': '/repo',
        'command': 'cat src/a.py tests/b.py', 'commandActions': [
            {'path': '/repo/src/a.py'}, {'path': 'a.py'}, {'path': './tests/b.py'}]})
    assert list(message['summary']) == ['/repo/src/a.py', '/repo/tests/b.py']
    message = from_codex({'type': 'commandExecution', 'cwd': '/repo',
        'command': 'cat src/a.py tests/a.py', 'commandActions': [{'path': 'a.py'}]})
    assert list(message['summary']) == ['/repo/src/a.py', '/repo/tests/a.py', '/repo/a.py']


def test_diff_counts_split_a_turn_diff_per_file():
    from meltygui.chat.messages import diff_counts
    from meltygui.chat.messages import count_diff_lines
    from meltygui.chat.messages import match_file
    diff = ('diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1,2 +1,2 @@\n-old\n+new\n+more\n'
            'diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n')
    assert diff_counts(diff) == {'src/a.py': (2, 1), 'gone.py': (0, 1)}
    assert diff_counts('') == {}
    assert count_diff_lines('+a\n-b\n-c\n') == (1, 2)
    assert match_file('/repo/src/a.py', 'src/a.py')
    assert match_file('/repo/src/a.py', '/repo/src/a.py')
    assert not match_file('/repo/src/a.py', 'b/src/a.py')
