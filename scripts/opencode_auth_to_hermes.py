#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import pathlib
import sys
from typing import Any, Iterator

PROVIDERS = {
    "openrouter": ("OPENROUTER_API_KEY", "openrouter"),
    "openai": ("OPENAI_API_KEY", "openai"),
    "anthropic": ("ANTHROPIC_API_KEY", "anthropic"),
    "deepseek": ("DEEPSEEK_API_KEY", "deepseek"),
    "nous": ("NOUS_API_KEY", "nous"),
}

SECRET_FIELDS = {
    "api_key",
    "apikey",
    "key",
    "token",
    "access_token",
    "accesstoken",
}


def walk(value: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Any]]:
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from walk(child, path + (str(key),))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk(child, path + (str(index),))


def normalize(text: str) -> str:
    return text.lower().replace("-", "").replace("_", "")


def detect_provider(path: tuple[str, ...], node: Any) -> str | None:
    haystack = " ".join(path).lower()
    if isinstance(node, dict):
        for field in ("provider", "provider_id", "providerId", "type", "id", "name"):
            raw = node.get(field)
            if isinstance(raw, str):
                haystack += " " + raw.lower()
    for provider in PROVIDERS:
        if provider in haystack:
            return provider
    return None


def find_secret(node: Any) -> str | None:
    if not isinstance(node, dict):
        return None
    for key, value in node.items():
        if normalize(str(key)) in {normalize(x) for x in SECRET_FIELDS} and isinstance(value, str) and value.strip():
            return value.strip()
    return None


def main() -> int:
    raw = os.environ.get("OPENCODE_AUTH_JSON_PRIMARY") or os.environ.get("OPENCODE_AUTH_JSON_FALLBACK")
    if not raw:
        print("AUTH_ADAPTER=FAILED")
        print("REASON=missing_opencode_auth_secret")
        return 2

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print("AUTH_ADAPTER=FAILED")
        print("REASON=invalid_json")
        return 3

    matches: list[tuple[str, str]] = []
    for path, node in walk(data):
        provider = detect_provider(path, node)
        secret = find_secret(node)
        if provider and secret:
            matches.append((provider, secret))

    if not matches:
        # A top-level provider -> secret mapping is also common.
        if isinstance(data, dict):
            for key, value in data.items():
                provider = str(key).lower()
                if provider in PROVIDERS and isinstance(value, str) and value.strip():
                    matches.append((provider, value.strip()))

    if not matches:
        print("AUTH_ADAPTER=FAILED")
        print("REASON=no_hermes_compatible_provider_credential_found")
        print("NOTE=OpenCode OAuth/session credentials may require a provider-specific adapter")
        return 4

    provider, secret = matches[0]
    env_name, hermes_provider = PROVIDERS[provider]

    github_env = os.environ.get("GITHUB_ENV")
    github_output = os.environ.get("GITHUB_OUTPUT")
    if not github_env or not github_output:
        print("AUTH_ADAPTER=FAILED")
        print("REASON=not_running_in_github_actions")
        return 5

    with open(github_env, "a", encoding="utf-8") as handle:
        handle.write(f"{env_name}={secret}\n")

    with open(github_output, "a", encoding="utf-8") as handle:
        handle.write(f"provider={hermes_provider}\n")
        handle.write(f"env_name={env_name}\n")

    print("AUTH_ADAPTER=PASS")
    print(f"PROVIDER={hermes_provider}")
    print(f"EXPORTED_ENV={env_name}")
    print("SECRET_VALUE=REDACTED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
