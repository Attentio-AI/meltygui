"""Install MeltyGUI next to a range of PyTorch releases and run the CUDA checks.

Each matrix entry gets a fresh venv outside the checkout, with a noneditable
MeltyGUI wheel built from this checkout, the `tensor` extra and that entry's
pinned torch. The checks are the CUDA/tensor test files, run against the
installed wheel. `--window-smoke` also runs the README example to its first
frame; it opens windows, so use it from a reserved agent desktop.

    python3 tools/torch_matrix.py --find-links /path/to/support-wheels
    python3 tools/torch_matrix.py --only py312-torch2.5 --window-smoke

Needs uv, an NVIDIA driver and a CUDA toolkit with nvcc on PATH. Torch wheels
are large: every entry downloads a few GB the first time (uv caches them).
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
TORCH_INDEX = "https://download.pytorch.org/whl/"
# name, python, torch requirement, torch wheel index (None: PyPI), extra pins.
# To add a release: copy a row, pick the CUDA build that torch version ships.
MATRIX = [
    ("py311-torch2.2", "3.11", "torch==2.2.2+cu121", "cu121", ["numpy<2"]),   # the declared minimum
    ("py311-torch2.4", "3.11", "torch==2.4.1+cu124", "cu124", []),
    ("py312-torch2.2", "3.12", "torch==2.2.2+cu121", "cu121", ["numpy<2"]),
    ("py312-torch2.5", "3.12", "torch==2.5.1+cu121", "cu121", []),
    ("py312-torch2.7", "3.12", "torch==2.7.1+cu126", "cu126", []),
    ("py312-torch-latest", "3.12", "torch", None, []),
    ("py313-torch2.6", "3.13", "torch==2.6.0+cu124", "cu124", []),             # first release with 3.13 wheels
    ("py313-torch-latest", "3.13", "torch", None, []),
]
CHECK_TESTS = [
    "test_cuda_context_core.py", "test_cuda_kernel_core.py", "test_cuda_interop_core.py",
    "test_cuda_interop.py", "test_cuda_march.py", "test_cuda_direct_lines.py",
    "test_voxel_backends.py", "test_tensor_model.py", "test_tensor_slices.py",
]
VERSIONS_SNIPPET = """
import json, sys, numpy, torch, meltygui, meltygui_pycuda.driver as driver
import importlib.metadata as metadata
driver.init()
print(json.dumps(dict(
    python=sys.version.split()[0], torch=torch.__version__, torch_cuda=torch.version.cuda,
    cuda_available=torch.cuda.is_available(), numpy=numpy.__version__,
    pycuda=metadata.version('meltygui-pycuda'), imgui=metadata.version('meltygui-imgui'),
    meltygui_path=meltygui.__file__)))
"""
STEP_TIMEOUT_SECONDS = 1800


def run(command, log, env=None):
    """Run one step, appending its output to the entry's log. Returns (ok, output)."""
    started = time.time()
    result = subprocess.run(command, env=env, close_fds=False, capture_output=True, text=True,
                            timeout=STEP_TIMEOUT_SECONDS)
    output = result.stdout + result.stderr
    with open(log, "a") as handle:
        handle.write(f"$ {' '.join(map(str, command))}\n{output}\n[exit {result.returncode}, "
                     f"{time.time() - started:.0f}s]\n\n")
    return result.returncode == 0, output


def readme_example():
    blocks = re.findall(r"```python\n(.*?)```", (ROOT / "README.md").read_text(), re.S)
    return blocks[0]


def check_entry(entry, wheel, work, find_links, window_smoke):
    name, python, torch_requirement, torch_index, pins = entry
    folder = work / name
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    log = folder / "log.txt"
    venv_python = folder / "venv/bin/python"
    uv = shutil.which("uv")
    report = {"name": name, "python": python, "torch_requirement": torch_requirement, "log": str(log)}

    ok, _ = run([uv, "venv", "--python", python, str(folder / "venv")], log)
    if not ok:
        return dict(report, install="failed: no interpreter")
    install = [uv, "pip", "install", "--python", str(venv_python), f"meltygui[tensor] @ {wheel.as_uri()}",
               torch_requirement, "pytest>=8", "PyGLM>=2.8", *pins]
    if torch_index:
        # Only torch's local versions (+cu121) live on that index; everything else stays on PyPI.
        install += ["--extra-index-url", TORCH_INDEX + torch_index, "--index-strategy", "unsafe-best-match"]
    for directory in find_links:
        install += ["--find-links", str(directory)]
    started = time.time()
    ok, output = run(install, log)
    report["install_seconds"] = round(time.time() - started)
    # A support package compiled here means its prebuilt wheel did not match.
    report["compiled_from_source"] = sorted(set(re.findall(r"Building (meltygui-(?:pycuda|imgui))", output)))
    if not ok:
        return dict(report, install="failed", error=output.strip().splitlines()[-1:])
    report["install"] = "ok"

    ok, output = run([str(venv_python), "-I", "-c", VERSIONS_SNIPPET], log)   # -I: never import the checkout from cwd
    if not ok:
        return dict(report, imports="failed", error=output.strip().splitlines()[-1:])
    report.update(json.loads(output.strip().splitlines()[-1]))
    report["imports"] = "ok"
    assert str(folder) in report["meltygui_path"], "the checks must exercise the installed wheel"

    # Separate cache/session paths: kernels compile fresh, no app state is touched.
    env = dict(os.environ)
    for variable in ("XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        (folder / variable.lower()).mkdir()
        env[variable] = str(folder / variable.lower())
    tests = folder / "tests"
    shutil.copytree(ROOT / "tests", tests, ignore=shutil.ignore_patterns("__pycache__"))
    ok, output = run([str(folder / "venv/bin/pytest"), "-q", "-p", "no:cacheprovider", "--rootdir", str(tests),
                      *[str(tests / test) for test in CHECK_TESTS]], log, env)
    summary = output.strip().splitlines()[-1] if output.strip() else ""
    report["tests"] = ("ok: " if ok else "failed: ") + summary.strip("= ")

    if window_smoke:
        script = folder / "readme_example.py"
        script.write_text(readme_example())
        ok, output = run([str(venv_python), str(script)], log, dict(env, MELTY_BENCH="1"))
        ok = ok and "Traceback" not in output and "window(s) created" in output
        report["window_smoke"] = "ok" if ok else "failed"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--work", type=Path, default=Path(tempfile.gettempdir()) / "meltygui-torch-matrix")
    parser.add_argument("--output", type=Path, help="JSON report (default: <work>/report.json)")
    parser.add_argument("--find-links", type=Path, action="append", default=[],
                        help="folder of unpublished meltygui-imgui / meltygui-pycuda wheels")
    parser.add_argument("--only", action="append", default=[], help="matrix entry name (repeatable)")
    parser.add_argument("--window-smoke", action="store_true", help="also run the README example (opens windows)")
    parser.add_argument("--keep", action="store_true", help="keep each venv (several GB each) instead of only its log")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.list:
        for name, python, torch_requirement, torch_index, pins in MATRIX:
            print(f"{name:22} python {python}  {torch_requirement}  {' '.join(pins)}")
        return
    unknown = set(args.only) - {entry[0] for entry in MATRIX}
    if unknown:
        parser.error(f"unknown matrix entries: {', '.join(sorted(unknown))}")
    args.work.mkdir(parents=True, exist_ok=True)
    wheels = args.work / "wheel"
    if wheels.exists():
        shutil.rmtree(wheels)
    subprocess.run([shutil.which("uv"), "build", "--wheel", "--out-dir", str(wheels), str(ROOT)],
                   check=True, close_fds=False, capture_output=True)
    wheel = next(wheels.glob("meltygui-*.whl"))

    reports = []
    for entry in MATRIX:
        if args.only and entry[0] not in args.only:
            continue
        print(f"{entry[0]} ...", flush=True)
        report = check_entry(entry, wheel, args.work, [d.resolve() for d in args.find_links], args.window_smoke)
        reports.append(report)
        print("   " + ", ".join(f"{key}={report[key]}" for key in
                                ("install", "imports", "tests", "window_smoke", "torch", "numpy", "compiled_from_source")
                                if report.get(key)), flush=True)
        if not args.keep:
            shutil.rmtree(args.work / entry[0] / "venv", ignore_errors=True)
    output = args.output or args.work / "report.json"
    output.write_text(json.dumps(reports, indent=2) + "\n")
    print(f"report: {output}")
    failed = [r["name"] for r in reports
              if not all(str(r.get(key, "failed")).startswith("ok") for key in ("install", "imports", "tests"))
              or r.get("window_smoke") == "failed"]
    if failed:
        sys.exit(f"failed: {', '.join(failed)}")


if __name__ == "__main__":
    main()
