"""Exact pre-runtime-migration instance initializer and active reader frame.

Captured from the repository baseline before terminal output subscriptions were
introduced. Used only to exercise live migration, including the old _ds access.
"""

class Terminal:
    def __init__(self, launch_cmd=None, tmux_session=None):
        # No args (the "+" add button calls Terminal()) -> create a brand-new OWNED
        # terminal: the in-app PTY itself creates the tmux session (new-session -A) when
        # it starts, then hands it off to its own gnome-terminal window (claude-d style).
        # No blocking `tmux new-session -d` on the UI thread first. With a launch_cmd/
        # session given (main/lsd/discovered claude-d) we just attach, as before.
        from meltygui.core.services.terminal_core import _new_owned_session_name
        from meltygui.core.services.terminal_core import _owned_launch_argv

        owned = launch_cmd is None and tmux_session is None
        if owned:
            tmux_session = _new_owned_session_name()
            launch_cmd = _owned_launch_argv(tmux_session)
        self.launch_cmd = launch_cmd or [os.environ.get("SHELL", "/bin/bash"), "-i"]
        self.tmux_session = tmux_session   # session name (if tmux attached)
        # add_to_collection keys a dict by `.id`, so a "+"-added terminal lands in the
        # store UNDER its session name; the studio poller then finds it already there
        # and doesn't create a duplicate.
        self.id = tmux_session
        self.master_fd = None
        self.pid = None            # the PTY child (a bash → tmux client); reaped by the reader
        self.screen = None
        self.stream = None
        self.lock = threading.Lock()
        self.started = False
        self.size = (0, 0)         # (cols, rows) currently applied to the PTY
        self.error = None
        self._ds = None            # this terminal's window draw_state (set by render)
                                   # so the reader thread can invalidate it on new output
        self._last_sig = None      # content signature at the last render - the reader
                                   # only invalidates/wakes when this actually changes
        self._reader_alive = False  # True while the reader thread runs; is_dead() reads it
        # For a brand-new OWNED terminal: the session the PTY creates, handed off to its
        # own gnome-terminal window once the PTY is up (in start()). None = attach-only.
        self._owned_session = tmux_session if owned else None
        self._appeared = False     # set text focus once, on the terminal's first render

    def _read_loop(self):
        from meltygui.core.windowing.glfw_utils import request_render

        fd = self.master_fd
        # Force a first paint. Guarded: this runs BEFORE the try below, so a None _ds
        # (a brand-new terminal whose screen hasn't rendered yet) would raise here and
        # kill the thread without the finally clearing _reader_alive, leaving a frozen,
        # never-drawn terminal. draw_terminal_screen sets _ds before start(), so it's
        # normally set, but stay defensive.
        if self._ds is not None:
            self._ds.invalidate()

        try:
          while True:
            try:
                r, _, _ = select.select([fd], [], [], 0.2)
                if fd not in r:
                    continue
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            with self.lock:
                self.stream.feed(data)
            # Coalesce a burst into ONE re-render. A flood of logs arrives as many
            # 64KB chunks; invalidating per chunk re-renders + re-blits the whole
            # window each time (the real cost; the pyte feed itself is cheap). So
            # keep feeding whatever was ALREADY queued before invalidating once. The
            # time/byte budget keeps continuous output updating (~100Hz) instead of
            # the drain ever starving the render. Each chunk feeds under its own lock
            # so the render can still interleave.
            ended = False
            deadline = time.monotonic() + 0.008
            read_total = len(data)
            while read_total < (1 << 20) and time.monotonic() < deadline:
                try:
                    r, _, _ = select.select([fd], [], [], 0)
                    if fd not in r:
                        break
                    more = os.read(fd, 65536)
                except OSError:
                    ended = True
                    break
                if not more:
                    ended = True
                    break
                with self.lock:
                    self.stream.feed(more)
                read_total += len(more)
            # Only invalidate + wake the loop if this read actually CHANGED what we
            # draw. The session pane (the studio's own console) dribbles bytes that
            # don't alter the visible output; invalidating per read re-rendered the app
            # every frame. The signature is computed once per read burst, so it's
            # cheap, and it's the only thing that calls request_render here - no data
            # change, no wake. (invalidate is the tile cache; request_render wakes
            # the render thread; both gated on a real change.)
            with self.lock:
                sig = self._screen_signature()
            if sig != self._last_sig:
                ds = self._ds
                if ds is not None:
                    ds.invalidate()
                try:
                    request_render()
                    self._last_sig = sig          # commit only after a successful wake
                except Exception:
                    # GLFW not initialized yet (a reader can fire during studio startup,
                    # before the window exists). request_render()'s get_current_context()
                    # raises then, which would otherwise kill this thread → frozen
                    # terminal. Swallow here and leave _last_sig stale so the next read
                    # retries once GLFW is up.
                    pass
            if ended:
                break
        finally:
            self._reap()
            self._reader_alive = False   # reader exited -> is_dead = True -> io drops us
