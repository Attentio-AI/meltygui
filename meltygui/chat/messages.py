"""Provider-neutral message values. Types select views; no rendering or file I/O."""
import re
from .command_parser import parse_command


class TextString(str):
    pass


class MarkdownString(TextString):
    pass


class CodeString(TextString):
    def __new__(cls, value="", language=""):
        obj = super().__new__(cls, value)
        obj.language = language
        return obj


class PythonString(CodeString):
    def __new__(cls, value="", language="python"):
        return super().__new__(cls, value, language)


class ShellString(CodeString):
    pass


class BashString(ShellString):
    def __new__(cls, value="", language="bash"):
        return super().__new__(cls, value, language)


class FileTags(dict):
    """Ordered file references with access intent and optional reported diff counts."""


class ToolOutput(TextString):
    pass


class DiffString(CodeString):
    pass


class JsonData(dict):
    """Structured tool arguments/results; keys and numeric/bool values stay native."""


class Reference(dict):
    label = "Reference"


class ImageReference(Reference):
    label = "Image"


class AudioReference(Reference):
    label = "Audio"


class FileReference(Reference):
    label = "File"


class SkillReference(Reference):
    label = "Skill"


class MentionReference(Reference):
    label = "Mention"


class Message(dict):
    role = "activity"
    label = "Activity"

    def __init__(self, kind="", content=None, status="", details=None):
        super().__init__(kind=kind, role=self.role, status=status,
                         content={} if content is None else content,
                         details=JsonData() if details is None else details)
        self.source_text = ""


class UserMessage(Message):
    role, label = "user", "You"


class AssistantMessage(Message):
    role, label = "assistant", "Assistant"


class PlanMessage(AssistantMessage):
    label = "Plan"


class ReasoningMessage(Message):
    label = "Reasoning summary"


class ToolCall(Message):
    label = "Tool call"


class CommandExecution(ToolCall):
    label = "Command"


class FileChange(ToolCall):
    label = "File changes"


class McpToolCall(ToolCall):
    label = "MCP tool"


class DynamicToolCall(ToolCall):
    label = "Tool"


class CollabAgentToolCall(ToolCall):
    label = "Agent tool"


class FunctionCallOutput(ToolCall):
    label = "Tool result"


class WebSearch(ToolCall):
    label = "Web search"


class HookPrompt(Message):
    label = "Hook"


class SubAgentActivity(Message):
    label = "Agent activity"


class ImageView(Message):
    label = "Image viewed"


class ImageGeneration(Message):
    label = "Image generated"


class Sleep(Message):
    label = "Waiting"


class EnteredReviewMode(Message):
    label = "Review started"


class ExitedReviewMode(Message):
    label = "Review completed"


class ContextCompaction(Message):
    label = "Context compacted"


class UnknownMessage(Message):
    label = "Other activity"


ITEM_TYPES = {
    "userMessage": UserMessage, "agentMessage": AssistantMessage, "plan": PlanMessage,
    "reasoning": ReasoningMessage, "commandExecution": CommandExecution, "fileChange": FileChange,
    "mcpToolCall": McpToolCall, "dynamicToolCall": DynamicToolCall,
    "collabAgentToolCall": CollabAgentToolCall, "functionCallOutput": FunctionCallOutput,
    "webSearch": WebSearch, "hookPrompt": HookPrompt, "subAgentActivity": SubAgentActivity,
    "imageView": ImageView, "imageGeneration": ImageGeneration, "sleep": Sleep,
    "enteredReviewMode": EnteredReviewMode, "exitedReviewMode": ExitedReviewMode,
    "contextCompaction": ContextCompaction,
}


def tagged(value):
    if isinstance(value, str):
        return TextString(value)
    if isinstance(value, dict):
        kind = value.get("type")
        cls = (ImageReference if kind in ("image", "localImage", "image_url", "inputImage") else
               AudioReference if kind in ("audio", "localAudio", "inputAudio") else
               FileReference if kind in ("resource", "resource_link", "file") else JsonData)
        result = cls((key, tagged(val)) for key, val in value.items())
        language = str(value.get("language", "")).lower()
        if language in ("python", "python3", "py") and isinstance(value.get("code"), str):
            result["code"] = PythonString(value["code"])
        elif language in ("bash", "sh", "shell", "zsh") and isinstance(value.get("code"), str):
            result["code"] = BashString(value["code"], language)
        return result
    if isinstance(value, list):
        return [tagged(val) for val in value]
    return value


def text_blocks(text):
    """Split fenced code from prose, including the open fence of a streamed reply.

    Explicit Python/py/python3 fences become PythonString. Other languages
    retain their language tag but do not pretend to be Python.
    """
    result = {}
    lines = str(text).splitlines(keepends=True)
    buffer, fence, language = [], None, ""

    def flush(code=False):
        value = "".join(buffer)
        if value or code:
            cls = PythonString if language in ("python", "py", "python3") else BashString if language in ("bash", "sh", "shell", "zsh") else CodeString
            result[str(len(result))] = cls(value, language) if code else MarkdownString(value)
        buffer.clear()

    for line in lines:
        if fence is None:
            match = re.match(r"^ {0,3}(`{3,}|~{3,})([^\r\n]*)\r?\n?$", line)
            if match:
                flush()
                fence = match[1]
                language = match[2].strip().split()[0].lower() if match[2].strip() else ""
            else:
                buffer.append(line)
        elif re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", line):
            flush(code=True)
            fence = None
        else:
            buffer.append(line)
    flush(code=fence is not None)
    if not result:
        result["0"] = MarkdownString("")
    return result


def _reuse(target, incoming):
    """Retain unchanged leaves by identity so sibling text tiles stay cached."""
    for key in list(target):
        if key not in incoming:
            del target[key]
    for key, value in incoming.items():
        old = target.get(key)
        if type(old) is type(value) and isinstance(value, dict):
            _reuse(old, value)
        elif type(old) is not type(value) or old != value or getattr(old, "language", None) != getattr(value, "language", None):
            target[key] = value


def set_text(message, text):
    message.source_text = text
    _reuse(message["content"], text_blocks(text))


def user_message(text):
    message = UserMessage("userMessage")
    set_text(message, text)
    return message


def input_text(message):
    def render(value):
        if isinstance(value, CodeString):
            return "```" + value.language + "\n" + str(value) + ("" if value.endswith("\n") else "\n") + "```\n"
        if isinstance(value, str):
            return str(value)
        if isinstance(value, dict):
            return "".join(render(v) for v in value.values())
        return ""
    return render(message["content"])


def _input(part):
    kind = part.get("type")
    if kind == "text":
        return text_blocks(part.get("text", ""))
    cls = {"image": ImageReference, "localImage": ImageReference,
           "audio": AudioReference, "localAudio": AudioReference,
           "skill": SkillReference, "mention": MentionReference}.get(kind, Reference)
    return cls(tagged(part))


def from_codex(item, scripts=None):
    kind = item.get("type", "unknown")
    cls = ITEM_TYPES.get(kind, UnknownMessage)
    message = cls(kind, status=item.get("status", ""))
    content = message["content"]
    consumed = {"id", "type", "status"}
    if cls in (AssistantMessage, PlanMessage):
        set_text(message, item.get("text", ""))
        consumed.add("text")
    elif cls is UserMessage:
        content.update((str(i), _input(part)) for i, part in enumerate(item.get("content", [])))
        consumed.add("content")
    elif cls is CommandExecution:
        source, files = parse_command(item.get("command", ""), item.get("commandActions"), scripts, cwd=item.get("cwd") or "")
        message["summary"] = FileTags((path, {"file": FileReference(path=TextString(path)),
            "access": access, "added": None, "removed": None}) for path, access in files.items())
        content.update(command=BashString(source),
                       output=ToolOutput(item.get("aggregatedOutput") or ""))
        consumed.update(("command", "aggregatedOutput"))
    elif cls is FileChange:
        for i, change in enumerate(item.get("changes", [])):
            content[str(i)] = {"file": FileReference(path=TextString(change.get("path", ""))),
                               "diff": DiffString(change.get("diff", ""), "diff"),
                               "kind": tagged(change.get("kind"))}
        consumed.add("changes")
    elif cls is ReasoningMessage:
        content["summary"] = {str(i): text_blocks(t) for i, t in enumerate(item.get("summary", []))}
        content["content"] = {str(i): text_blocks(t) for i, t in enumerate(item.get("content", []))}
        consumed.update(("summary", "content"))
    elif cls in (ImageView, ImageGeneration):
        content["image"] = ImageReference(path=TextString(item.get("savedPath") or item.get("path") or ""),
                                          result=TextString(item.get("result") or ""))
        consumed.update(("savedPath", "path", "result"))
    else:
        # Tool arguments/results and log-compatible events keep their
        # structure rather than being flattened into a lossy log string.
        for field in ("arguments", "result", "output", "contentItems", "query", "results",
                      "prompt", "review", "fragments", "agentsStates", "durationMs"):
            if field in item:
                content[field] = tagged(item[field])
                consumed.add(field)
    message["details"].update((k, tagged(v)) for k, v in item.items() if k not in consumed)
    if cls is FileChange:
        message["summary"] = changed_files(message)
    return message


def upsert(messages, identifier, incoming):
    old = messages.get(identifier)
    if type(old) is type(incoming):
        _reuse(old, incoming)
        old.source_text = incoming.source_text
        return old
    messages[identifier] = incoming
    return incoming


def count_diff_lines(diff):
    """(added, removed) of one file's diff; a bare +/- listing counts without hunks."""
    added = removed = 0
    has_hunks = "@@" in diff
    in_hunk = not has_hunks
    for line in diff.splitlines():
        if line.startswith("@@"):
            in_hunk = True
        elif line.startswith("diff --git"):
            in_hunk = False
        elif not has_hunks and line.startswith(("+++ ", "--- ")):
            continue
        elif in_hunk and line.startswith("+"):
            added += 1
        elif in_hunk and line.startswith("-"):
            removed += 1
    return added, removed


def diff_counts(diff):
    """Per-file (added, removed) of a multi-file unified diff, keyed by the new path.

    Codex's turn diff names files as `diff --git a/<path> b/<path>` (deleted
    files keep the `a/` side); paths stay as written, callers match them.
    """
    counts = {}
    path, lines = None, []
    def flush():
        if path is not None:
            counts[path] = count_diff_lines("\n".join(lines))
    for line in str(diff).splitlines():
        if line.startswith("diff --git "):
            flush()
            sides = line[len("diff --git "):].split(" b/", 1)
            path = sides[1] if len(sides) == 2 else sides[0].removeprefix("a/")
            lines = []
        else:
            lines.append(line)
    flush()
    return counts


def match_file(tag_path, diff_path):
    """A tag holds the command's absolute path; a diff names it relative or absolute."""
    tag_path, diff_path = str(tag_path), str(diff_path)
    return (tag_path == diff_path or tag_path.endswith("/" + diff_path)
            or diff_path.endswith("/" + tag_path))


def changed_files(message):
    """Normalize provider-reported diffs; command summaries are parsed on ingestion."""
    if not isinstance(message, FileChange):
        return message.get("summary", FileTags())
    signature = tuple((key, change.get("diff"), str(change.get("file", {}).get("path", "")))
                      for key, change in message["content"].items() if isinstance(change, dict))
    cached = getattr(message, "_changed_files", None)
    if (cached is not None and len(cached[0]) == len(signature)
            and all(a[0] == b[0] and a[1] is b[1] and a[2] == b[2]
                    for a, b in zip(cached[0], signature))):
        return cached[1]
    files = FileTags()
    for change in message["content"].values():
        if not isinstance(change, dict) or not isinstance(change.get("file"), FileReference):
            continue
        path = str(change["file"].get("path", ""))
        if not path:
            continue
        added, removed = count_diff_lines(change.get("diff", ""))
        entry = files.setdefault(path, {"added": 0, "removed": 0, "change": change,
                                        "file": change["file"], "access": "write"})
        entry["added"] += added
        entry["removed"] += removed
    message._changed_files = (signature, files)
    return files
