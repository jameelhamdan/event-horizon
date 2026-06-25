"""
Topic matchers.

TopicMatcher   — keyword-overlap matching (no LLM, fast, used for retroactive tagging).
LLMTopicMatcher — LLM-based batch matching (semantic, used for regular tagging pipeline).
"""
import json
import logging
import re

logger = logging.getLogger(__name__)

_SPLIT_RE = re.compile(r'[^a-zA-Z0-9]+')

_STOP = frozenset({
    'the', 'a', 'an', 'in', 'on', 'at', 'to', 'of', 'for', 'and', 'or',
    'but', 'is', 'are', 'was', 'were', 'be', 'been', 'has', 'have', 'had',
    'its', 'that', 'this', 'with', 'by', 'from', 'as', 'after', 'amid',
    'over', 'into', 'also', 'not', 'new', 'says', 'said', 'two', 'three',
})

# Minimum fraction of topic keywords that must match for a tag to apply
_MIN_OVERLAP = 0.1
# Minimum absolute keyword matches (whichever is higher wins)
_MIN_MATCHES = 1


def _tokenize(text: str) -> set[str]:
    return {
        t.lower() for t in _SPLIT_RE.split(text or '')
        if len(t) > 2 and t.lower() not in _STOP
    }


class TopicMatcher:

    def match(self, event, topics: list) -> dict[str, float]:
        """
        Match event against topics.

        Args:
            event: Event model instance (uses .title and .location_name)
            topics: list of Topic model instances

        Returns:
            dict mapping slug → confidence score (0.0–1.0) for matched topics
        """
        event_tokens = _tokenize(event.title or '') | _tokenize(event.location_name or '')
        if not event_tokens:
            return {}

        result: dict[str, float] = {}
        for topic in topics:
            kw_tokens: set[str] = set()
            for kw in (topic.keywords or []):
                kw_tokens |= _tokenize(kw)
            kw_tokens |= _tokenize(topic.name)

            if not kw_tokens:
                continue

            overlap = event_tokens & kw_tokens
            n = len(overlap)
            if n < _MIN_MATCHES:
                continue

            frac = n / len(kw_tokens)
            if frac < _MIN_OVERLAP:
                continue

            score = round(min(1.0, 0.3 + frac), 3)
            result[topic.slug] = score

        return result


class LLMTopicMatcher:
    """
    LLM-based batch topic matcher.

    Sends events to the LLM in batches and returns matched topic slugs with
    confidence scores. Falls back to TopicMatcher per-event on LLM error.
    """

    BATCH_SIZE = 10

    def match_batch(
        self,
        events: list,
        topics: list,
    ) -> tuple[dict[str, dict[str, float]], dict[str, str]]:
        """
        Match a list of Event objects against a list of Topic objects.

        Returns a ``(results, sources)`` tuple:
          - ``results``: {str(event.pk): {topic_slug: confidence}} for all events.
          - ``sources``: {str(event.pk): 'llm' | 'keyword'} — which matcher produced
            the result. ``'keyword'`` marks events tagged by the fallback (LLM was
            unavailable); the caller re-evaluates those on a later run.

        Falls back to TopicMatcher per-event on LLM error.
        """
        from services.llm import get_llm_service

        results: dict[str, dict[str, float]] = {str(e.pk): {} for e in events}
        # Default 'keyword' = not confidently LLM-tagged (covers pre-filtered events
        # that never reach the LLM); flipped to 'llm' below when the LLM succeeds.
        sources: dict[str, str] = {str(e.pk): 'keyword' for e in events}

        # Pre-filter with the free keyword matcher: only escalate events that have
        # at least one keyword candidate to the LLM. Events with zero keyword
        # overlap almost never match semantically, so this skips a large fraction
        # of LLM calls at no quality cost. Non-candidates keep their {} result.
        keyword = TopicMatcher()
        candidates = [e for e in events if keyword.match(e, topics)]
        skipped = len(events) - len(candidates)
        if skipped:
            logger.info(
                '[topics] pre-filter: %d/%d events have keyword candidates (%d skipped)',
                len(candidates), len(events), skipped,
            )
        if not candidates:
            return results, sources

        # Build prompt fragments shared across all batches
        situation_lines = '\n'.join(
            f'- {t.slug}: {t.name}'
            + (f' — {t.description[:120]}' if getattr(t, 'description', '') else '')
            for t in topics
        )
        valid_slugs = {t.slug for t in topics}

        llm = get_llm_service('topics')
        total_batches = (len(candidates) + self.BATCH_SIZE - 1) // self.BATCH_SIZE

        for batch_start in range(0, len(candidates), self.BATCH_SIZE):
            batch = candidates[batch_start: batch_start + self.BATCH_SIZE]
            batch_num = batch_start // self.BATCH_SIZE + 1
            logger.info('[topics] LLM batch %d/%d (%d events)', batch_num, total_batches, len(batch))

            event_lines = '\n'.join(
                f'{i + 1}. (id={e.pk}) {e.title or "(no title)"}'
                f' ({e.location_name or "unknown"} | {e.category or "general"})'
                for i, e in enumerate(batch)
            )

            prompt = (
                'You are a news analyst. Match each news event to the relevant ongoing situations.\n\n'
                f'ONGOING SITUATIONS:\n{situation_lines}\n\n'
                f'EVENTS:\n{event_lines}\n\n'
                'Return a JSON object where each key is the event id value shown as id=<value>,\n'
                'and each value is a dict of matched situation slugs with confidence 0.5–1.0.\n'
                'Only include matches with confidence ≥ 0.5. Use empty object {} if no match.\n'
                'Example: {"abc123": {"russo-ukrainian-war": 0.95}, "def456": {}}\n'
                'Respond with only the JSON object, no other text.'
            )

            try:
                response = llm.chat(
                    [{'role': 'user', 'content': prompt}],
                    temperature=0,
                    max_tokens=min(1800, 80 * len(batch) + 200),
                ).strip()
                # Strip markdown code fences if present
                response = re.sub(r'^```(?:json)?\s*', '', response)
                response = re.sub(r'\s*```$', '', response)
                batch_result = json.loads(response)
                if not isinstance(batch_result, dict):
                    raise ValueError('LLM returned non-dict')

                for event in batch:
                    event_key = str(event.pk)
                    raw = batch_result.get(event_key) or {}
                    if not isinstance(raw, dict):
                        raw = {}
                    # Filter to valid slugs and clamp confidence to [0.5, 1.0]
                    cleaned = {
                        slug: round(min(1.0, max(0.5, float(conf))), 3)
                        for slug, conf in raw.items()
                        if slug in valid_slugs
                    }
                    results[event_key] = cleaned
                    sources[event_key] = 'llm'
                    if cleaned:
                        logger.info(
                            '[topics] LLM tagged "%s" → %s',
                            (event.title or '')[:60],
                            ', '.join(f'{s}({c:.2f})' for s, c in cleaned.items()),
                        )
                    else:
                        logger.debug('[topics] LLM: no match for "%s"', (event.title or '')[:60])

            except Exception as exc:
                logger.warning(
                    '[topics] LLM batch %d/%d failed (%s) — falling back to TopicMatcher',
                    batch_num, total_batches, exc,
                )
                for event in batch:
                    results[str(event.pk)] = keyword.match(event, topics)
                    # source stays 'keyword' (default) — flagged for LLM retry later

        return results, sources
