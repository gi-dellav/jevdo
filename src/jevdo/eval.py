"""Eval harness: run [[test]] cases from a toml file against Jev with no execution.

Schema (eval.toml):

  [[test]]
  name = "brief history"          # optional; defaults to "test-N", must be unique
  input = "brief git history"     # required; natural-language request given to Jev
  expected = "git log --oneline"  # required unless expect_abstain = true;
                                  # parsed with shlex, compared as argv list
  expect_abstain = true           # optional; pass iff Jev abstains (no expected)
  cwd = "subdir"                  # optional; overrides base --cwd for this test
  min_confidence = 0.8            # optional; overrides CLI/global thresholds

  [meta]                          # optional defaults for all tests
  cwd = "."
  min_confidence = 0.5

In eval mode no shell commands are executed: each input goes through
dispatcher.dispatch() + executor.resolve_argv() only. Pass iff the resolved
argv equals shlex.split(expected) (or abstention matches expect_abstain).
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
    expected: tuple[str, ...] | None  # parsed argv; None when expect_abstain
    expected_str: str | None          # original string for reporting
    expect_abstain: bool = False
    cwd: str | None = None
    min_confidence: float | None = None


@dataclass(frozen=True)
class EvalTestResult:
    case: EvalCase
    passed: bool
    actual: tuple[str, ...] | None  # resolved argv; None on abstain/refusal
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

    raw_tests = data.get("test")
    if raw_tests is None:
        raise EvalError(f"{path}: need at least one [[test]]")
    if not isinstance(raw_tests, list) or not raw_tests:
        raise EvalError(f"{path}: need at least one [[test]]")

    cases: list[EvalCase] = []
    seen_names: set[str] = set()
    for i, raw in enumerate(raw_tests, 1):
        default_name = f"test-{i}"
        cases.append(_parse_case(raw, i, default_name, path, meta_cwd, meta_conf, seen_names))
    return cases


def _parse_case(d: dict, idx: int, default_name: str, path: str,
                meta_cwd: str | None, meta_conf: float | None,
                seen_names: set[str]) -> EvalCase:
    ctx = f"{path} [[test]] #{idx}"
    if not isinstance(d, dict):
        raise EvalError(f"{ctx}: test must be a table")
    allowed = {"name", "input", "expected", "expect_abstain", "cwd", "min_confidence"}
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
    expected: tuple[str, ...] | None = None
    expected_str: str | None = None
    if expect_abstain:
        if raw_expected is not None:
            raise EvalError(f"{ctx}: 'expected' must be omitted when expect_abstain = true")
    else:
        if not isinstance(raw_expected, str) or not raw_expected.strip():
            raise EvalError(f"{ctx}: 'expected' must be a non-empty command string")
        expected_str = raw_expected.strip()
        try:
            parts = shlex.split(expected_str)
        except ValueError as e:
            raise EvalError(f"{ctx}: 'expected' is not parseable: {e}")
        if not parts:
            raise EvalError(f"{ctx}: 'expected' must be a non-empty command string")
        expected = tuple(parts)

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

    return EvalCase(name=name, input=request, expected=expected,
                    expected_str=expected_str, expect_abstain=expect_abstain,
                    cwd=cwd, min_confidence=conf)


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


def _opt_threshold(d: dict, ctx: str) -> float | None:
    v = d.get("min_confidence")
    if v is None:
        return None
    if not isinstance(v, (int, float)) or not 0 <= v <= 1:
        raise EvalError(f"{ctx}: 'min_confidence' must be a number in [0, 1]")
    return float(v)


def run_eval(config, cases: list[EvalCase], base_cwd: str = ".", *,
             client=None, min_confidence: float | None = None) -> EvalSummary:
    """Plan each case via Jev and compare against expected. Never executes.

    Only dispatcher.dispatch() (Jev call) + executor.resolve_argv() (pure
    template resolution + filesystem revalidation) are used. No subprocess,
    no confirm prompt. Returns an EvalSummary.
    """
    from jevdo.dispatcher import dispatch
    from jevdo.executor import ExecutionError, resolve_argv

    results: list[EvalTestResult] = []
    for case in cases:
        cwd = os.path.join(base_cwd, case.cwd) if case.cwd else base_cwd
        bar = case.min_confidence if case.min_confidence is not None else min_confidence
        try:
            outcome, _q, _c, _r = dispatch(
                config, case.input, cwd, client=client, min_confidence=bar)
        except Exception as e:
            results.append(EvalTestResult(case, False, None, f"Jev call failed: {e}"))
            continue
        if not outcome.ok or outcome.action is None:
            conf = outcome.confidence
            if case.expect_abstain:
                results.append(EvalTestResult(case, True, None,
                                              f"abstained as expected: {outcome.reason}",
                                              confidence=conf))
            else:
                results.append(EvalTestResult(case, False, None,
                                              f"abstained: {outcome.reason}",
                                              confidence=conf))
            continue
        action = outcome.action
        try:
            actual = tuple(resolve_argv(config, action, cwd))
        except ExecutionError as e:
            results.append(EvalTestResult(case, False, None, f"refused: {e}",
                                          confidence=action.confidence,
                                          risk=action.risk))
            continue
        if case.expect_abstain:
            results.append(EvalTestResult(
                case, False, actual,
                f"expected abstention but planned '{_join(actual)}'",
                confidence=action.confidence, risk=action.risk))
            continue
        assert case.expected is not None
        if actual == case.expected:
            results.append(EvalTestResult(case, True, actual, "ok",
                                          confidence=action.confidence,
                                          risk=action.risk))
        else:
            results.append(EvalTestResult(
                case, False, actual,
                f"expected '{case.expected_str}' got '{_join(actual)}'",
                confidence=action.confidence, risk=action.risk))
    passed = sum(1 for r in results if r.passed)
    return EvalSummary(total=len(results), passed=passed,
                       failed=len(results) - passed, results=tuple(results))


def _join(argv: tuple[str, ...] | list[str]) -> str:
    try:
        return shlex.join(argv)
    except AttributeError:  # Python < 3.8 (defensive; requires >=3.10)
        return " ".join(argv)
