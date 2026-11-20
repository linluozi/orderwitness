# orderwitness

**Turn an order-dependent pytest failure into a verified `polluter -> victim` pair, plus one command that reproduces it.**

Most flaky-test tooling stops at "reshuffle and print the seed". A seed is not attribution: it tells you an order failed, not which test dirtied the state or which test was hurt by it. `orderwitness` shrinks the failing order with delta debugging, then *requires* a pairwise certificate before it will call the result a pair:

1. the polluter passes when run alone,
2. the victim passes when run alone,
3. the two fail when run together in that order.

If any of those three checks fails, the tool refuses to emit a pair and reports the minimal failing sequence instead.

Upstream: none

## Why this is not another shuffler

| Tool | Output |
| --- | --- |
| `pytest-randomly` / `pytest-random-order` | a seed and a reordered run |
| `pytest-flaky` / rerun plugins | more attempts at the same suite |
| `orderwitness` | a minimal sequence, and a verified pair only when the three checks above hold |

The contribution is the **witness protocol**, not the shuffling: a reported pair is falsifiable by construction, and the report contains the exact command to falsify it. A negative result is also explicit: `witness: null` after N seeded shuffles.

## Install

```bash
git clone git@github-linluozi:linluozi/orderwitness.git
cd orderwitness
python3 -m venv .venv && . .venv/bin/activate
pip install -e . pytest
```

## Run

```bash
orderwitness --root /path/to/your/project --shuffles 8 --seed 0
```

Useful flags: `--timeout 120` (per pytest run), `--max-runs 120` (shrink budget), `--stop-on-first`, `--output report.json`.

Exit codes: `0` no order dependence found, `1` witness found, `2` suite could not be measured (collection error or red baseline).

Sample report (abridged):

```json
{
  "baseline": "pass",
  "n_tests": 2,
  "shuffles": {"planned": 4, "run": 4, "failing_seeds": [1, 3]},
  "witness": {
    "kind": "pair",
    "polluter": "test_b_dirty.py::test_dirty",
    "victim": "test_a_clean.py::test_clean",
    "pair_verified": true,
    "repro": "cd /tmp/proj && /usr/bin/python3 -m pytest -q test_b_dirty.py::test_dirty test_a_clean.py::test_clean"
  }
}
```

## How it works

1. **Collect** every node id via `pytest --collect-only -q`, preserving the full id (parametrized ids may contain spaces).
2. **Baseline** the suite in collection order. A red baseline is refused: without a green baseline, a later failure cannot be attributed to ordering.
3. **Scan** seeded shuffles (`random.Random(seed + i)`), recording every failing seed.
4. **Shrink** the first failing order with ddmin (chunk removal, then a 1-minimal pass) under a strict pytest-run budget.
5. **Certify** the first and last tests of the minimal sequence as a pair: each must pass alone and fail together.
6. **Emit** JSON on stdout (human summary on stderr) with the failing order, the witness, and a copy-paste repro command.

## Evidence from this machine

All numbers below came from real runs on 2026-11-21; no projected values.

- Tool test suite: `python -m pytest tests -q` -> **12 passed in 14.85s**.
- Positive control (2 files: one leaks an env var, one asserts it is absent): **pair witness verified**, polluter `test_b_dirty.py::test_dirty`, victim `test_a_clean.py::test_clean`; the emitted repro command exits non-zero.
- Negative control (3 independent tests, 4 shuffles): **0 failing seeds, `witness: null`**, exit code 0.
- Red-baseline control: refused with exit code 2 and `baseline: "error"` in the JSON.
- ddmin unit controls: a 7-element sequence with one real polluter/victim shrinks to exactly `["P", "V"]`; the result is asserted 1-minimal.
- Real suite attempt (w3lib `88bf1c0`, 1,796 tests collected): **no result claimed**. The tool collected all ids but the baseline run exited 2 because pytest cannot round-trip one parametrized node id emitted by its own `--collect-only` (`test_safe_url_string_url[...-output231]`): passing that id back yields rc=4 "not found". The run was refused, not converted into a pass. This is a pytest node-id edge case and is listed under Limits.

### A real bug found by the real suite

The first run against a real suite exposed a parser bug in this tool: parametrized ids such as `test_all[a=1 b=2-expected2]` contain spaces, and the collector truncated them at the first space, producing unresolvable node ids. The fix keeps the whole id and requires only the module part to end in `.py`; `tests/test_core.py::ParametrizedIdTests` is the regression test. The initial run correctly refused to proceed (exit 2) instead of inventing a result.

## 本轮创新

把"顺序 flaky"从 seed 变成**可证伪的二元证书**：只有当 polluter 单跑绿、victim 单跑绿、两者按序同跑红时，才允许输出 pair；否则降级为 minimal sequence。`pytest-randomly` 等只给 seed，`pytest-flaky` 等只重跑，都不做归因；本工具的最小归因 + 可直接执行的复现命令，使每个结论都能被一条命令推翻。

## Limits (read before trusting a result)

- Requires a green baseline; otherwise exits 2 by design.
- One real suite (w3lib) could not be measured because a single self-emitted parametrized id is not accepted back by pytest; the tool exits 2 rather than guessing.
- Only deterministic in-process state is attributed: `os.environ`, module globals, cwd, files. Cross-process and parallel-only pollution is out of scope.
- Timeouts and pytest errors are not treated as failures for attribution.
- Cost is `1 + shuffles + shrink_runs` pytest invocations; cap the shrink with `--max-runs` on slow suites.
- It finds a witness, not a fix. The pair tells you where to look.

## Environment

- CPython 3.12.3, pytest 9.2.0
- AMD EPYC 7K62, 4 vCPU, 3,723 MB RAM, Linux 6.8.0-124
- Seeds: `--seed 0`, shuffle i uses `random.Random(0 + i)`.

## License

MIT, original work by linluozi. See `LICENSE`.