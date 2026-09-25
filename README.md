# jevdo
Jev, Do. | Connects Jev to your shell

A strict-allowlist harness: `environment.toml` defines which bash commands Jev
(the TypeSafe System One classifier) may select. Jev picks command →
subcommand → flags (boolean or valued) → path slots; only exact `argv`
templates from the toml may run. `read` actions run directly; `write` and
`destructive` actions ask for confirmation (`y/N`, default N, non-tty declines).
`--dry-run` previews without executing or prompting.

## Setup

```bash
pip install -e .
export TYPESAFE_API_KEY=...   # from https://console.typesafe.ai/keys
cp environment.toml.example environment.toml
```

### OpenRouter

jevdo can call Jev through [OpenRouter's System One API](https://openrouter.ai/docs/guides/community/typesafe-sdk)
(billed to your OpenRouter account) instead of TypeSafe Cloud. Pick one:

```toml
# environment.toml
[meta]
provider = "openrouter"   # default base_url: https://openrouter.ai/api
```

```bash
export OPENROUTER_API_KEY=...   # from https://openrouter.ai/settings/keys
```

Precedence for the endpoint is `--base-url` > `[meta] base_url` >
`TYPESAFE_BASE_URL` > provider default; for the key it is
`TYPESAFE_API_KEY` > `OPENROUTER_API_KEY` (the SDK itself only reads
`TYPESAFE_API_KEY`, so jevdo forwards `OPENROUTER_API_KEY` explicitly).
`TYPESAFE_BASE_URL=https://openrouter.ai/api` also works and keeps
`provider = "typesafe"`. Bare model IDs (`jev-latest`) and prefixed IDs
(`typesafe/jev-1.13`) are both passed through; model listing via the SDK is
not supported by OpenRouter — browse https://openrouter.ai/typesafe instead.

## `environment.toml`

```toml
[meta]
model = "jev-latest"
min_confidence = 0.5
timeout = 60
default_risk = "read"     # read | write | destructive
max_steps = 1             # chained multi-step budget 1..10
command_question = "What shell task is the user asking for?"
  [meta.risk_thresholds]
  read = 0.5
  write = 0.7
  destructive = 0.9

[[command]]
name = "git"
description = "version control operations"
risk = "read"
instructions_subcommand = "Which git subcommand does the request need?"

  [[command.subcommand]]
  name = "log"
  description = "show commit history"
  argv = ["git", "log"]

    [[command.subcommand.flag]]          # boolean flag
    name = "--oneline"
    description = "compact one-line-per-commit history"
    argv_fragment = ["--oneline"]
    question = "Does the user want compact one-line history?"  # optional

    [[command.subcommand.flag]]          # valued flag
    name = "--format"
    description = "history output format"
    argv_fragment = ["--format={value}"]
    values = ["json", "plain"]
    [command.subcommand.flag.value_descriptions]
    json = "one JSON object per commit"

  [[command.subcommand]]
  name = "add"
  description = "stage a file for commit"
  argv = ["git", "add", "{path:src}"]   # named slot; legacy "{path}" also works
  risk = "write"                        # prompts for confirmation

    [[command.subcommand.path]]
    name = "src"
    kind = "files"          # files | dirs | both
    optional = false
    description = "file to stage"
    base = "."              # relative root inside --cwd
    depth = 3               # 1 = top level only
    glob = "*.py"           # optional fnmatch
    include_dotfiles = false
    max_results = 200
    question = "Which Python file does the request want to stage?"
    stated_question = "Does the request name a specific Python file?"
```

Multi-path example: `argv = ["cp", "{path:src}", "{path:dst}"]` with two
`[[command.path]]` tables (max 3 slots, one placeholder per token).

Legacy v1 style (`takes_path`/`path_kind`/`path_optional` + bare `{path}`)
still loads, desugared to a single slot named `path`. Don't mix both styles.

## Usage

```bash
jevdo "show git status" --dry-run --show-probs
jevdo "brief git history"              # -> git log --oneline
jevdo "stage readme"                   # -> git add ... (asks [y/N], risk write)
jevdo "copy readme into docs"          # -> cp README.md docs
jevdo "run tests" --steps 2            # chained: re-plans after each step
jevdo "run tests" --min-confidence 0.8 # CLI flag wins over all config
jevdo "run tests" --provider openrouter --dry-run  # one-shot OpenRouter routing
jevdo --eval eval.toml                 # eval harness: plans only, never runs
```

`none_of_the_above` semantics: L1 (command) → abstain; L2 (subcommand) → run
base `argv` as-made (abstain if none); valued-flag/path layer → omit if
optional, abstain if required. Confidence = least-certain Choice layer.
Threshold resolution: **CLI `--min-confidence` > per-node > per-risk tier >
global**; the reason names the winner, e.g. `below minimum 0.70 (risk write)`.

Chaining (`--steps N` / `meta.max_steps`): after each step the CWD is
rescanned and `{argv, returncode, stdout_tail}` appended to Jev state history.
Stops on abstention, non-zero exit (unless `--no-stop-on-error`), decline, or
budget. `dispatch_sequence()` in `dispatcher.py` exposes this programmatically.

## Eval mode

`jevdo --eval eval.toml` runs each `[[test]]` input through Jev and compares
the planned argv to `expected` — nothing is executed, nothing prompts:

```toml
[[test]]
name = "brief history"            # optional (default test-N), must be unique
input = "brief git history"       # given to Jev
expected = "git log --oneline"    # shell-parsed, compared as argv list

[[test]]
name = "nonsense abstains"
input = "launch the rockets"
expect_abstain = true             # pass iff Jev abstains (no expected)
```

Per-test `cwd` (relative, joined onto `--cwd`) and `min_confidence`
overrides, plus `[meta]` defaults for both, are supported. Exit 0 when all
pass, 5 otherwise. See `eval.toml.example`.

## Layout

- `src/jevdo/config.py` – toml loading + validation, risk/threshold resolution
- `src/jevdo/client.py` – provider/base_url/API-key resolution, `TypeSafeClient` factory
- `src/jevdo/discovery.py` – recursive `discover()` + containment-safe validation
- `src/jevdo/questions.py` – Jev Choice/Noul builder + custom instructions
- `src/jevdo/dispatcher.py` – `system_one` call, branch reader, `dispatch_sequence`
- `src/jevdo/executor.py` – strict multi-slot/`{value}` resolution + `subprocess`
- `src/jevdo/cli.py` – `jevdo` entrypoint, confirm prompt, step transcript
- `src/jevdo/eval.py` – `--eval` toml loading + no-exec comparison harness
- `tests/` – 79 tests (`PYTHONPATH=src:tests pytest`)
