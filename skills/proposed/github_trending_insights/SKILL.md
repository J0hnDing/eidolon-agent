# GitHub Trending Insights

Use this automation to inspect GitHub Trending repositories and produce a concise, Codex-oriented insight report for the Tools page.

## Operating Expectations

1. Accept JSON input from stdin using the manifest input contract.
2. Validate `time_range` as one of `daily`, `weekly`, or `monthly`.
3. Clamp or reject `max_projects` outside the manifest range of 1 through 25 before fetching or reporting.
4. Fetch only GitHub Trending pages on `github.com`, applying the optional language filter and selected time range.
5. Parse only visible repository metadata needed for the report: repository name, URL, description, language, stars, and trending signal text when available.
6. Send only parsed repository data plus the user's optional focus text to the backend Skill Codex Call API.
7. Request concise analysis with summary, per-project insights, themes, recommendations, and risks only when `include_risks` is true.
8. Write a single JSON object to stdout.
9. Return controlled JSON error objects for fetch, parse, validation, or Codex-analysis failures where possible.

## Permission Boundaries

- Network access is limited to `github.com`.
- Filesystem access is limited to the skill-local `./cache` directory.
- Shell access is disabled.
- Secrets are not requested.
- Codex access is backend-mediated with `call_response` enabled and `internet_access` disabled.
- The skill must not install packages, invoke the Codex CLI, run browser automation, post publicly, perform purchases, access email/calendar/finance systems, delete files, or execute arbitrary commands.

## Tool Interface

The tool interface is declared in `manifest.json` through `tool_ui_schema`, `input_schema`, and `output_schema`. Do not add frontend app code for this skill. The Tools page should render the form using the declarative fields for language, time range, maximum projects, analysis focus, and risk inclusion, then display the summary using the result template metadata.

## Local-First Behavior

All runtime state should stay local to the skill package. Use `./cache` only for non-sensitive runtime cache data. Do not persist broad chat history, credentials, private repository data, or unrelated local file contents.
