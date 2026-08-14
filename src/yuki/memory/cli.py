import argparse
import json
import sys

from yuki.memory.manager import MemoryManager
from yuki.memory.store import MemoryError, MemoryStore


def build_manager(db_path: str, decay_base=1.0, decay_lambda=0.1, decay_threshold=0.02) -> MemoryManager:
    return MemoryManager(
        MemoryStore(db_path),
        decay_base=decay_base, decay_lambda=decay_lambda, decay_threshold=decay_threshold,
    )


def _fmt(mem: dict) -> str:
    meta = json.dumps(mem.get("metadata") or {}, ensure_ascii=False)
    score = mem.get("score")
    score_s = f" score={score:.3f}" if score is not None else ""
    return (
        f"#{mem['id']} [{mem['memory_type']}] conf={mem['confidence']} "
        f"sens={mem['sensitivity']} src={mem['source']} strong={mem['strengthened']} "
        f"last={mem['last_access']:.1f}{score_s} :: {mem['content']} (meta={meta})"
    )


def _cmd_list(args, manager: MemoryManager) -> None:
    for mem in manager.list(memory_type=args.type, min_sensitivity=args.min_sensitivity):
        print(_fmt(mem))


def _cmd_query(args, manager: MemoryManager) -> None:
    for mem in manager.query(
        args.text, memory_type=args.type, top_k=args.top_k, min_sensitivity=args.min_sensitivity,
    ):
        print(_fmt(mem))


def _cmd_add(args, manager: MemoryManager) -> None:
    metadata = {}
    if args.metadata:
        for pair in args.metadata:
            key, _, value = pair.partition("=")
            metadata[key] = value
    mem_id = manager.write(
        args.type, args.content,
        confidence=args.confidence, sensitivity=args.sensitivity,
        source=args.source, metadata=metadata,
    )
    print(mem_id)


def _cmd_get(args, manager: MemoryManager) -> int:
    mem = manager.get(args.id)
    if mem is None:
        print(f"memory #{args.id} not found", file=sys.stderr)
        return 1
    print(_fmt(mem))
    return 0


def _cmd_delete(args, manager: MemoryManager) -> None:
    print(manager.delete(args.id))


def _cmd_strengthen(args, manager: MemoryManager) -> None:
    print(manager.strengthen(args.id))


def _cmd_wipe(args, manager: MemoryManager) -> int:
    if not args.force:
        print("This will permanently delete ALL memories. Type 'yes' to confirm:", end=" ")
        sys.stdout.flush()
        if sys.stdin.readline().strip().lower() != "yes":
            print("aborted", file=sys.stderr)
            return 1
    print(manager.wipe())
    return 0


def _cmd_short_term(args, manager: MemoryManager) -> None:
    for item in manager.short_term_items():
        print(f"[{item['kind']}] {item['content']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="yuki.memory", description="Yuki memory store admin")
    parser.add_argument("--db", default="data/yuki.db", help="SQLite db path")
    parser.add_argument("--decay-base", type=float, default=1.0)
    parser.add_argument("--decay-lambda", type=float, default=0.1)
    parser.add_argument("--decay-threshold", type=float, default=0.02)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list")
    p.add_argument("--type")
    p.add_argument("--min-sensitivity", type=int, default=0)
    p.set_defaults(func=_cmd_list)

    p = sub.add_parser("query")
    p.add_argument("text")
    p.add_argument("--type")
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--min-sensitivity", type=int, default=0)
    p.set_defaults(func=_cmd_query)

    p = sub.add_parser("add")
    p.add_argument("--type", required=True,
                   choices=("preference", "personal", "scenario", "reflection"))
    p.add_argument("--content", required=True)
    p.add_argument("--confidence", type=float, default=0.5)
    p.add_argument("--sensitivity", type=int, default=0)
    p.add_argument("--source", default="cli")
    p.add_argument("--metadata", action="append")
    p.set_defaults(func=_cmd_add)

    p = sub.add_parser("get")
    p.add_argument("id", type=int)
    p.set_defaults(func=_cmd_get)

    p = sub.add_parser("delete")
    p.add_argument("id", type=int)
    p.set_defaults(func=_cmd_delete)

    p = sub.add_parser("strengthen")
    p.add_argument("id", type=int)
    p.set_defaults(func=_cmd_strengthen)

    p = sub.add_parser("wipe")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=_cmd_wipe)

    p = sub.add_parser("short-term")
    p.set_defaults(func=_cmd_short_term)

    args = parser.parse_args(argv)
    try:
        manager = build_manager(args.db, args.decay_base, args.decay_lambda, args.decay_threshold)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        result = args.func(args, manager)
        return result or 0
    except MemoryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        manager.close()
