from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path

from .codex_import import importable_codex_sessions
from .codex_integration import install_codex_hooks
from .config import resolve_paths
from .context import current_context
from .models import CheckpointPayload
from .storage import Registry
from .text import condense_message, derive_title_from_prompt, is_clear_task_prompt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="asm",
        usage="asm <command> [options]",
        description=(
            "Agent Session Manager for Codex.\n"
            "Record sessions, attach progress summaries, and recover the right session to resume."
        ),
        epilog=(
            "Most used commands:\n"
            "  asm init\n"
            "  asm ls --project my-project --long\n"
            "  asm current --explain\n"
            "  asm checkpoint-current --payload-file checkpoint.json\n"
            "  asm finalize-current --payload-file final.json\n"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True, title="commands")

    init_parser = subparsers.add_parser(
        "init",
        help="initialize ~/.asm and install Codex hooks/prompts",
        description="Initialize ASM storage and install Codex hook/prompt integration.",
    )
    init_parser.add_argument("--force", action="store_true")

    start_parser = subparsers.add_parser(
        "start",
        help="create or refresh a session record",
        description="Create or refresh a session record for an agent session reference.",
    )
    start_parser.add_argument("--agent", required=True)
    start_parser.add_argument("--agent-session-ref", required=True)

    checkpoint_parser = subparsers.add_parser(
        "checkpoint",
        help="write a structured checkpoint to a specific session",
        description="Write a structured progress checkpoint to a specific ASM session.",
    )
    checkpoint_parser.add_argument("--session", required=True)
    checkpoint_parser.add_argument("--payload", help="inline JSON payload")
    checkpoint_parser.add_argument("--payload-file", help="path to a JSON payload file")

    checkpoint_current_parser = subparsers.add_parser(
        "checkpoint-current",
        help="write a checkpoint to the best-matching current session",
        description="Write a checkpoint to the session that best matches the current workspace.",
    )
    checkpoint_current_parser.add_argument("--payload", help="inline JSON payload")
    checkpoint_current_parser.add_argument("--payload-file", help="path to a JSON payload file")

    stop_parser = subparsers.add_parser(
        "stop",
        help="stop a specific ASM session without writing a final agent checkpoint",
        description="Stop a specific ASM session without writing a final agent-authored checkpoint.",
    )
    stop_parser.add_argument("--session", required=True)

    stop_current_parser = subparsers.add_parser(
        "stop-current",
        help="stop the best-matching current session",
        description="Stop the ASM session that best matches the current workspace without writing a final agent-authored checkpoint.",
    )

    finalize_parser = subparsers.add_parser(
        "finalize",
        help="write a final agent checkpoint and stop a specific session",
        description="Write a final agent-authored checkpoint into ASM and stop a specific ASM session. This does not exit the agent session.",
    )
    finalize_parser.add_argument("--session", required=True)
    finalize_parser.add_argument("--payload", help="inline JSON payload")
    finalize_parser.add_argument("--payload-file", help="path to a JSON payload file")

    finalize_current_parser = subparsers.add_parser(
        "finalize-current",
        help="write a final agent checkpoint and stop the best-matching current session",
        description="Write a final agent-authored checkpoint into ASM and stop the best-matching ASM session. This does not exit the agent session.",
    )
    finalize_current_parser.add_argument("--payload", help="inline JSON payload")
    finalize_current_parser.add_argument("--payload-file", help="path to a JSON payload file")

    ls_parser = subparsers.add_parser(
        "ls",
        help="list recorded sessions",
        description="List recorded sessions with optional filters.",
    )
    ls_parser.add_argument("--limit", type=int, default=20, help="maximum number of sessions to show")
    ls_parser.add_argument("--agent", help="filter by agent name")
    ls_parser.add_argument("--status", choices=["active", "stopped"], help="filter by session status")
    ls_parser.add_argument("--branch", help="filter by git branch")
    ls_parser.add_argument("--project", help="filter by project name")
    ls_parser.add_argument(
        "--scope",
        choices=["project", "path"],
        default="project",
        help="show scope as project name or full path",
    )
    ls_parser.add_argument("--long", action="store_true", dest="long_output", help="show goal details")

    search_parser = subparsers.add_parser(
        "search",
        help="search recorded sessions by keyword",
        description="Search recorded sessions across task hints, summaries, project, branch, and path.",
    )
    search_parser.add_argument("query", help="free-text search query")
    search_parser.add_argument("--limit", type=int, default=20, help="maximum number of sessions to show")
    search_parser.add_argument("--agent", help="filter by agent name")
    search_parser.add_argument("--status", choices=["active", "stopped"], help="filter by session status")
    search_parser.add_argument("--branch", help="filter by git branch")
    search_parser.add_argument("--project", help="filter by project name")
    search_parser.add_argument(
        "--scope",
        choices=["project", "path"],
        default="project",
        help="show scope as project name or full path",
    )
    search_parser.add_argument("--long", action="store_true", dest="long_output", help="show goal details")
    search_parser.add_argument("--explain", action="store_true", help="show matched fields")

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="recommend sessions for the current workspace",
        description="Recommend the most relevant sessions for the current workspace.",
    )
    doctor_parser.add_argument("--limit", type=int, default=5, help="maximum number of recommendations to show")
    doctor_parser.add_argument("--explain", action="store_true", help="show scoring breakdown")
    doctor_parser.add_argument(
        "--scope",
        choices=["project", "path"],
        default="project",
        help="show scope as project name or full path",
    )

    current_parser = subparsers.add_parser(
        "current",
        help="show the single best-matching session",
        description="Show the single best-matching session for the current workspace.",
    )
    current_parser.add_argument("--explain", action="store_true", help="show scoring breakdown")
    current_parser.add_argument(
        "--scope",
        choices=["project", "path"],
        default="project",
        help="show scope as project name or full path",
    )
    subparsers.add_parser(
        "checkpoint-template",
        help="print an empty checkpoint JSON template",
        description="Print an empty checkpoint JSON template.",
    )
    subparsers.add_parser(
        "checkpoint-prompt",
        help="print a Codex-ready prompt for checkpoint JSON",
        description="Print a Codex-ready prompt that asks for checkpoint JSON only.",
    )

    import_codex_parser = subparsers.add_parser(
        "import-codex",
        help="import Codex sessions from ~/.codex/sessions",
        description="Scan Codex transcript files and backfill ASM sessions with conservative metadata only.",
    )
    import_codex_parser.add_argument(
        "--codex-home",
        help="override Codex home directory; defaults to CODEX_HOME or ~/.codex",
    )
    import_codex_parser.add_argument(
        "--limit",
        type=int,
        help="import only the most recent N transcript files",
    )

    subparsers.add_parser(
        "codex-hook",
        help="internal entrypoint for Codex hooks",
        description="Internal command used by installed Codex hooks.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv or argv == ["-h"] or argv == ["--help"]:
        print(_top_level_help())
        return 0

    parser = build_parser()
    args = parser.parse_args(argv)
    paths = resolve_paths()
    registry = Registry(paths.registry_db)
    stdin_cache: list[str | None] = [None]

    try:
        if args.command in {
            "ls",
            "search",
            "doctor",
            "current",
            "checkpoint-current",
            "finalize-current",
            "stop-current",
        }:
            registry.initialize()
            _auto_sync_codex_sessions(registry, paths)

        if args.command == "checkpoint-template":
            print(
                json.dumps(
                    {
                        "title": "",
                        "goal": "",
                        "summary": "",
                        "source": "agent",
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
                                "source": "agent",
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

        if args.command == "import-codex":
            registry.initialize()
            codex_home = Path(args.codex_home).expanduser() if args.codex_home else paths.codex_home
            created, updated, imported_count = _sync_codex_sessions(
                registry,
                codex_home=codex_home,
                limit=args.limit,
                force=True,
            )
            print(
                json.dumps(
                    {
                        "imported": imported_count,
                        "created": created,
                        "updated": updated,
                        "codex_home": str(codex_home),
                    },
                    ensure_ascii=False,
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
            registry.add_checkpoint(args.session, "progress", payload, source="agent")
            print(args.session)
            return 0

        if args.command == "checkpoint-current":
            matches = registry.doctor(current_context(), limit=1)
            if not matches:
                raise ValueError("No relevant sessions found.")
            payload = _load_payload_from_args(args, stdin_cache)
            session_id = matches[0].session_id
            registry.add_checkpoint(session_id, "progress", payload, source="agent")
            print(session_id)
            return 0

        if args.command == "finalize":
            payload = _load_payload_from_args(args, stdin_cache)
            registry.add_checkpoint(args.session, "final", payload, source="agent")
            registry.stop_session(args.session)
            print(args.session)
            return 0

        if args.command == "finalize-current":
            matches = registry.doctor(current_context(), limit=1)
            if not matches:
                raise ValueError("No relevant sessions found.")
            payload = _load_payload_from_args(args, stdin_cache)
            session_id = matches[0].session_id
            registry.add_checkpoint(session_id, "final", payload, source="agent")
            registry.stop_session(session_id)
            print(session_id)
            return 0

        if args.command == "stop":
            registry.stop_session(args.session)
            print(args.session)
            return 0

        if args.command == "stop-current":
            matches = registry.doctor(current_context(), limit=1)
            if not matches:
                raise ValueError("No relevant sessions found.")
            session_id = matches[0].session_id
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
                _print_session_card(
                    session_id=str(row["id"]),
                    agent=row["agent"],
                    title=row["title"],
                    initial_prompt=row["initial_prompt"],
                    project=row["project"],
                    path=row["path"],
                    branch=row["branch"],
                    status=row["status"],
                    resume_command=row["resume_command"],
                    updated_at=row["updated_at"],
                    goal=row["goal"] if args.long_output else None,
                    last=row["latest_summary"],
                    last_source=row["latest_summary_source"],
                    scope_mode=args.scope,
                )
            return 0

        if args.command == "search":
            rows = registry.search_sessions(
                args.query,
                limit=args.limit,
                agent=args.agent,
                status=args.status,
                branch=args.branch,
                project=args.project,
            )
            for row in rows:
                reasons = _search_match_reasons(args.query, row) if args.explain else None
                _print_session_card(
                    session_id=str(row["id"]),
                    agent=row["agent"],
                    title=row["title"],
                    initial_prompt=row["initial_prompt"],
                    project=row["project"],
                    path=row["path"],
                    branch=row["branch"],
                    status=row["status"],
                    resume_command=row["resume_command"],
                    updated_at=row["updated_at"],
                    goal=row["goal"] if args.long_output else None,
                    last=row["latest_summary"],
                    last_source=row["latest_summary_source"],
                    reasons=reasons,
                    scope_mode=args.scope,
                )
            return 0

        if args.command == "doctor":
            matches = registry.doctor(current_context(), limit=args.limit)
            if not matches:
                print("No relevant sessions found.")
                return 0
            for match in matches:
                _print_session_card(
                    session_id=match.session_id,
                    agent="codex",
                    title=match.title,
                    initial_prompt=match.initial_prompt,
                    project=match.project,
                    path=match.path,
                    branch=match.branch,
                    status=match.status,
                    resume_command=match.resume_command,
                    score=match.score,
                    last=match.latest_summary,
                    last_source=match.latest_summary_source,
                    reason_scores=match.reason_scores if args.explain else None,
                    reasons=match.reasons if not args.explain else None,
                    scope_mode=args.scope,
                )
            return 0

        if args.command == "current":
            matches = registry.doctor(current_context(), limit=1)
            if not matches:
                print("No relevant sessions found.")
                return 0
            match = matches[0]
            _print_session_card(
                session_id=match.session_id,
                agent="codex",
                title=match.title,
                initial_prompt=match.initial_prompt,
                project=match.project,
                path=match.path,
                branch=match.branch,
                status=match.status,
                resume_command=match.resume_command,
                score=match.score,
                last=match.latest_summary,
                last_source=match.latest_summary_source,
                reason_scores=match.reason_scores if args.explain else None,
                reasons=match.reasons if not args.explain else None,
                scope_mode=args.scope,
            )
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


def _condense_message(message: str, limit: int = 100) -> str:
    return condense_message(message, limit=limit)


def _is_clear_task_prompt(prompt: str) -> bool:
    return is_clear_task_prompt(prompt)


def _derive_title_from_prompt(prompt: str) -> str:
    return derive_title_from_prompt(prompt)


def _clarification_message() -> str:
    return "在继续之前，请先用一句话说明这次要处理的具体任务目标。"


def _top_level_help() -> str:
    return "\n".join(
        [
            "usage: asm <command> [options]",
            "",
            "Agent Session Manager for Codex.",
            "Record sessions, attach progress summaries, and recover the right session to resume.",
            "",
            "setup",
            "  init                 initialize ~/.asm and install Codex hooks/prompts",
            "",
            "capture",
            "  start                create or refresh a session record",
            "  checkpoint           write a structured checkpoint to a specific session",
            "  checkpoint-current   write a checkpoint to the best-matching current session",
            "  finalize             write a final agent checkpoint into ASM and stop a specific ASM session",
            "  finalize-current     write a final agent checkpoint into ASM and stop the best-matching ASM session",
            "  stop                 stop a specific ASM session without writing a final agent checkpoint",
            "  stop-current         stop the best-matching ASM session without writing a final agent checkpoint",
            "",
            "find",
            "  ls                   list recorded sessions",
            "  search               search recorded sessions by keyword",
            "  current              show the single best-matching session",
            "  doctor               recommend sessions for the current workspace",
            "  import-codex         import Codex transcript metadata from ~/.codex/sessions",
            "",
            "codex integration",
            "  checkpoint-template  print an empty checkpoint JSON template",
            "  checkpoint-prompt    print a Codex-ready prompt for checkpoint JSON",
            "",
            "internal",
            "  codex-hook           internal entrypoint used by installed Codex hooks",
            "",
            "most used commands",
            "  asm init",
            "  asm ls --project my-project --long",
            "  asm current --explain",
            "  asm checkpoint-current --payload-file checkpoint.json",
            "  asm finalize-current --payload-file final.json",
            "  asm import-codex --limit 50",
            "",
            "notes",
            "  asm-final            Codex prompt for end-of-session JSON only; does not exit the agent session",
            "  /quit                exits the agent session; ASM may still retain base session metadata for resume",
        ]
    )


def _print_session_card(
    *,
    session_id: str,
    agent: str | None = None,
    title: str | None,
    initial_prompt: str | None = None,
    project: str | None,
    path: str | None = None,
    branch: str | None,
    status: str,
    resume_command: str,
    updated_at: str | None = None,
    score: int | None = None,
    goal: str | None = None,
    last: str | None = None,
    last_source: str | None = None,
    reasons: list[str] | None = None,
    reason_scores: list[tuple[int, str]] | None = None,
    scope_mode: str = "project",
) -> None:
    display_title = _display_title(title, initial_prompt)
    display_goal = _display_goal(goal)
    display_last = _display_last(last)
    heading = display_title or "[untitled]"
    print(f"[{session_id}] {heading}")
    if agent and score is not None:
        _print_wrapped_field("agent", agent)
    scope_root = path if scope_mode == "path" else project
    scope = f"{scope_root or '-'} @ {branch or '-'}"
    _print_wrapped_field("scope", scope)
    meta_parts = [status]
    if score is not None:
        meta_parts.append(f"score {score}")
    if updated_at is not None:
        meta_parts.append(_format_timestamp(updated_at))
    _print_wrapped_field("state", " | ".join(meta_parts))
    _print_wrapped_field("resume", resume_command)
    if display_goal:
        _print_wrapped_field("goal", display_goal, width=88, max_lines=1)
    if display_last:
        _print_wrapped_field("last", display_last, width=88, max_lines=2)
    if last_source:
        _print_wrapped_field("from", last_source)
    if reason_scores:
        print("  why:")
        for value, label in reason_scores:
            print(f"    +{value} {label}")
    elif reasons:
        _print_wrapped_field("why", ", ".join(reasons))
    print()


NOISY_TITLE_PREFIXES = (
    "问题本质",
    "我做的修正",
    "改动文件",
    "验证结果",
    "你现在可以直接重新试",
    "我建议按这个顺序收敛",
)

NOISY_TITLE_PHRASES = (
    "改动文件：",
    "验证结果：",
    "你现在可以直接重新试：",
    "我建议按这个顺序收敛：",
    "agent codex scope",
    "[asm_",
)


def _is_noisy_display_text(value: str | None) -> bool:
    if not value:
        return False
    normalized = " ".join(value.strip().split())
    if not normalized:
        return False
    if any(normalized.startswith(prefix) for prefix in NOISY_TITLE_PREFIXES):
        return True
    if any(phrase in normalized for phrase in NOISY_TITLE_PHRASES):
        return True
    return False


def _display_title(title: str | None, initial_prompt: str | None) -> str | None:
    if title and not _is_noisy_display_text(title):
        return title
    hint = _condense_message(initial_prompt or "", limit=60)
    if hint:
        return hint
    return None


def _display_goal(goal: str | None) -> str | None:
    if goal and not _is_noisy_display_text(goal):
        return goal
    return None


def _display_last(last: str | None) -> str | None:
    if last and not _is_noisy_display_text(last):
        return last
    return None


def _search_match_reasons(query: str, row: object) -> list[str]:
    normalized_query = query.strip().lower()
    if not normalized_query:
        return []
    fields = [
        ("matched title", row["title"]),
        ("matched initial_prompt", row["initial_prompt"]),
        ("matched goal", row["goal"]),
        ("matched latest_summary", row["latest_summary"]),
        ("matched project", row["project"]),
        ("matched branch", row["branch"]),
        ("matched path", row["path"] if "path" in row.keys() else None),
    ]
    reasons: list[str] = []
    for label, value in fields:
        if isinstance(value, str) and normalized_query in value.lower():
            reasons.append(label)
    return reasons


def _print_wrapped_field(label: str, value: str, *, width: int = 88, max_lines: int | None = None) -> None:
    wrapped = textwrap.wrap(value, width=width) or [value]
    if max_lines is not None and len(wrapped) > max_lines:
        wrapped = wrapped[:max_lines]
        wrapped[-1] = wrapped[-1].rstrip() + "..."
    first, *rest = wrapped
    print(f"  {label:<6} {first}")
    for line in rest:
        print(f"         {line}")


def _format_timestamp(timestamp: str) -> str:
    try:
        dt = datetime.fromisoformat(timestamp)
    except ValueError:
        return timestamp
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    local_dt = dt.astimezone()
    return local_dt.strftime("%Y-%m-%d %H:%M")


def _handle_codex_hook(registry: Registry, payload: dict[str, object]) -> int:
    event_name = str(payload.get("hook_event_name"))
    session_ref = str(payload.get("session_id"))
    cwd = payload.get("cwd")
    context = current_context(Path(str(cwd))) if cwd else current_context()

    if event_name == "SessionStart":
        registry.start_or_touch_session("codex", session_ref, context)
        return 0

    if event_name == "UserPromptSubmit":
        session_id = registry.touch_session_by_agent_ref("codex", session_ref)
        prompt = payload.get("prompt")
        if isinstance(prompt, str):
            row = registry.session_row_by_agent_ref("codex", session_ref)
            has_semantics = bool(row["title"] and row["goal"])
            if _is_clear_task_prompt(prompt):
                registry.update_session_semantics(
                    session_id,
                    title=_derive_title_from_prompt(prompt),
                    goal=_condense_message(prompt, limit=100),
                )
            elif not has_semantics and not bool(row["clarification_requested"]):
                registry.record_initial_prompt(session_id, _condense_message(prompt, limit=100))
                registry.mark_clarification_requested(session_id)
                print(json.dumps({"systemMessage": _clarification_message()}))
        return 0

    if event_name == "Stop":
        session_id = registry.touch_session_by_agent_ref("codex", session_ref)
        last_assistant_message = payload.get("last_assistant_message")
        if (
            isinstance(last_assistant_message, str)
            and last_assistant_message.strip()
            and not registry.session_has_final_checkpoint(session_id)
        ):
            summary = _condense_message(last_assistant_message.strip(), limit=100)
            if registry.session_has_checkpoints(session_id):
                checkpoint_payload = CheckpointPayload(
                    summary=summary,
                    completed=[],
                    blockers=[],
                    next_actions=[],
                )
            else:
                checkpoint_payload = CheckpointPayload(
                    summary=summary,
                    completed=[],
                    blockers=[],
                    next_actions=[],
                )
            registry.add_checkpoint(session_id, "final", checkpoint_payload, source="fallback")
        registry.stop_session(session_id)
        print(json.dumps({"continue": True}))
        return 0

    return 0


def _find_git_root(path: Path) -> str | None:
    current = path.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return str(candidate)
    return None


def _auto_sync_codex_sessions(registry: Registry, paths: object) -> None:
    try:
        _sync_codex_sessions(registry, codex_home=paths.codex_home, limit=200, force=False)
    except (OSError, ValueError):
        return


def _sync_codex_sessions(
    registry: Registry,
    *,
    codex_home: Path,
    limit: int | None,
    force: bool,
) -> tuple[int, int, int]:
    known_mtimes = None if force else registry.imported_codex_file_mtimes()
    imported = importable_codex_sessions(codex_home, limit=limit, known_mtimes=known_mtimes)
    created = 0
    updated = 0
    current_thread_id = os.environ.get("CODEX_THREAD_ID")

    for item in imported:
        imported_path = Path(item.path).expanduser()
        git_root = _find_git_root(imported_path)
        project = Path(git_root).name if git_root else imported_path.name
        branch = _find_git_branch(imported_path)
        status = "active" if current_thread_id and item.agent_session_ref == current_thread_id else "stopped"
        if status == "active":
            context = current_context()
            imported_path = Path(context.path)
            git_root = context.git_root
            project = context.project
            branch = context.branch
        _, action = registry.import_codex_session(
            agent_session_ref=item.agent_session_ref,
            path=str(imported_path),
            git_root=git_root,
            project=project,
            branch=branch,
            started_at=item.started_at,
            updated_at=item.updated_at,
            initial_prompt=item.initial_prompt,
            latest_summary=item.latest_summary,
            session_file_path=item.session_file_path,
            session_file_mtime_ns=item.session_file_mtime_ns,
            status=status,
        )
        if action == "created":
            created += 1
        else:
            updated += 1
    return created, updated, len(imported)


def _find_git_root(path: Path) -> str | None:
    current = path.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return str(candidate)
    return None


def _find_git_branch(path: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(path.resolve()),
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


if __name__ == "__main__":
    raise SystemExit(main())
