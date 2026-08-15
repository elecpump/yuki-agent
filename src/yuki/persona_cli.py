import argparse
import json
import sys

from yuki.cognition.brain.snapshots import PersonaStore


def build_store(args) -> PersonaStore:
    return PersonaStore(args.path, max_versions=args.max_versions)


def _cmd_list(args, store):
    for snap in store.list_versions():
        mark = " [locked]" if snap.locked else ""
        print(f"v{snap.version}{mark} :: {snap.persona_prompt[:40]}")
    active = store.active()
    print(f"active: v{active.version if active else 'none'}")


def _cmd_active(args, store):
    snap = store.active()
    if snap is None:
        print("no active snapshot", file=sys.stderr)
        return 1
    print(snap.persona_prompt)
    return 0


def _cmd_rollback(args, store):
    store.rollback(args.version)
    print(f"rolled back to v{args.version}")


def _cmd_lock(args, store):
    store.lock(args.version)
    print(f"locked v{args.version}")


def _cmd_reset(args, store):
    store.reset()
    print("reset to base snapshot")


def _cmd_diff(args, store):
    print(store.diff(args.v1, args.v2))


def _cmd_export(args, store):
    print(json.dumps(store.export(args.version), ensure_ascii=False, indent=2))


def _cmd_import(args, store):
    with open(args.file, encoding="utf-8") as fh:
        data = json.load(fh)
    store.import_snapshot(data)
    print(f"imported v{data['version']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="yuki.persona", description="Yuki persona snapshots admin")
    parser.add_argument("--path", default="data/persona_snapshots.json")
    parser.add_argument("--max-versions", type=int, default=50)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list"); p.set_defaults(func=_cmd_list)
    p = sub.add_parser("active"); p.set_defaults(func=_cmd_active)
    p = sub.add_parser("rollback"); p.add_argument("version", type=int); p.set_defaults(func=_cmd_rollback)
    p = sub.add_parser("lock"); p.add_argument("version", type=int); p.set_defaults(func=_cmd_lock)
    p = sub.add_parser("reset"); p.set_defaults(func=_cmd_reset)
    p = sub.add_parser("diff"); p.add_argument("v1", type=int); p.add_argument("v2", type=int); p.set_defaults(func=_cmd_diff)
    p = sub.add_parser("export"); p.add_argument("version", type=int); p.set_defaults(func=_cmd_export)
    p = sub.add_parser("import"); p.add_argument("file"); p.set_defaults(func=_cmd_import)

    args = parser.parse_args(argv)
    store = build_store(args)
    try:
        return args.func(args, store) or 0
    except (ValueError, KeyError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
