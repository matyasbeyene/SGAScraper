from __future__ import annotations

import json
import logging
from collections import defaultdict
from itertools import zip_longest

import httpx
from anthropic import Anthropic
from anthropic.types import TextBlock

from agentsenate.models import AnalysisBatch, InitiativeAnalysis, SourceCategory, SourceItem

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are writing a daily student-government brief.
Read the aggregated campus corpus as a whole. Do not score every item. Select only the strongest
campus-facing ideas, policy changes, service problems, funding decisions, and emerging student
needs for today's brief.

Reject jokes, unsupported rumors, personal attacks, routine administrative maintenance, duplicate
chatter, and content without a concrete issue or initiative.

Return JSON only with this shape:
{"initiatives":[{"external_id":"...","is_useful":true,"topic_tags":["..."],
"impact_classification":"Operational/Actionable|Symbolic|Internal Governance",
"target_stakeholder":"...","executive_summary":"two factual sentences maximum",
"actionability_score":1,"trend_alert_flag":false,"evidence":"short quote or precise fact",
"ranking_rationale":"one sentence"}]}

Return at most the requested number of initiatives. Use only external_id values from the corpus.
Do not invent facts or IDs. A trend flag requires at least three independent schools describing
substantially the same issue. Treat anonymous forum content as a signal requiring verification,
not as established fact.
"""

FORUM_SNIPPET = 280
BOARD_SNIPPET = 700
MAX_CORPUS_CHARS = 24_000
MAX_OUTPUT_TOKENS = 1_200
DEEPSEEK_MAX_OUTPUT_TOKENS = 4_000
MAX_COST_USD = 0.20
INPUT_USD_PER_MTOK = 3.0
OUTPUT_USD_PER_MTOK = 15.0
CHARS_PER_TOKEN = 4


class CostCapExceeded(RuntimeError):
    """Raised when a Claude call would exceed the configured token budget."""


class ClaudeAnalyzer:
    def __init__(
        self,
        api_key: str,
        model: str,
        max_topics: int = 8,
        max_cost_usd: float = MAX_COST_USD,
    ) -> None:
        if not api_key:
            raise RuntimeError("Anthropic API key is required")
        self.client = Anthropic(api_key=api_key)
        self.model = model
        self.max_topics = max_topics
        self.max_cost_usd = max_cost_usd
        self.spent_usd = 0.0
        self.api_calls = 0

    def analyze(self, items: list[SourceItem]) -> list[InitiativeAnalysis]:
        if not items:
            return []
        if self.api_calls:
            raise CostCapExceeded("Claude already wrote the brief; refusing another API call")
        corpus = compact_items(items)
        user_content = _brief_prompt(self.max_topics, corpus)
        while len(corpus) > 1 and _prompt_cost(user_content) > self.max_cost_usd:
            corpus = corpus[: max(1, len(corpus) * 3 // 4)]
            user_content = _brief_prompt(self.max_topics, corpus)
            logger.warning(
                "Shrunk corpus to %s items to stay under $%.2f",
                len(corpus),
                self.max_cost_usd,
            )
        estimated = _prompt_cost(user_content)
        if estimated > self.max_cost_usd:
            raise CostCapExceeded(
                f"One Claude brief would cost about ${estimated:.4f}, "
                f"over the ${self.max_cost_usd:.2f} cap"
            )
        logger.info(
            "Aggregated %s/%s items; one Claude brief, estimated $%.4f",
            len(corpus),
            len(items),
            estimated,
        )
        message = self.client.messages.create(
            model=self.model,
            max_tokens=MAX_OUTPUT_TOKENS,
            temperature=0,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        self.api_calls += 1
        actual = _usage_cost(message)
        self.spent_usd += actual
        logger.info("Claude brief spent $%.4f (cap $%.2f)", actual, self.max_cost_usd)
        if self.spent_usd > self.max_cost_usd:
            logger.warning(
                "Claude call exceeded the $%.2f cap; no further calls will be made",
                self.max_cost_usd,
            )
        text = "".join(block.text for block in message.content if isinstance(block, TextBlock))
        parsed = AnalysisBatch.model_validate_json(_strip_code_fence(text))
        known = {item.external_id for item in items}
        selected = [
            analysis
            for analysis in parsed.initiatives
            if analysis.external_id in known and analysis.is_useful
        ]
        dropped = len(parsed.initiatives) - len(selected)
        if dropped:
            logger.warning("Dropped %s Claude initiatives with unknown IDs or not useful", dropped)
        return selected[: self.max_topics]


def fallback_analyses(items: list[SourceItem], max_topics: int) -> list[InitiativeAnalysis]:
    scored = sorted(
        items,
        key=lambda item: (_fallback_score(item), item.published_at or item.observed_at),
        reverse=True,
    )
    analyses: list[InitiativeAnalysis] = []
    for item in scored:
        score = _fallback_score(item)
        if score < 3:
            continue
        analyses.append(
            InitiativeAnalysis(
                external_id=item.external_id,
                is_useful=True,
                topic_tags=_fallback_tags(item),
                impact_classification="Operational/Actionable",
                target_stakeholder="Relevant campus administrator",
                executive_summary=_fallback_summary(item),
                actionability_score=score,
                evidence=_snippet(item.raw_text or item.title, 180),
                ranking_rationale="Selected by fallback keyword scoring after model JSON failed.",
            )
        )
        if len(analyses) >= max_topics:
            break
    return analyses


def _fallback_score(item: SourceItem) -> int:
    text = f"{item.title} {item.raw_text}".lower()
    strong = ("parking", "dining", "housing", "safety", "bus", "tuition", "fee")
    useful = ("library", "wifi", "registration", "advising", "class", "meal", "transit")
    score = 1
    if any(word in text for word in strong):
        score += 3
    if any(word in text for word in useful):
        score += 2
    if "?" in item.title or "why" in text or "how" in text:
        score += 1
    return max(1, min(5, score))


def _fallback_tags(item: SourceItem) -> list[str]:
    text = f"{item.title} {item.raw_text}".lower()
    tags = []
    for tag, words in (
        ("Parking/Transit", ("parking", "bus", "transit")),
        ("Dining", ("dining", "meal", "food")),
        ("Housing", ("housing", "dorm", "apartment")),
        ("Academic Services", ("library", "registration", "advising", "class")),
        ("Safety", ("safety", "police", "security")),
        ("Student Costs", ("tuition", "fee", "fees", "cost")),
    ):
        if any(word in text for word in words):
            tags.append(tag)
    return tags or ["Student Sentiment"]


def _fallback_summary(item: SourceItem) -> str:
    school = item.university_name or "The monitored campus"
    body = _snippet(item.raw_text, 140) if item.raw_text else item.title
    return f"{school} discussion flagged a possible student-government issue: {body}"


class DeepSeekAnalyzer:
    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-flash",
        base_url: str = "https://api.deepseek.com",
        max_topics: int = 8,
        max_cost_usd: float = 0.05,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise RuntimeError("DeepSeek API key is required")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.max_topics = max_topics
        self.max_cost_usd = max_cost_usd
        self.client = client or httpx.Client(timeout=60)
        self.spent_usd = 0.0
        self.api_calls = 0

    def analyze(self, items: list[SourceItem]) -> list[InitiativeAnalysis]:
        if not items:
            return []
        if self.api_calls:
            raise CostCapExceeded("DeepSeek already wrote the brief; refusing another API call")
        corpus = compact_items(items)
        user_content = _brief_prompt(self.max_topics, corpus)
        while len(corpus) > 1 and _deepseek_prompt_cost(user_content) > self.max_cost_usd:
            corpus = corpus[: max(1, len(corpus) * 3 // 4)]
            user_content = _brief_prompt(self.max_topics, corpus)
            logger.warning(
                "Shrunk corpus to %s items to stay under $%.2f",
                len(corpus),
                self.max_cost_usd,
            )
        estimated = _deepseek_prompt_cost(user_content)
        if estimated > self.max_cost_usd:
            raise CostCapExceeded(
                f"One DeepSeek brief would cost about ${estimated:.4f}, "
                f"over the ${self.max_cost_usd:.2f} cap"
            )
        logger.info(
            "Aggregated %s/%s items; one DeepSeek brief, estimated $%.4f",
            len(corpus),
            len(items),
            estimated,
        )
        response = self.client.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                "thinking": {"type": "disabled"},
                "reasoning_effort": "none",
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "max_tokens": DEEPSEEK_MAX_OUTPUT_TOKENS,
            },
        )
        response.raise_for_status()
        self.api_calls += 1
        payload = response.json()
        actual = _deepseek_usage_cost(payload)
        self.spent_usd += actual
        logger.info("DeepSeek brief spent about $%.4f (cap $%.2f)", actual, self.max_cost_usd)
        text = str(payload["choices"][0]["message"].get("content") or "")
        if not text.strip():
            raise RuntimeError("DeepSeek returned an empty brief; leaving items pending for retry")
        try:
            parsed = AnalysisBatch.model_validate_json(_strip_code_fence(text))
        except Exception as exc:
            logger.warning("DeepSeek returned invalid JSON; using fallback analysis: %s", exc)
            return fallback_analyses(items, self.max_topics)
        known = {item.external_id for item in items}
        selected = [
            analysis
            for analysis in parsed.initiatives
            if analysis.external_id in known and analysis.is_useful
        ]
        dropped = len(parsed.initiatives) - len(selected)
        if dropped:
            logger.warning(
                "Dropped %s DeepSeek initiatives with unknown IDs or not useful", dropped
            )
        return selected[: self.max_topics]


def compact_items(
    items: list[SourceItem], max_chars: int = MAX_CORPUS_CHARS
) -> list[dict[str, str | None]]:
    board_items = [
        item for item in items if item.source_category == SourceCategory.BOARD_OF_REGENTS
    ]
    by_school: dict[str, list[SourceItem]] = defaultdict(list)
    for item in items:
        if item.source_category != SourceCategory.BOARD_OF_REGENTS:
            by_school[item.university_name or "Unknown"].append(item)
    # Interleave schools so the input budget cannot be exhausted by the first campus.
    ordered = board_items + [
        item for row in zip_longest(*by_school.values()) for item in row if item is not None
    ]
    corpus: list[dict[str, str | None]] = []
    used = 2
    for item in ordered:
        snippet_limit = (
            BOARD_SNIPPET
            if item.source_category == SourceCategory.BOARD_OF_REGENTS
            else FORUM_SNIPPET
        )
        row = {
            "external_id": item.external_id,
            "school": item.university_name,
            "source": item.source,
            "title": item.title[:240],
            "text": _snippet(item.raw_text, snippet_limit),
            "url": str(item.source_url),
            "published_at": item.published_at.isoformat() if item.published_at else None,
        }
        encoded = json.dumps(row, ensure_ascii=False)
        extra = len(encoded) + (1 if corpus else 0)
        if used + extra > max_chars:
            logger.warning(
                "Corpus truncated at %s/%s items to stay within one model request",
                len(corpus),
                len(items),
            )
            break
        corpus.append(row)
        used += extra
    return corpus


def estimate_cost_usd(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens / 1_000_000) * INPUT_USD_PER_MTOK + (
        output_tokens / 1_000_000
    ) * OUTPUT_USD_PER_MTOK


def estimate_deepseek_flash_cost_usd(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens / 1_000_000) * 0.30 + (output_tokens / 1_000_000) * 1.20


def _brief_prompt(max_topics: int, corpus: list[dict[str, str | None]]) -> str:
    return (
        f"Select up to {max_topics} initiatives for today's brief "
        f"from this aggregated corpus of {len(corpus)} items:\n"
        + json.dumps(corpus, ensure_ascii=False)
    )


def _prompt_cost(user_content: str) -> float:
    input_tokens = _estimate_tokens(SYSTEM_PROMPT) + _estimate_tokens(user_content)
    return estimate_cost_usd(input_tokens, MAX_OUTPUT_TOKENS)


def _deepseek_prompt_cost(user_content: str) -> float:
    input_tokens = _estimate_tokens(SYSTEM_PROMPT) + _estimate_tokens(user_content)
    return estimate_deepseek_flash_cost_usd(input_tokens, DEEPSEEK_MAX_OUTPUT_TOKENS)


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


def _usage_cost(message: object) -> float:
    usage = getattr(message, "usage", None)
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    if not input_tokens and not output_tokens:
        return 0.0
    return estimate_cost_usd(input_tokens, output_tokens)


def _deepseek_usage_cost(payload: dict[str, object]) -> float:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return 0.0
    input_tokens = int(usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or 0)
    if not input_tokens and not output_tokens:
        return 0.0
    return estimate_deepseek_flash_cost_usd(input_tokens, output_tokens)


def _snippet(text: str, limit: int) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        stripped = stripped[first_newline + 1 :]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
    return stripped.strip()
