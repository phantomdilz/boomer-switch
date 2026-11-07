"""Run isolated synthetic tests through the supported project entry point."""

import argparse
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import uuid


def _protect_owned_options(parser: argparse.ArgumentParser, extra: list[str]) -> None:
    for index, arg in enumerate(extra):
        if arg == "--" or arg.startswith("--basetemp"):
            parser.error("The runner owns temporary paths. Do not override --basetemp.")
        override = None
        if arg in ("-o", "--override-ini") and index + 1 < len(extra):
            override = extra[index + 1]
        elif arg.startswith("--override-ini="):
            override = arg.split("=", 1)[1]
        elif arg.startswith("-o") and len(arg) > 2:
            override = arg[2:]
        if override is not None and override.split("=", 1)[0].strip() in ("cache_dir", "addopts"):
            parser.error("The runner owns cache_dir and addopts overrides.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tier", choices=("full", "smoke", "lights", "demo", "registration", "people", "tooling"), nargs="?", default="full")
    parser.add_argument("--workers", type=int, default=min(8, max(2, (getattr(os, "process_cpu_count", os.cpu_count)() or 2))))
    parser.add_argument("--durations", type=int)
    parser.add_argument("--slow-threshold", type=float, default=1)
    parser.add_argument("--max-seconds", type=float, default=120)
    options, extra = parser.parse_known_args()
    if options.workers < 2:
        parser.error("Use at least 2 workers.")
    if not 0 < options.max_seconds <= 3600:
        parser.error("Use a total limit above 0 and at most 3600 seconds.")
    if any(arg == "-n" or arg.startswith("-n") or arg.startswith("--numprocesses") or arg.startswith("--dist") for arg in extra):
        parser.error("Use --workers to set parallelism.")
    _protect_owned_options(parser, extra)
    started = time.monotonic()
    print(f"tier={options.tier} workers={options.workers} dist=worksteal start={time.strftime('%Y-%m-%dT%H:%M:%S')}", flush=True)
    root = Path(__file__).resolve().parent
    scratch = root / ".test-runs" / uuid.uuid4().hex
    scratch.mkdir(parents=True)
    try:
        command = [sys.executable, "-m", "pytest", *extra]
        # Keep owned options last. PYTEST_ADDOPTS precedes command-line options.
        command += ["-n", str(options.workers), "--dist", "worksteal", "-o", f"cache_dir={scratch}/cache", f"--basetemp={scratch}/tmp"]
        if options.tier != "full":
            command += ["-m", options.tier]
        if options.durations is not None:
            command += [f"--durations={options.durations}", f"--durations-min={options.slow_threshold}"]
        process = subprocess.Popen(command, cwd=root, start_new_session=os.name != "nt")
        try:
            while True:
                try:
                    remaining = options.max_seconds - (time.monotonic() - started)
                    if remaining <= 0:
                        raise TimeoutError("The test run exceeded its total time limit.")
                    result = process.wait(timeout=min(30, remaining))
                    break
                except subprocess.TimeoutExpired:
                    print(f"elapsed={time.monotonic() - started:.1f}s", flush=True)
        except BaseException:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], check=False, capture_output=True)
            else:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait()
            raise
    finally:
        if scratch.resolve().parent != (root / ".test-runs").resolve():
            raise RuntimeError("Test cleanup escaped its owned directory.")
        shutil.rmtree(scratch)
    print(f"exit={result} elapsed={time.monotonic() - started:.1f}s finish={time.strftime('%Y-%m-%dT%H:%M:%S')}", flush=True)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
