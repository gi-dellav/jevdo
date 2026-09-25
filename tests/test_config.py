import textwrap

import pytest

from jevdo.config import ConfigError, load_config


def _write(tmp_path, body: str) -> str:
    p = tmp_path / "environment.toml"
    p.write_text(textwrap.dedent(body))
    return str(p)


BASE = """\
[meta]
model = "jev-latest"

[[command]]
name = "git"
description = "vcs"

  [[command.subcommand]]
  name = "status"
  description = "st"
  argv = ["git", "status"]
"""


def test_load_example_ok(tmp_path):
    p = _write(tmp_path, BASE)
    cfg = load_config(p)
    assert cfg.model == "jev-latest"
    assert cfg.commands[0].name == "git"
    assert cfg.commands[0].subcommands[0].argv == ("git", "status")


def test_reserved_name_rejected(tmp_path):
    p = _write(tmp_path, BASE + """
[[command]]
name = "none_of_the_above"
description = "x"
argv = ["x"]
""")
    with pytest.raises(ConfigError, match="reserved"):
        load_config(p)


def test_path_placeholder_must_match_takes_path(tmp_path):
    bad = BASE.replace('argv = ["git", "status"]', 'argv = ["git", "add", "{path}"]')
    with pytest.raises(ConfigError, match="takes_path = false"):
        load_config(_write(tmp_path, bad))


def test_flag_with_path_rejected(tmp_path):
    bad = BASE + """
  [[command.subcommand.flag]]
  name = "--out"
  description = "x"
  argv_fragment = ["--out", "{path}"]
"""
    with pytest.raises(ConfigError, match="not allowed in flags"):
        load_config(_write(tmp_path, bad))


def test_duplicate_subcommand_rejected(tmp_path):
    bad = BASE + """
  [[command.subcommand]]
  name = "status"
  description = "dup"
  argv = ["git", "status"]
"""
    with pytest.raises(ConfigError, match="duplicate subcommand"):
        load_config(_write(tmp_path, bad))


def test_leaf_needs_argv(tmp_path):
    bad = """\
[[command]]
name = "foo"
description = "x"
"""
    with pytest.raises(ConfigError, match="needs 'argv'"):
        load_config(_write(tmp_path, bad))


def test_missing_file():
    with pytest.raises(ConfigError, match="not found"):
        load_config("/nonexistent/environment.toml")


def test_invalid_risk_rejected(tmp_path):
    bad = BASE.replace('name = "git"', 'name = "git"\nrisk = "nuke"')
    with pytest.raises(ConfigError, match="'risk' must be one of"):
        load_config(_write(tmp_path, bad))


def test_risk_thresholds_validation(tmp_path):
    bad = "[meta]\nmodel = \"jev-latest\"\n[meta.risk_thresholds]\nexplode = 0.9\n" + BASE.replace("[meta]\nmodel = \"jev-latest\"\n", "")
    with pytest.raises(ConfigError, match="unknown risk"):
        load_config(_write(tmp_path, bad))
    bad2 = "[meta]\nmodel = \"jev-latest\"\n[meta.risk_thresholds]\nread = 2\n" + BASE.replace("[meta]\nmodel = \"jev-latest\"\n", "")
    with pytest.raises(ConfigError, match="must be a number in"):
        load_config(_write(tmp_path, bad2))


def test_multipath_slots_ok(tmp_path):
    body = """\
[[command]]
name = "cp"
description = "copy"
argv = ["cp", "{path:src}", "{path:dst}"]

  [[command.path]]
  name = "src"
  kind = "files"

  [[command.path]]
  name = "dst"
  kind = "dirs"
  optional = true
"""
    cfg = load_config(_write(tmp_path, body))
    cmd = cfg.commands[0]
    assert [s.name for s in cmd.paths] == ["src", "dst"]
    assert cmd.paths[1].optional is True


def test_mixed_legacy_and_slots_rejected(tmp_path):
    body = """\
[[command]]
name = "cp"
description = "copy"
argv = ["cp", "{path:src}"]
takes_path = true

  [[command.path]]
  name = "src"
  kind = "files"
"""
    with pytest.raises(ConfigError, match="not both"):
        load_config(_write(tmp_path, body))


def test_undeclared_slot_rejected(tmp_path):
    body = """\
[[command]]
name = "cp"
description = "copy"
argv = ["cp", "{path:src}"]

  [[command.path]]
  name = "other"
  kind = "files"
"""
    with pytest.raises(ConfigError, match="has no .* slot"):
        load_config(_write(tmp_path, body))


def test_valued_flag_ok_and_errors(tmp_path):
    good = BASE + """
  [[command.subcommand.flag]]
  name = "--format"
  description = "fmt"
  argv_fragment = ["--format={value}"]
  values = ["json", "plain"]
"""
    cfg = load_config(_write(tmp_path, good))
    sub = cfg.commands[0].subcommands[0]
    assert sub.flags[0].values == ("json", "plain")

    bad = good.replace('values = ["json", "plain"]', 'values = ["only"]')
    with pytest.raises(ConfigError, match="'values' must be a list of 2..50"):
        load_config(_write(tmp_path, bad))

    bad2 = BASE + """
  [[command.subcommand.flag]]
  name = "--format"
  description = "fmt"
  argv_fragment = ["--format"]
  values = ["json", "plain"]
"""
    with pytest.raises(ConfigError, match="needs '\\{value\\}'"):
        load_config(_write(tmp_path, bad2))


def test_instruction_length_capped(tmp_path):
    doc = BASE.replace('model = "jev-latest"',
                       'model = "jev-latest"\ncommand_question = "' + "q" * 501 + '"')
    with pytest.raises(ConfigError, match="<= 500 chars"):
        load_config(_write(tmp_path, doc))
