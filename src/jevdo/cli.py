"""CLI: jevdo "<request>" [--env ...] [--dry-run] [--show-probs] [--min-confidence X]
  [--provider typesafe|openrouter] [--base-url URL]

Eval mode: jevdo --eval eval.toml [--env ...] [--cwd ...] (never executes)."""

from __future__ import annotations

import argparse
import dataclasses
import sys

from jevdo.config import ConfigError, load_config
from jevdo.dispatcher import dispatch, dispatch_sequence
from jevdo.eval import EvalError, load_eval, run_eval
from jevdo.executor import ExecutionError, execute, resolve_argv

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_ABSTAIN = 3
EXIT_EXEC_FAIL = 4
EXIT_EVAL_FAIL = 5


def apply_cli_overrides(config, args):
    """Fold --provider/--base-url into the loaded config. Returns new config."""
    provider = getattr(args, "provider", None)
    base_url = getattr(args, "base_url", None)
    if provider is None and base_url is None:
        return config
    from jevdo.config import PROVIDERS, ConfigError
    if provider is not None and provider not in PROVIDERS:
        raise ConfigError(f"--provider must be one of {PROVIDERS}")
    if base_url is not None:
        base_url = base_url.strip().rstrip("/")
        if not base_url or not (base_url.startswith("http://")
                                or base_url.startswith("https://")):
            raise ConfigError("--base-url must start with http:// or https://")
    return dataclasses.replace(
        config,
        provider=provider if provider is not None else config.provider,
        base_url=base_url if base_url is not None else config.base_url,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jevdo",
        description="Jev, Do. Route a natural-language request to an allowlisted shell command.",
    )
    p.add_argument("request", nargs="?",
                   help="natural-language request, e.g. 'show git status'")
    p.add_argument("--env", default="environment.toml", help="path to environment.toml")
    p.add_argument("--cwd", default=".", help="working dir for file candidates + execution")
    p.add_argument("--dry-run", action="store_true", help="print argv, do not execute")
    p.add_argument("--show-probs", action="store_true", help="print per-layer distributions")
    p.add_argument("--min-confidence", type=float, default=None,
                   help="override all configured thresholds (CLI wins)")
    p.add_argument("--steps", type=int, default=None,
                   help="chained multi-step budget 1..10 (default: meta.max_steps)")
    p.add_argument("--stop-on-error", dest="stop_on_error", action="store_true", default=True)
    p.add_argument("--no-stop-on-error", dest="stop_on_error", action="store_false",
                   help="continue chaining after non-zero exit")
    p.add_argument("--eval", dest="eval_file", default=None, metavar="EVAL_TOML",
                   help="eval mode: run [[test]] cases from a toml file, "
                   "compare planned argv to expected, never execute")
    p.add_argument("--provider", choices=("typesafe", "openrouter"), default=None,
                   help="Jev provider: typesafe cloud or openrouter "
                   "(default: meta.provider)")
    p.add_argument("--base-url", default=None, metavar="URL",
                   help="override the System One API base URL "
                   "(default: meta.base_url, TYPESAFE_BASE_URL, or provider default; "
                   "OpenRouter: https://openrouter.ai/api)")
    return p


def confirm(action, argv: list[str]) -> bool:
    """Simple y/N gate for non-read actions. Default N; non-tty => decline."""
    if not sys.stdin.isatty():
        return False
    try:
        ans = input(f"+ {' '.join(argv)}  (confidence {action.confidence:.2f}, "
                    f"risk {action.risk})\nRun this command? [y/N]: ")
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans.strip().lower() in ("y", "yes")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.env)
    except ConfigError as e:
        print(f"jevdo: config error: {e}", file=sys.stderr)
        return EXIT_CONFIG
    if args.min_confidence is not None:
        if not 0 <= args.min_confidence <= 1:
            print("jevdo: --min-confidence must be in [0, 1]", file=sys.stderr)
            return EXIT_CONFIG
    if args.steps is not None and not 1 <= args.steps <= 10:
        print("jevdo: --steps must be in [1, 10]", file=sys.stderr)
        return EXIT_CONFIG
    try:
        config = apply_cli_overrides(config, args)
    except ConfigError as e:
        print(f"jevdo: config error: {e}", file=sys.stderr)
        return EXIT_CONFIG

    from jevdo.client import has_api_key

    if not has_api_key(config):
        print("jevdo: no API key found: set TYPESAFE_API_KEY or OPENROUTER_API_KEY"
              " (see README for OpenRouter setup)", file=sys.stderr)
        return EXIT_CONFIG

    if args.eval_file is not None:
        return run_eval_file(config, args)
    if args.request is None:
        print("jevdo: the following arguments are required: request", file=sys.stderr)
        return EXIT_CONFIG

    budget = args.steps if args.steps is not None else config.max_steps
    if budget > 1:
        return run_sequence(config, args, budget)

    try:
        outcome, _questions, _ctx, _resp = dispatch(
            config, args.request, args.cwd, min_confidence=args.min_confidence)
    except ConfigError as e:
        print(f"jevdo: config error: {e}", file=sys.stderr)
        return EXIT_CONFIG
    except ValueError as e:
        print(f"jevdo: cannot build questions: {e}", file=sys.stderr)
        return EXIT_CONFIG
    except Exception as e:
        print(f"jevdo: Jev call failed: {e}", file=sys.stderr)
        return EXIT_ABSTAIN

    if args.show_probs:
        _print_layers(outcome)
    if not outcome.ok or outcome.action is None:
        print(f"jevdo: abstaining: {outcome.reason} (confidence {outcome.confidence:.2f})",
              file=sys.stderr)
        return EXIT_ABSTAIN
    return run_action(config, args, outcome.action)


def run_action(config, args, action) -> int:
    try:
        preview = resolve_argv(config, action, args.cwd)
    except ExecutionError as e:
        print(f"jevdo: refused: {e}", file=sys.stderr)
        return EXIT_ABSTAIN
    if args.dry_run:
        print(f"+ {' '.join(preview)}  (confidence {action.confidence:.2f}, "
              f"risk {action.risk}) [dry-run]")
        return EXIT_OK
    if action.risk in ("write", "destructive"):
        print(f"+ {' '.join(preview)}  (confidence {action.confidence:.2f}, "
              f"risk {action.risk})")
        if not confirm(action, preview):
            print("jevdo: declined by user", file=sys.stderr)
            return EXIT_ABSTAIN
    else:
        print(f"+ {' '.join(preview)}  (confidence {action.confidence:.2f})")

    try:
        result = execute(config, action, args.cwd)
    except ExecutionError as e:
        print(f"jevdo: execution refused/failed: {e}", file=sys.stderr)
        return EXIT_EXEC_FAIL
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return EXIT_OK if result.returncode == 0 else EXIT_EXEC_FAIL


def run_sequence(config, args, budget: int) -> int:
    """Plan+confirm+execute loop with refreshed CWD between steps."""
    history: list[dict] = []
    final = EXIT_OK
    for i in range(budget):
        try:
            outcome, _q, _c, _r = dispatch(
                config, args.request, args.cwd,
                min_confidence=args.min_confidence, history=history or None)
        except (ConfigError, ValueError) as e:
            print(f"jevdo: step {i+1}: {e}", file=sys.stderr)
            return EXIT_CONFIG if isinstance(e, ConfigError) else EXIT_ABSTAIN
        except Exception as e:
            print(f"jevdo: Jev call failed at step {i+1}: {e}", file=sys.stderr)
            return EXIT_ABSTAIN
        if args.show_probs:
            print(f"# step {i+1}/{budget}:")
            _print_layers(outcome)
        if not outcome.ok or outcome.action is None:
            if i == 0:
                print(f"jevdo: abstaining: {outcome.reason} "
                      f"(confidence {outcome.confidence:.2f})", file=sys.stderr)
                return EXIT_ABSTAIN
            print(f"jevdo: stop: {outcome.reason}")
            return final
        action = outcome.action
        try:
            preview = resolve_argv(config, action, args.cwd)
        except ExecutionError as e:
            print(f"jevdo: step {i+1} refused: {e}", file=sys.stderr)
            return EXIT_ABSTAIN
        tag = f"step {i+1}/{budget}"
        if args.dry_run:
            print(f"{tag}: + {' '.join(preview)}  (confidence {action.confidence:.2f}, "
                  f"risk {action.risk}) [dry-run]")
            continue
        confirmed = True
        if action.risk in ("write", "destructive"):
            print(f"{tag}: + {' '.join(preview)}  (confidence {action.confidence:.2f}, "
                  f"risk {action.risk})")
            confirmed = confirm(action, preview)
            if not confirmed:
                print("jevdo: declined by user", file=sys.stderr)
                return EXIT_ABSTAIN
        else:
            print(f"{tag}: + {' '.join(preview)}  (confidence {action.confidence:.2f})")
        try:
            result = execute(config, action, args.cwd)
        except ExecutionError as e:
            print(f"jevdo: step {i+1} failed: {e}", file=sys.stderr)
            return EXIT_EXEC_FAIL
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        history.append({"step": i + 1, "argv": preview, "returncode": result.returncode,
                        "stdout_tail": result.stdout[-2000:], "stderr_tail": result.stderr[-2000:]})
        if result.returncode != 0:
            final = EXIT_EXEC_FAIL
            if args.stop_on_error:
                print(f"jevdo: stop: step {i+1} exited {result.returncode}")
                return final
    if args.dry_run:
        return EXIT_OK
    print("jevdo: max_steps reached" if len(history) == budget else "jevdo: done")
    return final


def run_eval_file(config, args) -> int:
    """Eval mode: plan each [[test]] input via Jev, compare argv, never execute."""
    try:
        cases = load_eval(args.eval_file)
    except EvalError as e:
        print(f"jevdo: eval error: {e}", file=sys.stderr)
        return EXIT_CONFIG
    summary = run_eval(config, cases, args.cwd, min_confidence=args.min_confidence)
    for r in summary.results:
        status = "PASS" if r.passed else "FAIL"
        if r.actual is None:
            print(f"{status} {r.case.name}: {r.reason}")
        else:
            print(f"{status} {r.case.name}: + {' '.join(r.actual)}"
                  f"  (confidence {r.confidence:.2f}"
                  + (f", risk {r.risk}" if r.risk else "") + ")"
                  + ("" if r.passed else f" -- {r.reason}"))
    print(f"jevdo: eval {summary.passed}/{summary.total} passed")
    return EXIT_OK if summary.ok else EXIT_EVAL_FAIL


def _print_layers(outcome) -> None:
    if outcome.action is None:
        print(f"# abstained: {outcome.reason}")
        return
    for layer in outcome.action.layers:
        extra = ""
        if layer.confidence is not None:
            extra += f" conf={layer.confidence:.2f}"
        else:
            extra += " noul"
        if layer.probabilities:
            extra += f" dist={_top3(layer.probabilities)}"
        print(f"# {layer.question_id}: choice={layer.choice} p={layer.probability:.2f}{extra}")
    a = outcome.action
    print(f"# risk={a.risk} threshold={a.threshold:.2f} ({a.threshold_source})")


def _top3(probs: dict) -> dict:
    return {k: round(v, 3) for k, v in sorted(probs.items(), key=lambda kv: -kv[1])[:3]}


if __name__ == "__main__":
    raise SystemExit(main())
