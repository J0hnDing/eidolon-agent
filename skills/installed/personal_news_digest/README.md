# Personal News Digest

Offline demo skill for Milestone 4.

This skill reads JSON input from stdin, filters local sample article data, deduplicates by URL or headline, ranks matches by topic overlap, and prints a structured JSON digest to stdout.

It does not fetch RSS feeds, browse websites, use credentials, send messages, or access the network.

## Input

```json
{
  "topics": ["AI infrastructure", "Nvidia", "AMD", "Broadcom", "optical networking", "data centers"],
  "max_items": 5
}
```

If no input is provided, the skill uses the default demo topics above and returns up to five items.

## Output

```json
{
  "title": "Personal News Digest",
  "topics": ["AI infrastructure"],
  "items": [
    {
      "headline": "Example",
      "source": "Local Demo Wire",
      "url": "https://local.example/articles/example",
      "summary": "Short summary.",
      "why_it_matters": "Short explanation.",
      "matched_topics": ["AI infrastructure"]
    }
  ],
  "warnings": []
}
```

## Permissions

The manifest declares only low-risk demo permissions:

- `network`: none
- `filesystem_read`: none
- `filesystem_write`: `./cache`
- `secrets`: none
- `shell`: false
