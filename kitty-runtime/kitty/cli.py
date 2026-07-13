from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
import webbrowser
from pathlib import Path

from kitty.config_file import load_env_file
from kitty.core.config import KittyConfig
from kitty.core.context import AgentRecord, RecordMeta
from kitty.memory.session_store import SQLiteSessionStore
from kitty.onboarding import OnboardingASGIApp, OnboardingService
from kitty.runtime import KittyRuntime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Kitty Feishu bot runtime",
        epilog=(
            "Single process: `kitty serve`. Distributed: "
            "`kitty server`, `kitty worker`, and `kitty sender`."
        ),
    )
    parser.add_argument("--session", default="cli-default", help="Local chat session identifier")
    parser.add_argument("--state-dir", default=".kitty", help="Runtime state directory")
    parser.add_argument("--hook", action="append", default=[], help="Python event hook to load")
    parser.add_argument("--json-events", action="store_true", help="Write runtime events as JSONL")
    parser.add_argument("--once", help="Process one local message and exit")
    parser.add_argument("--delivery-status", action="store_true", help="Print delivery counts")
    parser.add_argument("--retry-delivery", metavar="MESSAGE_ID", help="Requeue one dead delivery")

    commands = parser.add_subparsers(dest="command")
    setup = commands.add_parser("setup", help="Open the guided 10-minute setup wizard")
    setup.add_argument("--host", default="127.0.0.1")
    setup.add_argument("--port", type=int, default=8787)
    setup.add_argument("--output", default=".env", help="Configuration file to create")
    setup.add_argument("--project-root", default=".")
    setup.add_argument("--no-open", action="store_true", help="Do not open a browser")

    doctor = commands.add_parser("doctor", help="Run preflight checks")
    doctor.add_argument("--env-file", default=".env")
    doctor.add_argument("--live", action="store_true", help="Call the model and Feishu APIs")
    doctor.add_argument("--json", action="store_true", dest="json_output")

    serve = commands.add_parser("serve", help="Start the Feishu bot")
    serve.add_argument("--env-file", default=".env")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)

    server = commands.add_parser("server", help="Start stateless distributed ingress")
    server.add_argument("--env-file", help="Optional configuration file")
    server.add_argument("--host", default="0.0.0.0")
    server.add_argument("--port", type=int, default=8000)

    worker = commands.add_parser("worker", help="Start a distributed agent worker")
    worker.add_argument("--env-file", help="Optional configuration file")

    sender = commands.add_parser("sender", help="Start a distributed Feishu sender")
    sender.add_argument("--env-file", help="Optional configuration file")

    jobs = commands.add_parser("jobs", help="Show distributed inbox and outbox counts")
    jobs.add_argument("--env-file", help="Optional configuration file")

    retry_job = commands.add_parser("retry-job", help="Requeue one distributed dead job")
    retry_job.add_argument("kind", choices=("inbox", "outbox"))
    retry_job.add_argument("job_id")
    retry_job.add_argument("--env-file", help="Optional configuration file")
    return parser


async def run_cli(args: argparse.Namespace) -> int:
    if args.command == "setup":
        return await _run_setup(args)
    if args.command == "doctor":
        return await _run_doctor(args)
    if args.command == "serve":
        return await _run_serve(args)
    if args.command == "server":
        return await _run_distributed_server(args)
    if args.command == "worker":
        return await _run_distributed_worker(args)
    if args.command == "sender":
        return await _run_distributed_sender(args)
    if args.command == "jobs":
        return await _run_distributed_jobs(args)
    if args.command == "retry-job":
        return await _run_distributed_retry(args)
    return await _run_local_chat(args)


async def _run_setup(args: argparse.Namespace) -> int:
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("setup wizard only listens on the local machine")
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("uvicorn is required; install the project dependencies") from exc

    project_root = Path(args.project_root).expanduser().resolve()
    output = Path(args.output).expanduser()
    if not output.is_absolute():
        output = Path.cwd() / output
    token = secrets.token_urlsafe(24)
    service = OnboardingService(project_root=project_root, output_path=output)
    app = OnboardingASGIApp(service, setup_token=token)
    host_for_url = "127.0.0.1" if args.host == "::1" else args.host
    url = f"http://{host_for_url}:{args.port}/?token={token}"
    print("\nKitty setup is ready. Secrets stay on this machine.")
    print(url)
    print("Press Ctrl+C when setup is complete.\n")
    if not args.no_open:
        await asyncio.to_thread(webbrowser.open, url)
    server = uvicorn.Server(
        uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
    )
    await server.serve()
    return 0


async def _run_doctor(args: argparse.Namespace) -> int:
    env_path = Path(args.env_file).expanduser().resolve()
    values = load_env_file(env_path)
    project_root = Path(values.get("KITTY_PROJECT_ROOT") or env_path.parent).expanduser().resolve()
    service = OnboardingService(project_root=project_root, output_path=env_path)
    result = await service.check(service.payload_from_environment(values), live=args.live)
    if args.json_output:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print("Kitty launch check\n")
        for item in result["checks"]:
            mark = "✓" if item["ok"] else "✕"
            print(f"{mark} {item['label']}: {item['detail']} ({item['duration_ms']}ms)")
        if not args.live:
            print("\nTip: add --live to verify the model and Feishu credentials.")
        print("\nREADY" if result["ready"] else "\nNOT READY")
    return 0 if result["ready"] else 1


async def _run_serve(args: argparse.Namespace) -> int:
    load_env_file(args.env_file)
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("uvicorn is required; install the project dependencies") from exc
    server = uvicorn.Server(
        uvicorn.Config(
            "kitty.server:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            proxy_headers=True,
        )
    )
    await server.serve()
    return 0


async def _run_distributed_server(args: argparse.Namespace) -> int:
    if args.env_file:
        load_env_file(args.env_file)
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("uvicorn is required; install the project dependencies") from exc
    from kitty.distributed.ingress import create_ingress_app

    server = uvicorn.Server(
        uvicorn.Config(
            create_ingress_app(),
            host=args.host,
            port=args.port,
            proxy_headers=True,
        )
    )
    await server.serve()
    return 0


async def _run_distributed_worker(args: argparse.Namespace) -> int:
    if args.env_file:
        load_env_file(args.env_file)
    from kitty.distributed.worker import create_agent_worker

    await create_agent_worker().run_forever()
    return 0


async def _run_distributed_sender(args: argparse.Namespace) -> int:
    if args.env_file:
        load_env_file(args.env_file)
    from kitty.distributed.sender import create_sender

    await create_sender().run_forever()
    return 0


def _distributed_store(env_file: str | None):
    if env_file:
        load_env_file(env_file)
    from kitty.distributed.config import DistributedSettings
    from kitty.memory.postgres_store import PostgresStore

    return PostgresStore(DistributedSettings.from_env("operator").database_url)


async def _run_distributed_jobs(args: argparse.Namespace) -> int:
    store = _distributed_store(args.env_file)
    try:
        counts = await asyncio.to_thread(store.job_counts)
        print(json.dumps(counts, ensure_ascii=False))
    finally:
        await asyncio.to_thread(store.close)
    return 0


async def _run_distributed_retry(args: argparse.Namespace) -> int:
    store = _distributed_store(args.env_file)
    try:
        requeued = await asyncio.to_thread(store.requeue_dead, args.kind, args.job_id)
        print(json.dumps({"kind": args.kind, "job_id": args.job_id, "requeued": requeued}))
    finally:
        await asyncio.to_thread(store.close)
    return 0 if requeued else 1


async def _run_local_chat(args: argparse.Namespace) -> int:
    config = KittyConfig(state_dir=Path(args.state_dir))
    if args.delivery_status or args.retry_delivery:
        store = SQLiteSessionStore(config.session_db_path)
        if args.delivery_status:
            print(json.dumps(store.feishu_job_counts(), ensure_ascii=False))
            return 0
        requeued = store.requeue_dead_feishu_job(args.retry_delivery)
        print(json.dumps({"job_id": args.retry_delivery, "requeued": requeued}, ensure_ascii=False))
        return 0 if requeued else 1

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
        print(f"Kitty local chat · session={args.session} · type /exit to quit")
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
    try:
        code = asyncio.run(run_cli(args))
    except KeyboardInterrupt:
        code = 130
    except Exception as exc:
        print(f"Kitty: {type(exc).__name__}: {exc}", file=sys.stderr)
        code = 1
    raise SystemExit(code)
