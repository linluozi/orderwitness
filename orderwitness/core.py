"""Core engine: collect pytest tests, shuffle them, and shrink a failing order.

The design goal is narrow on purpose.  Other tools reorder or rerun tests; this
module turns a failing order into a *witness*: a short sequence that still
fails, and, when possible, a single (polluter, victim) pair that reproduces the
failure in one command.
"""

from __future__ import annotations

import json
import os
import re
import random
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

VERSION = "0.1.0"

PASS = "pass"
FAIL = "fail"
ERROR = "error"
TIMEOUT = "timeout"
NO_TESTS = "no-tests"


@dataclass
class RunResult:
    status: str
    returncode: int
    stdout: str
    stderr: str
    seconds: float
    command: list[str]

    @property
    def failed(self) -> bool:
        return self.status == FAIL


@dataclass
class Report:
    root: str
    n_tests: int
    baseline: str
    shuffles_planned: int
    shuffles_run: int
    failing_seeds: list[int] = field(default_factory=list)
    first_failing_order: list[str] | None = None
    witness: dict | None = None
    runs: int = 0
    elapsed_s: float = 0.0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "tool": "orderwitness",
            "version": VERSION,
            "root": self.root,
            "python": sys.version.split()[0],
            "n_tests": self.n_tests,
            "baseline": self.baseline,
            "shuffles": {
                "planned": self.shuffles_planned,
                "run": self.shuffles_run,
                "failing_seeds": self.failing_seeds,
            },
            "first_failing_order": self.first_failing_order,
            "witness": self.witness,
            "pytest_runs": self.runs,
            "elapsed_s": round(self.elapsed_s, 3),
            "notes": self.notes,
        }


class OrderWitnessError(RuntimeError):
    """Raised when the suite cannot be measured at all."""


def _pytest(root: Path, node_ids: Sequence[str], timeout: float) -> RunResult:
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--tb=no",
        "-p",
        "no:cacheprovider",
        "-p",
        "no:randomly",
        *node_ids,
    ]
    env = dict(os.environ)
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return RunResult(TIMEOUT, 124, _text(exc.stdout), _text(exc.stderr),
                         time.perf_counter() - started, cmd)
    seconds = time.perf_counter() - started
    if proc.returncode == 0:
        status = PASS
    elif proc.returncode == 1:
        status = FAIL
    elif proc.returncode == 5:
        status = NO_TESTS
    else:
        status = ERROR
    return RunResult(status, proc.returncode, proc.stdout, proc.stderr, seconds, cmd)


def _text(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def collect(root: Path, timeout: float = 120.0) -> list[str]:
    """Return pytest node ids in deterministic collection order."""
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        "-p",
        "no:cacheprovider",
        "-p",
        "no:randomly",
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=str(root), capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise OrderWitnessError(
            f"collection timed out after {timeout:.0f}s"
        ) from exc
    if proc.returncode not in (0, 5):
        tail = (proc.stdout + proc.stderr)[-1500:]
        raise OrderWitnessError(f"pytest collection failed (rc={proc.returncode})\n{tail}")
    ids: list[str] = []
    seen: set[str] = set()
    ansi = re.compile(r"\x1b\[[0-9;]*m")
    for raw in proc.stdout.splitlines():
        line = ansi.sub("", raw).strip()
        if "::" not in line or line.startswith("="):
            continue
        module = line.split("::", 1)[0]
        # Keep the whole node id: parametrized ids legitimately contain spaces,
        # e.g. ``test_all[a=1 b=2-expected2]``.  Splitting on whitespace here
        # silently produces node ids pytest cannot resolve.
        if not module.endswith(".py"):
            continue
        if line in seen:
            continue
        seen.add(line)
        ids.append(line)
    if not ids:
        raise OrderWitnessError("pytest collected 0 tests")
    return ids


def _split(items: Sequence[str], n: int) -> list[list[str]]:
    size = len(items)
    n = max(1, min(n, size))
    cuts = [round(i * size / n) for i in range(n + 1)]
    chunks: list[list[str]] = []
    for i in range(n):
        chunk = list(items[cuts[i]:cuts[i + 1]])
        if chunk:
            chunks.append(chunk)
    return chunks


def ddmin(
    order: Sequence[str],
    fails: Callable[[Sequence[str]], bool],
    max_calls: int = 120,
) -> tuple[list[str], int]:
    """Delta-debug a failing sequence down to a 1-minimal failing sequence.

    ``fails`` must be a pure predicate over an ordered sequence.  Results are
    cached by tuple so repeated candidates cost nothing.  Returns
    ``(minimal_order, number_of_predicate_calls)``.
    """
    current = list(order)
    cache: dict[tuple[str, ...], bool] = {}
    calls = 0

    def pred(seq: Sequence[str]) -> bool:
        nonlocal calls
        key = tuple(seq)
        if key not in cache:
            cache[key] = bool(fails(seq))
            calls += 1
        return cache[key]

    if not pred(current):
        return current, calls

    n = 2
    while len(current) >= 2 and calls < max_calls:
        chunks = _split(current, n)
        reduced = False
        for chunk in chunks:
            drop = set(chunk)
            candidate = [item for item in current if item not in drop]
            if candidate and pred(candidate):
                current = candidate
                n = max(2, n - 1)
                reduced = True
                break
        if reduced:
            continue
        if n >= len(current):
            break
        n = min(len(current), n * 2)

    # Guarantee 1-minimality even when the chunk pass stopped early.
    changed = True
    while changed and len(current) > 1 and calls < max_calls:
        changed = False
        for index in range(len(current)):
            candidate = current[:index] + current[index + 1:]
            if candidate and pred(candidate):
                current = candidate
                changed = True
                break
    return current, calls


def _repro(root: Path, order: Sequence[str]) -> str:
    nodes = " ".join(shlex.quote(node) for node in order)
    return f"cd {shlex.quote(str(root))} && {shlex.quote(sys.executable)} -m pytest -q {nodes}"


def _failure_tail(result: RunResult, limit: int = 1600) -> str:
    text = (result.stdout + "\n" + result.stderr).strip()
    return text[-limit:]


def audit(
    root: str | Path,
    shuffles: int = 8,
    seed: int = 0,
    timeout: float = 120.0,
    max_runs: int = 120,
    stop_on_first: bool = False,
) -> Report:
    """Run the whole pipeline and return a machine-readable report."""
    started = time.perf_counter()
    root = Path(root).resolve()
    ids = collect(root, timeout=timeout)
    report = Report(
        root=str(root),
        n_tests=len(ids),
        baseline=PASS,
        shuffles_planned=shuffles,
        shuffles_run=0,
    )

    baseline = _pytest(root, ids, timeout)
    report.runs += 1
    if baseline.status != PASS:
        report.baseline = baseline.status
        raise OrderWitnessError(
            "baseline suite is not green "
            f"(status={baseline.status}, rc={baseline.returncode}); "
            "order dependence cannot be attributed. Tail:\n"
            + _failure_tail(baseline)
        )

    first_failing: list[str] | None = None
    first_result: RunResult | None = None
    for index in range(shuffles):
        order = list(ids)
        random.Random(seed + index).shuffle(order)
        result = _pytest(root, order, timeout)
        report.runs += 1
        report.shuffles_run += 1
        if result.failed:
            report.failing_seeds.append(seed + index)
            if first_failing is None:
                first_failing = order
                first_result = result
                if stop_on_first:
                    break

    if first_failing is None or first_result is None:
        report.elapsed_s = time.perf_counter() - started
        report.notes.append(
            f"no order-dependent failure in {report.shuffles_run} seeded shuffles"
        )
        return report

    report.first_failing_order = first_failing
    calls = 0

    def fails(seq: Sequence[str]) -> bool:
        nonlocal calls
        calls += 1
        return _pytest(root, list(seq), timeout).failed

    minimal, _ = ddmin(first_failing, fails, max_calls=max_runs)
    report.runs += calls

    witness: dict = {
        "kind": "sequence",
        "order": minimal,
        "subset_size": len(minimal),
        "repro": _repro(root, minimal),
        "failure_tail": _failure_tail(first_result),
        "pair_verified": False,
    }
    if len(minimal) >= 2:
        polluter, victim = minimal[0], minimal[-1]
        polluter_alone = _pytest(root, [polluter], timeout)
        victim_alone = _pytest(root, [victim], timeout)
        pair = _pytest(root, [polluter, victim], timeout)
        report.runs += 3
        if (
            polluter_alone.status == PASS
            and victim_alone.status == PASS
            and pair.failed
        ):
            witness.update(
                {
                    "kind": "pair",
                    "polluter": polluter,
                    "victim": victim,
                    "pair_verified": True,
                    "repro": _repro(root, [polluter, victim]),
                    "failure_tail": _failure_tail(pair),
                }
            )
    report.witness = witness
    report.elapsed_s = time.perf_counter() - started
    return report


def write_report(report: Report, output: str | Path | None) -> None:
    payload = json.dumps(report.as_dict(), indent=2, ensure_ascii=False)
    if output:
        Path(output).write_text(payload + "\n", encoding="utf-8")
    print(payload)