from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from kitty.core.config import KittyConfig
from kitty.core.context import AgentRecord, RecordMeta
from kitty.runtime import KittyRuntime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Kitty-compatible local agent runtime")
    parser.add_argument("--session", default="cli-default", help="Persistent session identifier")
    parser.add_argument("--state-dir", default=".kitty", help="Runtime state directory")
    parser.add_argument("--hook", action="append", default=[], help="Python event hook to load")
    parser.add_argument("--json-events", action="store_true", help="Write runtime events as JSONL to stderr")
    parser.add_argument("--once", help="Process one message instead of opening an interactive prompt")
    return parser


async def run_cli(args: argparse.Namespace) -> int:
    config = KittyConfig(state_dir=Path(args.state_dir))
    runtime = KittyRuntime(config=config, project_root=Path.cwd())

    if args.json_events:
        async def print_event(event, ctx):
            print(json.dumps(event.to_dict(), ensure_ascii=False), file=sys.stderr, flush=True)

        runtime.hooks.register(print_event, name="cli-json-printer")

    for hook_path in args.hook:
        runtime.load_hook(hook_path)

    record = AgentRecord(
        user_id="cli-user",
        from_user="cli-user",
        sender="cli-user",
        chat_id=args.session,
        meta=RecordMeta(title="CLI", channel="cli"),
    )
    try:
        if args.once is not None:
            result = await runtime.dispatch(args.session, args.once, record=record)
            print(result.reply)
            return 0

        print(f"Kitty mock runtime · session={args.session} · type /exit to quit")
        while True:
            try:
                message = await asyncio.to_thread(input, "You> ")
            except EOFError:
                break
            if message.strip().lower() in {"/exit", "/quit"}:
                break
            if not message.strip():
                continue
            result = await runtime.dispatch(args.session, message, record=record)
            print(f"Kitty> {result.reply}")
        return 0
    finally:
        await runtime.close()


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(run_cli(args)))
