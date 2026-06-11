from __future__ import annotations

import argparse
import json
import re
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path

from .codex_integration import install_codex_hooks
from .config import resolve_paths
from .context import current_context
from .models import CheckpointPayload
from .storage import Registry


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
    search_parser.add_argument("--long", action="store_true", dest="long_output", help="show goal details")
    search_parser.add_argument("--explain", action="store_true", help="show matched fields")

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="recommend sessions for the current workspace",
        description="Recommend the most relevant sessions for the current workspace.",
    )
    doctor_parser.add_argument("--limit", type=int, default=5, help="maximum number of recommendations to show")
    doctor_parser.add_argument("--explain", action="store_true", help="show scoring breakdown")

    current_parser = subparsers.add_parser(
        "current",
        help="show the single best-matching session",
        description="Show the single best-matching session for the current workspace.",
    )
    current_parser.add_argument("--explain", action="store_true", help="show scoring breakdown")
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
                    branch=row["branch"],
                    status=row["status"],
                    resume_command=row["resume_command"],
                    updated_at=row["updated_at"],
                    goal=row["goal"] if args.long_output else None,
                    last=row["latest_summary"],
                    last_source=row["latest_summary_source"],
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
                    branch=row["branch"],
                    status=row["status"],
                    resume_command=row["resume_command"],
                    updated_at=row["updated_at"],
                    goal=row["goal"] if args.long_output else None,
                    last=row["latest_summary"],
                    last_source=row["latest_summary_source"],
                    reasons=reasons,
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
                    branch=match.branch,
                    status=match.status,
                    resume_command=match.resume_command,
                    score=match.score,
                    last=match.latest_summary,
                    last_source=match.latest_summary_source,
                    reason_scores=match.reason_scores if args.explain else None,
                    reasons=match.reasons if not args.explain else None,
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
                branch=match.branch,
                status=match.status,
                resume_command=match.resume_command,
                score=match.score,
                last=match.latest_summary,
                last_source=match.latest_summary_source,
                reason_scores=match.reason_scores if args.explain else None,
                reasons=match.reasons if not args.explain else None,
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


SUMMARY_POSITIVE_MARKERS = (
    "已确认",
    "确认",
    "完成",
    "实现",
    "修复",
    "更新",
    "验证",
    "测试结果",
    "结论",
    "当前问题",
    "可用",
    "正常",
    "通过",
)

SUMMARY_PRIORITY_PREFIXES = (
    "已确认",
    "确认",
    "结论",
    "当前问题",
)

SUMMARY_NEGATIVE_MARKERS = (
    "如果你要",
    "如果你愿意",
    "我也可以",
    "可以继续",
    "下一步",
    "比如",
    "你觉得",
    "我会",
    "建议",
)


def _split_summary_candidates(text: str) -> list[str]:
    parts = re.split(r"(?<=[。！？!?])|\n+", text)
    candidates: list[str] = []
    for part in parts:
        candidate = " ".join(part.strip().split())
        if candidate:
            candidates.append(candidate)
    return candidates


def _score_summary_candidate(candidate: str) -> int:
    score = 0
    if len(candidate) <= 100:
        score += 5
    if any(candidate.startswith(prefix) for prefix in SUMMARY_PRIORITY_PREFIXES):
        score += 40
    if any(marker in candidate for marker in SUMMARY_POSITIVE_MARKERS):
        score += 30
    if "：" in candidate or ":" in candidate:
        score += 5
    if any(marker in candidate for marker in SUMMARY_NEGATIVE_MARKERS):
        score -= 20
    if candidate.endswith(("?", "？", "吗", "么", "呢")):
        score -= 20
    if candidate.startswith(("-", "*", "`")):
        score -= 20
    return score


def _condense_message(message: str, limit: int = 100) -> str:
    normalized = message.replace("\r", "\n").strip()
    if not normalized:
        return ""

    candidates: list[str] = []
    for raw_line in normalized.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(("-", "*", "`")):
            continue
        if "可以直接用任一种方式" in line:
            continue
        if "如果你愿意" in line:
            continue
        if "我会按这个方式" in line:
            continue
        candidates.append(line)

    if not candidates:
        candidates = [normalized.splitlines()[0].strip()]

    text = " ".join(candidates)
    text = " ".join(text.split())
    sentence_candidates = _split_summary_candidates(text)
    if sentence_candidates:
        best = max(sentence_candidates, key=lambda candidate: (_score_summary_candidate(candidate), -len(candidate)))
        if _score_summary_candidate(best) > 0:
            text = best
    if len(text) <= limit:
        return text
    truncated = text[:limit].rstrip()
    return truncated


AMBIGUOUS_PROMPTS = {
    "继续",
    "继续吧",
    "继续一下",
    "帮我看看",
    "看一下",
    "先看看",
    "review 一下",
    "review一下",
    "解释一下",
}

AMBIGUOUS_PHRASES = (
    "你觉得",
    "你认为",
    "我应该",
    "做什么",
    "怎么做",
    "怎么推进",
    "给我建议",
    "帮我想",
    "下一步",
    "从哪开始",
)

EXPLORATORY_PREFIXES = (
    "我想",
    "想先",
    "先想",
    "先看看",
    "试试",
    "我先试",
)

STRONG_TASK_MARKERS = (
    "请",
    "请你",
    "帮我",
    "麻烦",
    "麻烦你",
    "需要你",
    "让你",
)

TASK_VERBS = (
    "实现",
    "修复",
    "补上",
    "增加",
    "新增",
    "编写",
    "更新",
    "整理",
    "分析",
    "排查",
    "解释",
    "审查",
    "review",
    "测试",
    "优化",
    "重构",
)


def _is_clear_task_prompt(prompt: str) -> bool:
    normalized = " ".join(prompt.strip().split())
    if not normalized:
        return False
    if normalized in AMBIGUOUS_PROMPTS:
        return False
    if any(phrase in normalized for phrase in AMBIGUOUS_PHRASES):
        return False
    if normalized.endswith(("?", "？", "吗", "么", "呢")):
        return False
    short_ambiguous_prefixes = ("继续", "看看", "分析一下", "解释一下", "review")
    if any(normalized.startswith(prefix) for prefix in short_ambiguous_prefixes) and len(normalized) <= 12:
        return False
    has_task_verb = any(verb in normalized for verb in TASK_VERBS)
    if not has_task_verb or len(normalized) < 8:
        return False
    if any(normalized.startswith(prefix) for prefix in EXPLORATORY_PREFIXES):
        return any(marker in normalized for marker in STRONG_TASK_MARKERS)
    return True


def _derive_title_from_prompt(prompt: str) -> str:
    return _condense_message(prompt, limit=60) or "Untitled Session"


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
) -> None:
    display_title = _display_title(title, initial_prompt)
    display_goal = _display_goal(goal)
    display_last = _display_last(last)
    heading = display_title or "[untitled]"
    print(f"[{session_id}] {heading}")
    if agent and score is not None:
        _print_wrapped_field("agent", agent)
    scope = f"{project or '-'} @ {branch or '-'}"
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


if __name__ == "__main__":
    raise SystemExit(main())
