"""FIM (fill-in-the-middle) code completion: the contract, the registries and
the per-editor broker. `draw_text` talks ONLY to `FimState`; providers talk
ONLY to `FimRequest`/`FimResult`; nothing in the editor knows which backend
answered.

    draw_text ──► FimState (per draw_state, injected like GLState)
                    │  debounce · key dedup · cancel · chunked buffer · prefetch
                    ▼
                  @fim_provider function (stateless, one per backend)
                    │  session kwargs split off its FimSession.__init__
                    ▼
                  FimSession (per connection config: LS process, HTTP client, login)
                    pooled on Melty, REFCOUNTED by editors, idle-closed

Three lifetimes, deliberately separate:
  * FimSession  — one per (provider, connection config). Two editors on the
                  same config share one; "copilot-work" and "copilot-home"
                  are two. Never a singleton.
  * FimState    — one per editor draw_state: which profile, the in-flight
                  request, the chunked ghost buffer, the pinned context.
  * FimRequest  — one per keystroke burst.

Chunked acceptance: the provider always fetches a LONG completion
(`Toggles.Fim.max_tokens`, streamed); the editor only ever SHOWS one chunk of
it (`Toggles.Fim.chunk_lines` newline-terminated segments). Tab splices the
visible chunk and the next chunk becomes the ghost instantly — no request.
When the buffer runs low and the stream has ended, a continuation request
is prefetched from the virtual caret so arbitrarily long completions stay
snappy, and a small chunk gives the user a steering decision every few
lines instead of after a 40-line dump.

Coordinates: the broker's virtual document lives in BUFFER coordinates (the
editor span, what `draw_text` edits); a request is built in FILE coordinates
(`EditorView.file_head` + buffer + `file_tail`, PENDING truth) so providers
see the whole file.

Registries live on `Melty` (`_fim_providers`, `_fim_profiles`,
`_fim_context_sources`, `_fim_sessions`) so hotswap's registry reconcile
(`_FUNC_REGISTRY_NAMES` in file_converters) keeps them pointing at the live
functions after a recompile re-runs the decorators.
"""
from __future__ import annotations

import inspect
import re
import threading
import time
import traceback
import weakref
from dataclasses import dataclass, field
from typing import Any, Callable


# ──────────────────────────────────────────────────────────────────────────
# Registries (on Melty - see module docstring). Accessed lazily so this
# module imports headless (tests) without dragging imgui in.
# ──────────────────────────────────────────────────────────────────────────

def _melty():
    from meltygui.core.melty import Melty
    return Melty


def _reg(name):
    m = _melty()
    reg = getattr(m, name, None)
    if reg is None:
        reg = {}
        setattr(m, name, reg)
    return reg


def providers() -> dict:
    return _reg("_fim_providers")


def profiles() -> dict:
    return _reg("_fim_profiles")


def context_sources() -> dict:
    return _reg("_fim_context_sources")


def _sessions() -> dict:
    """The live session pool — kept on `sys` (not Melty) so it SURVIVES an
    in-process restart (the server purges every `src.*` module and
    re-imports; `sys` does not), keeping Copilot language-server processes
    and HTTP clients alive so a restart never re-spawns or re-logs-in.

    The pool's `session_key` is (module string, qualname string, kwargs),
    all stable across a re-exec, so the re-imported session classes resolve
    to the same cached instances. A cached instance pins its BIRTH module
    generation through its class (and the Copilot reader thread) — bounded
    to one generation because sessions are created once and reused, which
    is the unavoidable cost of holding a live subprocess across restarts.
    Real process exit: the Copilot LS self-terminates on parent-PID death
    (`processId` in initialize), and HTTP clients hold nothing that leaks,
    so no atexit closer is registered (that would pin gen-1 Melty)."""
    import sys
    pool = getattr(sys, "_lsd_fim_sessions", None)
    if pool is None:
        pool = sys._lsd_fim_sessions = {}
    return pool


def _session_alive(sess) -> bool:
    """A pooled session is reusable unless its `alive()` says otherwise (a
    Copilot LS that exited, an HTTP client closed)."""
    try:
        chk = getattr(sess, "alive", None)
        return bool(chk()) if callable(chk) else True
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────────
# Contract
# ──────────────────────────────────────────────────────────────────────────

TIERS = ("stable", "run", "volatile")


@dataclass(frozen=True)
class ContextItem:
    """One piece of context the model sees. `tier` decides WHERE in the
    prompt it goes (stable → run → volatile, cache breakpoints after the
    first two); `score` decides membership only — never order, a wobbling
    order would re-byte the cached prefix."""
    kind: str                      # definition | signature | enclosing | caller | outline | runtime_types | live_values | call_stack | error
    key: tuple                     # identity for dedupe/diff
    text: str                      # final rendered text
    tier: str = "stable"           # stable | run | volatile
    score: float = 1.0
    path: str | None = None
    line: int | None = None        # 1-based file line of the item's start
    end: int | None = None
    version: Any = None            # an invalidation token (pending gen / run gen)
    data: Any = None               # kind-specific: definition → its signature line (degrade target);
                                   # live_values → {0-based buffer line: summary}

    @property
    def tokens(self) -> int:
        return max(1, len(self.text) // 4)


@dataclass
class FimContext:
    """The fitted, deterministically ordered context for one editor."""
    items: list = field(default_factory=list)
    key: tuple = ()

    def tier(self, name) -> list:
        return [it for it in self.items if it.tier == name]

    def tokens(self) -> int:
        return sum(it.tokens for it in self.items)

    def render(self, tiers=("stable",), header="# {path}:{line}-{end}") -> str:
        """Plain-text rendering of the given tiers, in stored order, each
        item under a provenance header when it has one."""
        out = []
        for it in self.items:
            if it.tier not in tiers or it.kind == "live_values":
                continue
            if it.path and it.line:
                out.append(header.format(path=it.path, line=it.line,
                                         end=it.end if it.end else it.line))
            out.append(it.text.rstrip("\n"))
            out.append("")
        return "\n".join(out).rstrip("\n")

    def line_annotations(self) -> dict:
        """{0-based buffer line: summary} merged from every `live_values`
        item — providers splice these into the prefix as trailing comments."""
        ann = {}
        for it in self.items:
            if it.kind == "live_values" and isinstance(it.data, dict):
                ann.update(it.data)
        return ann


@dataclass
class EditorView:
    """What the editor knows about itself — the input to context sources
    and the file-coordinate frame for requests. Built per editor body run,
    so everything O(file) is lazy: `file_head`/`file_tail` (the PENDING
    file text before/after the buffer) are computed on first access, i.e.
    at submit time, and `fn` (the live function object / store owner) is
    resolved when context is assembled."""
    path: str | None
    text: str                       # editor text (the span)
    cursor: int                     # caret offset into `text`
    address: Any = None             # the span's Address (path/start/end)
    fn: Any = None
    language: str = "python"
    version: Any = None             # pending version of `path`
    _head: str | None = None
    _tail: str | None = None

    def _split(self):
        from meltygui.completion.fim_context import file_head_tail
        self._head, self._tail = file_head_tail(self.text, self.address)

    @property
    def file_head(self) -> str:
        if self._head is None:
            self._split()
        return self._head

    @property
    def file_tail(self) -> str:
        if self._tail is None:
            self._split()
        return self._tail

    @property
    def span_start(self) -> int:
        """0-based file line the buffer starts at."""
        start = getattr(self.address, "start", None) if self.address is not None else None
        return int(start) if isinstance(start, int) and start >= 0 else 0

    @property
    def caret_line(self) -> int:
        return self.text.count("\n", 0, self.cursor)

    def resolve_fn(self):
        if self.fn is None and self.path is not None:
            from meltygui.completion.fim_context import span_function
            self.fn = span_function(self.path, self.span_start)
        return self.fn


@dataclass
class FimRequest:
    """One completion request in FILE coordinates: `text` is the whole file
    (pending truth) so a provider can window it however it likes;
    `prefix`/`suffix` are the convenience split at `cursor`."""
    path: str | None
    text: str
    cursor: int
    language: str
    version: Any
    context: FimContext
    max_tokens: int
    cancelled: threading.Event
    emit: Callable[[str], None]     # emit the FULL text produced so far (streaming)
    span_start: int = 0             # buffer line 0 == file line span_start (for requests)

    @property
    def prefix(self) -> str:
        return self.text[:self.cursor]

    @property
    def suffix(self) -> str:
        return self.text[self.cursor:]

    def annotated_prefix(self, prefix=None, marker="  # ← ") -> str:
        """`prefix` (default: the request's) with the context's live-value
        summaries appended to their lines as trailing comments — what the
        live view shows a human, shown to the model. The caret's own
        (partial) line is never annotated."""
        if prefix is None:
            prefix = self.prefix
        ann = self.context.line_annotations() if self.context is not None else {}
        if not ann:
            return prefix
        lines = prefix.split("\n")
        for bl, summary in ann.items():
            fl = bl + self.span_start
            if 0 <= fl < len(lines) - 1 and lines[fl].strip():
                lines[fl] = lines[fl].rstrip() + marker + summary
        return "\n".join(lines)


@dataclass
class FimResult:
    text: str                        # insert at cursor
    replace_to: int = 0              # consume this many suffix chars (for text edits)
    alternatives: tuple = ()         # other candidates' full text
    token: Any = None                # arbitrary, handed back to the provider's _on_accept
    provider: str = ""
    truncated: bool = False           # the model hit the token limit (more to come) or
                                     # finished on a stop token. Only a truncated
                                     # completion is auto-continued when the buffer
                                     # drains - its natural stop is the end.


# ──────────────────────────────────────────────────────────────────────────
# Sessions
# ──────────────────────────────────────────────────────────────────────────

class FimSession:
    """Base class for a live connection (LS subprocess, HTTP client, login).
    Subclass `__init__(self, **config)` — its parameter NAMES are what
    `fim_profile(...)` kwargs get routed here (everything else goes to the
    provider function per request). Must be safe for concurrent `complete`
    calls from several editors' worker threads."""
    _refs = 0
    _idle_since = 0.0
    _key = None
    _profile = ""

    def status(self):
        """("ready",) | ("needs_login", user_code, url) | ("error", msg)"""
        return ("ready",)

    def alive(self):
        """False when the session can no longer serve requests (a crashed
        subprocess) and must be recreated on the next acquire. HTTP-client
        sessions stay alive; process-backed ones override this."""
        return True

    def close(self):
        pass

    def restart(self):
        self.close()


def _session_param_names(session_cls) -> set:
    if session_cls is None:
        return set()
    try:
        sig = inspect.signature(session_cls.__init__)
    except (TypeError, ValueError):
        return set()
    return {n for n, p in sig.parameters.items()
            if n != "self" and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)}


def split_kwargs(provider_fn, kwargs: dict):
    """(session_kwargs, request_kwargs) for a profile's kwargs."""
    names = _session_param_names(getattr(provider_fn, "_fim_session_cls", None))
    s = {k: v for k, v in kwargs.items() if k in names}
    r = {k: v for k, v in kwargs.items() if k not in names}
    return s, r


def session_key(session_cls, session_kwargs: dict) -> tuple:
    return (session_cls.__module__, session_cls.__qualname__,
            tuple(sorted(session_kwargs.items())))


_pool_lock = threading.Lock()


def acquire_session(profile_name: str):
    """The pooled session for `profile_name` (+1 ref), or None for a
    provider without sessions. Raises on construction failure. Called from
    worker threads (constructing a session may import an SDK or spawn a
    process — never on the render thread); the pool is locked."""
    with _pool_lock:
        return _acquire_session_locked(profile_name)


def _acquire_session_locked(profile_name: str):
    prof = profiles().get(profile_name)
    if prof is None:
        raise KeyError(f"unknown FIM profile {profile_name!r} "
                       f"(known: {sorted(profiles())})")
    fn = providers().get(prof.provider)
    if fn is None:
        raise KeyError(f"profile {profile_name!r}: unknown provider {prof.provider!r}")
    cls = getattr(fn, "_fim_session_cls", None)
    if cls is None:
        return None
    skw, _ = split_kwargs(fn, dict(prof.kwargs))
    key = session_key(cls, skw)
    pool = _sessions()
    sess = pool.get(key)
    if sess is not None and not _session_alive(sess):
        pool.pop(key, None)
        try:
            sess.close()
        except Exception:
            pass
        sess = None
    if sess is None:
        sess = cls(**skw)
        sess._key = key
        sess._profile = profile_name
        sess._refs = 0
        pool[key] = sess
    sess._refs += 1
    return sess


def session_for(session_cls, session_kwargs: dict, create=True):
    """The pooled session for an explicit (class, kwargs) — the SAME key a
    profile with those session kwargs resolves to, so e.g. the Internet
    Accounts window and the copilot provider share one LS process. No
    refcount is taken (the idle sweep reclaims it). `create=False` only
    peeks."""
    key = session_key(session_cls, session_kwargs)
    with _pool_lock:
        pool = _sessions()
        sess = pool.get(key)
        if sess is not None and not _session_alive(sess):
            pool.pop(key, None)
            try:
                sess.close()
            except Exception:
                pass
            sess = None
        if sess is None and create:
            sess = session_cls(**session_kwargs)
            sess._key = key
            sess._profile = ""
            sess._refs = 0
            sess._idle_since = time.monotonic()
            pool[key] = sess
        return sess


def drop_sessions(pred):
    """Close every pooled session `pred(sess)` selects and make the editors
    holding it re-acquire (a credential changed). Returns the count."""
    pool = _sessions()
    dropped = []
    with _pool_lock:
        for key, sess in list(pool.items()):
            try:
                hit = pred(sess)
            except Exception:
                hit = False
            if hit:
                pool.pop(key, None)
                dropped.append(sess)
    for sess in dropped:
        for st in list(_live_states):
            if st.session is sess:
                st.session = None
                st._session_resolved = False
                st._session_error = None
        try:
            sess.close()
        except Exception as e:
            print(f"[fim] session close failed: {e}")
    return len(dropped)


def release_session(sess):
    if sess is None:
        return
    sess._refs = max(0, sess._refs - 1)
    if sess._refs == 0:
        sess._idle_since = time.monotonic()


def sweep_sessions(now=None, idle_s=None):
    """Close zero-ref sessions idle longer than `idle_s`
    (`Toggles.Fim.session_idle_s`). Returns the number closed."""
    if idle_s is None:
        from meltygui.core.runtime.toggles import Toggles
        idle_s = Toggles.Fim.session_idle_s
    now = time.monotonic() if now is None else now
    pool = _sessions()
    n = 0
    for key, sess in list(pool.items()):
        if sess._refs <= 0 and now - sess._idle_since >= idle_s:
            pool.pop(key, None)
            try:
                sess.close()
            except Exception as e:
                print(f"[fim] session close failed: {e}")
            n += 1
    return n


def restart_session(profile_name: str):
    """Drop the pooled session for a profile (editors re-acquire lazily) —
    the escape hatch after hotswapping a session class's __init__."""
    pool = _sessions()
    for key, sess in list(pool.items()):
        if sess._profile == profile_name:
            pool.pop(key, None)
            try:
                sess.close()
            except Exception as e:
                print(f"[fim] session close failed: {e}")
    for st in list(_live_states):
        if st.profile_name == profile_name:
            st.session = None
            st._session_resolved = False
            st._session_error = None


def detach_sessions_for_restart():
    """Keep every pooled session alive across an in-process restart: zero
    its refcount (the current generation's FimStates are about to be
    dropped with their module) and stamp it idle-now, so the next
    generation re-acquires it within the idle window rather than
    re-spawning. Closes nothing."""
    now = time.monotonic()
    for sess in list(_sessions().values()):
        sess._refs = 0
        sess._idle_since = now


def shutdown_all_sessions():
    """Close and drop EVERY pooled session (real teardown). Not used on the
    restart path — see FimState.shutdown_all — only where a genuine full
    close is wanted."""
    pool = _sessions()
    for key, sess in list(pool.items()):
        pool.pop(key, None)
        try:
            sess.close()
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────────
# Decorators
# ──────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FimProfile:
    name: str
    provider: str          # provider NAME (hotswap-stable; resolved at runtime)
    kwargs: tuple          # sorted (k, v) pairs


def fim_provider(name: str, session=None):
    """Register a provider function `fn(req: FimRequest, session, **params)
    -> FimResult`. `session` is the FimSession subclass whose instances the
    broker hands in (None for stateless providers). Params on the function's
    signature are the per-request knobs; a profile overrides them by name.
    Optional hooks by attribute: `fn._on_shown(req, result)`,
    `fn._on_accept(result, accepted_chars)`.
    Also registers a default profile under the provider's own name."""
    def deco(fn):
        fn._fim_name = name
        fn._fim_session_cls = session
        providers()[name] = fn
        profiles().setdefault(name, FimProfile(name, name, ()))
        return fn
    return deco


def fim_profile(name: str, provider, **kwargs):
    """A named configuration of a provider: `fim_profile("copilot-work",
    copilot_fim, config_dir=...)`. Kwargs naming the session class's __init__
    params select/construct the session; the rest override the provider
    function's per-request params."""
    pname = provider if isinstance(provider, str) else getattr(provider, "_fim_name", None)
    if not pname:
        raise ValueError(f"fim_profile({name!r}): provider must be a name or a @fim_provider")
    prof = FimProfile(name, pname, tuple(sorted(kwargs.items())))
    profiles()[name] = prof
    return prof


def fim_context_source(kind: str, tier: str = "stable", order: int = 100):
    """Register `fn(view: EditorView) -> Iterable[ContextItem]`. `order` is
    the deterministic position of this source's items within its tier."""
    def deco(fn):
        fn._fim_kind = kind
        fn._fim_tier = tier
        fn._fim_order = order
        context_sources()[kind] = fn
        return fn
    return deco


def profile_names() -> list:
    return sorted(profiles())


# ──────────────────────────────────────────────────────────────────────────
# Chunking
# ──────────────────────────────────────────────────────────────────────────

def _segments(buffer: str) -> list:
    return buffer.splitlines(keepends=True)


_CURSOR_TAIL_RE = re.compile(r"\n[ \t]+$")


def split_chunk(buffer: str, chunk_lines: int, complete_only: bool = False) -> str:
    """The first chunk of `buffer`: any leading blank segments (the model
    moving to a new line) plus up to `chunk_lines` content segments — and
    NEVER a trailing newline: the chunk's line end stays in the buffer, so
    accepting leaves the caret at the end of what was inserted and the
    NEXT chunk begins with "\\n" + the model's own indentation (the model
    decides where the caret goes). With `complete_only` (stream still
    running) an unterminated last segment is held back. A whitespace-only
    remainder is a chunk only when it is "\\n" + indentation (a final
    caret placement) and the stream is done."""
    parts = _segments(buffer)
    if not parts:
        return ""
    n = len(parts)
    i = 0
    while i < n and parts[i].strip() == "":
        i += 1
    if i == n:
        if not complete_only and _CURSOR_TAIL_RE.search(buffer):
            return buffer
        return ""
    taken = 0
    while i < n and taken < max(1, chunk_lines):
        i += 1
        taken += 1
    if complete_only and i == n and not parts[-1].endswith("\n"):
        i -= 1
    chunk = "".join(parts[:i])
    if chunk.endswith("\n"):
        chunk = chunk[:-1]
    return chunk if chunk.strip() else ""


def strip_cursor_tail(buffer: str) -> str:
    """Everything in `buffer` worth inserting at once (accept-all): a bare
    trailing newline run is dropped, "\\n" + indentation is kept."""
    if _CURSOR_TAIL_RE.search(buffer):
        return buffer
    return buffer.rstrip("\n")


def content_lines(buffer: str) -> int:
    return sum(1 for p in _segments(buffer) if p.strip())


def at_line_end(text: str, cursor: int) -> bool:
    """True when nothing but whitespace follows the caret on its own line —
    the FIM trigger condition (don't generate in the MIDDLE of a line).
    Trailing spaces and the rest of the file below are ignored."""
    rest = text[cursor:]
    nl = rest.find("\n")
    line_rest = rest if nl < 0 else rest[:nl]
    return line_rest.strip() == ""


_WORD_RE = re.compile(r"[^\S\n]*(?:\n[^\S\n]*|[A-Za-z0-9_]+|[^\sA-Za-z0-9_]+)")


def first_word(chunk: str) -> str:
    m = _WORD_RE.match(chunk)
    return m.group(0) if m else chunk[:1]


# ──────────────────────────────────────────────────────────────────────────
# Per-editor state (injected: `fim_state: FimState = None` on draw_text)
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class Ghost:
    text: str                 # the visible chunk ("" while the first chunk streams in)
    more_lines: int = 0       # content lines buffered beyond the chunk
    pending: bool = False     # a request is in flight
    profile: str = ""


_live_states = weakref.WeakSet()


def _wake(ds):
    """Wake the render loop once from a worker thread (tile invalidate +
    glfw event) — the loop is parked in wait_events, and request_render
    no-ops off the GL thread. Mirrors text_editor._wake_on_future."""
    try:
        tile = getattr(ds, "_tile_id", None) if ds is not None else None
        if tile is not None:
            _melty().cache.invalidate(tile)
    except Exception:
        pass
    try:
        import meltygui.core.windowing.glfw_utils as glfw_utils
        glfw_utils._needs_render.set()
    except Exception:
        pass
    try:
        if getattr(_melty(), "vis", None) is not None:     # a window exists (not headless)
            import meltygui.core.windowing.window_api as glfw
            glfw.post_empty_event()
    except Exception:
        pass


class FimState:
    """Broker + ghost buffer for ONE editor. Constructed by render_func's
    injected-state path (no-arg), bound to its draw_state via `_owner_ds`.

    Virtual document model (buffer coordinates): `_vtext` = prefix-at-
    request + everything the provider produced; `_vcursor` = how far into
    it the real buffer has caught up (accept / prefix-consume move it
    forward). The ghost buffer is `_vtext[_vcursor:]`. `_vsuffix` is the
    suffix the completion was made for — any change to it invalidates the
    generation."""
    _owner_ds = None

    def __init__(self):
        self.profile_name = ""
        self.session = None
        self.error = None
        self._lock = threading.RLock()
        self._gen = 0                 # increments on every invalidation
        self._vtext = ""
        self._vcursor = 0
        self._vsuffix = None          # None = no active generation
        self._streaming = False
        self._inflight = None         # FimRequest in flight
        self._inflight_origin = 0     # virtual offset the text started at
        self._thread = None
        self._timer = None
        self._key = None              # last scheduled request key
        self._dismissed_key = None
        self._exhausted = False       # continuation returned nothing
        self._last_wake = 0.0
        self._wake_lines = 0
        self._last_sweep = 0.0
        self._session_profile = None  # profile the session slot was resolved for
        self._session_error = None
        self._session_err_time = 0.0
        self.alternatives = ()
        self.context = None
        self.context_key = None
        self.context_time = 0.0
        self.last_result = None
        self.stats = {"requests": 0, "accepted_chunks": 0, "invalidations": 0}
        _live_states.add(self)

    def __reduce__(self):
        return (FimState, ())

    # ── profile / session ─────────────────────────────────────────────
    def _ensure_profile(self, profile_name):
        """Render thread: switch profiles (drop the old session ref and any
        generation made with it). The new session is acquired lazily on the
        worker thread by `_session_for_work`."""
        if profile_name == self._session_profile:
            return
        release_session(self.session)
        self.session = None
        self._session_resolved = False
        self.profile_name = profile_name
        self._session_profile = profile_name
        self._session_error = None
        self._session_err_time = 0.0
        self._key = None                  # re-request at the current site
        if self.active or self._timer is not None:
            self.invalidate("profile")

    _session_resolved = False

    def _session_for_work(self):
        """Worker thread: the profile's pooled session (None for a
        sessionless provider). A failed construction is re-raised for 5 s
        without retrying the constructor."""
        if self._session_resolved:
            return self.session
        if (self._session_error is not None
                and time.monotonic() - self._session_err_time < 5.0):
            raise RuntimeError(self._session_error)
        try:
            sess = acquire_session(self.profile_name)
        except Exception as e:
            self._session_error = str(e)
            self._session_err_time = time.monotonic()
            raise
        self._session_error = None
        self.session = sess
        self._session_resolved = True
        return sess

    def _provider(self):
        prof = profiles().get(self.profile_name)
        if prof is None:
            return None, {}
        fn = providers().get(prof.provider)
        if fn is None:
            return None, {}
        _, rkw = split_kwargs(fn, dict(prof.kwargs))
        return fn, rkw

    # ── buffer ─────────────────────────────────────────────────────────
    @property
    def buffer(self) -> str:
        with self._lock:
            return self._vtext[self._vcursor:] if self._vsuffix is not None else ""

    @property
    def active(self) -> bool:
        return self._vsuffix is not None

    def invalidate(self, reason=""):
        """Drop the generation: cancel in-flight work, clear the buffer."""
        with self._lock:
            self._gen += 1
            if self._inflight is not None:
                self._inflight.cancelled.set()
                self._inflight = None
            self._vtext = ""
            self._vcursor = 0
            self._vsuffix = None
            self._streaming = False
            self._exhausted = False
            self.alternatives = ()
            self.stats["invalidations"] += 1
            t = self._timer
            self._timer = None
            self._armed = None
        if t is not None:
            t.cancel()

    def dismiss(self):
        """Esc: drop the buffer and don't re-request at this site (the
        caret's CURRENT site, which accepts may have moved past the last
        scheduled one) until the caret moves on."""
        self._dismissed_key = self._cur_key
        self.invalidate("dismiss")

    _cur_key = None

    def _reconcile(self, text, cursor):
        """Advance the virtual cursor when the real buffer typed/accepted
        exactly what the ghost predicted; invalidate on any divergence."""
        with self._lock:
            if self._vsuffix is None:
                return
            prefix = text[:cursor]
            if text[cursor:] != self._vsuffix:
                self.invalidate("suffix")
                return
            n = len(prefix)
            if n < self._vcursor:
                self.invalidate("backspace")
                return
            if n > len(self._vtext):
                # typed past anything produced so far (exhausted, or
                # ahead of a stream) - a fresh request from here is cheap
                self.invalidate("overrun")
                return
            if self._vtext[:n] != prefix:
                self.invalidate("diverged")
                return
            self._vcursor = n

    # ── scheduling ──────────────────────────────────────────────────
    @staticmethod
    def _request_key(text, cursor):
        return (cursor, len(text), text[max(0, cursor - 48):cursor], text[cursor:cursor + 48])

    def poll(self, text, cursor, *, view: EditorView, profile: str = "",
             enabled=True, ds=None, now=None, typed=True) -> Ghost | None:
        """Called from the editor body after the key handlers. Reconciles
        the buffer with the real text, schedules/continues requests, and
        returns what to draw (or None). `typed` = the buffer changed this
        frame: only typing arms a fresh request — a caret move never does,
        and one during the debounce cancels the armed request."""
        from meltygui.core.runtime.toggles import Toggles
        if ds is not None:
            self._owner_ds = ds
        if not enabled or not Toggles.Fim.enabled:
            if self.active or self._timer is not None:
                self.invalidate("disabled")
            return None
        now = time.monotonic() if now is None else now
        self._ensure_profile(profile or Toggles.Fim.profile)
        self._reconcile(text, cursor)
        key = self._request_key(text, cursor)
        self._cur_key = key
        if key != self._dismissed_key:
            self._dismissed_key = None
        chunk_lines = max(1, Toggles.Fim.chunk_lines)
        with self._lock:
            chunk = self._chunk(chunk_lines)
            ahead = content_lines(self.buffer[len(chunk):])
            active = self.active
            need_more = (active and not self._streaming and self._inflight is None
                         and not self._exhausted and Toggles.Fim.prefetch
                         and ahead < chunk_lines)
            pending = self._inflight is not None
        if not active and key != self._key and self._armed is not None:
            # the site moved during the debounce: drop the armed request
            self._armed = None
            t = self._timer
            self._timer = None
            if t is not None:
                t.cancel()
        if not active:
            if key != self._key:
                self._key = key
                # Only generate on typing, at a site the user hasn't seen,
                # and (when only_at_line_end is on) with nothing but space
                # after the cursor on this line - no mid-line completions.
                if (typed and self._dismissed_key != key
                        and (not Toggles.Fim.only_at_line_end or at_line_end(text, cursor))):
                    self._schedule(text, cursor, view, Toggles.Fim.debounce_s, now)
            elif self._fire_armed(text, cursor, view, now):
                pending = True
        elif need_more:
            self._key = key
            self._schedule(None, None, view, 0.0, now, continuation=True)
            pending = True
        if now - self._last_sweep > 30.0:
            self._last_sweep = now
            try:
                sweep_sessions(now)
            except Exception:
                pass
        if not chunk:
            return Ghost("", 0, True, self.profile_name) if pending else None
        return Ghost(chunk, ahead, pending, self.profile_name)

    def _chunk(self, chunk_lines):
        buf = self.buffer
        if not buf:
            return ""
        # Hold back an unterminated final line ONLY while it is genuinely
        # still streaming in - i.e. the in-flight request appends at (or
        # before) the shown buffer's start (the initial request:
        # _inflight_origin == _vcursor). A CONTINUATION appends beyond the
        # already-shown buffer (_inflight_origin > _vcursor), so the visible
        # first chunk is settled text and must never be held back: doing so
        # blinked the ghost out for each continuation round trip (the
        # appear → disappear → reappear flicker).
        streaming_tail = self._streaming and self._inflight_origin <= self._vcursor
        return split_chunk(buf, chunk_lines, complete_only=streaming_tail)

    def _refresh_context(self, view, now):
        if self.context is not None and not self._context_stale(view, now):
            return
        try:
            self.context = assemble_context(view)
            self.context_key = self.context.key
            self.context_time = now
        except Exception as e:
            if self.context is None:
                self.context = FimContext()
            self.error = f"context: {e}"

    def _schedule(self, text, cursor, view, delay, now, continuation=False):
        """Arm (or fire) a request. With a delay the request is only ARMED:
        a timer wakes the render loop when the debounce elapses and the
        next `poll` (render thread — the roster is not thread-safe) does
        the context assembly + submit. So a caret-move burst costs nothing
        but timer churn; the heavy work runs once, when the user pauses.
        Continuations (delay 0) assemble + submit right here."""
        with self._lock:
            t = self._timer
            self._timer = None
            if continuation:
                if self._vsuffix is None:
                    return
                text = self._vtext + self._vsuffix
                cursor = len(self._vtext)
            gen = self._gen
            self._armed = None
        if t is not None:
            t.cancel()
        if delay <= 0:
            self._submit(text, cursor, view, gen, continuation)
            return
        self._armed = (gen, now + delay)
        ds = self._owner_ds

        def fire():
            self._timer = None
            _wake(ds)

        timer = threading.Timer(delay, fire)
        timer.daemon = True
        self._timer = timer
        timer.start()

    _armed = None      # (gen, due) of a debounced fresh request awaiting its poll

    def _fire_armed(self, text, cursor, view, now):
        """Submit the armed request if its debounce has elapsed (called
        from poll with the CURRENT buffer — same key, so the same site)."""
        armed = self._armed
        if armed is None or self._timer is not None:
            return False
        gen, due = armed
        if now < due:
            return False
        self._armed = None
        if gen != self._gen:
            return False
        self._submit(text, cursor, view, gen, False)
        return True

    def _context_stale(self, view, now):
        from meltygui.core.runtime.toggles import Toggles
        try:
            key = context_key_for(view)
        except Exception:
            return False
        if key != self.context_key:
            return True
        return now - self.context_time > Toggles.Fim.stable_refresh_s

    def _submit(self, text, cursor, view, gen, continuation):
        """Start the worker for a request over the virtual buffer
        (`text`/`cursor` in buffer coordinates)."""
        from meltygui.core.runtime.toggles import Toggles
        fn, rkw = self._provider()
        if fn is None:
            self.error = f"no provider for profile {self.profile_name!r}"
            return
        self._refresh_context(view, time.monotonic())
        head = view.file_head if view is not None else ""
        tail = view.file_tail if view is not None else ""
        with self._lock:
            if gen != self._gen or self._inflight is not None:
                return
            cancelled = threading.Event()
            if not continuation:
                self._vtext = text[:cursor]
                self._vcursor = cursor
                self._vsuffix = text[cursor:]
                self._exhausted = False
            origin = cursor
            self._inflight_origin = origin
            self._streaming = True
            req = FimRequest(
                path=view.path if view is not None else None,
                text=head + text + tail, cursor=len(head) + cursor,
                language=view.language if view is not None else "python",
                version=view.version if view is not None else None,
                context=self.context or FimContext(),
                max_tokens=Toggles.Fim.max_tokens,
                cancelled=cancelled,
                emit=lambda t, g=gen, o=origin: self._on_partial(g, o, t),
                span_start=view.span_start if view is not None else 0)
            self._inflight = req
            self.stats["requests"] += 1
        ds = self._owner_ds

        def work():
            result = None
            err = None
            try:
                session = self._session_for_work()
                result = fn(req, session, **rkw)
            except Exception as e:
                err = e
                if not cancelled.is_set() and Toggles.Fim.debug_print:
                    traceback.print_exc()
            self._on_done(gen, origin, req, result, err)
            _wake(ds)

        th = threading.Thread(target=work, name=f"fim-{self.profile_name}", daemon=True)
        self._thread = th
        th.start()

    # ── worker thread callbacks ────────────────────────────────────────
    def _on_partial(self, gen, origin, full_text):
        with self._lock:
            if gen != self._gen or self._vsuffix is None:
                return
            self._vtext = self._vtext[:origin] + full_text
            self._vcursor = min(self._vcursor, len(self._vtext))
            lines = full_text.count("\n")
        now = time.monotonic()
        if lines != self._wake_lines or now - self._last_wake > 0.15:
            self._wake_lines = lines
            self._last_wake = now
            _wake(self._owner_ds)

    def _on_done(self, gen, origin, req, result, err):
        with self._lock:
            if self._inflight is req:
                self._inflight = None
            if gen != self._gen or self._vsuffix is None:
                return
            self._streaming = False
            if err is not None:
                self.error = f"{self.profile_name}: {err}"
                self._exhausted = True
                return
            self.error = None
            text = result.text if result is not None else ""
            self.last_result = result
            self.alternatives = tuple(result.alternatives) if result is not None else ()
            self._vtext = self._vtext[:origin] + text
            self._vcursor = min(self._vcursor, len(self._vtext))
            # A continuation is fetched ONLY when the model was cut off by the
            # token limit (truncated). A natural stop-text end - or an empty
            # reply - is the end: don't queue another request behind it.
            if not text or not getattr(result, "truncated", False):
                self._exhausted = True
        fn, _ = self._provider()
        hook = getattr(fn, "_on_shown", None)
        if hook is not None and result is not None:
            try:
                hook(req, result)
            except Exception:
                pass

    # ── acceptance ─────────────────────────────────────────────────────
    def accept(self, mode="chunk") -> str:
        """The text the editor should splice at the caret ("" if nothing).
        Advances the buffer; the editor then moves its caret by len()."""
        from meltygui.core.runtime.toggles import Toggles
        with self._lock:
            chunk = self._chunk(max(1, Toggles.Fim.chunk_lines))
            if not chunk:
                return ""
            if mode == "word":
                chunk = first_word(chunk)
            elif mode == "line":
                nl = chunk.find("\n")
                chunk = chunk if nl < 0 else chunk[:nl + 1]
            elif mode == "all" and not self._streaming:
                chunk = strip_cursor_tail(self.buffer) or chunk
            self._vcursor += len(chunk)
            self.stats["accepted_chunks"] += 1
            accepted = self._vcursor - self._inflight_origin
        fn, _ = self._provider()
        hook = getattr(fn, "_on_accept", None)
        if hook is not None and self.last_result is not None:
            try:
                hook(self.last_result, accepted)
            except Exception:
                pass
        return chunk

    # ── lifecycle ────────────────────────────────────────────────────
    def release(self):
        self.invalidate("release")
        release_session(self.session)
        self.session = None
        self._session_resolved = False
        self._session_profile = None
        self._session_error = None
        self.profile_name = ""
        self.context = None

    @classmethod
    def on_window_deleted(cls, window_ds):
        """GLState.on_window_deleted's twin: release every state owned by a
        draw_state under the deleted window."""
        if window_ds is None:
            return
        for state in list(_live_states):
            node = state._owner_ds
            hops = 0
            while node is not None and hops < 64:
                if node is window_ds:
                    state.release()
                    break
                nxt = getattr(node, "parent_window", None)
                if nxt is None or nxt is node:
                    break
                node = nxt
                hops += 1

    @classmethod
    def shutdown_all(cls):
        """Called from Melty.cleanup — which runs on an in-process RESTART,
        not just real exit. So it does NOT close the pooled sessions (they
        live on `sys` and are reused next generation): it only cancels the
        current generation's in-flight requests and detaches the sessions so
        the next generation re-acquires them instead of re-spawning /
        re-logging-in. Sessions are closed only by the idle sweep, an
        explicit restart_session / credential change (drop_sessions), or the
        Copilot LS self-terminating on process exit."""
        for state in list(_live_states):
            try:
                state.invalidate("shutdown")   # cancel in-flight, keep the session
            except Exception:
                pass
        detach_sessions_for_restart()


# ──────────────────────────────────────────────────────────────────────────
# Context assembly (sources live in fim_context.py; this is the fitter)
# ──────────────────────────────────────────────────────────────────────────

def context_key_for(view: EditorView) -> tuple:
    """What stable-tier membership is keyed on: the file, its pending gen,
    and the enclosing def of the caret (its line in the buffer) — NOT the
    caret itself, so typing inside one function reuses the prefix."""
    from meltygui.completion.fim_context import enclosing_def_line
    return (view.path, view.version, enclosing_def_line(view.text, view.cursor))


_TIER_SHARE = {"volatile": 0.15, "run": 0.25}   # stable takes the remainder


def assemble_context(view: EditorView, budget_tokens=None) -> FimContext:
    """Run every registered context source, dedupe by key, fit each tier to
    its budget share by score (degrading definition → signature before
    dropping), and order deterministically within a tier (source order,
    then key). Stored order is prompt order: stable, run, volatile."""
    from meltygui.core.runtime.toggles import Toggles
    import meltygui.completion.fim_context  # noqa: F401  (registers the built-in sources)
    if budget_tokens is None:
        budget_tokens = Toggles.Fim.context_tokens
    view.resolve_fn()
    seen = {}
    order_of = {}
    for kind, src in context_sources().items():
        order_of[kind] = getattr(src, "_fim_order", 100)
        try:
            items = list(src(view) or ())
        except Exception as e:
            if Toggles.Fim.debug_print:
                print(f"[fim] context source {kind} failed: {e}")
            continue
        for it in items:
            prev = seen.get(it.key)
            if prev is None or it.score > prev.score:
                seen[it.key] = it
    items = list(seen.values())
    kept = []
    spent = 0
    for tier in ("volatile", "run", "stable"):
        cap = int(budget_tokens * _TIER_SHARE[tier]) if tier in _TIER_SHARE else max(0, budget_tokens - spent)
        ranked = sorted((it for it in items if it.tier == tier), key=lambda it: -it.score)
        used = 0
        for it in ranked:
            t = it.tokens
            if used + t <= cap:
                kept.append(it)
                used += t
                continue
            if it.kind == "definition" and isinstance(it.data, str) and it.data:
                sig = ContextItem("signature", it.key, it.data, it.tier, it.score,
                                  it.path, it.line, it.line, it.version)
                if used + sig.tokens <= cap:
                    kept.append(sig)
                    used += sig.tokens
        spent += used
    kept.sort(key=lambda it: (TIERS.index(it.tier) if it.tier in TIERS else 9,
                              order_of.get(it.kind, 100), repr(it.key)))
    return FimContext(kept, context_key_for(view))
