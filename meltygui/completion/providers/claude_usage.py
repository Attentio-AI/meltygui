"""Claude subscription usage — the Max / Pro rate-limit windows (session,
weekly all-models, weekly per-model such as Fable) and the extra-usage
spend, for the Internet Accounts window's "Claude subscription" row.

Source: the claude.ai OAuth login Claude Code keeps in
`~/.claude/.credentials.json` (`claudeAiOauth.accessToken`). The studio
only READS that file — it never refreshes the token and never sends it
for inference; the numbers come from GET /api/oauth/usage, the call
Claude Code's own `/usage` makes. An API-org token (the studio's Console
sign-in, anthropic_oauth.py) is refused by that endpoint ("Usage limits
are not applicable to API organizations"), which is why the subscription
is its own account kind. A stale token is Claude Code's to refresh: run
`claude` once and the file is rewritten.

`parse_usage` normalises the payload to rows the window paints as bars:
the `limits` list (kind / percent / severity / resets_at / scope — the
general form, one entry per window incl. per-model ones), with the
legacy `five_hour` / `seven_day*` blocks as the fallback, plus `spend`
(or `extra_usage`) as an "Extra usage" row.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


# ──────────────────────────────────────────────────────────────────────────
# Claude Code's login file
# ──────────────────────────────────────────────────────────────────────────

def read_login(path):
    """Claude Code's claude.ai login: {"token", "expires_at" (unix s or
    None), "expired", "plan" ("Max 20x" / "Pro" / …), "scopes", "email",
    "organization", "path"} — or None when the file / login is missing.
    `email` / `organization` come from Claude Code's identity file next to
    it (`read_identity`); they say WHOSE usage the file yields, so two
    Internet Accounts rows sharing one file don't both paint it. No
    network."""
    path = Path(path).expanduser()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        return None
    expires_at = oauth.get("expiresAt")
    try:
        expires_at = float(expires_at) / 1000.0 if expires_at is not None else None
    except (TypeError, ValueError):
        expires_at = None
    identity = read_identity(path)
    return {"token": oauth["accessToken"],
            "expires_at": expires_at,
            "expired": expires_at is not None and expires_at <= time.time(),
            "plan": plan_label(oauth.get("subscriptionType"), oauth.get("rateLimitTier")),
            "scopes": list(oauth.get("scopes") or []),
            "email": (identity.get("emailAddress") or "").strip(),
            "organization": (identity.get("organizationName") or "").strip(),
            "path": path}


def read_identity(credentials_path) -> dict:
    """The `oauthAccount` block of Claude Code's `.claude.json` (no secrets:
    emailAddress, organizationName, displayName, …) for a credentials
    file: `<CLAUDE_CONFIG_DIR>/.claude.json` beside it, else the default
    layout's `~/.claude.json` one level up from `~/.claude/`. {} when
    absent."""
    credentials_path = Path(credentials_path).expanduser()
    for candidate in (credentials_path.parent / ".claude.json",
                      credentials_path.parent.parent / ".claude.json"):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        account = data.get("oauthAccount") if isinstance(data, dict) else None
        if isinstance(account, dict):
            return account
    return {}


def plan_label(subscription_type, rate_limit_tier) -> str:
    """"Max 20x" from subscriptionType "max" + rateLimitTier
    "default_claude_max_20x"; "Pro"; or whatever the type says."""
    name = (subscription_type or "").strip()
    name = name[:1].upper() + name[1:] if name else "Claude"
    multiplier = re.search(r"_(\d+)x$", rate_limit_tier or "")
    if multiplier:
        name += f" {multiplier.group(1)}x"
    return name


# ──────────────────────────────────────────────────────────────────────────
# The usage endpoint
# ──────────────────────────────────────────────────────────────────────────

def fetch_usage(token: str, url: str = USAGE_URL, timeout_s: float = 15.0) -> dict:
    """GET the usage payload with the claude.ai OAuth token (Bearer + the
    oauth beta header). RuntimeError with the status on failure."""
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20",
                      "Accept": "application/json", "User-Agent": "latent-descent"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            detail = detail.strip()[:160]
        if error.code == 401:
            detail = "token rejected — open Claude Code to refresh its login"
        raise RuntimeError(f"usage endpoint returned {error.code}: {detail}") from None
    except urllib.error.URLError as error:
        raise RuntimeError(f"usage endpoint unreachable: {error.reason}") from None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise RuntimeError("usage endpoint returned a non-JSON response") from None
    if not isinstance(payload, dict):
        raise RuntimeError("usage endpoint returned an unexpected payload")
    return payload


# ──────────────────────────────────────────────────────────────────────────
# Normalisation
# ──────────────────────────────────────────────────────────────────────────

def parse_usage(payload: dict) -> list[dict]:
    """Rows for the window: {"key", "label", "percent", "severity"
    ("normal" | "warning" | "critical" | "exceeded" | …), "resets_at" (unix
    s or None), "active", "detail"}. Limits first (the `limits` list, or
    the legacy blocks), the spend row last."""
    rows = []
    limits = payload.get("limits")
    if isinstance(limits, list) and limits:
        for entry in limits:
            if not isinstance(entry, dict):
                continue
            rows.append({"key": _limit_key(entry),
                         "label": _limit_label(entry),
                         "percent": _percent(entry.get("percent")),
                         "severity": entry.get("severity") or "normal",
                         "resets_at": parse_iso(entry.get("resets_at")),
                         "active": bool(entry.get("is_active")),
                         "detail": ""})
    else:
        for key, label in (("five_hour", "Session (5 h)"), ("seven_day", "Week · all models")):
            block = payload.get(key)
            if isinstance(block, dict):
                rows.append(_legacy_row(key, label, block))
        for key, block in payload.items():
            if (key.startswith("seven_day_") and isinstance(block, dict)
                    and key not in ("seven_day",)):
                rows.append(_legacy_row(key, "Week · " + key[len("seven_day_"):].replace("_", " ").title(), block))

    spend = payload.get("spend")
    if isinstance(spend, dict) and (spend.get("used") or spend.get("limit")):
        used = _money(spend.get("used"))
        limit = _money(spend.get("limit"))
        detail = f"{used} of {limit}" if limit else used
        if not spend.get("enabled"):
            reason = (spend.get("disabled_reason") or "off").replace("_", " ")
            detail += f" · off ({reason})"
        rows.append({"key": "spend", "label": "Extra usage",
                     "percent": _percent(spend.get("percent")),
                     "severity": spend.get("severity") or "normal",
                     "resets_at": None, "active": bool(spend.get("enabled")), "detail": detail})
    else:
        extra = payload.get("extra_usage")
        if isinstance(extra, dict) and extra.get("monthly_limit") is not None:
            places = int(extra.get("decimal_places") or 2)
            scale = 10 ** places
            currency = extra.get("currency") or "USD"
            used = _format_money(float(extra.get("used_credits") or 0) / scale, currency, places)
            limit = _format_money(float(extra["monthly_limit"]) / scale, currency, places)
            detail = f"{used} of {limit}"
            if not extra.get("is_enabled"):
                detail += f" · off ({(extra.get('disabled_reason') or 'off').replace('_', ' ')})"
            rows.append({"key": "spend", "label": "Extra usage",
                         "percent": _percent(extra.get("utilization")),
                         "severity": "normal", "resets_at": None,
                         "active": bool(extra.get("is_enabled")), "detail": detail})
    return rows


def summary(rows) -> str:
    """"session 13% · week 46% · Fable 88%" — the account row's status text."""
    parts = []
    for row in rows:
        if row["key"] == "spend":
            continue
        parts.append(f"{_short_label(row)} {row['percent']:.0f}%")
    return " · ".join(parts)


def reset_text(resets_at, now=None) -> str:
    """"resets in 3d 2h" / "2h 13m" / "14m" / "resetting…"; "" for None."""
    if resets_at is None:
        return ""
    now = time.time() if now is None else now
    remaining = int(resets_at - now)
    if remaining <= 0:
        return "resetting…"
    days, rest = divmod(remaining, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"resets in {days}d {hours}h"
    if hours:
        return f"resets in {hours}h {minutes:02d}m"
    return f"resets in {max(1, minutes)}m"


def parse_iso(text):
    """ISO-8601 (with offset) → unix seconds, None when absent/unparseable."""
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


# -- helpers ---------------------------------------------------------------

def _percent(value) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _scope_name(entry) -> str:
    scope = entry.get("scope") or {}
    model = scope.get("model") or {}
    return (model.get("display_name") or model.get("id") or scope.get("surface") or "").strip()


def _limit_key(entry) -> str:
    kind = entry.get("kind") or "limit"
    scope = _scope_name(entry)
    return f"{kind}:{scope}" if scope else kind


def _limit_label(entry) -> str:
    kind = entry.get("kind") or ""
    scope = _scope_name(entry)
    if kind == "session":
        return "Session (5 h)"
    if kind == "weekly_all":
        return "Week · all models"
    if kind.startswith("weekly"):
        return f"Week · {scope}" if scope else "Week"
    label = kind.replace("_", " ") or "limit"
    return f"{label} · {scope}" if scope else label


def _short_label(row) -> str:
    key = row["key"]
    if key == "session":
        return "session"
    if key == "weekly_all":
        return "week"
    if ":" in key:
        return key.split(":", 1)[1]
    return row["label"].lower()


def _legacy_row(key, label, block) -> dict:
    percent = _percent(block.get("utilization"))
    severity = "exceeded" if percent >= 100 else "critical" if percent >= 90 else "warning" if percent >= 75 else "normal"
    return {"key": key, "label": label, "percent": percent, "severity": severity,
            "resets_at": parse_iso(block.get("resets_at")), "active": False, "detail": ""}


def _money(block) -> str:
    if not isinstance(block, dict) or block.get("amount_minor") is None:
        return ""
    exponent = int(block.get("exponent") or 2)
    return _format_money(float(block["amount_minor"]) / (10 ** exponent),
                         block.get("currency") or "USD", exponent)


def _format_money(amount: float, currency: str, places: int) -> str:
    symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency, f"{currency} ")
    return f"{symbol}{amount:,.{places}f}"