# GitHub Trending Insights

GitHub Trending Insights is a hidden weekly automation. The host schedules it for Monday at 09:00 in the configured host-local timezone (`America/Toronto` in the manifest). It retrieves GitHub Trending and creates a structured report for up to 10 repositories.

The report preserves trending rank and includes repository identifier, URL, description, primary language, stars, forks, recent trending stars, and a concise analysis covering purpose, notable qualities, likely use cases, and potential limitations. All repositories are analyzed in one bounded backend Codex request, not one request per repository.

Missing page metadata is represented as `null` and listed in `metadata_missing`. Retrieval, entry parsing, metadata, and Codex-analysis issues are reported separately in `failures`; successful repository records are retained when another entry or the analysis call fails.

Runtime permissions are limited to HTTPS access to `github.com`, skill-local `./cache` read/write if needed by the host, and one backend-mediated Codex response call with Codex internet access disabled. The automation uses no secrets, shell access, browser automation, third-party dependencies, package installation, or filesystem access outside the approved skill-local cache. It is not user-facing and defines no tool UI.
