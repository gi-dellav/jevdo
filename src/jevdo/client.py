"""Client factory: build a TypeSafe SDK client for TypeSafe Cloud or OpenRouter.

OpenRouter hosts TypeSafe's System One API at ``https://openrouter.ai/api``
(the SDK appends ``/v1/systemone``). Pointing the official ``typesafe_sdk``
client there with an OpenRouter API key routes Jev calls through OpenRouter
billing — see https://openrouter.ai/docs/guides/community/typesafe-sdk.

Resolution precedence:
- base URL: explicit arg > ``[meta] base_url`` > ``TYPESAFE_BASE_URL`` env
  > provider default (OpenRouter URL for ``provider = "openrouter"``,
  ``None``/SDK default for ``provider = "typesafe"``).
- API key: explicit arg > ``TYPESAFE_API_KEY`` env > ``OPENROUTER_API_KEY``
  env. The ``OPENROUTER_API_KEY`` fallback is passed explicitly because the
  SDK itself only reads ``TYPESAFE_API_KEY`` from the environment.

Model IDs are passed through unchanged: the System One API accepts bare IDs
(``jev-latest`` → newest release) as well as prefixed IDs
(``typesafe/jev-1.13`` used as-is).
"""

from __future__ import annotations

import os
from typing import Mapping

OPENROUTER_BASE_URL = "https://openrouter.ai/api"
PROVIDERS = ("typesafe", "openrouter")


def _env(source: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if source is None else source


def resolve_base_url(config, *, env: Mapping[str, str] | None = None) -> str | None:
    """Return the effective base URL, or None for the SDK default."""
    if config is not None and getattr(config, "base_url", None):
        raw = config.base_url.strip().rstrip("/")
        if raw:
            return raw
    e = _env(env)
    raw = (e.get("TYPESAFE_BASE_URL") or "").strip()
    if raw:
        return raw.rstrip("/")
    if config is not None and getattr(config, "provider", "typesafe") == "openrouter":
        return OPENROUTER_BASE_URL
    return None


def resolve_api_key(
    config=None, *, env: Mapping[str, str] | None = None
) -> tuple[str | None, str | None]:
    """Return (key, source_env_name); (None, None) when unset.

    ``TYPESAFE_API_KEY`` wins so an explicit TypeSafe setup is never
    shadowed; ``OPENROUTER_API_KEY`` is the fallback for OpenRouter users.
    """
    e = _env(env)
    for name in ("TYPESAFE_API_KEY", "OPENROUTER_API_KEY"):
        raw = e.get(name)
        if raw is not None and raw.strip():
            return raw.strip(), name
    return None, None


def has_api_key(config=None, *, env: Mapping[str, str] | None = None) -> bool:
    key, _ = resolve_api_key(config, env=env)
    return key is not None


def create_client(config=None, *, api_key: str | None = None,
                  base_url: str | None = None,
                  timeout: float | None = None,
                  env: Mapping[str, str] | None = None):
    """Build a ``TypeSafeClient`` honoring config + env resolution.

    Explicit ``api_key``/``base_url`` args win; otherwise config then env.
    ``timeout`` defaults to ``config.timeout`` when a config is given.
    """
    from typesafe_sdk import TypeSafeClient

    eff_base = base_url if base_url is not None else resolve_base_url(config, env=env)
    if eff_base is not None:
        eff_base = eff_base.strip().rstrip("/") or None
    if api_key is not None and not api_key.strip():
        api_key = None
    if api_key is None:
        key, _ = resolve_api_key(config, env=env)
        api_key = key
    else:
        api_key = api_key.strip()
    if timeout is None and config is not None:
        timeout = getattr(config, "timeout", None)
    kwargs: dict = {}
    if api_key is not None:
        kwargs["api_key"] = api_key
    if eff_base is not None:
        kwargs["base_url"] = eff_base
    if timeout is not None:
        kwargs["timeout"] = timeout
    return TypeSafeClient(**kwargs)
