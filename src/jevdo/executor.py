"""Strict executor: template lookup + fresh revalidation + subprocess (no shell)."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

from jevdo.config import EnvConfig, node_for_selection, node_slots
from jevdo.discovery import validate_selection
from jevdo.dispatcher import PlannedAction

PATH_SLOT_RE = re.compile(r"\{path(?::([a-z0-9_-]+))?\}")
VALUE_PH = "{value}"


class ExecutionError(Exception):
    pass


@dataclass(frozen=True)
class ExecutionResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    dry_run: bool = False


def resolve_argv(config: EnvConfig, action: PlannedAction, cwd: str = ".") -> list[str]:
    """Build argv from the toml template only. Raises ExecutionError on any mismatch."""
    cmd, sub = node_for_selection(config, action.command, action.subcommand)

    if action.subcommand is None and cmd.subcommands and cmd.argv is None:
        raise ExecutionError(f"'{cmd.name}' has no base command to run without a subcommand")

    template = list(sub.argv if sub is not None else cmd.argv)
    flags_spec = list(sub.flags if sub is not None else cmd.flags)
    known_flags = {f.name: f for f in flags_spec}
    for fl in action.flags:
        if fl not in known_flags:
            raise ExecutionError(f"unknown flag '{fl}' for '{action.node_key}'")
    for fl, val in (action.flag_values or {}).items():
        spec = known_flags.get(fl)
        if spec is None:
            raise ExecutionError(f"unknown flag '{fl}' for '{action.node_key}'")
        if spec.values is None:
            raise ExecutionError(f"flag '{fl}' takes no value")
        if val not in spec.values:
            raise ExecutionError(f"flag '{fl}' value {val!r} not allowlisted")
        if fl not in action.flags:
            raise ExecutionError(f"flag '{fl}' has a value but was not selected")

    slots = {s.name: s for s in node_slots(sub if sub is not None else cmd)}
    paths = dict(action.paths or {})
    if action.path is not None and not paths:
        # legacy single-slot actions: map onto the node's only slot
        if len(slots) == 1:
            paths = {next(iter(slots)): action.path}
        elif action.path is not None:
            raise ExecutionError(f"'{action.node_key}' takes no path, got {action.path!r}")
    for name in paths:
        if name not in slots:
            raise ExecutionError(f"unknown path slot '{name}' for '{action.node_key}'")
    for name, slot in slots.items():
        if name not in paths and not slot.optional:
            # required slot missing: only ok if template has no ref (misconfig caught at load)
            refs = {m.group(1) or "path" for t in template for m in PATH_SLOT_RE.finditer(t)}
            if name in refs or "path" in refs:
                raise ExecutionError(f"missing required path for '{action.node_key}.{name}'")

    argv: list[str] = []
    for token in template:
        refs = [(m.group(1) or "path") for m in PATH_SLOT_RE.finditer(token)]
        if len(set(refs)) > 1:
            raise ExecutionError(f"argv token {token!r} mixes path slots")
        if not refs:
            argv.append(token)
            continue
        slot_name = refs[0]
        slot = slots.get(slot_name)
        if slot is None:
            raise ExecutionError(f"unknown path slot '{slot_name}' for '{action.node_key}'")
        val = paths.get(slot_name)
        if val is None:
            if slot.optional:
                continue  # drop token
            raise ExecutionError(f"missing required path for '{action.node_key}.{slot_name}'")
        try:
            validate_selection(val, slot, cwd)
        except ValueError as e:
            raise ExecutionError(str(e))
        argv.append(token.replace(f"{{path:{slot_name}}}", val).replace("{path}", val))

    # append chosen flag fragments in toml order
    for f in flags_spec:
        if f.name in action.flags:
            frag = list(f.argv_fragment)
            if f.values:
                frag = [t.replace(VALUE_PH, action.flag_values[f.name]) for t in frag]
                if any(VALUE_PH in t for t in frag):
                    raise ExecutionError(f"flag '{f.name}' fragment missing '{{value}}' substitution")
            argv.extend(frag)
    return argv


def execute(
    config: EnvConfig,
    action: PlannedAction,
    cwd: str = ".",
    *,
    dry_run: bool = False,
    timeout: float | None = None,
) -> ExecutionResult:
    argv = resolve_argv(config, action, cwd)
    if dry_run:
        return ExecutionResult(argv=argv, returncode=0, stdout="", stderr="", dry_run=True)
    try:
        proc = subprocess.run(
            argv, shell=False, cwd=cwd or ".",
            capture_output=True, text=True,
            timeout=config.timeout if timeout is None else timeout,
        )
    except subprocess.TimeoutExpired:
        raise ExecutionError(f"command timed out after {timeout or config.timeout}s: {' '.join(argv)}")
    except FileNotFoundError:
        raise ExecutionError(f"executable not found: {argv[0]!r}")
    except OSError as e:
        raise ExecutionError(f"failed to run {' '.join(argv)}: {e}")
    return ExecutionResult(argv=argv, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)
