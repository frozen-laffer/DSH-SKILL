#!/usr/bin/env python3
"""qbuild.py - Quartus stage-wise build with live progress, ETA and cancel.

Why staged: `quartus_sh --flow compile` is one opaque process. Running
quartus_map / quartus_fit / quartus_asm / quartus_sta as separate child
processes gives hard stage boundaries for free.

Progress is honest: within a stage the percentage is elapsed/expected for that
stage (expected learned from previous runs), capped at 95%; the label names the
sub-step when the log announced one. Quartus flushes most of a stage's output
only at the end, so in practice only the Fitter yields live sub-step labels.

Runs from anywhere: --dir points at a project (auto-detected *.qpf), all state
is written into that project directory, never next to this script.

Subcommands:
  qbuild.py --dir P                 start a build, live progress
  qbuild.py --dir P --cancel        ask a running build (other shell) to stop cleanly
  qbuild.py --dir P --status        print qbuild_status.json
  qbuild.py --dir P --log fit --tail 40   print the tail of a stage log
  qbuild.py --dir P --report        print the history table
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# Quartus install root: override with $env:QUARTUS_BIN on machines with another version.
QUARTUS_BIN = Path(os.environ.get("QUARTUS_BIN") or r"F:\altera_lite\25.1std\quartus\bin64")

# stage key, human label, exe, default expected seconds (used only before any history)
STAGES = [
    ("map", "Analysis & Synthesis", "quartus_map.exe", 25.0),
    ("fit", "Fitter",              "quartus_fit.exe", 45.0),
    ("asm", "Assembler",           "quartus_asm.exe",  6.0),
    ("sta", "Timing Analyzer",     "quartus_sta.exe",  9.0),
]

# log anchors that mark a sub-step *inside a given stage*
ANCHORS: dict[str, list[tuple[re.Pattern, str]]] = {
    "map": [
        (re.compile(r"Elaborating", re.I), "elaborating"),
        (re.compile(r"Analyzing|Inferred \d+", re.I), "analyzing RTL"),
        (re.compile(r"Synthesi[sz]ing|Optimizing|Technology mapping", re.I), "synthesizing"),
    ],
    "fit": [
        (re.compile(r"Beginning Fitter|Placing|Placement", re.I), "placing"),
        (re.compile(r"Routing|Router", re.I), "routing"),
        (re.compile(r"Retiming|Physical synthesis", re.I), "retiming"),
    ],
    "asm": [
        (re.compile(r"Assembl", re.I), "assembling"),
    ],
    "sta": [
        (re.compile(r"Timing analysis|Slack|Report(s)? ", re.I), "timing analysis"),
    ],
}

# Errors and warnings are counted separately: a Critical Warning (e.g. pins not
# assigned) does not fail the build and must not look like one in the history.
ERR_RE = re.compile(r"^Error\b", re.I)
# Quartus prints its own bookkeeping under the same "Error:" prefix when a stage
# fails ("Error: Elapsed time: ..."), which is not an error message at all.
ERR_NOISE_RE = re.compile(
    r"^Error:\s*(Peak (virtual )?memory|Processing ended|Elapsed time|Total CPU time)",
    re.I)
WARN_RE = re.compile(r"^(Critical Warning|Warning)\b", re.I)


def human(sec: float) -> str:
    sec = max(0, int(round(sec)))
    return f"{sec // 60:02d}:{sec % 60:02d}"


def atomic_write(path: Path, text: str) -> None:
    """Write via a temp file + os.replace so a concurrent reader never sees a
    half-written JSON document."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class StageRunner:
    def __init__(self, exe: str, project: str, revision: str, cwd: Path):
        self.cmd = [str(QUARTUS_BIN / exe), project, "-c", revision]
        self.cwd = cwd
        self.proc: subprocess.Popen | None = None
        self.q: queue.Queue = queue.Queue()
        self.lines = 0
        self.raw: list[str] = []
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.last_msg = ""
        self.stop = False

    def _pump(self):
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            if self.stop:
                break
            self.raw.append(line.rstrip("\r\n"))
            self.q.put(line.rstrip("\r\n"))

    def start(self):
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP
        self.proc = subprocess.Popen(
            self.cmd, cwd=str(self.cwd), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, errors="replace",
            bufsize=1, creationflags=flags,
        )
        threading.Thread(target=self._pump, daemon=True).start()

    def poll_line(self, timeout: float) -> str | None:
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None

    def kill(self):
        if not self.proc or self.proc.poll() is not None:
            return
        # taskkill /T is tried first so any helper process dies too, but under this
        # sandbox it is refused ("ERROR: Access denied"), so TerminateProcess via
        # Popen.kill() is the mechanism that actually works.
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                           capture_output=True)
        try:
            self.proc.kill()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass
        self.stop = True


def load_history(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def estimate(history: list[dict]) -> dict[str, float]:
    """Median of the last 5 successful runs per stage, else the built-in default."""
    per: dict[str, list[float]] = {k: [] for k, *_ in STAGES}
    # `ok` is only written by newer runs; legacy entries without it are treated as
    # usable samples (their `errors` field counted Critical Warnings, so it cannot
    # be used as a success flag).
    for run in [r for r in history if r.get("ok", True)][-5:]:
        for k, v in run.get("stages", {}).items():
            if k in per and isinstance(v, (int, float)) and v > 0:
                per[k].append(float(v))
    out: dict[str, float] = {}
    for k, _label, _exe, default in STAGES:
        out[k] = statistics.median(per[k]) if per[k] else default
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=".")
    ap.add_argument("--project", default="",
                    help="project name; auto-detected from *.qpf when omitted")
    ap.add_argument("--revision", default="",
                    help="revision; defaults to the project name")
    ap.add_argument("--cancel-after", type=float, default=0.0,
                    help="testing hook: cancel the run after N seconds")
    ap.add_argument("--interrupt-after", type=float, default=0.0,
                    help="testing hook: raise KeyboardInterrupt after N seconds")
    ap.add_argument("--cancel", action="store_true",
                    help="request a clean stop of the build running in this directory")
    ap.add_argument("--status", action="store_true", help="print qbuild_status.json")
    ap.add_argument("--log", choices=[k for k, *_ in STAGES],
                    help="print the tail of one stage log")
    ap.add_argument("--tail", type=int, default=40)
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    cwd = Path(args.dir).resolve()
    if not cwd.is_dir():
        print(f"not a directory: {cwd}", file=sys.stderr)
        return 2

    # Auto-detect the project so the tool can live outside the project tree.
    if not args.project:
        qpfs = sorted(p.stem for p in cwd.glob("*.qpf"))
        if args.report and not qpfs:
            hists = sorted(p.name[:-len(".history.json")] for p in cwd.glob("*.history.json"))
            qpfs = hists
        if not qpfs:
            print(f"no .qpf (or --project) in {cwd}", file=sys.stderr)
            return 2
        args.project = qpfs[0]
    if not args.revision:
        args.revision = args.project

    hist_path = cwd / f"{args.revision}.history.json"
    status_path = cwd / "qbuild_status.json"
    events_path = cwd / "qbuild_events.jsonl"
    cancel_flag = cwd / "qbuild.cancel"
    history = load_history(hist_path)

    if args.status:
        print(status_path.read_text(encoding="utf-8") if status_path.exists() else "{}")
        return 0

    if args.log:
        p = cwd / f"log_{args.log}.txt"
        if not p.exists():
            print(f"no such log: {p}", file=sys.stderr)
            return 1
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines()[-args.tail:]:
            print(line)
        return 0

    if args.cancel:
        cancel_flag.write_text("1", encoding="utf-8")
        print(f"cancel requested ({cancel_flag})")
        return 0

    if args.report:
        if not history:
            print("no history yet")
            return 1
        print(f"{'run':>3} {'total':>7}  " + "  ".join(f"{k:>6}" for k, *_ in STAGES) +
              f"  {'warn':>4} {'err':>3}")
        for i, run in enumerate(history, 1):
            st = run.get("stages", {})
            print(f"{i:>3} {run.get('total', 0):>7.1f}  " +
                  "  ".join(f"{st.get(k, 0):>6.1f}" for k, *_ in STAGES) +
                  f"  {run.get('warnings', 0):>4} {run.get('errors', 0):>3}")
        est = estimate(history)
        print("estimate:", {k: round(v, 1) for k, v in est.items()})
        return 0

    est = estimate(history)
    total_est = sum(est.values())
    weights = {k: est[k] / total_est for k, *_ in STAGES}

    t_start = time.time()
    done: dict[str, float] = {}
    events = events_path.open("a", encoding="utf-8")
    cancelled = False
    interrupted = False
    exit_code = 0
    err_count = 0
    warn_count = 0
    last_pct = 0.0

    def emit(pct: float, stage: str, label: str, eta: float, state: str = "running"):
        nonlocal last_pct
        if state in ("running", "done"):
            last_pct = pct
        now = time.time()
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "state": state, "pct": round(pct, 1), "stage": stage,
            "label": label, "elapsed": round(now - t_start, 1),
            "eta": round(max(0.0, eta), 1),
        }
        atomic_write(status_path, json.dumps(rec, ensure_ascii=False))
        events.write(json.dumps(rec, ensure_ascii=False) + "\n")
        events.flush()
        bar = "#" * int(pct / 4) + "." * (25 - int(pct / 4))
        print(f"qbuild {pct:5.1f}% |{bar}| {label:<22} "
              f"elapsed {human(now - t_start)} eta {human(eta)}", flush=True)

    active: list[StageRunner | None] = [None]
    active_key: list[str] = [""]
    active_t0: list[float] = [0.0]

    def write_stage_log(key: str, runner: StageRunner, dur: float, state: str):
        """Always materialise log_<stage>.txt, whatever the outcome was."""
        rc = runner.proc.returncode if runner.proc else None
        atomic_write(cwd / f"log_{key}.txt",
                     "\n".join(runner.raw) +
                     f"\n\n[{key} rc={rc} dur={dur:.1f}s lines={runner.lines} "
                     f"errors={len(runner.errors)} warnings={len(runner.warnings)} "
                     f"state={state}]")
    cancel_flag.unlink(missing_ok=True)
    try:
        for idx, (key, label, exe, _default) in enumerate(STAGES):
            base = sum(weights[k] for k, *_ in STAGES[:idx]) * 100.0
            span = weights[key] * 100.0
            est_s = max(est[key], 1.0)
            runner = StageRunner(exe, args.project, args.revision, cwd)
            active[0] = runner
            t_stage = time.time()
            active_key[0] = key
            active_t0[0] = t_stage
            emit(base, key, f"{label}: start", total_est - (time.time() - t_start))
            try:
                runner.start()
            except FileNotFoundError:
                print(f"missing executable: {QUARTUS_BIN / exe}", file=sys.stderr)
                return 2

            substep = ""
            last_emit = 0.0
            while True:
                line = runner.poll_line(0.25)
                if line is not None:
                    runner.lines += 1
                    runner.last_msg = line.strip()
                    if ERR_RE.match(line.strip()) and not ERR_NOISE_RE.match(line.strip()):
                        runner.errors.append(line.strip()[:200])
                    elif WARN_RE.match(line.strip()):
                        runner.warnings.append(line.strip()[:200])
                    for rx, name in ANCHORS[key]:
                        if rx.search(line):
                            substep = name
                            break
                if runner.proc.poll() is not None and runner.q.empty():
                    break
                now = time.time()
                if cancel_flag.exists():
                    runner.kill()
                    cancel_flag.unlink(missing_ok=True)   # consume the request
                    cancelled = True
                    break
                if args.cancel_after and (now - t_start) >= args.cancel_after:
                    runner.kill()
                    cancelled = True
                    break
                if args.interrupt_after and (now - t_start) >= args.interrupt_after:
                    raise KeyboardInterrupt   # test hook: exercise the Ctrl+C path
                if now - last_emit >= 1.0:
                    last_emit = now
                    frac = min(0.95, (now - t_stage) / est_s)
                    pct = base + span * frac
                    remaining = sum(est[k] for k, *_ in STAGES[idx + 1:]) + \
                        max(0.0, est_s * (1.0 - frac))
                    emit(pct, key, f"{label}: {substep or 'running'}", remaining)
            dur = time.time() - t_stage
            rc = runner.proc.returncode
            # write the stage log for every outcome, so --log never shows a stale run
            state = "cancelled" if cancelled else ("failed" if rc != 0 else "done")
            write_stage_log(key, runner, dur, state)
            if cancelled:
                break
            if rc != 0:
                emit(base + span, key, f"{label}: FAILED rc={rc}", 0, "failed")
                exit_code = rc or 1
                print(f"\n--- {key} failed, rc={rc}; last lines:", file=sys.stderr)
                for e in runner.errors[:10]:
                    print("  " + e, file=sys.stderr)
                break
            err_count += len(runner.errors)
            warn_count += len(runner.warnings)
            done[key] = round(dur, 1)
            emit(base + span, key, f"{label}: done {dur:.1f}s",
                 total_est - (time.time() - t_start))
    except KeyboardInterrupt:
        cancelled = True
        interrupted = True
        if active[0] is not None:
            active[0].kill()
            # the stage log must exist even for Ctrl+C, or --log shows the previous run
            write_stage_log(active_key[0], active[0],
                            time.time() - active_t0[0], "interrupted")
        print("\ninterrupted; stage process tree stopped", file=sys.stderr)

    total = time.time() - t_start
    if not cancelled and exit_code == 0:
        history.append({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "stages": done, "total": round(total, 1),
            "errors": err_count, "warnings": warn_count, "ok": True,
            "predicted": round(total_est, 1),
        })
        atomic_write(hist_path, json.dumps(history[-20:], indent=1))
        emit(100.0, "done", f"BUILD OK {human(total)}", 0, "done")
        print(f"\nBUILD OK in {human(total)}  predicted {human(total_est)}  "
              f"error {(total - total_est) / total_est * 100:+.0f}% of predicted")
        print("stages:", done, f" warnings={warn_count} errors={err_count}")
    elif cancelled:
        state = "interrupted" if interrupted else "cancelled"
        emit(last_pct, state, state.upper(), 0, state)
        print(f"\n{state.upper()}; stage process stopped, project files untouched")
        return 130
    else:
        print(f"\nBUILD FAILED after {human(total)}")
        return exit_code or 1
    events.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
