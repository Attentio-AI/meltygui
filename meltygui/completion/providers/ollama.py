"""Ollama FIM provider — native fill-in-the-middle through a local Ollama
server (`/api/generate` with `suffix`, streamed). Any FIM-trained model
works (`qwen2.5-coder:*`, `codellama:*-code`, `deepseek-coder*`,
`starcoder2`); a chat-only model ignores `suffix` and completes the prefix.

Session: one keep-alive HTTP client per host (an Internet Accounts entry of
kind "ollama"). The account also carries the DEVICE the model should live
on (`device`: "auto" | "cpu" | "gpu:N" in Ollama's own GPU numbering) —
every request passes it as llama.cpp options (`main_gpu` / `num_gpu: 0`),
which is how Ollama decides placement (verified 2026-08-22: main_gpu=2 put
the model on the H100). Ollama numbers GPUs in CUDA-runtime order —
`gpu_inventory()` reads that order (names + live memory) from `torch.cuda`,
which shares the process's already-initialized CUDA runtime and is thread-
safe, so the probe never spins up a second CUDA context (a bare
`pycuda.driver.init()` on a probe thread could race the render thread's GL
context at boot and hang the load) and never shells out to nvidia-smi.
"""
from __future__ import annotations

import json
import threading
import time

from meltygui.completion.fim import FimRequest
from meltygui.completion.fim import FimResult
from meltygui.completion.fim import FimSession
from meltygui.completion.fim import fim_provider


class OllamaSession(FimSession):
    """One keep-alive client for the Ollama host of Internet Accounts entry
    `account` (kind "ollama": `host`); an explicit `host` overrides it."""
    KIND = "ollama"

    def __init__(self, account="default", host=None, timeout_s=30.0):
        import httpx
        from meltygui.accounts.internet_accounts import account_field
        self.account = account
        host = host or account_field("ollama", account, "host") or "http://localhost:11434"
        self.host = host.rstrip("/")
        # Short CONNECT timeout so a down/unreachable server fails fast instead
        # of blocking a gui thread for the long read timeout; generation
        # itself keeps the long read timeout.
        self.client = httpx.Client(base_url=self.host,
                                   timeout=httpx.Timeout(timeout_s, connect=2.0))
        self._status = ("ready",)

    def status(self):
        return self._status

    def close(self):
        try:
            self.client.close()
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────

def device_options(device) -> dict:
    """llama.cpp options for an account's `device` setting."""
    if not device or device == "auto":
        return {}
    if device == "cpu":
        return {"num_gpu": 0}
    if device.startswith("gpu:"):
        try:
            return {"main_gpu": int(device[4:])}
        except ValueError:
            return {}
    return {}


_inventory_cache = [0.0, None]


def gpu_inventory(max_age=3.0) -> list:
    """[{ollama_index, name, short, used_mib, total_mib}] in OLLAMA (CUDA
    runtime) order — read from torch.cuda, which shares the process's
    already-initialized CUDA runtime (same device order Ollama's llama.cpp
    sees) and is thread-safe. Deliberately NOT pycuda: a bare
    `pycuda.driver.init()` on a probe thread can race the render thread's
    CUDA/GL context creation at boot and hang the load. No subprocess
    either — no nvidia-smi. Best-effort; cached briefly."""
    now = time.monotonic()
    if _inventory_cache[1] is not None and now - _inventory_cache[0] < max_age:
        return _inventory_cache[1]
    gpus = []
    try:
        import torch
        if torch.cuda.is_available():
            for i in range(torch.cuda.device_count()):
                p = torch.cuda.get_device_properties(i)
                try:
                    free, total = torch.cuda.mem_get_info(i)
                except Exception:
                    free, total = 0, int(getattr(p, "total_memory", 0))
                name = p.name.replace("NVIDIA ", "").replace("GeForce ", "")
                gpus.append({"ollama_index": i, "name": name,
                             "short": name.replace(" NVL", "").replace("RTX ", ""),
                             "used_mib": int((total - free) / 1048576),
                             "total_mib": int(total / 1048576), "order": "cuda"})
    except Exception:
        gpus = []
    _inventory_cache[0] = now
    _inventory_cache[1] = gpus
    return gpus


def _gpu_for_vram(gpus, size_vram) -> dict | None:
    """The GPU a model of `size_vram` bytes most likely sits on — the one
    whose used memory best matches (torch mem_get_info, no per-process
    attribution needed). Good enough for one resident model."""
    want = size_vram / 1048576
    best, best_d = None, None
    for g in gpus:
        if g["used_mib"] >= 0.4 * want:
            d = abs(g["used_mib"] - want)
            if best_d is None or d < best_d:
                best, best_d = g, d
    return best


def device_label(device, gpus=None) -> str:
    """Label for a device setting. `gpus` is the CACHED inventory (or None) —
    this NEVER queries hardware, so it is safe on the render thread. Without
    an inventory a GPU shows as a bare "GPUn" until a probe fills the names."""
    if not device or device == "auto":
        return "auto"
    if device == "cpu":
        return "CPU"
    if device.startswith("gpu:"):
        try:
            i = int(device[4:])
        except ValueError:
            return device
        for g in (gpus or ()):
            if g["ollama_index"] == i:
                return f"GPU{i} {g['short']}"
        return f"GPU{i}"
    return device


def device_choices(gpus=None) -> list:
    """Device options from the CACHED inventory (or None) — no hardware
    query. Falls back to auto/cpu only until a probe supplies the GPUs."""
    return ["auto"] + [f"gpu:{g['ollama_index']}" for g in (gpus or ())] + ["cpu"]


# ──────────────────────────────────────────────────────────────────────────
# Model management (used by the Internet Accounts window)
# ──────────────────────────────────────────────────────────────────────────

def list_models(client) -> list:
    """[{name, size, loaded, size_vram, expires_at, where}] — /api/tags
    joined with /api/ps and the runners' GPU placement."""
    tags = client.get("/api/tags", timeout=5.0).json().get("models") or []
    try:
        running = {m["name"]: m for m in (client.get("/api/ps", timeout=5.0).json().get("models") or [])}
    except Exception:
        running = {}
    gpus = gpu_inventory() if running else []
    out = []
    for t in tags:
        name = t.get("name", "?")
        r = running.get(name)
        loaded = r is not None
        vram = int(r.get("size_vram") or 0) if r else 0
        w = None
        if loaded:
            if vram == 0:
                w = "CPU"
            else:
                g = _gpu_for_vram(gpus, vram)
                w = f"GPU{g['ollama_index']} {g['short']}" if g else "GPU"
        out.append({"name": name, "size": int(t.get("size") or 0), "loaded": loaded,
                    "size_vram": vram, "expires_at": (r or {}).get("expires_at"), "where": w,
                    "family": ((t.get("details") or {}).get("family") or "")})
    out.sort(key=lambda m: (not m["loaded"], m["name"]))
    return out


def load_model(client, model, device="auto", keep_alive="30m"):
    """Load `model` onto `device` (an empty generate with keep_alive) — also
    how a loaded model is MOVED: Ollama reloads when the options change."""
    payload = {"model": model, "keep_alive": keep_alive, "options": device_options(device)}
    r = client.post("/api/generate", json=payload, timeout=600.0)
    if r.status_code != 200:
        raise RuntimeError(f"ollama {r.status_code}: {r.text[:200]}")


def unload_model(client, model):
    r = client.post("/api/generate", json={"model": model, "keep_alive": 0}, timeout=60.0)
    if r.status_code != 200:
        raise RuntimeError(f"ollama {r.status_code}: {r.text[:200]}")


# ──────────────────────────────────────────────────────────────────────────
# Provider
# ──────────────────────────────────────────────────────────────────────────

def _window(req: FimRequest, prefix_chars: int, suffix_chars: int):
    prefix = req.annotated_prefix()
    if len(prefix) > prefix_chars:
        cut = prefix.rfind("\n", 0, len(prefix) - prefix_chars)
        prefix = prefix[cut + 1:] if cut >= 0 else prefix[-prefix_chars:]
    suffix = req.suffix
    if len(suffix) > suffix_chars:
        cut = suffix.find("\n", suffix_chars)
        suffix = suffix[:cut] if cut >= 0 else suffix[:suffix_chars]
    return prefix, suffix


@fim_provider(name="ollama", session=OllamaSession)
def ollama_fim(req: FimRequest, session: OllamaSession, model="qwen2.5-coder:7b",
               prefix_chars=6000, suffix_chars=1500, context_chars=3000,
               temperature=0.2, device=None, keep_alive=None) -> FimResult:
    """Local FIM. Stable context rides ahead of the prefix as commented
    blocks (FIM models have no side channel for it); the run block is
    inlined through `annotated_prefix`. `device` / `keep_alive` default to
    the account's settings (Internet Accounts → Ollama)."""
    from meltygui.core.runtime.toggles import Toggles
    from meltygui.accounts.internet_accounts import account_field
    if device is None:
        device = account_field("ollama", session.account, "device", "auto")
    if keep_alive is None:
        keep_alive = Toggles.Fim.ollama_keep_alive
    prefix, suffix = _window(req, prefix_chars, suffix_chars)
    ctx_text = req.context.render(("stable",)) if req.context is not None else ""
    if ctx_text:
        ctx_text = ctx_text[-context_chars:]
        commented = "\n".join("# " + ln if ln.strip() else "#" for ln in ctx_text.split("\n"))
        prefix = "# --- context ---\n" + commented + "\n# --- end context ---\n\n" + prefix
    options = {"num_predict": max(16, req.max_tokens), "temperature": temperature}
    options.update(device_options(device))
    payload = {"model": model, "prompt": prefix, "suffix": suffix, "stream": True,
               "keep_alive": keep_alive, "options": options}
    acc = []
    try:
        try:
            reason = _generate(session, payload, req, acc)
        except _Unsupported as e:
            # Not a FIM model - retry prefix-only (and with thinking off for a
            # reasoning model, whose output would otherwise all be thinking).
            # Quality is lower - the model can't see the suffix - but the
            # provider still works; the status says so.
            if "insert" in e.what:
                payload.pop("suffix", None)
            reason = None
            if "insert" in e.what or "think" in e.what:
                payload["think"] = False
                try:
                    reason = _generate(session, payload, req, acc)
                except _Unsupported as e2:
                    if "think" not in e2.what:
                        raise
                    payload.pop("think", None)
                    reason = _generate(session, payload, req, acc)
            session._status = ("ready", f"{model}: no FIM support, prefix-only")
            return FimResult("".join(acc), provider="ollama", truncated=(reason == "length"))
    except Exception as e:
        session._status = ("error", str(e))
        raise
    session._status = ("ready",)
    # done_reason "length" = hit num_predict (more to come → continue on Tab);
    # "stop" = the model emitted an end token (done, don't auto-continue).
    return FimResult("".join(acc), provider="ollama", truncated=(reason == "length"))


class _Unsupported(RuntimeError):
    def __init__(self, what):
        super().__init__(f"ollama: {what}")
        self.what = what


def _generate(session, payload, req, acc):
    """Stream one /api/generate call into `acc` (list of pieces), emitting
    the running text. Returns the final `done_reason` ("stop" | "length" |
    None). Raises _Unsupported for the model-capability errors ("does not
    support insert/thinking") so the caller can adapt."""
    acc.clear()
    saw_thinking = False
    done_reason = None
    with session.client.stream("POST", "/api/generate", json=payload) as resp:
        if resp.status_code != 200:
            body = resp.read().decode("utf-8", "replace")[:300]
            if "does not support" in body:
                raise _Unsupported(body)
            raise RuntimeError(f"ollama {resp.status_code}: {body}")
        for line in resp.iter_lines():
            if req.cancelled.is_set():
                break
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("error"):
                err = str(msg["error"])
                if "does not support" in err:
                    raise _Unsupported(err)
                raise RuntimeError(f"ollama: {err}")
            if msg.get("thinking"):
                saw_thinking = True
            piece = msg.get("response", "")
            if piece:
                acc.append(piece)
                req.emit("".join(acc))
            if msg.get("done"):
                done_reason = msg.get("done_reason")
                break
    if saw_thinking and not acc and "think" not in payload and not req.cancelled.is_set():
        # A reasoning model spent the entire budget thinking (happens when the
        # suffix is empty, so Ollama didn't reject the insert) - retry with
        # thinking off so the tokens go to the completion.
        raise _Unsupported("thinking consumed the budget")
    return done_reason
