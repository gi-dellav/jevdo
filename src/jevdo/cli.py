"""CLI: jevdo "<request>" [--env ...] [--dry-run] [--show-probs] [--min-confidence X]
  [--max-steps N] [--max-history N] [--temperature T] [--log PATH]
  [--provider typesafe|openrouter] [--base-url URL]

Eval mode: jevdo --eval eval.toml [--env ...] [--cwd ...] (never executes; a
string `expected` runs max_steps = 1, an array runs len(expected) steps)."""

from __future__ import annotations

import argparse
import dataclasses
import sys

from jevdo.config import ConfigError, load_config
from jevdo.dispatcher import continue_info, continue_requested, dispatch
from jevdo.eval import EvalError, _join, load_eval, run_eval
from jevdo.executor import ExecutionError, execute, resolve_argv
from jevdo.runlog import JsonlLogger, eval_event, plan_event, result_event

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
    p.add_argument("--max-steps", "--steps", dest="max_steps", type=int, default=None,
                   metavar="N",
                   help="upper bound on chained steps 1..10; 1 disables chaining "
                        "(default: meta.max_steps). Jev decides whether to continue.")
    p.add_argument("--max-history", type=int, default=None, metavar="N",
                   help="max history entries sent back to Jev when chaining "
                        "(default: meta.max_history; 0 sends none)")
    p.add_argument("--temperature", type=float, default=None, metavar="T",
                   help="sampling temperature passed to Jev in extra_body "
                        "(default: meta.temperature)")
    p.add_argument("--log", dest="log_path", default=None, metavar="PATH",
                   help="opt-in JSONL run log: append plan/result events to PATH")
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
    if args.max_steps is not None and not 1 <= args.max_steps <= 10:
        print("jevdo: --max-steps must be in [1, 10]", file=sys.stderr)
        return EXIT_CONFIG
    if args.max_history is not None and args.max_history < 0:
        print("jevdo: --max-history must be >= 0", file=sys.stderr)
        return EXIT_CONFIG
    if args.temperature is not None and not 0 <= args.temperature <= 2:
        print("jevdo: --temperature must be in [0, 2]", file=sys.stderr)
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

    logger = JsonlLogger(args.log_path) if args.log_path else None
    try:
        if args.eval_file is not None:
            return run_eval_file(config, args, logger=logger)
        if args.request is None:
            print("jevdo: the following arguments are required: request",
                  file=sys.stderr)
            return EXIT_CONFIG

        budget = args.max_steps if args.max_steps is not None else config.max_steps
        if budget > 1:
            return run_sequence(config, args, budget, logger=logger)

        try:
            outcome, _questions, context, response = dispatch(
                config, args.request, args.cwd,
                min_confidence=args.min_confidence,
                max_history=args.max_history, max_steps=budget,
                temperature=args.temperature)
        except ConfigError as e:
            print(f"jevdo: config error: {e}", file=sys.stderr)
            return EXIT_CONFIG
        except ValueError as e:
            print(f"jevdo: cannot build questions: {e}", file=sys.stderr)
            return EXIT_CONFIG
        except Exception as e:
            print(f"jevdo: Jev call failed: {e}", file=sys.stderr)
            return EXIT_ABSTAIN

        if logger is not None:
            logger.log(plan_event(
                1, args.request, args.cwd, outcome, context,
                continue_asked=False, continue_value=None,
                max_steps=budget, max_history=args.max_history))
        if args.show_probs:
            _print_layers(outcome)
        if not outcome.ok or outcome.action is None:
            print(f"jevdo: abstaining: {outcome.reason} "
                  f"(confidence {outcome.confidence:.2f})", file=sys.stderr)
            if logger is not None:
                logger.log(result_event(1, error=outcome.reason,
                                        stop_reason=outcome.reason))
            return EXIT_ABSTAIN
        return run_action(config, args, outcome.action, logger=logger, step=1)
    finally:
        if logger is not None:
            logger.close()


def run_action(config, args, action, logger=None, step: int = 1) -> int:
    try:
        preview = resolve_argv(config, action, args.cwd)
    except ExecutionError as e:
        print(f"jevdo: refused: {e}", file=sys.stderr)
        if logger is not None:
            logger.log(result_event(step, error=str(e)))
        return EXIT_ABSTAIN
    if args.dry_run:
        print(f"+ {' '.join(preview)}  (confidence {action.confidence:.2f}, "
              f"risk {action.risk}) [dry-run]")
        if logger is not None:
            logger.log(result_event(step, preview, dry_run=True))
        return EXIT_OK
    if action.risk in ("write", "destructive"):
        print(f"+ {' '.join(preview)}  (confidence {action.confidence:.2f}, "
              f"risk {action.risk})")
        if not confirm(action, preview):
            print("jevdo: declined by user", file=sys.stderr)
            if logger is not None:
                logger.log(result_event(step, preview, declined=True))
            return EXIT_ABSTAIN
    else:
        print(f"+ {' '.join(preview)}  (confidence {action.confidence:.2f})")

    try:
        result = execute(config, action, args.cwd)
    except ExecutionError as e:
        print(f"jevdo: execution refused/failed: {e}", file=sys.stderr)
        if logger is not None:
            logger.log(result_event(step, preview, error=str(e)))
        return EXIT_EXEC_FAIL
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if logger is not None:
        logger.log(result_event(step, preview, returncode=result.returncode,
                                stdout=result.stdout, stderr=result.stderr))
    return EXIT_OK if result.returncode == 0 else EXIT_EXEC_FAIL


def run_sequence(config, args, budget: int, logger=None) -> int:
    """Plan+confirm+execute loop; Jev decides after each step whether to continue.

    `budget` is the upper bound. With budget == 1 no `__continue__` gate is
    asked. `--max-history` caps how many prior steps are sent back to Jev.
    """
    history: list[dict] = []
    final = EXIT_OK
    for i in range(budget):
        ask_continue = budget > 1 and i + 1 < budget
        try:
            outcome, _q, context, response = dispatch(
                config, args.request, args.cwd,
                min_confidence=args.min_confidence, history=history or None,
                max_steps=budget, max_history=args.max_history,
                allow_continue=ask_continue, temperature=args.temperature)
        except (ConfigError, ValueError) as e:
            print(f"jevdo: step {i+1}: {e}", file=sys.stderr)
            return EXIT_CONFIG if isinstance(e, ConfigError) else EXIT_ABSTAIN
        except Exception as e:
            print(f"jevdo: Jev call failed at step {i+1}: {e}", file=sys.stderr)
            return EXIT_ABSTAIN
        if logger is not None:
            logger.log(plan_event(
                i + 1, args.request, args.cwd, outcome, context,
                continue_asked=ask_continue,
                continue_value=continue_info(response),
                max_steps=budget, max_history=args.max_history,
                history_len=len(history)))
        if args.show_probs:
            print(f"# step {i+1}/{budget}:")
            _print_layers(outcome)
        if not outcome.ok or outcome.action is None:
            if i == 0:
                print(f"jevdo: abstaining: {outcome.reason} "
                      f"(confidence {outcome.confidence:.2f})", file=sys.stderr)
                if logger is not None:
                    logger.log(result_event(i + 1, error=outcome.reason,
                                            stop_reason=outcome.reason))
                return EXIT_ABSTAIN
            print(f"jevdo: stop: {outcome.reason}")
            if logger is not None:
                logger.log(result_event(i + 1, error=outcome.reason,
                                        stop_reason=outcome.reason))
            return final
        action = outcome.action
        try:
            preview = resolve_argv(config, action, args.cwd)
        except ExecutionError as e:
            print(f"jevdo: step {i+1} refused: {e}", file=sys.stderr)
            if logger is not None:
                logger.log(result_event(i + 1, error=str(e)))
            return EXIT_ABSTAIN
        tag = f"step {i+1}/{budget}"
        if args.dry_run:
            print(f"{tag}: + {' '.join(preview)}  (confidence {action.confidence:.2f}, "
                  f"risk {action.risk}) [dry-run]")
            if logger is not None:
                logger.log(result_event(i + 1, preview, dry_run=True))
            if ask_continue and not continue_requested(response):
                print(f"jevdo: done after step {i+1} (dry-run)")
                return EXIT_OK
            continue
        confirmed = True
        if action.risk in ("write", "destructive"):
            print(f"{tag}: + {' '.join(preview)}  (confidence {action.confidence:.2f}, "
                  f"risk {action.risk})")
            confirmed = confirm(action, preview)
            if not confirmed:
                print("jevdo: declined by user", file=sys.stderr)
                if logger is not None:
                    logger.log(result_event(i + 1, preview, declined=True))
                return EXIT_ABSTAIN
        else:
            print(f"{tag}: + {' '.join(preview)}  (confidence {action.confidence:.2f})")
        try:
            result = execute(config, action, args.cwd)
        except ExecutionError as e:
            print(f"jevdo: step {i+1} failed: {e}", file=sys.stderr)
            if logger is not None:
                logger.log(result_event(i + 1, preview, error=str(e)))
            return EXIT_EXEC_FAIL
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        if logger is not None:
            logger.log(result_event(i + 1, preview,
                                    returncode=result.returncode,
                                    stdout=result.stdout, stderr=result.stderr))
        history.append({"step": i + 1, "argv": preview, "returncode": result.returncode,
                        "stdout_tail": result.stdout[-2000:], "stderr_tail": result.stderr[-2000:]})
        if result.returncode != 0:
            final = EXIT_EXEC_FAIL
            if args.stop_on_error:
                print(f"jevdo: stop: step {i+1} exited {result.returncode}")
                return final
        if ask_continue and not continue_requested(response):
            print(f"jevdo: done after step {i+1}")
            return final
    if args.dry_run:
        return EXIT_OK
    print("jevdo: max_steps reached" if len(history) == budget else "jevdo: done")
    return final


def run_eval_file(config, args, logger=None) -> int:
    """Eval mode: plan each [[test]] input via Jev, compare argv, never execute."""
    try:
        cases = load_eval(args.eval_file)
    except EvalError as e:
        print(f"jevdo: eval error: {e}", file=sys.stderr)
        return EXIT_CONFIG
    summary = run_eval(config, cases, args.cwd, min_confidence=args.min_confidence,
                       temperature=args.temperature)
    for r in summary.results:
        if logger is not None:
            logger.log(eval_event(r.case, r))
        status = "PASS" if r.passed else "FAIL"
        if r.actual is None:
            print(f"{status} {r.case.name}: {r.reason}")
        else:
            cmds = " ; ".join(_join(a) for a in r.actual)
            print(f"{status} {r.case.name}: + {cmds}"
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
