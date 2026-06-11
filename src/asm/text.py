from __future__ import annotations

import re


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


def split_summary_candidates(text: str) -> list[str]:
    parts = re.split(r"(?<=[。！？!?])|\n+", text)
    candidates: list[str] = []
    for part in parts:
        candidate = " ".join(part.strip().split())
        if candidate:
            candidates.append(candidate)
    return candidates


def score_summary_candidate(candidate: str) -> int:
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


def condense_message(message: str, limit: int = 100) -> str:
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
    sentence_candidates = split_summary_candidates(text)
    if sentence_candidates:
        best = max(
            sentence_candidates,
            key=lambda candidate: (score_summary_candidate(candidate), -len(candidate)),
        )
        if score_summary_candidate(best) > 0:
            text = best
    if len(text) <= limit:
        return text
    return text[:limit].rstrip()


def is_clear_task_prompt(prompt: str) -> bool:
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


def derive_title_from_prompt(prompt: str) -> str:
    return condense_message(prompt, limit=60) or "Untitled Session"
