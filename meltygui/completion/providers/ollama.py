"""Ollama FIM provider — native fill-in-the-middle through a local Ollama
server (`/api/generate` with `suffix`, streamed). Any FIM-trained model
works (`qwen2.5-coder:*`, `codellama:*-code`, `deepseek-coder*`,
`starcoder2`); a chat-only model ignores `suffix` and completes the prefix.

Session: one keep-alive HTTP client per host (an Internet Accounts entry of
kind "ollama"). The account also carries the DEVICE the model should live
on (`device`: "auto" | "cpu" | "gpu:N" in Ollama's own GPU numbering) —
every request passes it as llama.cpp options (`main_gpu` / `num_gpu: 0`),
which is how Ollama decides placement (verified 2026-08-22: main_gpu=2 put
the model on the H100). Ollama numbers GPUs in CUDA-runtime order, NOT
nvidia-smi order; `gpu_inventory()` joins the two through PCI bus ids so
the UI can name them and show where a loaded model sits.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time

from src.lsd.gl_gui.fim import FimRequest, FimResult, FimSession, fim_provider

_NVIDIA_SMI = shutil.which("nvidia-smi") or "/usr/bin/nvidia-smi"


class OllamaSession(FimSession):
    """One keep-alive client for the Ollama host of Internet Accounts entry
    `account` (kind "ollama": `host`); an explicit `host` overrides it."""
    KIND = "ollama"

    def __init__(self, account="default", host=None, timeout_s=30.0):
        import httpx
        from src.lsd.gl_gui.view.playground.internet_accounts import account_field
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


def _run(args, timeout=5.0):
    """Short subprocess via posix_spawn (full path, close_fds=False)."""
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, close_fds=False)


_inventory_cache = [0.0, None]


def gpu_inventory(max_age=3.0) -> list:
    """[{ollama_index, smi_index, uuid, name, short, used_mib, total_mib,
    bus}] in OLLAMA (CUDA runtime) order. nvidia-smi supplies names, memory
    and PCI bus ids; PyCUDA supplies the CUDA enumeration order by bus id.
    Without PyCUDA the nvidia-smi order is assumed (and flagged)."""
    now = time.monotonic()
    if _inventory_cache[1] is not None and now - _inventory_cache[0] < max_age:
        return _inventory_cache[1]
    gpus = []
    try:
        out = _run([_NVIDIA_SMI, "--query-gpu=index,uuid,name,memory.used,memory.total,pci.bus_id",
                    "--format=csv,noheader,nounits"]).stdout
        for line in out.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 6:
                continue
            name = parts[2].replace("NVIDIA ", "").replace("GeForce ", "")
            gpus.append({"smi_index": int(parts[0]), "uuid": parts[1], "name": name,
                         "short": name.replace(" NVL", "").replace("RTX ", ""),
                         "used_mib": int(float(parts[3])), "total_mib": int(float(parts[4])),
                         "bus": parts[5].lower()})
    except Exception:
        gpus = []
    order = None
    try:
        import pycuda.driver as cuda
        cuda.init()
        order = [cuda.Device(i).pci_bus_id().lower() for i in range(cuda.Device.count())]
    except Exception:
        order = None
    if order:
        by_bus = {g["bus"][-12:]: g for g in gpus}
        ordered = []
        for i, bus in enumerate(order):
            g = by_bus.get(bus[-12:])
            if g is not None:
                g = dict(g, ollama_index=i, order="cuda")
                ordered.append(g)
        for g in gpus:
            if not any(o["uuid"] == g["uuid"] for o in ordered):
                ordered.append(dict(g, ollama_index=len(ordered), order="smi"))
        gpus = ordered
    else:
        gpus = [dict(g, ollama_index=g["smi_index"], order="smi") for g in gpus]
    _inventory_cache[0] = now
    _inventory_cache[1] = gpus
    return gpus


def runner_placement() -> dict:
    """{gpu uuid: used MiB} for every Ollama runner process on the GPUs —
    where the loaded models actually sit."""
    out = {}
    try:
        txt = _run([_NVIDIA_SMI, "--query-compute-apps=pid,process_name,used_memory,gpu_uuid",
                    "--format=csv,noheader,nounits"]).stdout
        for line in txt.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 4 and "ollama" in parts[1]:
                out[parts[3]] = out.get(parts[3], 0) + int(float(parts[2]))
    except Exception:
        pass
    return out


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
    place = runner_placement() if running else {}
    gpus = gpu_inventory() if running else []
    where = None
    if place:
        names = []
        for g in gpus:
            if g["uuid"] in place:
                names.append(f"GPU{g['ollama_index']} {g['short']} ({place[g['uuid']] / 1024:.1f} GB)")
        where = " + ".join(names) if names else None
    out = []
    for t in tags:
        name = t.get("name", "?")
        r = running.get(name)
        loaded = r is not None
        vram = int(r.get("size_vram") or 0) if r else 0
        w = None
        if loaded:
            w = "CPU" if vram == 0 else (where or "GPU")
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
    from src.lsd.gl_gui.toggles import Toggles
    from src.lsd.gl_gui.view.playground.internet_accounts import account_field
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
            _generate(session, payload, req, acc)
        except _Unsupported as e:
            # Not a FIM model - retry prefix-only (and with thinking off for a
            # reasoning model, whose output would otherwise all be thinking).
            # Quality is lower - the model can't see the suffix - but the
            # provider still works; the status says so.
            if "insert" in e.what:
                payload.pop("suffix", None)
            if "insert" in e.what or "think" in e.what:
                payload["think"] = False
                try:
                    _generate(session, payload, req, acc)
                except _Unsupported as e2:
                    if "think" not in e2.what:
                        raise
                    payload.pop("think", None)
                    _generate(session, payload, req, acc)
            session._status = ("ready", f"{model}: no FIM support, prefix-only")
            return FimResult("".join(acc), provider="ollama")
    except Exception as e:
        session._status = ("error", str(e))
        raise
    session._status = ("ready",)
    return FimResult("".join(acc), provider="ollama")


class _Unsupported(RuntimeError):
    def __init__(self, what):
        super().__init__(f"ollama: {what}")
        self.what = what


def _generate(session, payload, req, acc):
    """Stream one /api/generate call into `acc` (list of pieces), emitting
    the running text. Raises _Unsupported for the model-capability errors
    ("does not support insert/thinking") so the caller can adapt."""
    acc.clear()
    saw_thinking = False
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
                break
    if saw_thinking and not acc and "think" not in payload and not req.cancelled.is_set():
        # A reasoning model spent the entire budget thinking (happens when the
        # suffix is empty, so Ollama didn't reject the insert) - retry with
        # thinking off so the tokens go to the completion.
        raise _Unsupported("thinking consumed the budget")
