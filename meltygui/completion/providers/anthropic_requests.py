"""Every Anthropic API request the studio makes announces itself — a toast
in the notifications overlay (tag "anthropic"; repeats while visible
coalesce into a count) plus a console line — so "who is hitting the API"
is always visible (born from an /api/oauth/usage rate-limit hunt, 08-25).
Gate: Toggles.InternetAccounts.notify_requests.

Two hooks cover the studio's traffic:
- `notify_request(what, detail=)` — hand-written call sites: the usage
  fetch, the browser sign-in's token exchange, launching Claude Code's
  `claude auth login` (that one is Claude Code's own traffic, labelled so).
- `sdk_middleware()` — anthropic SDK middleware for
  `Anthropic(middleware=[…])` (ClaudeSession and the Test button pass it):
  the chain runs once per HTTP ATTEMPT inside the SDK's retry loop, so FIM
  completions, Test, AND every retry announce with method + path, and a
  non-2xx attempt is called out with its status ("429 · rate limited").

Known gap: the SDK's own profile token refresh (anthropic/lib/credentials,
its private httpx client) doesn't run through client middleware.
"""
from __future__ import annotations

import time


def enabled() -> bool:
    from meltygui.toggles import Toggles
    return bool(Toggles.InternetAccounts.notify_requests)


def notify_request(what, detail=""):
    """One announced request: console line + overlay toast (never raises,
    never imports the SDK)."""
    if not enabled():
        return
    text = what + (f" · {detail}" if detail else "")
    print(f"[anthropic] {time.strftime('%H:%M:%S')} {text}")
    try:
        from meltygui.notifications import notify
        notify(text, tint=(0.85, 0.55, 0.35, 1.0), tag="anthropic")
    except Exception:
        pass


_sdk_middleware = None


def sdk_middleware():
    """The (cached) middleware instance for `anthropic.Anthropic(middleware=
    [sdk_middleware()])`. Built lazily so importing this module never pays
    the anthropic import."""
    global _sdk_middleware
    if _sdk_middleware is None:
        from anthropic import Middleware

        class RequestNotifier(Middleware):
            def handle(self, request, call_next):
                url = getattr(request, "url", None)
                path = getattr(url, "path", "") or str(url or "?")
                method = getattr(request, "method", "?")
                notify_request(f"{method} {path}")
                response = call_next(request)
                status = getattr(response, "status_code", None)
                if status is not None and status >= 400:
                    notify_request(f"{method} {path} → {status}",
                                   "rate limited" if status == 429 else "")
                return response

        _sdk_middleware = RequestNotifier()
    return _sdk_middleware