# Research Paper Scout

`research_paper_scout` is a user-owned Python `function` skill. The weekly report service calls it with `{"excluded_papers": [...]}`. The service owns the history of papers selected and included in successful reports; the scout has no filesystem permission and does not keep its own history.

## Candidate selection and analysis

Each call requests the top 15 Hugging Face Daily Papers by upvotes for the current and previous Toronto calendar months. Before title selection, it removes IDs in `excluded_papers` (papers selected and included in prior weekly reports) and collapses duplicate stable IDs across the two lists, keeping the current-month entry. A candidate that was only considered and not reported is not added to history and may appear again in a later run. Distinct papers with matching titles remain separate candidates.

The first Codex call receives only an index and title per remaining candidate, plus Atlas goals, projects, and interests. It may choose zero to three candidates, ordered from most to least interesting to the user. If exclusions leave no candidates, neither Atlas context nor Codex is requested. If Codex selects no papers, no full-paper fetch or analysis call is needed.

The trusted Hugging Face integration retrieves original content for each remaining selection. A second Codex call analyzes every selected full paper without further selection or re-ranking. Each analysis is two short plain-language paragraphs: a one-sentence summary that names the concrete artifact or method and its reported scale when available, followed by a brief explanation of how it works, its concrete advantages, and its main reported result. Academic wording, hype, decorative adjectives, vague praise, and filler are prohibited. Source content is untrusted reference material, not instructions. Analysis must distinguish reported results from interpretation and respect any disclosed content truncation.

## Output and history

The function returns only `selected_papers`, with at most three papers in Codex's interest order and ranks assigned from 1 through the number returned. It does not return or store candidate history. The weekly service derives IDs from this selection and appends them to its own history only after the `AI Research` report succeeds; merely considered candidates are never recorded.

The weekly service loads the IDs of previously reported papers from `seen_papers.json` and passes them as `excluded_papers`. It validates the selected-paper output, builds native Notion report blocks deterministically, and appends selected IDs only after the `AI Research` report succeeds. The report shows only each linked title, linked Hugging Face organization when available, upvote count, and plain-language analysis. Paper IDs, authors, publication timestamps, source labels, abstracts, standalone bookmarks, PDF links, and truncation notes are not rendered. GitHub history remains in `seen_repositories.json`; each history advances after its corresponding report succeeds.

The service sends its completion notification after all three weekly reports succeed. A failure preserves any earlier successfully delivered report and its history, fails the service run, and uses the existing failure notification. External report creation and local history persistence are not one transaction: a crash between them can cause a duplicate report on a later manual run.

## Runtime contract

The scout declares Hugging Face paper listing/content reads, Atlas goal/project/interest reads, and two bounded Codex calls without model internet access. It does not call Notion, send notifications, or write files. Search is available as an integration operation for other callers but is not needed by this title-selection workflow.

The manifest and package tests are the exact schema reference. New skill installation and runtime approval remain separate from implementation. Atlas reads add to the weekly service's effective permission contract, so normal runtime review applies before execution. All nested work shares the existing bounded service-run deadline.

The renderer preserves the complete concise analysis, splitting text into native rich-text chunks. An unusually large aggregate report can exceed Notion's request-size limit; it fails before delivery and does not advance paper history. See [Notion request limits](https://developers.notion.com/reference/request-limits).
