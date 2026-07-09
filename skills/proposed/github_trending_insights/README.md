# GitHub Trending Insights

GitHub Trending Insights is a local-first automation tool package for reviewing current GitHub Trending repositories from the Tools page. It fetches the selected GitHub Trending view, parses repository metadata, and uses backend-mediated Codex analysis to return a concise structured report.

This proposed skill is not installed, enabled, scheduled, or approved to run by this file. Runtime approval is still controlled by the platform manifest, permission review, validation, and tests.

## Inputs

- `language`: optional GitHub language filter such as `python`, `typescript`, or `rust`. Leave blank to inspect all languages.
- `time_range`: required trending period. Supported values are `daily`, `weekly`, and `monthly`.
- `max_projects`: required maximum repository count. Supported range is 1 through 25.
- `focus`: optional analysis lens for the Codex report, such as developer tools, AI agents, or local-first apps.
- `include_risks`: optional boolean that controls whether the output includes caveats, adoption risks, and weak signals.

## Outputs

The tool returns a JSON-compatible report with:

- `summary`: concise overview of the trending set.
- `projects`: parsed repositories with name, URL, description, language when available, stars when available, trend signal, and focused insights.
- `themes`: common technical or product patterns found across the set.
- `recommendations`: actionable takeaways for the user.
- `risks`: optional caveats and adoption concerns when `include_risks` is true.

## Permission Boundaries

Runtime network access is limited to `github.com` for fetching GitHub Trending pages. The skill may read and write only its own `./cache` directory for local runtime state. Shell access is disabled, secrets are not requested, and no browser automation, email, calendar, finance, purchase, public posting, file deletion, or arbitrary command execution capability is declared.

Codex analysis must be performed through the backend Skill Codex Call API with `call_response` enabled and `internet_access` disabled. The Codex prompt should receive only parsed repository data and the user's focus text, not unrelated local files or chat history.

## Tool UI Contract

The Tools page UI is declarative and driven by `manifest.json`. The manifest defines labels, field types, defaults, select options, help text, submit label, input schema, output schema, and summary rendering hints. This package does not include React, HTML, JavaScript, or frontend application source code.
