"""Claude FIM provider — prompt-engineered fill-in-the-middle over the
Messages API (Claude has no native FIM mode). Prompt layout is cache-
friendly: a frozen system prompt, then the editor's STABLE context block
(cache breakpoint), then the RUN block (breakpoint), then the volatile
file window with a `<|cursor|>` marker. Output is streamed so the first
line ghosts in while the rest arrives.

Session: one `anthropic.Anthropic` client per (api_key_env, base_url) —
thread-safe, connection-pooled, shared by every editor on that profile.
"""
from __future__ import annotations

import os
import re

from src.lsd.gl_gui.fim import FimRequest, FimResult, FimSession, fim_provider

SYSTEM = """You are a code completion engine inside an editor. The user message contains:
1. optional context blocks (other definitions, observed runtime types, values from the last run)
2. the current file with a <|cursor|> marker where the user is typing.

Reply with ONLY the code to insert at <|cursor|> — no explanation, no markdown fences, no echo of the text before or after the cursor.
Rules:
- Continue naturally from the exact character before the cursor (respect partial identifiers, open brackets, indentation).
- Stop where the existing code after the cursor would resume; never repeat it.
- Match the file's style, names and indentation. Prefer the names/APIs shown in the context blocks.
- Lines annotated `# ← value` show what that variable held on the last run; use them to pick sensible operations and shapes.
- If the cursor is mid-statement, finish the statement first. A multi-line completion may continue for several logical lines when the intent is clear.
- If nothing sensible can be inserted, reply with an empty message."""

_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_+-]*\n(.*?)\n?```\s*$", re.S)


def has_credentials(account="default") -> bool:
    """True when Claude can be reached at all: an account key, an
    ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN env var, or an `ant auth login`
    profile on disk. Cheap — filesystem + env only, no import, no network."""
    try:
        from src.lsd.gl_gui.view.playground.internet_accounts import account_field
        if account_field("anthropic", account, "api_key"):
            return True
    except Exception:
        pass
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    d = os.environ.get("ANTHROPIC_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".config", "anthropic")
    creds = os.path.join(d, "credentials")
    try:
        return os.path.isdir(creds) and any(f.endswith(".json") for f in os.listdir(creds))
    except OSError:
        return False


class ClaudeSession(FimSession):
    """One Anthropic client. Credentials come from the Internet Accounts
    entry `account` (kind "anthropic": `api_key`, `base_url`); with no key
    stored the SDK's normal resolution applies (ANTHROPIC_API_KEY /
    ANTHROPIC_AUTH_TOKEN env, an `ant auth login` profile).

    Claude is NOT loaded (no `import anthropic`, no client) unless a
    credential exists — construction raises early otherwise, so a studio
    with no key never pays the anthropic import or a doomed request."""
    KIND = "anthropic"

    def __init__(self, account="default", base_url=None, timeout_s=30.0):
        self.account = account
        if not has_credentials(account):
            raise RuntimeError("no Anthropic API key — add one in Internet Accounts")
        import anthropic
        from src.lsd.gl_gui.view.playground.internet_accounts import account_field
        key = account_field("anthropic", account, "api_key")
        base_url = base_url or account_field("anthropic", account, "base_url")
        kw = {"timeout": timeout_s, "max_retries": 1}
        if key:
            kw["api_key"] = key
        if base_url:
            kw["base_url"] = base_url
        self.client = anthropic.Anthropic(**kw)
        self._status = ("ready",)

    def status(self):
        return self._status

    def close(self):
        try:
            self.client.close()
        except Exception:
            pass

    def alive(self):
        return getattr(self, "client", None) is not None


def _window(req: FimRequest, prefix_chars: int, suffix_chars: int):
    """(prefix, suffix) cut to whole lines within the char budgets, the
    prefix annotated with last-run values."""
    prefix = req.annotated_prefix()
    if len(prefix) > prefix_chars:
        cut = prefix.rfind("\n", 0, len(prefix) - prefix_chars)
        prefix = prefix[cut + 1:] if cut >= 0 else prefix[-prefix_chars:]
    suffix = req.suffix
    if len(suffix) > suffix_chars:
        cut = suffix.find("\n", suffix_chars)
        suffix = suffix[:cut] if cut >= 0 else suffix[:suffix_chars]
    return prefix, suffix


def clean_completion(text: str, suffix: str) -> str:
    """Strip a whole-message code fence and trim a tail that duplicates
    the start of the suffix (models like to close what the file already
    closes)."""
    m = _FENCE_RE.match(text)
    if m:
        text = m.group(1)
    s = suffix.lstrip()
    if s and text:
        probe = s.split("\n", 1)[0].rstrip()
        if probe:
            t = text.rstrip()
            for n in range(min(len(probe), len(t)), 0, -1):
                if t.endswith(probe[:n]) and (n >= 3 or probe[:n] in (")", "]", "}", '"', "'")):
                    text = t[:-n]
                    break
    return text


@fim_provider(name="claude", session=ClaudeSession)
def claude_fim(req: FimRequest, session: ClaudeSession, model="claude-opus-5",
               effort="low", prefix_chars=8000, suffix_chars=2000,
               fallbacks=True) -> FimResult:
    """Claude over the Messages API. `effort` is the Opus 5 thinking depth
    (None omits it — for models without effort). `fallbacks` opts into the
    server-side refusal fallback (`fallbacks: "default"`) so a classifier
    decline is rerouted instead of leaving the ghost empty."""
    prefix, suffix = _window(req, prefix_chars, suffix_chars)
    ctx = req.context
    blocks = []
    stable = ctx.render(("stable",)) if ctx is not None else ""
    if stable:
        blocks.append({"type": "text", "text": "<context>\n" + stable + "\n</context>",
                       "cache_control": {"type": "ephemeral"}})
    run = ctx.render(("run",)) if ctx is not None else ""
    if run:
        blocks.append({"type": "text", "text": "<last_run>\n" + run + "\n</last_run>",
                       "cache_control": {"type": "ephemeral"}})
    volatile = ctx.render(("volatile",)) if ctx is not None else ""
    body = ""
    if volatile:
        body += "<nearby_definitions>\n" + volatile + "\n</nearby_definitions>\n\n"
    body += (f"<file path=\"{req.path or 'buffer'}\" language=\"{req.language}\">\n"
             f"{prefix}<|cursor|>{suffix}\n</file>")
    blocks.append({"type": "text", "text": body})

    kwargs = dict(model=model, max_tokens=max(16, req.max_tokens),
                  system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
                  messages=[{"role": "user", "content": blocks}])
    if effort:
        kwargs["output_config"] = {"effort": effort}
    use_beta = bool(fallbacks)
    if use_beta:
        kwargs["betas"] = ["server-side-fallback-2026-07-01"]
        kwargs["fallbacks"] = "default"
    api = session.client.beta.messages if use_beta else session.client.messages

    acc = []
    with api.stream(**kwargs) as stream:
        for event in stream:
            if req.cancelled.is_set():
                stream.close()
                break
            if getattr(event, "type", None) == "text":
                acc.append(event.text)
                req.emit("".join(acc))
        final = None
        if not req.cancelled.is_set():
            try:
                final = stream.get_final_message()
            except Exception:
                final = None
    if final is not None and getattr(final, "stop_reason", None) == "refusal":
        return FimResult("", provider="claude")
    text = clean_completion("".join(acc), suffix)
    # Only "max_tokens" means the model was cut off (continue on Tab). "end_sequence"
    # / "stop_sequence" is a natural finish - don't auto-emit another suggestion.
    truncated = getattr(final, "stop_reason", None) == "max_tokens"
    return FimResult(text, provider="claude", truncated=truncated)
