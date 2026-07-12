# GitHub Trending Insights

This hidden automation retrieves GitHub's Trending HTML page and reports at most the first 10 unique repositories in displayed rank order. It accepts one JSON object on stdin and emits one JSON object on stdout.

## Input

- `language`: optional GitHub Trending language slug; the empty string means all languages.
- `since`: `daily`, `weekly`, or `monthly`; defaults to `weekly`.
- `limit`: integer from 1 through 10; defaults to 10.

## Bounded execution

The automation makes one HTTPS retrieval from `github.com`. It uses no browser automation, credentials, shell commands, third-party packages, or other external domains. It then submits every successfully parsed repository together in one request to the backend Skill Codex Call API. Codex internet access is disabled. The caller timeout is 45 seconds, leaving time within the 120-second entrypoint budget for retrieval, parsing, and graceful output.

## Report contract

Each project preserves its displayed rank, stable `owner/repository` identifier, GitHub URL, description, primary language, total stars, forks, and recent trending-star count. Unavailable optional values are JSON `null` and are also named in `metadata_missing`; they are never inferred.

The single batched Codex response is matched by identifier and rank. A valid analysis contains purpose, notable qualities, likely use cases, and potential limitations. Missing, partial, malformed, duplicate, or unmatched analyses remain explicit failures, while successfully parsed repositories remain in the report.

The top-level `status` is `complete`, `partial`, or `failed`. The `failures` array distinguishes input, retrieval, parsing, and Codex-analysis problems. Retrieval failure produces a valid failed report. Parsing or Codex failure produces a useful partial report whenever repository data is available.

This skill is hidden and intended for the host-managed weekly Monday 09:00 local-time schedule. It does not expose a tool UI or perform actions on repositories.
