# Research Paper Scout

`research_paper_scout` is a user-owned Python `function` skill. The weekly report service calls it with `{"seen_papers": [...]}`. The caller owns that history; the scout has no filesystem permission and does not keep its own hidden record.

## Monthly analysis

The scout requests the six highest-upvoted unseen Hugging Face Daily Papers for the current calendar month, using `America/Toronto`. It passes `seen_papers` into the trusted provider as exclusions. The provider paginates the complete bounded month feed, rejects missing or out-of-month submission dates, deduplicates stable arXiv paper IDs, ranks by descending Hugging Face upvotes with a stable paper-ID tie-breaker, removes seen IDs, and then returns the next six. If fewer than six unseen papers exist in the month, the scout analyzes the available remainder.

The trusted Hugging Face integration retrieves original content for every returned paper. One Codex call must analyze all of them without selecting, omitting, or ranking papers. Hugging Face upvote order remains authoritative. Each analysis is two short plain-language paragraphs: a one-sentence summary that names the concrete artifact or method and its reported scale when available, followed by a brief explanation of how it works, its concrete advantages, and its main reported result. Academic wording, hype, decorative adjectives, vague praise, and filler are prohibited. Source content is untrusted reference material, not instructions. Analysis must distinguish reported results from interpretation and respect any disclosed content truncation.

When no unseen papers remain in the month, no full-paper fetch or Codex call is necessary.

## Output and history

The function returns only `selected_papers` and `seen_papers`. `selected_papers` retains the provider's upvote order, with ranks assigned deterministically from 1 through 6. `seen_papers` contains the IDs successfully analyzed in that run and does not echo previous history. Failed analysis does not return a successful history update.

The weekly service loads a separate `seen_papers.json` in its own cache, passes those IDs to the scout, validates that returned `seen_papers` exactly matches the selected papers, and builds native Notion report blocks deterministically. It creates an `AI Research` report that shows only each linked title, linked Hugging Face organization when available, upvote count, and plain-language analysis. Paper IDs, authors, publication timestamps, source labels, abstracts, standalone bookmarks, PDF links, and truncation notes are not rendered. Only after that report succeeds does it append the selected IDs to `seen_papers.json`. GitHub history remains in `seen_repositories.json`; each history advances after its own corresponding report succeeds.

The service sends its existing completion notification after both reports succeed. A failure preserves any earlier successfully delivered report and its history, fails the service run, and uses the existing failure notification. External report creation and local history persistence are not one transaction: a crash between them can cause a duplicate report on a later manual run.

## Runtime contract

The scout declares only Hugging Face paper listing/content reads and one bounded Codex analysis call without model internet access. It does not read Atlas, call Notion, send notifications, or write files. Search is available as an integration operation for other callers but is not needed by this ranking-based scout.

The manifest and package tests are the exact schema reference. New skill installation and runtime approval remain separate from implementation. Adding this child function changes the weekly service's effective permission contract, so normal runtime review applies before execution. All nested work shares the existing bounded service-run deadline.

The renderer preserves the complete concise analysis, splitting text into native rich-text chunks. An unusually large aggregate report can exceed Notion's request-size limit; it fails before delivery and does not advance paper history. See [Notion request limits](https://developers.notion.com/reference/request-limits).
