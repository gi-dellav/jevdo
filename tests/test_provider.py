"""Provider selection: typesafe cloud vs OpenRouter System One API."""

import textwrap

import pytest

from jevdo import client as client_mod
from jevdo.config import ConfigError, OPENROUTER_BASE_URL, load_config
from conftest import sample_config


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


def test_provider_defaults_to_typesafe(tmp_path):
    cfg = load_config(_write(tmp_path, BASE))
    assert cfg.provider == "typesafe"
    assert cfg.base_url is None
    assert cfg.effective_base_url is None


def test_provider_openrouter_sets_default_base_url(tmp_path):
    cfg = load_config(_write(tmp_path, '[meta]\nprovider = "openrouter"\n'
                            + BASE.replace("[meta]\nmodel = \"jev-latest\"\n", "")))
    assert cfg.provider == "openrouter"
    assert cfg.effective_base_url == OPENROUTER_BASE_URL == "https://openrouter.ai/api"


def test_explicit_base_url_wins_and_strips_slash(tmp_path):
    body = '[meta]\nprovider = "openrouter"\nbase_url = "https://openrouter.ai/api/"\n' \
        + BASE.replace("[meta]\nmodel = \"jev-latest\"\n", "")
    cfg = load_config(_write(tmp_path, body))
    assert cfg.effective_base_url == "https://openrouter.ai/api"


def test_bad_provider_and_base_url_rejected(tmp_path):
    with pytest.raises(ConfigError, match="meta.provider must be one of"):
        load_config(_write(tmp_path, BASE.replace('model = "jev-latest"',
                                                  'model = "jev-latest"\nprovider = "bogus"')))
    with pytest.raises(ConfigError, match="meta.base_url must start with http"):
        load_config(_write(tmp_path, BASE.replace('model = "jev-latest"',
                                                  'model = "jev-latest"\nbase_url = "example.com"')))
    with pytest.raises(ConfigError, match="meta.base_url must be a non-empty string"):
        load_config(_write(tmp_path, BASE.replace('model = "jev-latest"',
                                                  'model = "jev-latest"\nbase_url = ""')))


def test_resolve_base_url_precedence():
    cfg = sample_config()
    # 1. SDK default when typesafe + no env
    assert client_mod.resolve_base_url(cfg, env={}) is None
    # 2. provider default
    cfg_or = sample_config(provider="openrouter")
    assert client_mod.resolve_base_url(cfg_or, env={}) == OPENROUTER_BASE_URL
    # 3. env TYPESAFE_BASE_URL wins over provider default
    assert client_mod.resolve_base_url(
        cfg_or, env={"TYPESAFE_BASE_URL": "https://custom.example/"}) == "https://custom.example"
    # 4. explicit config base_url wins over env
    cfg_explicit = sample_config(base_url="https://explicit.example/api/")
    assert client_mod.resolve_base_url(
        cfg_explicit, env={"TYPESAFE_BASE_URL": "https://custom.example"}) \
        == "https://explicit.example/api"


def test_resolve_api_key_typesafe_wins_and_strips():
    key, src = client_mod.resolve_api_key(env={"TYPESAFE_API_KEY": " tsk ",
                                                "OPENROUTER_API_KEY": "ork"})
    assert (key, src) == ("tsk", "TYPESAFE_API_KEY")
    key, src = client_mod.resolve_api_key(env={"OPENROUTER_API_KEY": "ork"})
    assert (key, src) == ("ork", "OPENROUTER_API_KEY")
    assert client_mod.resolve_api_key(env={}) == (None, None)
    assert client_mod.resolve_api_key(env={"TYPESAFE_API_KEY": "   "}) == (None, None)
    assert client_mod.has_api_key(env={"OPENROUTER_API_KEY": "ork"}) is True
    assert client_mod.has_api_key(env={}) is False


def test_create_client_passes_base_url_key_timeout(monkeypatch):
    import typesafe_sdk

    seen = {}

    class FakeClient:
        def __init__(self, **kw):
            seen.update(kw)

        def close(self):
            pass

    monkeypatch.setattr(typesafe_sdk, "TypeSafeClient", FakeClient)
    cfg = sample_config(provider="openrouter", timeout=11.0)
    c = client_mod.create_client(cfg, env={"OPENROUTER_API_KEY": "ork"})
    assert isinstance(c, FakeClient)
    assert seen == {"api_key": "ork", "base_url": OPENROUTER_BASE_URL, "timeout": 11.0}

    # explicit args win over config/env
    seen.clear()
    client_mod.create_client(cfg, api_key="x", base_url="https://e.example/",
                             timeout=5.0, env={"OPENROUTER_API_KEY": "ork"})
    assert seen == {"api_key": "x", "base_url": "https://e.example", "timeout": 5.0}


def test_dispatch_uses_provider_client(monkeypatch, config, workdir):
    import jevdo.client as cli_mod
    import jevdo.dispatcher as disp
    from conftest import FakeChoice

    created = {}

    class FakeClient:
        def system_one(self, **kw):
            created.update(kw)
            class R:
                answers = {"__command__": FakeChoice("pytest", {"pytest": 1.0}, 0.99)}
            return R()

        def close(self):
            created["closed"] = True

    def fake_create(cfg, **kw):
        created["cfg_provider"] = cfg.provider
        return FakeClient()

    monkeypatch.setattr(cli_mod, "create_client", fake_create)
    # force re-import path: dispatcher.create_client may not exist; call dispatch
    # with an explicit provider config to prove it flows through
    import dataclasses
    cfg = dataclasses.replace(config, provider="openrouter")
    outcome, _q, _c, _r = disp.dispatch(cfg, "run tests", workdir)
    assert outcome.ok
    assert created.get("cfg_provider") == "openrouter"
    assert created.get("closed") is True


def test_cli_provider_and_base_url_overrides():
    from jevdo import cli as cli_mod
    from jevdo.config import EnvConfig

    cfg = sample_config()
    args = cli_mod.build_parser().parse_args(["x", "--provider", "openrouter"])
    out = cli_mod.apply_cli_overrides(cfg, args)
    assert out.provider == "openrouter"
    assert out.effective_base_url == OPENROUTER_BASE_URL

    args = cli_mod.build_parser().parse_args(["x", "--base-url", "https://e.example/api/"])
    out = cli_mod.apply_cli_overrides(cfg, args)
    assert out.base_url == "https://e.example/api"

    args = cli_mod.build_parser().parse_args(["x", "--base-url", "notaurl"])
    with pytest.raises(ConfigError, match="--base-url"):
        cli_mod.apply_cli_overrides(cfg, args)


def test_cli_accepts_openrouter_key(monkeypatch, tmp_path, capsys):
    from jevdo import cli as cli_mod
    from jevdo.dispatcher import PlanOutcome, PlannedAction

    env = _write(tmp_path, '[meta]\nmodel = "jev-latest"\nprovider = "openrouter"\n' + """\
[[command]]
name = "pytest"
description = "tests"
argv = ["pytest", "-q"]
""")
    monkeypatch.setenv("OPENROUTER_API_KEY", "ork")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

    import jevdo.dispatcher as disp

    def fake_dispatch(cfg, request, cwd=".", **kw):
        assert cfg.provider == "openrouter"
        a = PlannedAction(command="pytest", subcommand=None, confidence=0.99, risk="read")
        return PlanOutcome(ok=True, action=a, reason="ok", confidence=0.99), None, None, None

    monkeypatch.setattr(disp, "dispatch", fake_dispatch)
    monkeypatch.setattr(cli_mod, "dispatch", fake_dispatch)
    rc = cli_mod.main(["--env", env, "--cwd", str(tmp_path), "--dry-run", "run tests"])
    assert rc == cli_mod.EXIT_OK
    assert "pytest -q" in capsys.readouterr().out


def test_cli_missing_key_message_names_both(monkeypatch, tmp_path, capsys):
    from jevdo import cli as cli_mod

    env = _write(tmp_path, '[meta]\nmodel = "jev-latest"\n' + """\
[[command]]
name = "pytest"
description = "tests"
argv = ["pytest", "-q"]
""")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    rc = cli_mod.main(["--env", env, "run tests"])
    assert rc == cli_mod.EXIT_CONFIG
    assert "OPENROUTER_API_KEY" in capsys.readouterr().err
