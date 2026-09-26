# Examples

Two real-world, multi-command jevdo setups. Each example bundles:

- `environment.toml` — allowlisted commands (provider `openrouter` by default)
- `eval.toml` — `[[test]]` cases (plans only, never executes)
- `workspace/` — fixture files/dirs so path slots have real candidates
- `run_eval.sh` — runs the eval via OpenRouter with `OPENROUTER_API_KEY`

## 1. git-workflow — daily version-control tasks

Commands: `git` (status / log / diff / add / branch), `ls`, `cp`.

```bash
export OPENROUTER_API_KEY=...   # https://openrouter.ai/settings/keys
cd examples/git-workflow
./run_eval.sh                   # jevdo --provider openrouter --env environment.toml --cwd workspace --eval eval.toml --log eval-results.jsonl
```

64 eval cases: status/log/diff/branch + paraphrases, `--max-count` valued
flag (incl. stacked `--oneline --max-count=5/10/20`), 15 harder one-shots
(bare/slang/negated phrasings, optional-slot omit, multi-select
abstentions, blame/amend near-misses), 23 round-2 one-shots (antonym flag
pairs, idiom-heavy phrasings, synonym tool references, destructive
near-miss negatives), two-slot copy (`cp src dst`), one chain
(`stage then status`, per-test `continue_threshold = 0.3`), 19 abstention
cases.

## 2. python-qa — test / lint / format / run loop

Commands: `pytest` (boolean `-x`, valued `--tb`), `ruff` (check / format
subcommands), `python`, `ls`.

```bash
export OPENROUTER_API_KEY=...
cd examples/python-qa
./run_eval.sh
```

52 eval cases: pytest base + paraphrase, `-x`, all three `--tb` values,
stacked `-x --tb` combos, bare/pathed `ruff check` and `ruff format
--check`, script run + paraphrase, two dir listings, 10 harder one-shots
(synonym/tool-less requests, flagless degradation to base argv), 18 round-2
one-shots (unsupported-option degradation, destructive near-miss negatives),
one kept-failing chain (`lint then format`), 7 abstention cases.

## Notes

- Both `environment.toml` files set `[meta] provider = "openrouter"`, so no
  extra flag is needed; `./run_eval.sh` also passes `--provider openrouter`
  explicitly (CLI wins, same value).
- Eval mode never executes and never prompts: `write`-risk nodes like
  `git add` / `cp` are only *planned*.
- Results of the last benchmark run live in [`BENCHMARKS.md`](BENCHMARKS.md).
  `eval-results.jsonl` files are git-ignored build artifacts — regenerate with
  the scripts above.
