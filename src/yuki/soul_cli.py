"""Offline administration CLI for Soul state and version recovery."""

import argparse
import json
import sys

from yuki.cognition.brain.soul import SoulStore
from yuki.config import Config


def build_store(args: argparse.Namespace) -> SoulStore:
    config = Config.load(args.config)
    return SoulStore(
        args.path or config.soul.path,
        args.persona_name or config.persona_name,
        cooldown_state_path=config.soul.cooldown_state_path,
        legacy_tuner_state_path=config.soul.legacy_tuner_state_path,
        snapshots_dir=args.snapshots_dir or config.soul.snapshots_dir,
        max_versions=config.soul.max_versions,
        min_snapshot_interval_s=config.soul.min_snapshot_interval_s,
        max_description_chars=config.soul.max_description_chars,
    )


def _cmd_show(_args: argparse.Namespace, store: SoulStore) -> int:
    print(json.dumps(store.load_or_default(), ensure_ascii=False, indent=2))
    return 0


def _cmd_list(_args: argparse.Namespace, store: SoulStore) -> int:
    current = int(store.load_or_default().get("revision", 0))
    revisions = store.list_revisions()
    if not revisions:
        print(f"current: r{current} (no restorable snapshots)")
        return 0
    print(f"current: r{current}")
    for revision in revisions:
        active = " [current]" if revision == current else ""
        print(f"r{revision}{active}")
    return 0


def _cmd_restore(args: argparse.Namespace, store: SoulStore) -> int:
    if not args.yes:
        try:
            confirmed = input(
                f"Restore r{args.revision} as a new revision? Type 'yes' to continue: "
            )
        except EOFError:
            confirmed = ""
        if confirmed.strip().lower() != "yes":
            print("restore cancelled")
            return 0
    result = store.restore(args.revision)
    print(
        f"restored r{result['restored_revision']} as new revision r{result['revision']}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m yuki.soul_cli",
        description="Offline Yuki Soul inspection and recovery",
    )
    parser.add_argument("--config", help="config file; defaults to config.yaml when present")
    parser.add_argument("--path", help="override soul.json path")
    parser.add_argument("--snapshots-dir", help="override soul snapshot directory")
    parser.add_argument("--persona-name", help="override configured persona name")
    subparsers = parser.add_subparsers(dest="command", required=True)

    command = subparsers.add_parser("show", help="print the current Soul JSON")
    command.set_defaults(func=_cmd_show)
    command = subparsers.add_parser("list", help="list committed restorable revisions")
    command.set_defaults(func=_cmd_list)
    command = subparsers.add_parser("restore", help="restore a snapshot as a new revision")
    command.add_argument("revision", type=int)
    command.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    command.set_defaults(func=_cmd_restore)

    args = parser.parse_args(argv)
    try:
        return args.func(args, build_store(args))
    except (ValueError, KeyError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
