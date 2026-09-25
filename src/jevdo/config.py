"""Config loading + validation for environment.toml.

Schema (v2):

[meta]
model = "jev-latest"
min_confidence = 0.5        # global fallback
timeout = 60
provider = "typesafe"       # typesafe | openrouter (OpenRouter System One API)
base_url = "https://..."    # optional override; wins over provider default and
                            # TYPESAFE_BASE_URL. OpenRouter default:
                            # https://openrouter.ai/api
default_risk = "read"       # read | write | destructive
max_steps = 1               # 1..10, chained multi-step loop budget
command_question = "..."    # optional override for the L1 question text
  [meta.risk_thresholds]
  read = 0.5
  write = 0.7
  destructive = 0.9

[[command]]
name = "git"
description = "version control operations"
risk = "read"               # optional; inherits meta.default_risk, subs inherit cmd
min_confidence = 0.6        # optional per-node override
argv = [...]                # leaf: required; parent: optional base fallback
instructions_subcommand = "..."  # optional L2 question override for this command

  [[command.flag]]
  name = "--quiet"
  description = "suppress output"
  argv_fragment = ["--quiet"]
  question = "..."          # optional Noul text override
  # valued flag variant:
  # argv_fragment = ["--format={value}"]
  # values = ["json", "plain"]
  # [command.flag.value_descriptions]
  # json = "machine-readable"

  [[command.subcommand]]
  name = "copy"
  description = "copy a file into a directory"
  argv = ["cp", "{path:src}", "{path:dst}"]
  risk = "write"
    [[command.subcommand.path]]   # one table per {path:<name>} slot
    name = "src"
    kind = "files"                # files | dirs | both
    optional = false
    description = "source file to copy"
    base = "."                    # relative root inside --cwd
    depth = 1                     # 1 = top level only (v1 behavior)
    glob = "*.py"                 # optional fnmatch on basename
    include_dotfiles = false
    max_results = 200
    question = "..."              # optional Choice text override
    stated_question = "..."       # optional Noul gate text override

Legacy v1 style still works: takes_path/path_kind/path_optional on a node
with a bare "{path}" placeholder desugars to a single slot named "path".
Mixing legacy keys with [[path]] tables is an error.

Rules:
- names match ^[a-z0-9_-]+$ (flags: ^-+[a-zA-Z0-9_.-]+$), unique per level.
- "none_of_the_above" is reserved at every layer.
- every {path:<name>} placeholder needs a slot; every required slot appears.
- flag fragments containing "{value}" require values (and vice versa).
- instruction overrides: non-empty, max 500 chars.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

COMMAND_NAME_RE = re.compile(r"^[a-z0-9_-]+$")
FLAG_NAME_RE = re.compile(r"^-+[A-Za-z0-9_.-]+$")
PATH_SLOT_RE = re.compile(r"^\{path(?::([a-z0-9_-]+))?\}$|^([^{}]*)\{path(?::([a-z0-9_-]+))?\}([^{}]*)$")
RESERVED = "none_of_the_above"
PATH_PLACEHOLDER = "{path}"
VALUE_PLACEHOLDER = "{value}"
PATH_KINDS = ("files", "dirs", "both")
RISKS = ("read", "write", "destructive")
PROVIDERS = ("typesafe", "openrouter")
OPENROUTER_BASE_URL = "https://openrouter.ai/api"
MAX_INSTRUCTION_LEN = 500
MAX_SLOTS = 3


class ConfigError(Exception):
    """Raised when environment.toml is missing or invalid."""


@dataclass(frozen=True)
class Flag:
    name: str
    description: str
    argv_fragment: tuple[str, ...]
    values: tuple[str, ...] | None = None
    value_descriptions: dict = field(default_factory=dict)
    question: str | None = None


@dataclass(frozen=True)
class PathSlot:
    name: str
    kind: str = "files"
    optional: bool = False
    description: str | None = None
    base: str = "."
    depth: int = 1
    glob: str | None = None
    include_dotfiles: bool = False
    max_results: int = 200
    question: str | None = None
    stated_question: str | None = None


@dataclass(frozen=True)
class Subcommand:
    name: str
    description: str
    argv: tuple[str, ...]
    takes_path: bool = False
    path_kind: str = "files"
    path_optional: bool = False
    flags: tuple[Flag, ...] = ()
    paths: tuple[PathSlot, ...] = ()
    risk: str | None = None
    min_confidence: float | None = None


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    argv: tuple[str, ...] | None  # None => no base fallback (abstain on sub=none)
    takes_path: bool = False
    path_kind: str = "files"
    path_optional: bool = False
    flags: tuple[Flag, ...] = ()
    subcommands: tuple[Subcommand, ...] = ()
    paths: tuple[PathSlot, ...] = ()
    risk: str | None = None
    min_confidence: float | None = None
    instructions_subcommand: str | None = None

    @property
    def is_leaf(self) -> bool:
        return len(self.subcommands) == 0


@dataclass(frozen=True)
class EnvConfig:
    model: str = "jev-latest"
    min_confidence: float = 0.5
    timeout: float = 60.0
    commands: tuple[Command, ...] = ()
    default_risk: str = "read"
    risk_thresholds: dict = field(default_factory=dict)
    max_steps: int = 1
    command_question: str | None = None
    provider: str = "typesafe"
    base_url: str | None = None

    def get_command(self, name: str) -> Command | None:
        for c in self.commands:
            if c.name == name:
                return c
        return None

    @property
    def effective_base_url(self) -> str | None:
        """SDK base URL: explicit base_url, else OpenRouter default, else SDK default."""
        if self.base_url:
            return self.base_url.rstrip("/")
        if self.provider == "openrouter":
            return OPENROUTER_BASE_URL
        return None


# ---------------------------------------------------------------- helpers

def _req_str(d: dict, key: str, ctx: str) -> str:
    v = d.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ConfigError(f"{ctx}: '{key}' must be a non-empty string")
    return v.strip()


def _opt_str(d: dict, key: str, ctx: str) -> str | None:
    v = d.get(key)
    if v is None:
        return None
    if not isinstance(v, str) or not v.strip():
        raise ConfigError(f"{ctx}: '{key}' must be a non-empty string")
    return v.strip()


def _opt_bool(d: dict, key: str, default: bool, ctx: str) -> bool:
    v = d.get(key, default)
    if not isinstance(v, bool):
        raise ConfigError(f"{ctx}: '{key}' must be true/false")
    return v


def _opt_instruction(d: dict, key: str, ctx: str) -> str | None:
    v = _opt_str(d, key, ctx)
    if v is not None and len(v) > MAX_INSTRUCTION_LEN:
        raise ConfigError(f"{ctx}: '{key}' must be <= {MAX_INSTRUCTION_LEN} chars")
    return v


def _opt_risk(d: dict, ctx: str) -> str | None:
    v = d.get("risk")
    if v is None:
        return None
    if v not in RISKS:
        raise ConfigError(f"{ctx}: 'risk' must be one of {RISKS}")
    return v


def _opt_threshold(d: dict, ctx: str) -> float | None:
    v = d.get("min_confidence")
    if v is None:
        return None
    if not isinstance(v, (int, float)) or not 0 <= v <= 1:
        raise ConfigError(f"{ctx}: 'min_confidence' must be a number in [0, 1]")
    return float(v)


def _check_name(name: str, ctx: str, *, is_flag: bool = False) -> str:
    if name == RESERVED:
        raise ConfigError(f"{ctx}: name '{RESERVED}' is reserved")
    pat = FLAG_NAME_RE if is_flag else COMMAND_NAME_RE
    if not pat.match(name):
        want = "^-+[A-Za-z0-9_.-]+$" if is_flag else "^[a-z0-9_-]+$"
        raise ConfigError(f"{ctx}: invalid name '{name}' (must match {want})")
    return name


def _check_argv_tokens(tokens: list[str], ctx: str) -> tuple[str, ...]:
    if not isinstance(tokens, list) or not tokens:
        raise ConfigError(f"{ctx}: 'argv' must be a non-empty list of strings")
    out: list[str] = []
    for t in tokens:
        if not isinstance(t, str) or not t:
            raise ConfigError(f"{ctx}: 'argv' entries must be non-empty strings")
        if "\0" in t or "\n" in t:
            raise ConfigError(f"{ctx}: 'argv' entry {t!r} contains illegal characters")
        out.append(t)
    return tuple(out)


def _slot_refs_in_token(token: str) -> list[str | None]:
    """Return slot names referenced via {path} (None) or {path:name} in token."""
    refs: list[str | None] = []
    i = 0
    while True:
        j = token.find("{path", i)
        if j == -1:
            return refs
        k = token.find("}", j)
        if k == -1:
            raise ConfigError(f"unclosed '{{path' placeholder in {token!r}")
        inner = token[j + 1:k]  # e.g. "path" or "path:src"
        if inner == "path":
            refs.append(None)
        elif inner.startswith("path:"):
            nm = inner[5:]
            if not COMMAND_NAME_RE.match(nm):
                raise ConfigError(f"invalid slot name '{nm}' in {token!r}")
            refs.append(nm)
        else:
            # something like {pathX} — not our placeholder; ignore
            pass
        i = k + 1


# ---------------------------------------------------------------- parsers

def _parse_flag(d: dict, ctx: str) -> Flag:
    if not isinstance(d, dict):
        raise ConfigError(f"{ctx}: flag must be a table")
    name = _check_name(_req_str(d, "name", ctx), ctx, is_flag=True)
    desc = _req_str(d, "description", ctx)
    fctx = f"{ctx} flag '{name}'"
    frag_raw = d.get("argv_fragment")
    if frag_raw is None:
        raise ConfigError(f"{fctx}: 'argv_fragment' must be a non-empty list")
    frag = _check_argv_tokens(frag_raw, fctx)
    for t in frag:
        if PATH_PLACEHOLDER in t or "{path:" in t:
            raise ConfigError(f"{fctx}: '{{path}}' not allowed in flags")
    values = d.get("values")
    value_descs: dict = {}
    if values is not None:
        if not isinstance(values, list) or not 2 <= len(values) <= 50:
            raise ConfigError(f"{fctx}: 'values' must be a list of 2..50 strings")
        for v in values:
            if not isinstance(v, str) or not v or "\0" in v or "\n" in v or " " in v:
                raise ConfigError(f"{fctx}: each value must be a single token, got {v!r}")
        if len(set(values)) != len(values):
            raise ConfigError(f"{fctx}: duplicate entries in 'values'")
        if RESERVED in values:
            raise ConfigError(f"{fctx}: value '{RESERVED}' is reserved")
        has_slot = any(VALUE_PLACEHOLDER in t for t in frag)
        if not has_slot:
            raise ConfigError(f"{fctx}: valued flag needs '{{value}}' in 'argv_fragment'")
        vd = d.get("value_descriptions", {})
        if not isinstance(vd, dict):
            raise ConfigError(f"{fctx}: 'value_descriptions' must be a table")
        for k, v in vd.items():
            if k not in values:
                raise ConfigError(f"{fctx}: value_descriptions has unknown value '{k}'")
            if not isinstance(v, str) or not v.strip():
                raise ConfigError(f"{fctx}: description for '{k}' must be non-empty")
            value_descs[k] = v.strip()
        values = tuple(values)
    else:
        if any(VALUE_PLACEHOLDER in t for t in frag):
            raise ConfigError(f"{fctx}: '{{value}}' needs 'values' list")
        if "value_descriptions" in d:
            raise ConfigError(f"{fctx}: 'value_descriptions' needs 'values' list")
    question = _opt_instruction(d, "question", fctx)
    return Flag(name=name, description=desc, argv_fragment=frag,
                values=values, value_descriptions=value_descs, question=question)


def _parse_path_slot(d: dict, ctx: str) -> PathSlot:
    if not isinstance(d, dict):
        raise ConfigError(f"{ctx}: path slot must be a table")
    name = _check_name(_req_str(d, "name", ctx), ctx)
    sctx = f"{ctx} path '{name}'"
    kind = d.get("kind", "files")
    if kind not in PATH_KINDS:
        raise ConfigError(f"{sctx}: 'kind' must be one of {PATH_KINDS}")
    optional = d.get("optional", False)
    if not isinstance(optional, bool):
        raise ConfigError(f"{sctx}: 'optional' must be true/false")
    desc_raw = d.get("description")
    desc = None
    if desc_raw is not None:
        if not isinstance(desc_raw, str) or not desc_raw.strip():
            raise ConfigError(f"{sctx}: 'description' must be a non-empty string")
        desc = desc_raw.strip()
    base = d.get("base", ".")
    if not isinstance(base, str) or not base or base.startswith("/") or "\0" in base:
        raise ConfigError(f"{sctx}: 'base' must be a relative path")
    # normalize: reject .. escapes at load
    parts = [p for p in base.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ConfigError(f"{sctx}: 'base' must stay inside the working directory")
    base = "/".join(parts) if parts else "."
    depth = d.get("depth", 1)
    if not isinstance(depth, int) or isinstance(depth, bool) or depth < 1:
        raise ConfigError(f"{sctx}: 'depth' must be an integer >= 1")
    glob = d.get("glob")
    if glob is not None and (not isinstance(glob, str) or not glob.strip()):
        raise ConfigError(f"{sctx}: 'glob' must be a non-empty string")
    include_dotfiles = _opt_bool(d, "include_dotfiles", False, sctx)
    max_results = d.get("max_results", 200)
    if not isinstance(max_results, int) or isinstance(max_results, bool) or max_results < 1:
        raise ConfigError(f"{sctx}: 'max_results' must be a positive integer")
    question = _opt_instruction(d, "question", sctx)
    stated_question = _opt_instruction(d, "stated_question", sctx)
    return PathSlot(name=name, kind=kind, optional=optional, description=desc,
                    base=base, depth=depth, glob=glob.strip() if glob else None,
                    include_dotfiles=include_dotfiles, max_results=max_results,
                    question=question, stated_question=stated_question)


def _parse_path_opts_legacy(d: dict, ctx: str, takes_path: bool) -> tuple[str, bool]:
    kind = d.get("path_kind", "files")
    optional = d.get("path_optional", False)
    if not takes_path:
        return "files", False
    if kind not in PATH_KINDS:
        raise ConfigError(f"{ctx}: 'path_kind' must be one of {PATH_KINDS}")
    if not isinstance(optional, bool):
        raise ConfigError(f"{ctx}: 'path_optional' must be true/false")
    return kind, optional


def _finalize_paths(d: dict, ctx: str, argv: tuple[str, ...] | None,
                    takes_path: bool, kind: str, optional: bool) -> tuple[PathSlot, ...]:
    """Merge [[path]] tables with legacy takes_path/* keys. Validates placeholders."""
    raw_slots = d.get("path", [])
    if raw_slots is None:
        raw_slots = []
    if not isinstance(raw_slots, list):
        raise ConfigError(f"{ctx}: 'path' must be a list of tables")
    slots: list[PathSlot] = []
    seen: set[str] = set()
    for s in raw_slots:
        slot = _parse_path_slot(s, ctx)
        if slot.name in seen:
            raise ConfigError(f"{ctx}: duplicate path slot '{slot.name}'")
        seen.add(slot.name)
        slots.append(slot)

    if slots:
        if takes_path or d.get("path_kind") is not None or d.get("path_optional") is not None:
            raise ConfigError(f"{ctx}: use either [[path]] tables or takes_path/path_kind/path_optional, not both")
        if len(slots) > MAX_SLOTS:
            raise ConfigError(f"{ctx}: at most {MAX_SLOTS} path slots supported")
        if argv is not None:
            refs: set[str] = set()
            for t in argv:
                for r in _slot_refs_in_token(t):
                    refs.add(r if r is not None else "path")
                # a token mixing two different slots is ambiguous when omitting
                distinct = {r if r is not None else "path" for r in _slot_refs_in_token(t)}
                if len(distinct) > 1:
                    raise ConfigError(f"{ctx}: argv token {t!r} mixes path slots; keep one per token")
            by_name = {s.name for s in slots}
            for r in refs:
                if r not in by_name:
                    raise ConfigError(f"{ctx}: '{{path:{r}}}' has no [[path]] slot named '{r}'")
            for s in slots:
                if not s.optional and s.name not in refs and (s.name != "path" or None not in
                        {r for t in argv for r in _slot_refs_in_token(t)}):
                    # required slot must appear in argv
                    if s.name not in refs and not (s.name == "path" and "path" in refs):
                        raise ConfigError(f"{ctx}: required path slot '{s.name}' missing from 'argv'")
        return tuple(slots)

    # legacy desugar
    if takes_path:
        return (PathSlot(name="path", kind=kind, optional=optional),)
    if argv is not None:
        for t in argv:
            if _slot_refs_in_token(t):
                if any(r is not None for t2 in argv for r in _slot_refs_in_token(t2)):
                    raise ConfigError(f"{ctx}: '{{path:<name>}}' needs [[path]] tables")
                raise ConfigError(f"{ctx}: 'argv' has '{{path}}' but takes_path = false")
    return ()


def _parse_subcommand(d: dict, ctx: str) -> Subcommand:
    if not isinstance(d, dict):
        raise ConfigError(f"{ctx}: subcommand must be a table")
    sub_ctx = f"{ctx} subcommand '{d.get('name', '?')}'"
    name = _check_name(_req_str(d, "name", ctx), sub_ctx)
    desc = _req_str(d, "description", sub_ctx)
    takes_path = _opt_bool(d, "takes_path", False, sub_ctx)
    argv = _check_argv_tokens(d.get("argv"), sub_ctx, )
    kind, optional = _parse_path_opts_legacy(d, sub_ctx, takes_path)
    paths = _finalize_paths(d, sub_ctx, argv, takes_path, kind, optional)
    # legacy consistency: bare {path} required iff legacy takes_path
    if not paths:
        has_path = any(_slot_refs_in_token(t) for t in argv)
        if takes_path and not has_path:
            raise ConfigError(f"{sub_ctx}: takes_path = true but 'argv' has no '{{path}}'")
    flags: list[Flag] = []
    seen: set[str] = set()
    for f in d.get("flag", []):
        flag = _parse_flag(f, sub_ctx)
        if flag.name in seen:
            raise ConfigError(f"{sub_ctx}: duplicate flag '{flag.name}'")
        seen.add(flag.name)
        flags.append(flag)
    return Subcommand(
        name=name, description=desc, argv=argv,
        takes_path=takes_path, path_kind=kind, path_optional=optional,
        flags=tuple(flags), paths=paths,
        risk=_opt_risk(d, sub_ctx), min_confidence=_opt_threshold(d, sub_ctx),
    )


def _parse_command(d: dict) -> Command:
    if not isinstance(d, dict):
        raise ConfigError("command must be a table")
    ctx = f"command '{d.get('name', '?')}'"
    name = _check_name(_req_str(d, "name", "command"), ctx)
    desc = _req_str(d, "description", ctx)
    takes_path = _opt_bool(d, "takes_path", False, ctx)

    raw_subs = d.get("subcommand", [])
    if raw_subs is None:
        raw_subs = []
    if not isinstance(raw_subs, list):
        raise ConfigError(f"{ctx}: 'subcommand' must be a list of tables")

    raw_argv = d.get("argv")
    argv: tuple[str, ...] | None
    if raw_subs:
        argv = None if raw_argv is None else _check_argv_tokens(raw_argv, ctx)
    else:
        if raw_argv is None:
            raise ConfigError(f"{ctx}: leaf command needs 'argv'")
        argv = _check_argv_tokens(raw_argv, ctx)

    kind, optional = _parse_path_opts_legacy(d, ctx, takes_path)
    paths = _finalize_paths(d, ctx, argv, takes_path, kind, optional)
    if not paths and argv is not None:
        has_path = any(_slot_refs_in_token(t) for t in argv)
        if takes_path and not has_path:
            raise ConfigError(f"{ctx}: takes_path = true but 'argv' has no '{{path}}'")
        if not takes_path and has_path:
            raise ConfigError(f"{ctx}: 'argv' has '{{path}}' but takes_path = false")

    flags: list[Flag] = []
    seen_flags: set[str] = set()
    for f in d.get("flag", []) or []:
        flag = _parse_flag(f, ctx)
        if flag.name in seen_flags:
            raise ConfigError(f"{ctx}: duplicate flag '{flag.name}'")
        seen_flags.add(flag.name)
        flags.append(flag)

    subs: list[Subcommand] = []
    seen_subs: set[str] = set()
    for s in raw_subs:
        sub = _parse_subcommand(s, ctx)
        if sub.name in seen_subs:
            raise ConfigError(f"{ctx}: duplicate subcommand '{sub.name}'")
        seen_subs.add(sub.name)
        subs.append(sub)

    return Command(
        name=name, description=desc, argv=argv,
        takes_path=takes_path, path_kind=kind, path_optional=optional,
        flags=tuple(flags), subcommands=tuple(subs), paths=paths,
        risk=_opt_risk(d, ctx), min_confidence=_opt_threshold(d, ctx),
        instructions_subcommand=_opt_instruction(d, "instructions_subcommand", ctx),
    )


def load_config(path: str) -> EnvConfig:
    """Load and validate environment.toml. Raises ConfigError."""
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"environment file not found: {path}")
    except Exception as e:
        raise ConfigError(f"failed to parse {path}: {e}")
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be tables")

    meta = data.get("meta", {})
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise ConfigError("meta must be a table")
    model = meta.get("model", "jev-latest")
    if not isinstance(model, str) or not model.strip():
        raise ConfigError("meta.model must be a non-empty string")
    min_conf = meta.get("min_confidence", 0.5)
    if not isinstance(min_conf, (int, float)) or not 0 <= min_conf <= 1:
        raise ConfigError("meta.min_confidence must be a number in [0, 1]")
    timeout = meta.get("timeout", 60)
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ConfigError("meta.timeout must be a positive number")
    default_risk = meta.get("default_risk", "read")
    if default_risk not in RISKS:
        raise ConfigError(f"meta.default_risk must be one of {RISKS}")
    risk_thresholds: dict = {}
    rt = meta.get("risk_thresholds", {})
    if rt is None:
        rt = {}
    if not isinstance(rt, dict):
        raise ConfigError("meta.risk_thresholds must be a table")
    for k, v in rt.items():
        if k not in RISKS:
            raise ConfigError(f"meta.risk_thresholds: unknown risk '{k}'")
        if not isinstance(v, (int, float)) or not 0 <= v <= 1:
            raise ConfigError(f"meta.risk_thresholds.{k} must be a number in [0, 1]")
        risk_thresholds[k] = float(v)
    max_steps = meta.get("max_steps", 1)
    if not isinstance(max_steps, int) or isinstance(max_steps, bool) or not 1 <= max_steps <= 10:
        raise ConfigError("meta.max_steps must be an integer in [1, 10]")
    command_question = None
    if meta.get("command_question") is not None:
        command_question = _opt_instruction(meta, "command_question", "meta")
    provider = meta.get("provider", "typesafe")
    if not isinstance(provider, str) or provider.strip() not in PROVIDERS:
        raise ConfigError(f"meta.provider must be one of {PROVIDERS}")
    provider = provider.strip()
    base_url = meta.get("base_url")
    if base_url is not None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ConfigError("meta.base_url must be a non-empty string")
        base_url = base_url.strip().rstrip("/")
        if not (base_url.startswith("http://") or base_url.startswith("https://")):
            raise ConfigError("meta.base_url must start with http:// or https://")

    raw_cmds = data.get("command", [])
    if raw_cmds is None:
        raw_cmds = []
    if not isinstance(raw_cmds, list) or not raw_cmds:
        raise ConfigError("need at least one [[command]]")
    commands: list[Command] = []
    seen: set[str] = set()
    for c in raw_cmds:
        cmd = _parse_command(c)
        if cmd.name in seen:
            raise ConfigError(f"duplicate command '{cmd.name}'")
        seen.add(cmd.name)
        commands.append(cmd)

    return EnvConfig(
        model=model.strip(), min_confidence=float(min_conf),
        timeout=float(timeout), commands=tuple(commands),
        default_risk=default_risk, risk_thresholds=risk_thresholds,
        max_steps=max_steps, command_question=command_question,
        provider=provider, base_url=base_url,
    )


def node_for_selection(config: EnvConfig, command: str, subcommand: str | None):
    """Return (Command, Subcommand|None) for a planned selection. Raises ConfigError."""
    cmd = config.get_command(command)
    if cmd is None:
        raise ConfigError(f"unknown command '{command}'")
    if subcommand is None:
        return cmd, None
    for s in cmd.subcommands:
        if s.name == subcommand:
            return cmd, s
    raise ConfigError(f"unknown subcommand '{command} {subcommand}'")


def node_slots(node: Command | Subcommand) -> tuple[PathSlot, ...]:
    """Effective path slots: explicit [[path]] tables, else legacy desugar."""
    if node.paths:
        return node.paths
    if node.takes_path:
        return (PathSlot(name="path", kind=node.path_kind, optional=node.path_optional),)
    return ()


def node_risk(config: EnvConfig, cmd: Command, sub: Subcommand | None) -> str:
    """Effective risk: sub > cmd > meta.default_risk."""
    if sub is not None and sub.risk is not None:
        return sub.risk
    if cmd.risk is not None:
        return cmd.risk
    return config.default_risk


def effective_threshold(config: EnvConfig, cmd: Command, sub: Subcommand | None,
                        risk: str, *, cli_override: float | None = None) -> tuple[float, str]:
    """Threshold resolution: CLI > node > risk tier > global. Returns (bar, source)."""
    if cli_override is not None:
        return cli_override, "cli --min-confidence"
    node_bar = sub.min_confidence if sub is not None else cmd.min_confidence
    if sub is not None and sub.min_confidence is None:
        node_bar = cmd.min_confidence  # fall back to parent for sub nodes
    if node_bar is not None:
        who = f"{cmd.name}.{sub.name}" if sub is not None else cmd.name
        return node_bar, f"node {who}"
    if risk in config.risk_thresholds:
        return config.risk_thresholds[risk], f"risk {risk}"
    return config.min_confidence, "global"
