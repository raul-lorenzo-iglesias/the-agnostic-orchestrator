"""CLI entry point — thin wrapper over api.py.


Commands:
    tao run F [F..] — submit task(s) from JSON file(s) and serve
    tao serve       — start queue loop (background process)
    tao submit      — add task to queue
    tao unblock N   — resume a blocked task
    tao stop N      — emergency stop
    tao status [N]  — task list (no ID) or detail (with ID)
    tao traces N    — execution traces
    tao summary N   — aggregated metrics
    tao llm         — LLM service (called by steps, JSON on stdin)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any

from src.api import Engine, _build_provider_pool, load_config
from src.fmt import render_summary, render_task_detail, render_task_list, render_traces
from src.models import TaoError
from src.providers.llm_service import run_llm_service
from src.store import Store

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser with all subcommands."""
    parser = argparse.ArgumentParser(
        prog="tao",
        description="TAO — The Agnostic Orchestrator",
    )
    parser.add_argument(
        "--config",
        default="tao.toml",
        help="path to TOML config file (default: tao.toml)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="enable debug logging",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="output raw JSON (machine-readable)",
    )

    sub = parser.add_subparsers(dest="command")

    # --- serve ---
    p_serve = sub.add_parser("serve", help="start the queue loop")
    p_serve.add_argument("--http", action="store_true", help="enable HTTP API server")
    p_serve.add_argument(
        "--host", default="127.0.0.1", help="HTTP bind address (default: 127.0.0.1)",
    )
    p_serve.add_argument("--port", type=int, default=8321, help="HTTP port (default: 8321)")

    # --- submit ---
    p_submit = sub.add_parser("submit", help="add a task to the queue")
    p_submit.add_argument("--task-id", type=int, default=None, help="task ID (auto-assigned if omitted)")
    p_submit.add_argument("--title", required=True, help="task title")
    p_submit.add_argument("--body", default="", help="task description")
    p_submit.add_argument("--pack", default=None, help="path to pack directory (sets cwd)")
    p_submit.add_argument("--task-config", default="{}", help="JSON dict with flow config")

    # --- unblock ---
    p_unblock = sub.add_parser("unblock", help="resume a blocked task")
    p_unblock.add_argument("task_id", type=int, help="task ID to unblock")
    p_unblock.add_argument("--context", default=None, help="JSON dict to merge into context")
    p_unblock.add_argument("--task-config", default=None, help="JSON dict to update task config")

    # --- stop ---
    p_stop = sub.add_parser("stop", help="request graceful stop for a task")
    p_stop.add_argument("task_id", type=int, help="task ID to stop")

    # --- cancel ---
    p_cancel = sub.add_parser("cancel", help="cancel a task (terminal, cannot be resumed)")
    p_cancel.add_argument("task_id", type=int, help="task ID to cancel")

    # --- restart ---
    p_restart = sub.add_parser("restart", help="restart a task from scratch")
    p_restart.add_argument("task_id", type=int, help="task ID to restart")

    # --- status ---
    p_status = sub.add_parser("status", help="show task state and progress")
    p_status.add_argument(
        "task_id", type=int, nargs="?", default=None, help="task ID (omit for list)",
    )
    p_status.add_argument("--filter", default=None, help="comma-separated status filter")

    # --- traces ---
    p_traces = sub.add_parser("traces", help="show execution traces (JSON)")
    p_traces.add_argument("task_id", type=int, help="task ID")

    # --- summary ---
    p_summary = sub.add_parser("summary", help="show aggregated metrics")
    p_summary.add_argument("task_id", type=int, help="task ID")

    # --- run ---
    p_run = sub.add_parser("run", help="submit task(s) from JSON file(s) and serve")
    p_run.add_argument("files", nargs="+", help="path(s) to task JSON file(s)")

    # --- llm ---
    sub.add_parser("llm", help="LLM service (JSON on stdin/stdout)")

    return parser


# --- Subcommand handlers ---


def _cmd_serve(args: argparse.Namespace) -> None:
    with Engine(config_path=args.config) as engine:
        if args.http:
            from src.server import start_http_server

            # Start queue in background, HTTP server in foreground
            engine._queue.start()
            logger.info("queue started (max_concurrent=%d)", engine._queue._max_concurrent)
            start_http_server(engine, host=args.host, port=args.port)
        else:
            engine.serve()


def _cmd_submit(args: argparse.Namespace) -> None:
    try:
        task_config: dict[str, Any] = json.loads(args.task_config)
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON in --task-config: {e}", file=sys.stderr)
        sys.exit(1)

    if args.pack:
        task_config.setdefault("cwd", args.pack)

    with Engine(config_path=args.config) as engine:
        assigned_id = engine.submit(
            args.task_id,
            args.title,
            args.body,
            config=task_config,
        )
    print(f"submitted task {assigned_id}")


def _cmd_unblock(args: argparse.Namespace) -> None:
    context: dict[str, Any] | None = None
    if args.context is not None:
        try:
            context = json.loads(args.context)
        except json.JSONDecodeError as e:
            print(f"error: invalid JSON in --context: {e}", file=sys.stderr)
            sys.exit(1)

    task_config: dict[str, Any] | None = None
    if args.task_config is not None:
        try:
            task_config = json.loads(args.task_config)
        except json.JSONDecodeError as e:
            print(f"error: invalid JSON in --task-config: {e}", file=sys.stderr)
            sys.exit(1)

    with Engine(config_path=args.config) as engine:
        engine.unblock(args.task_id, context, config=task_config)
    print(f"unblocked task {args.task_id}")


def _cmd_stop(args: argparse.Namespace) -> None:
    with Engine(config_path=args.config) as engine:
        engine.stop(args.task_id)
    print(f"stopped task {args.task_id}")


def _cmd_cancel(args: argparse.Namespace) -> None:
    with Engine(config_path=args.config) as engine:
        engine.cancel(args.task_id)
    print(f"cancelled task {args.task_id}")


def _cmd_restart(args: argparse.Namespace) -> None:
    with Engine(config_path=args.config) as engine:
        engine.restart(args.task_id)
    print(f"restarted task {args.task_id}")


def _cmd_status(args: argparse.Namespace) -> None:
    with Engine(config_path=args.config) as engine:
        if args.task_id is None:
            # List view
            tasks = engine.list_tasks()
            if args.filter:
                allowed = {s.strip() for s in args.filter.split(",")}
                tasks = [t for t in tasks if t["status"] in allowed]
            if args.json:
                print(json.dumps(tasks, indent=2, default=str))
            else:
                summaries = {t["task_id"]: engine.summary(t["task_id"]) for t in tasks}
                print(render_task_list(tasks, summaries))
        else:
            # Detail view
            if args.json:
                print(json.dumps(engine.get_status(args.task_id), indent=2, default=str))
            else:
                task = engine.get_status(args.task_id)
                traces = engine.get_traces(args.task_id)
                summary = engine.summary(args.task_id)
                print(render_task_detail(task, traces, summary))


def _cmd_traces(args: argparse.Namespace) -> None:
    with Engine(config_path=args.config) as engine:
        data = engine.get_traces(args.task_id)
    if args.json:
        print(json.dumps(data, indent=2, default=str))
    else:
        print(render_traces(data))


def _cmd_summary(args: argparse.Namespace) -> None:
    with Engine(config_path=args.config) as engine:
        data = engine.summary(args.task_id)
    if args.json:
        print(json.dumps(data, indent=2, default=str))
    else:
        print(render_summary(data))


def _parse_task_file(file_path: str) -> tuple[int | None, str, str, dict[str, Any]]:
    """Parse a task JSON file into (task_id, title, body, config).

    Raises:
        SystemExit: on file not found, invalid JSON, or missing body_file.
    """
    try:
        with open(file_path, encoding="utf-8") as f:
            task_def = json.load(f)
    except FileNotFoundError:
        print(f"error: file not found: {file_path}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON in {file_path}: {e}", file=sys.stderr)
        sys.exit(1)

    title = task_def.pop("title", "")
    body = task_def.pop("body", "")
    body_file = task_def.pop("body_file", None)
    task_id = task_def.pop("task_id", None)

    if body_file:
        base_dir = os.path.dirname(os.path.abspath(file_path))
        body_path = os.path.join(base_dir, body_file)
        try:
            with open(body_path, encoding="utf-8") as bf:
                body = bf.read()
        except FileNotFoundError:
            print(f"error: body_file not found: {body_path}", file=sys.stderr)
            sys.exit(1)

    return task_id, title, body, task_def


def _cmd_run(args: argparse.Namespace) -> None:
    with Engine(config_path=args.config) as engine:
        for file_path in args.files:
            task_id, title, body, config = _parse_task_file(file_path)
            assigned_id = engine.submit(task_id, title, body, config=config)
            print(f"submitted task {assigned_id}")
        engine.serve()


def _cmd_llm(args: argparse.Namespace) -> None:
    # TAO_CONFIG env var is set by the engine for steps calling tao llm.
    # Falls back to --config flag (default: tao.toml).
    config_path = os.environ.get("TAO_CONFIG", args.config)
    config = load_config(config_path)
    pool = _build_provider_pool(config)

    on_trace = None
    store = None
    task_id_env = os.environ.get("TAO_TASK_ID")
    if task_id_env is not None:
        engine_config = config.get("engine", {})
        db_path = engine_config.get("db_path", ".tao/engine.db")
        store = Store(db_path)
        tid = int(task_id_env)

        def _trace_callback(trace: dict[str, Any]) -> None:
            store.record_trace(tid, trace)

        on_trace = _trace_callback

    try:
        exit_code = run_llm_service(pool, on_trace=on_trace)
    finally:
        if store is not None:
            store.close()
    sys.exit(exit_code)


_COMMANDS: dict[str, Any] = {
    "serve": _cmd_serve,
    "submit": _cmd_submit,
    "unblock": _cmd_unblock,
    "stop": _cmd_stop,
    "cancel": _cmd_cancel,
    "restart": _cmd_restart,
    "status": _cmd_status,
    "traces": _cmd_traces,
    "summary": _cmd_summary,
    "run": _cmd_run,
    "llm": _cmd_llm,
}


def main(argv: list[str] | None = None) -> None:
    """CLI entry point with two-tier error handling.

    Exit codes:
        0 — success
        1 — expected error (TaoError) — user can fix
        2 — unexpected error (bug)
    """
    # Fix Unicode output on Windows — cp1252 can't encode status icons (✔, ✘, ─).
    # Reconfigure stdout to replace unencodable chars rather than crashing.
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except Exception:
            pass

    parser = _build_parser()
    args = parser.parse_args(argv)

    level = logging.DEBUG if getattr(args, "verbose", False) else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] [%(threadName)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.command is None:
        parser.print_help(sys.stderr)
        sys.exit(1)

    handler = _COMMANDS.get(args.command)
    if handler is None:
        parser.print_help(sys.stderr)
        sys.exit(1)

    try:
        handler(args)
    except SystemExit:
        raise
    except TaoError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception:
        logger.exception("unexpected error")
        sys.exit(2)


if __name__ == "__main__":
    main()
