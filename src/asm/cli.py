from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .codex_integration import install_codex_hooks
from .config import resolve_paths
from .context import current_context
from .models import CheckpointPayload
from .storage import Registry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="asm")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--force", action="store_true")

    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("--agent", required=True)
    start_parser.add_argument("--agent-session-ref", required=True)

    checkpoint_parser = subparsers.add_parser("checkpoint")
    checkpoint_parser.add_argument("--session", required=True)
    checkpoint_parser.add_argument("--payload")
    checkpoint_parser.add_argument("--payload-file")

    checkpoint_current_parser = subparsers.add_parser("checkpoint-current")
    checkpoint_current_parser.add_argument("--payload")
    checkpoint_current_parser.add_argument("--payload-file")

    stop_parser = subparsers.add_parser("stop")
    stop_parser.add_argument("--session", required=True)
    stop_parser.add_argument("--payload")
    stop_parser.add_argument("--payload-file")

    stop_current_parser = subparsers.add_parser("stop-current")
    stop_current_parser.add_argument("--payload")
    stop_current_parser.add_argument("--payload-file")

    ls_parser = subparsers.add_parser("ls")
    ls_parser.add_argument("--limit", type=int, default=20)
    ls_parser.add_argument("--agent")
    ls_parser.add_argument("--status", choices=["active", "stopped"])
    ls_parser.add_argument("--branch")
    ls_parser.add_argument("--project")
    ls_parser.add_argument("--long", action="store_true", dest="long_output")

    doctor_parser = subparsers.add_parser("doctor")
    doctor_parser.add_argument("--limit", type=int, default=5)
    doctor_parser.add_argument("--explain", action="store_true")

    subparsers.add_parser("current")
    subparsers.add_parser("checkpoint-template")
    subparsers.add_parser("checkpoint-prompt")

    subparsers.add_parser("codex-hook")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = resolve_paths()
    registry = Registry(paths.registry_db)
    stdin_cache: list[str | None] = [None]

    try:
        if args.command == "checkpoint-template":
            print(
                json.dumps(
                    {
                        "title": "",
                        "goal": "",
                        "summary": "",
                        "completed": [],
                        "blockers": [],
                        "next_actions": [],
                    },
                    indent=2,
                )
            )
            return 0

        if args.command == "checkpoint-prompt":
            print(
                "\n".join(
                    [
                        "Please output only JSON that matches this ASM checkpoint schema.",
                        "Do not add markdown fences, prose, or commentary.",
                        "",
                        json.dumps(
                            {
                                "title": "",
                                "goal": "",
                                "summary": "",
                                "completed": [],
                                "blockers": [],
                                "next_actions": [],
                            },
                            indent=2,
                        ),
                    ]
                )
            )
            return 0

        if args.command == "init":
            registry.initialize()
            install_codex_hooks(paths, force=args.force)
            print(paths.registry_db)
            return 0

        registry.initialize()

        if args.command == "start":
            session_id = registry.start_session(
                agent=args.agent,
                agent_session_ref=args.agent_session_ref,
                context=current_context(),
            )
            print(session_id)
            return 0

        if args.command == "checkpoint":
            payload = _load_payload_from_args(args, stdin_cache)
            registry.add_checkpoint(args.session, "progress", payload)
            print(args.session)
            return 0

        if args.command == "checkpoint-current":
            matches = registry.doctor(current_context(), limit=1)
            if not matches:
                raise ValueError("No relevant sessions found.")
            payload = _load_payload_from_args(args, stdin_cache)
            session_id = matches[0].session_id
            registry.add_checkpoint(session_id, "progress", payload)
            print(session_id)
            return 0

        if args.command == "stop":
            if _payload_available(args, stdin_cache):
                payload = _load_payload_from_args(args, stdin_cache)
                registry.add_checkpoint(args.session, "final", payload)
            registry.stop_session(args.session)
            print(args.session)
            return 0

        if args.command == "stop-current":
            matches = registry.doctor(current_context(), limit=1)
            if not matches:
                raise ValueError("No relevant sessions found.")
            session_id = matches[0].session_id
            if _payload_available(args, stdin_cache):
                payload = _load_payload_from_args(args, stdin_cache)
                registry.add_checkpoint(session_id, "final", payload)
            registry.stop_session(session_id)
            print(session_id)
            return 0

        if args.command == "ls":
            rows = registry.list_sessions(
                limit=args.limit,
                agent=args.agent,
                status=args.status,
                branch=args.branch,
                project=args.project,
            )
            for row in rows:
                title = row["title"] or "[untitled]"
                print(f'{row["id"]}  {row["agent"]}  {title}')
                print(
                    f'  project: {row["project"] or "-"}  branch: {row["branch"] or "-"}  '
                    f'status: {row["status"]}  updated: {row["updated_at"]}'
                )
                print(f'  resume: {row["resume_command"]}')
                if args.long_output and row["goal"]:
                    print(f'  goal: {row["goal"]}')
                if row["latest_summary"]:
                    print(f'  last: {row["latest_summary"]}')
                print()
            return 0

        if args.command == "doctor":
            matches = registry.doctor(current_context(), limit=args.limit)
            if not matches:
                print("No relevant sessions found.")
                return 0
            for match in matches:
                title = match.title or "[untitled]"
                print(f"{match.session_id}  {title}")
                print(
                    f"  project: {match.project or '-'}  branch: {match.branch or '-'}  "
                    f"status: {match.status}  score: {match.score}"
                )
                print(f"  resume: {match.resume_command}")
                if match.latest_summary:
                    print(f"  last: {match.latest_summary}")
                if args.explain:
                    print("  why:")
                    for value, label in match.reason_scores:
                        print(f"    +{value} {label}")
                else:
                    print(f"  reasons: {', '.join(match.reasons)}")
                print()
            return 0

        if args.command == "current":
            matches = registry.doctor(current_context(), limit=1)
            if not matches:
                print("No relevant sessions found.")
                return 0
            match = matches[0]
            title = match.title or "[untitled]"
            print(f"{match.session_id}  {title}")
            print(
                f"  project: {match.project or '-'}  branch: {match.branch or '-'}  "
                f"status: {match.status}  score: {match.score}"
            )
            print(f"  resume: {match.resume_command}")
            if match.latest_summary:
                print(f"  last: {match.latest_summary}")
            print(f"  reasons: {', '.join(match.reasons)}")
            return 0

        if args.command == "codex-hook":
            payload = json.load(sys.stdin)
            return _handle_codex_hook(registry, payload)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return 0


def _parse_payload(raw: str) -> CheckpointPayload:
    data = json.loads(raw)
    return CheckpointPayload(
        title=data.get("title"),
        goal=data.get("goal"),
        summary=data["summary"],
        completed=list(data.get("completed", [])),
        blockers=list(data.get("blockers", [])),
        next_actions=list(data.get("next_actions", [])),
    )


def _load_payload_from_args(args: argparse.Namespace, stdin_cache: list[str | None]) -> CheckpointPayload:
    inline_payload = getattr(args, "payload", None)
    file_payload = getattr(args, "payload_file", None)
    if inline_payload and file_payload:
        raise ValueError("provide checkpoint payload via only one of --payload, --payload-file, or stdin")

    if inline_payload:
        return _parse_payload(inline_payload)
    if file_payload:
        raw = Path(args.payload_file).read_text()
        return _parse_payload(raw)

    raw_stdin = _read_stdin(stdin_cache)
    if raw_stdin.strip():
        return _parse_payload(raw_stdin)
    raise ValueError("checkpoint payload required via --payload, --payload-file, or stdin")


def _payload_available(args: argparse.Namespace, stdin_cache: list[str | None]) -> bool:
    if getattr(args, "payload", None) or getattr(args, "payload_file", None):
        return True
    return bool(_read_stdin(stdin_cache).strip())


def _read_stdin(stdin_cache: list[str | None]) -> str:
    if stdin_cache[0] is None:
        stdin_cache[0] = sys.stdin.read()
    return stdin_cache[0]


def _handle_codex_hook(registry: Registry, payload: dict[str, object]) -> int:
    event_name = str(payload.get("hook_event_name"))
    session_ref = str(payload.get("session_id"))
    cwd = payload.get("cwd")
    context = current_context(Path(str(cwd))) if cwd else current_context()

    if event_name == "SessionStart":
        registry.start_or_touch_session("codex", session_ref, context)
        return 0

    if event_name == "UserPromptSubmit":
        registry.touch_session_by_agent_ref("codex", session_ref)
        return 0

    if event_name == "Stop":
        session_id = registry.touch_session_by_agent_ref("codex", session_ref)
        last_assistant_message = payload.get("last_assistant_message")
        if isinstance(last_assistant_message, str) and last_assistant_message.strip():
            if registry.session_has_checkpoints(session_id):
                registry.add_checkpoint(
                    session_id,
                    "final",
                    CheckpointPayload(
                        summary=last_assistant_message.strip(),
                        completed=[],
                        blockers=[],
                        next_actions=[],
                    ),
                )
        registry.stop_session(session_id)
        print(json.dumps({"continue": True}))
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
