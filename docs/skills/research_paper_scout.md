# Research Paper Scout

`research_paper_scout` is a user-owned Python `function` skill. The weekly report service calls it with `{"seen_papers": [...]}`. The caller owns that history; the scout has no filesystem permission and does not keep its own hidden record.

## Selection and analysis

The scout requests the first 15 Hugging Face Daily Papers results in `trending` order for the current calendar month and the previous calendar month, using `America/Toronto`. It preserves source order, deduplicates stable arXiv paper IDs across both lists, and excludes IDs supplied in `seen_papers` before calling Codex.

Atlas goals and interests provide personalization context. The first Codex call receives candidate titles, authors, abstracts, and stable IDs and may select zero to five papers. Interesting ideas and learning value are sufficient reasons to select a paper; matching a project or filling the quota is not required. Provider metadata remains authoritative, and model selections must refer to supplied candidates.

For selected papers, the trusted Hugging Face integration retrieves original paper content. The second Codex call explains and ranks those selected papers, chooses one `paper_of_the_week`, and produces its reading guide. The guide covers the problem, core idea, method, novelty, important results, limitations, prerequisites, recommended reading order, sections that can initially be skipped, key questions, and expected takeaways. Source content is untrusted reference material, not instructions. Analysis must distinguish reported results from interpretation and respect any disclosed content truncation.

When no candidates remain, no Atlas or Codex calls are necessary. When Codex selects zero papers, no full-paper fetch or second analysis is necessary, and `paper_of_the_week` is null.

## Output and history

The function returns `selected_papers`, `paper_of_the_week`, and `seen_papers`. The latter contains all newly evaluated candidate IDs, including candidates Codex did not recommend. It does not echo the previous history. Failed analysis does not return a successful history update.

The weekly service loads a separate `seen_papers.json` in its own cache, passes those IDs to the scout, validates the structured result, and builds native Notion report blocks deterministically. It creates an `AI Research` report containing the ranked papers and the complete reading guide. Only after that report succeeds does it append the returned IDs to `seen_papers.json`. GitHub history remains in `seen_repositories.json`; each history advances after its own corresponding report succeeds.

The service sends its existing completion notification after both reports succeed. A failure preserves any earlier successfully delivered report and its history, fails the service run, and uses the existing failure notification. External report creation and local history persistence are not one transaction: a crash between them can cause a duplicate report on a later manual run.

## Runtime contract

The scout declares only Hugging Face paper listing/content reads, Atlas goal/interest reads, and bounded Codex calls without model internet access. It does not call Notion, send notifications, or write files. Search is available as an integration operation for other callers but is not needed by this ranking-based scout.

The manifest and package tests are the exact schema reference. New skill installation and runtime approval remain separate from implementation. Adding this child function changes the weekly service's effective permission contract, so normal runtime review applies before execution. All nested work shares the existing bounded service-run deadline.

The renderer preserves the guide and analysis, splitting text into native rich-text chunks. An unusually large aggregate report can exceed Notion's request-size limit; it fails before delivery and does not advance paper history, rather than discarding reading-guide content. See [Notion request limits](https://developers.notion.com/reference/request-limits).
