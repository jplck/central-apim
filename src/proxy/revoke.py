"""revoke.py — add/remove an agent id in the App Config `revocations` set.

The operational "kill an agent" tool, and the core an Event Hub consumer calls when a
Defender/Purview alert fires. Read-merge-write so it never clobbers existing revocations
(a raw `az appconfig kv set` overwrites the whole value).

Auth: DefaultAzureCredential (operator `az login`, or a managed identity). The principal
needs **App Configuration Data Owner** on the store (server.py's read-only MI is not enough).

Usage (from src/proxy, with APP_CONFIG_ENDPOINT set):
  python revoke.py --add <blueprintAppId> [--add <oid> ...]
  python revoke.py --remove <blueprintAppId>
  python revoke.py --list
  python revoke.py --self-test
"""
from __future__ import annotations

import argparse
import json
import sys

from server import ENDPOINT, KEY, LABEL, parse_revocations  # same dir; stdlib-only at import


def apply(current: set[str], add: list[str], remove: list[str]) -> set[str]:
    """Pure merge: add wins into the set, then remove takes ids out. Idempotent."""
    return (current | set(add)) - set(remove)


def _client():
    from azure.appconfiguration import AzureAppConfigurationClient
    from azure.identity import DefaultAzureCredential

    return AzureAppConfigurationClient(base_url=ENDPOINT, credential=DefaultAzureCredential())


def _read(client) -> set[str]:
    from azure.core.exceptions import ResourceNotFoundError

    try:
        return parse_revocations(client.get_configuration_setting(key=KEY, label=LABEL).value)
    except ResourceNotFoundError:
        return set()


def _write(client, ids: set[str]) -> None:
    from azure.appconfiguration import ConfigurationSetting

    client.set_configuration_setting(
        ConfigurationSetting(
            key=KEY, label=LABEL, value=json.dumps(sorted(ids)), content_type="application/json"
        )
    )


def _selftest() -> None:
    assert apply(set(), ["a"], []) == {"a"}
    assert apply({"a"}, ["a"], []) == {"a"}          # idempotent add
    assert apply({"a", "b"}, [], ["a"]) == {"b"}      # remove
    assert apply({"a"}, ["b"], ["a"]) == {"b"}        # add + remove in one call
    assert apply(set(), [], ["x"]) == set()           # removing an absent id is a no-op
    print("self-test ok")


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="edit the App Config revocations set")
    p.add_argument("--add", action="append", default=[], metavar="ID")
    p.add_argument("--remove", action="append", default=[], metavar="ID")
    p.add_argument("--list", action="store_true")
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args(argv)

    if args.self_test:
        _selftest()
        return 0
    if not ENDPOINT:
        print("set APP_CONFIG_ENDPOINT", file=sys.stderr)
        return 2

    client = _client()
    current = _read(client)
    if args.list and not (args.add or args.remove):
        print(json.dumps(sorted(current)))
        return 0

    updated = apply(current, args.add, args.remove)
    if updated != current:
        _write(client, updated)
    print(json.dumps(sorted(updated)))
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        _selftest()
    else:
        raise SystemExit(main(sys.argv[1:]))
