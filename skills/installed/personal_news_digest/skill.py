import json
import sys
from pathlib import Path
from typing import Any


TITLE = "Personal News Digest"
DEFAULT_TOPICS = [
    "AI infrastructure",
    "Nvidia",
    "AMD",
    "Broadcom",
    "optical networking",
    "data centers",
]
DEFAULT_MAX_ITEMS = 5
SAMPLE_ARTICLES_PATH = Path(__file__).with_name("sample_articles.json")

TOPIC_ALIASES = {
    "ai infrastructure": ["ai infrastructure", "ai clusters", "ai servers", "accelerators"],
    "nvidia": ["nvidia", "gpu"],
    "amd": ["amd"],
    "broadcom": ["broadcom"],
    "optical networking": ["optical networking", "optical interconnects", "optical"],
    "data centers": ["data centers", "data center", "facilities"],
}


def normalize_topic(topic: Any) -> str:
    return str(topic).strip()


def load_articles() -> list[dict[str, Any]]:
    return json.loads(SAMPLE_ARTICLES_PATH.read_text(encoding="utf-8"))


def keywords_for_topic(topic: str) -> list[str]:
    normalized = topic.lower()
    aliases = TOPIC_ALIASES.get(normalized, [])
    return [normalized, *aliases]


def article_search_text(article: dict[str, Any]) -> str:
    values = [
        article.get("headline", ""),
        article.get("summary", ""),
        article.get("why_it_matters", ""),
        " ".join(str(keyword) for keyword in article.get("keywords", [])),
    ]
    return " ".join(values).lower()


def matched_topics(article: dict[str, Any], topics: list[str]) -> list[str]:
    text = article_search_text(article)
    matches = []
    for topic in topics:
        if any(keyword and keyword in text for keyword in keywords_for_topic(topic)):
            matches.append(topic)
    return matches


def dedupe_articles(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    unique_articles = []
    for article in articles:
        key = (article.get("url") or article.get("headline") or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        unique_articles.append(article)
    return unique_articles


def parse_input(raw_input: str) -> tuple[list[str], int, list[str]]:
    warnings = []
    if not raw_input.strip():
        payload: dict[str, Any] = {}
    else:
        payload = json.loads(raw_input)
        if not isinstance(payload, dict):
            raise ValueError("Input must be a JSON object")

    raw_topics = payload.get("topics") or DEFAULT_TOPICS
    if not isinstance(raw_topics, list):
        warnings.append("topics must be a list; used default demo topics")
        raw_topics = DEFAULT_TOPICS

    topics = [topic for topic in (normalize_topic(item) for item in raw_topics) if topic]
    if not topics:
        warnings.append("No topics provided; used default demo topics")
        topics = DEFAULT_TOPICS

    raw_max_items = payload.get("max_items", DEFAULT_MAX_ITEMS)
    try:
        max_items = int(raw_max_items)
    except (TypeError, ValueError):
        warnings.append("max_items must be an integer; used default")
        max_items = DEFAULT_MAX_ITEMS
    if max_items < 1:
        warnings.append("max_items must be at least 1; used default")
        max_items = DEFAULT_MAX_ITEMS

    return topics, max_items, warnings


def build_digest(raw_input: str) -> dict[str, Any]:
    topics, max_items, warnings = parse_input(raw_input)
    articles = dedupe_articles(load_articles())
    ranked_articles = []

    for index, article in enumerate(articles):
        matches = matched_topics(article, topics)
        if not matches:
            continue
        ranked_articles.append((article, matches, index))

    ranked_articles.sort(
        key=lambda item: (
            -len(item[1]),
            str(item[0].get("published_at", "")),
            item[2],
        )
    )

    items = []
    for article, matches, _index in ranked_articles[:max_items]:
        items.append(
            {
                "headline": article.get("headline", ""),
                "source": article.get("source", ""),
                "url": article.get("url", ""),
                "summary": article.get("summary", ""),
                "why_it_matters": article.get("why_it_matters", ""),
                "matched_topics": matches,
            }
        )

    if not items:
        warnings.append("No local sample articles matched the requested topics")

    return {
        "title": TITLE,
        "topics": topics,
        "items": items,
        "warnings": warnings,
    }


def error_digest(message: str) -> dict[str, Any]:
    return {
        "title": TITLE,
        "topics": [],
        "items": [],
        "warnings": [message],
    }


def main() -> None:
    raw_input = sys.stdin.read()
    try:
        result = build_digest(raw_input)
    except Exception as exc:
        result = error_digest(str(exc))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
