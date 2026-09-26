"""Eval harness: run [[test]] cases from a toml file against Jev with no execution.

Schema (eval.toml):

  [[test]]
  name = "brief history"          # optional; defaults to "test-N", must be unique
  input = "brief git history"     # required; natural-language request given to Jev
  expected = "git log --oneline"  # required unless expect_abstain = true;
                                  # parsed with shlex, compared as argv list.
                                  # A list of strings requests a multi-step run:
                                  # max_steps = len(expected) and every planned
                                  # step's argv must match the matching entry.
  expect_abstain = true           # optional; pass iff Jev abstains (no expected)
  cwd = "subdir"                  # optional; overrides base --cwd for this test
  min_confidence = 0.8            # optional; overrides CLI/global thresholds
  continue_threshold = 0.4        # optional; overrides the __continue__ gate bar

  [[test]]
  name = "stage then status"
  input = "stage readme then show status"
  expected = ["git add README.md", "git status"]   # 2 steps, max_steps = 2

  [meta]                          # optional defaults for all tests
  cwd = "."
  min_confidence = 0.5

In eval mode no shell commands are executed: each input goes through
dispatcher.dispatch()/dispatch_sequence() + executor.resolve_argv() only.
Single-string cases run with `max_steps=1` (no `__continue__` gate); array
cases run with `max_steps=len(expected)` and must produce exactly that many
steps whose argv match entry-for-entry.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib


class EvalError(Exception):
    """Raised when an eval toml file is missing or invalid."""


@dataclass(frozen=True)
class EvalCase:
    name: str
    input: str
    expected: tuple[tuple[str, ...], ...] | None  # per-step argv; None abstain
    expected_str: tuple[str, ...] | None          # original strings for reporting
    expect_abstain: bool = False
    cwd: str | None = None
    min_confidence: float | None = None
    continue_threshold: float | None = None

    @property
    def steps(self) -> int:
        return len(self.expected) if self.expected else 1


@dataclass(frozen=True)
class EvalTestResult:
    case: EvalCase
    passed: bool
    actual: tuple[tuple[str, ...], ...] | None  # per-step argv; None abstain/refusal
    reason: str
    confidence: float = 0.0
    risk: str | None = None


@dataclass(frozen=True)
class EvalSummary:
    total: int
    passed: int
    failed: int
    results: tuple[EvalTestResult, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.failed == 0


def load_eval(path: str) -> list[EvalCase]:
    """Load and validate an eval toml file. Raises EvalError."""
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise EvalError(f"eval file not found: {path}")
    except Exception as e:
        raise EvalError(f"failed to parse {path}: {e}")
    if not isinstance(data, dict):
        raise EvalError(f"{path}: top level must be tables")

    allowed_top = {"test", "meta"}
    for k in data:
        if k not in allowed_top:
            raise EvalError(f"{path}: unknown top-level key '{k}' (want [[test]] and optional [meta])")

    meta = data.get("meta", {})
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise EvalError(f"{path}: [meta] must be a table")
    meta_cwd = _opt_cwd(meta, f"{path} [meta]")
    meta_conf = _opt_threshold(meta, f"{path} [meta]")
    meta_cont = _opt_threshold(meta, f"{path} [meta]", key="continue_threshold")

    raw_tests = data.get("test")
    if raw_tests is None:
        raise EvalError(f"{path}: need at least one [[test]]")
    if not isinstance(raw_tests, list) or not raw_tests:
        raise EvalError(f"{path}: need at least one [[test]]")

    cases: list[EvalCase] = []
    seen_names: set[str] = set()
    for i, raw in enumerate(raw_tests, 1):
        default_name = f"test-{i}"
        cases.append(_parse_case(raw, i, default_name, path, meta_cwd, meta_conf, seen_names, meta_cont))
    return cases


def _parse_case(d: dict, idx: int, default_name: str, path: str,
                meta_cwd: str | None, meta_conf: float | None,
                seen_names: set[str], meta_cont: float | None = None) -> EvalCase:
    ctx = f"{path} [[test]] #{idx}"
    if not isinstance(d, dict):
        raise EvalError(f"{ctx}: test must be a table")
    allowed = {"name", "input", "expected", "expect_abstain", "cwd",
               "min_confidence", "continue_threshold"}
    for k in d:
        if k not in allowed:
            raise EvalError(f"{ctx}: unknown key '{k}'")

    name = d.get("name", default_name)
    if not isinstance(name, str) or not name.strip():
        raise EvalError(f"{ctx}: 'name' must be a non-empty string")
    name = name.strip()
    if name in seen_names:
        raise EvalError(f"{path}: duplicate test name '{name}'")
    seen_names.add(name)
    ctx = f"{path} test '{name}'"

    request = d.get("input")
    if not isinstance(request, str) or not request.strip():
        raise EvalError(f"{ctx}: 'input' must be a non-empty string")
    request = request.strip()

    expect_abstain = d.get("expect_abstain", False)
    if not isinstance(expect_abstain, bool):
        raise EvalError(f"{ctx}: 'expect_abstain' must be true/false")

    raw_expected = d.get("expected")
    expected: tuple[tuple[str, ...], ...] | None = None
    expected_str: tuple[str, ...] | None = None
    if expect_abstain:
        if raw_expected is not None:
            raise EvalError(f"{ctx}: 'expected' must be omitted when expect_abstain = true")
    else:
        if isinstance(raw_expected, str):
            raw_list = [raw_expected]
        elif isinstance(raw_expected, list) and raw_expected:
            raw_list = raw_expected
        else:
            raise EvalError(
                f"{ctx}: 'expected' must be a non-empty command string "
                "or a non-empty list of command strings")
        steps: list[tuple[str, ...]] = []
        strs: list[str] = []
        for j, item in enumerate(raw_list, 1):
            label = f"'expected[{j}]'" if len(raw_list) > 1 else "'expected'"
            if not isinstance(item, str) or not item.strip():
                raise EvalError(f"{ctx}: {label} must be a non-empty command string")
            text = item.strip()
            try:
                parts = shlex.split(text)
            except ValueError as e:
                raise EvalError(f"{ctx}: {label} is not parseable: {e}")
            if not parts:
                raise EvalError(f"{ctx}: {label} must be a non-empty command string")
            steps.append(tuple(parts))
            strs.append(text)
        expected = tuple(steps)
        expected_str = tuple(strs)

    cwd = d.get("cwd", meta_cwd)
    if cwd is not None and (not isinstance(cwd, str) or not cwd.strip()):
        raise EvalError(f"{ctx}: 'cwd' must be a non-empty string")
    cwd = cwd.strip() if isinstance(cwd, str) else None
    if cwd is not None and os.path.isabs(cwd):
        raise EvalError(f"{ctx}: 'cwd' must be relative (joined onto --cwd)")

    if "min_confidence" in d:
        conf = _opt_threshold(d, ctx)
    else:
        conf = meta_conf

    if "continue_threshold" in d:
        cont = _opt_threshold(d, ctx, key="continue_threshold")
    else:
        cont = meta_cont

    return EvalCase(name=name, input=request, expected=expected,
                    expected_str=expected_str, expect_abstain=expect_abstain,
                    cwd=cwd, min_confidence=conf, continue_threshold=cont)


def _opt_cwd(d: dict, ctx: str) -> str | None:
    v = d.get("cwd")
    if v is None:
        return None
    if not isinstance(v, str) or not v.strip():
        raise EvalError(f"{ctx}: 'cwd' must be a non-empty string")
    v = v.strip()
    if os.path.isabs(v):
        raise EvalError(f"{ctx}: 'cwd' must be relative (joined onto --cwd)")
    return v


def _opt_threshold(d: dict, ctx: str, key: str = "min_confidence") -> float | None:
    v = d.get(key)
    if v is None:
        return None
    if not isinstance(v, (int, float)) or not 0 <= v <= 1:
        raise EvalError(f"{ctx}: '{key}' must be a number in [0, 1]")
    return float(v)


def run_eval(config, cases: list[EvalCase], base_cwd: str = ".", *,
             client=None, min_confidence: float | None = None,
             temperature: float | None = None,
             continue_threshold: float | None = None) -> EvalSummary:
    """Plan each case via Jev and compare against expected. Never executes.

    Single-string cases use dispatcher.dispatch() with max_steps=1.
    Array cases (``case.steps > 1``) use dispatcher.dispatch_sequence() with
    max_steps=len(expected) and an execute_fn that only resolves argv, so no
    subprocess runs and no confirm prompt appears. Every step's argv must match
    the matching expected entry. Returns an EvalSummary.

    NOTE: eval runs with ``repeat_guard=False`` so a pinned identical plan
    across steps (e.g. ``["pytest -q", "pytest -q"]``) is accepted as-is;
    the P2 anti-repeat retry only fires on live chained runs.
    """
    from jevdo.dispatcher import dispatch, dispatch_sequence
    from jevdo.executor import ExecutionError, resolve_argv

    def no_exec(action, cwd):
        return (resolve_argv(config, action, cwd), 0, "", "")

    results: list[EvalTestResult] = []
    for case in cases:
        cwd = os.path.join(base_cwd, case.cwd) if case.cwd else base_cwd
        bar = case.min_confidence if case.min_confidence is not None else min_confidence
        cont_bar = (case.continue_threshold if case.continue_threshold is not None
                    else continue_threshold)
        n = case.steps
        try:
            if case.expect_abstain:
                outcome, _q, _c, _r = dispatch(
                    config, case.input, cwd, client=client, min_confidence=bar,
                    max_steps=1, temperature=temperature)
                if not outcome.ok or outcome.action is None:
                    results.append(EvalTestResult(case, True, None,
                                                  f"abstained as expected: {outcome.reason}",
                                                  confidence=outcome.confidence))
                    continue
                action = outcome.action
                try:
                    actual = (tuple(resolve_argv(config, action, cwd)),)
                except ExecutionError:
                    actual = None
                results.append(EvalTestResult(
                    case, False, actual,
                    f"expected abstention but planned '{_fmt(actual)}'",
                    confidence=action.confidence, risk=action.risk))
                continue

            if n == 1:
                outcome, _q, _c, _r = dispatch(
                    config, case.input, cwd, client=client, min_confidence=bar,
                    max_steps=1, temperature=temperature)
                if not outcome.ok or outcome.action is None:
                    results.append(EvalTestResult(case, False, None,
                                                  f"abstained: {outcome.reason}",
                                                  confidence=outcome.confidence))
                    continue
                action = outcome.action
                actual = (tuple(resolve_argv(config, action, cwd)),)
                conf, risk = action.confidence, action.risk
            else:
                steps, reason = dispatch_sequence(
                    config, case.input, cwd, client=client, min_confidence=bar,
                    max_steps=n, temperature=temperature, execute_fn=no_exec,
                    continue_threshold=cont_bar, repeat_guard=False)
                if not steps:
                    results.append(EvalTestResult(case, False, None, reason))
                    continue
                actual = tuple(tuple(s.argv) for s in steps)
                conf = min(s.action.confidence for s in steps)
                risk = steps[-1].action.risk
        except ExecutionError as e:
            results.append(EvalTestResult(case, False, None, f"refused: {e}"))
            continue
        except Exception as e:
            results.append(EvalTestResult(case, False, None, f"Jev call failed: {e}"))
            continue

        if len(actual) != n:
            detail = f" ({reason})" if n > 1 else ""
            results.append(EvalTestResult(
                case, False, actual,
                f"expected {n} step(s) but Jev produced {len(actual)}{detail}",
                confidence=conf, risk=risk))
        elif actual == case.expected:
            results.append(EvalTestResult(case, True, actual, "ok",
                                          confidence=conf, risk=risk))
        else:
            results.append(EvalTestResult(
                case, False, actual,
                f"expected '{_fmt(case.expected)}' got '{_fmt(actual)}'",
                confidence=conf, risk=risk))
    passed = sum(1 for r in results if r.passed)
    return EvalSummary(total=len(results), passed=passed,
                       failed=len(results) - passed, results=tuple(results))


def _join(argv: tuple[str, ...] | list[str]) -> str:
    try:
        return shlex.join(argv)
    except AttributeError:  # Python < 3.8 (defensive; requires >=3.10)
        return " ".join(argv)


def _fmt(steps: tuple[tuple[str, ...], ...] | None) -> str:
    if steps is None:
        return ""
    return " ; ".join(_join(s) for s in steps)
