import { useEffect, useState } from "react";

import { DailyMemorySummary, MemoryChatMessage, MemoryPageResult, api } from "../api/client";
import { AppIcon } from "./AppIcon";
import { Modal } from "./Modal";
import { formatSystemDateTime } from "../lib/dateTime";
import "../pages/records.css";
import "../pages/memory-details.css";

export function MemorySummaries() {
  const [offset, setOffset] = useState(0);
  const [revision, setRevision] = useState(0);
  const [page, setPage] = useState<MemoryPageResult<DailyMemorySummary> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [month, setMonth] = useState("");
  const [allSummaries, setAllSummaries] = useState<DailyMemorySummary[] | null>(null);
  const [filterError, setFilterError] = useState<string | null>(null);
  const [filterRevision, setFilterRevision] = useState(0);
  const [selected, setSelected] = useState<DailyMemorySummary | null>(null);
  const filtering = !!(query.trim() || month);
  useEffect(() => {
    if (filtering) return;
    let current = true;
    setPage(null); setError(null);
    api.listDailyMemorySummaries(offset).then((result) => { if (current) setPage(result); })
      .catch((reason) => { if (current) setError(message(reason)); });
    return () => { current = false; };
  }, [offset, revision, filtering]);
  useEffect(() => {
    if (!filtering || allSummaries) return;
    let current = true;
    setFilterError(null);
    async function loadSearchCollection() {
      const items: DailyMemorySummary[] = [];
      let nextOffset = 0;
      while (current) {
        const result = await api.listDailyMemorySummaries(nextOffset, 100);
        if (!current) return;
        items.push(...result.items);
        nextOffset = result.offset + result.items.length;
        if (nextOffset >= result.total) break;
        if (!result.items.length) throw new Error("Could not load all daily summaries. Try again.");
      }
      if (current) setAllSummaries(items);
    }
    void loadSearchCollection().catch((reason) => { if (current) setFilterError(message(reason)); });
    return () => { current = false; };
  }, [filtering, allSummaries, filterRevision]);
  const matches = allSummaries?.filter((item) => (!month || item.day.startsWith(month))
    && `${item.day} ${item.summary}`.toLowerCase().includes(query.trim().toLowerCase())) ?? [];
  const visiblePage = filtering ? { items: matches.slice(offset, offset + 20), total: matches.length, offset, limit: 20 } : page;
  const currentError = filtering ? filterError : error;
  const loading = filtering ? !allSummaries : !page;
  return <section className="stack memory-archive" aria-labelledby="daily-memory-title">
    <div className="record-section-title"><h2 id="daily-memory-title">Daily summaries</h2><p className="muted">The actions and decisions that mattered. Written each day, newest first.</p></div>
    <div className="records-toolbar memory-summary-filters">
      <label className="records-search"><AppIcon name="search" size={16} /><input aria-label="Search daily summaries" placeholder="Search daily summaries…" value={query} onChange={(event) => { setQuery(event.target.value); setOffset(0); }} /></label>
      <label className="memory-inline-filter">Month<input aria-label="Filter summaries by month" type="month" value={month} onChange={(event) => { setMonth(event.target.value); setOffset(0); }} /></label>
      {filtering && <button type="button" className="secondary compact" onClick={() => { setQuery(""); setMonth(""); setOffset(0); }}>Clear filters</button>}
    </div>
    {currentError ? <LoadError error={currentError} onRetry={() => filtering ? setFilterRevision(filterRevision + 1) : setRevision(revision + 1)} /> : loading || !visiblePage ? <p className="muted" role="status">Loading daily summaries…</p> : <>
      {!visiblePage.total && <p className="muted">{filtering ? "No daily summaries match these filters." : "No daily summaries saved yet."}</p>}
      <div className="memory-journal">{visiblePage.items.map((item) => <button type="button" className="memory-summary-row" key={item.day} onClick={() => setSelected(item)} aria-label={`Read summary for ${summaryDay(item.day)}`}>
        <time dateTime={item.day}>{summaryDay(item.day)}</time><span className="memory-row-preview">{item.summary}</span><span className="memory-row-arrow" aria-hidden="true">→</span>
      </button>)}</div>
      <MemoryPagination page={visiblePage} onChange={setOffset} label="daily summaries" />
    </>}
    {selected && <MemorySummaryDetail item={selected} onClose={() => setSelected(null)} />}
  </section>;
}

export function MemorySummaryDetail({ item, onClose }: { item: DailyMemorySummary; onClose: () => void }) {
  return <Modal title={`Daily summary · ${summaryDay(item.day)}`} className="memory-detail-dialog" onClose={onClose}>
    <div className="memory-detail-body"><p className="preserve-whitespace memory-detail-text">{item.summary}</p>
      <section className="memory-detail-section"><h3>Source file</h3><code className="memory-source-path">{item.path}</code></section>
    </div>
  </Modal>;
}

function summaryDay(day: string) {
  return new Date(`${day}T12:00:00Z`).toLocaleDateString(undefined, { month: "long", day: "numeric", year: "numeric", timeZone: "UTC" });
}

export function MemoryHistory() {
  const [query, setQuery] = useState("");
  const [days, setDays] = useState<string[]>([]);
  const [dayTotal, setDayTotal] = useState(0);
  const [day, setDay] = useState("");
  const [daysLoading, setDaysLoading] = useState(true);
  const [daysError, setDaysError] = useState<string | null>(null);
  const [dayRevision, setDayRevision] = useState(0);
  const [agent, setAgent] = useState("");
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<MemoryPageResult<MemoryChatMessage> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    let current = true;
    setDaysLoading(true); setDaysError(null);
    api.listMemoryChatDays().then((result) => {
      if (!current) return;
      setDays(result.items); setDayTotal(result.total); setDay(result.items[0] ?? ""); setOffset(0);
    }).catch((reason) => { if (current) setDaysError(message(reason)); })
      .finally(() => { if (current) setDaysLoading(false); });
    return () => { current = false; };
  }, [dayRevision]);

  useEffect(() => {
    let current = true;
    setPage(null); setError(null);
    if (day) api.listMemoryChatHistory(day, agent, offset).then((result) => { if (current) setPage(result); })
      .catch((reason) => { if (current) setError(message(reason)); });
    return () => { current = false; };
  }, [day, agent, offset, revision]);

  async function loadOlderDays() {
    setDaysLoading(true); setDaysError(null);
    try {
      const result = await api.listMemoryChatDays(days.length);
      setDays((previous) => [...previous, ...result.items]); setDayTotal(result.total);
    } catch (reason) { setDaysError(message(reason)); }
    finally { setDaysLoading(false); }
  }

  const visibleMessages = page?.items.filter((item) => item.text.toLowerCase().includes(query.trim().toLowerCase())) ?? [];
  return <section className="stack memory-archive" aria-labelledby="chat-memory-title">
    <div className="record-section-title"><h2 id="chat-memory-title">Chat history</h2><p className="muted">Revisit your conversations with Assistant, Act, and Observer.</p><details className="memory-retention"><summary>About this archive</summary><p className="muted">Dates are grouped in UTC; message times use your local timezone. Oldest messages are discarded after 30 days or when the shared 50 MiB limit is reached.</p></details></div>
    {daysError && <LoadError error={daysError} onRetry={() => days.length ? void loadOlderDays() : setDayRevision(dayRevision + 1)} />}
    {daysLoading && <p className="muted" role="status">Loading conversation dates…</p>}
    {!daysLoading && !daysError && !days.length && <p className="muted">No retained chat history yet.</p>}
    {!!days.length && <>
      <div className="records-toolbar memory-history-filters">
        <label className="records-search"><AppIcon name="search" size={16} /><input aria-label="Search messages on this page" placeholder="Search this page…" value={query} onChange={(event) => setQuery(event.target.value)} /></label>
        <label>Conversation date (UTC)<select value={day} onChange={(event) => { setDay(event.target.value); setOffset(0); }}>
          {days.map((value) => <option key={value} value={value}>{value}</option>)}
        </select></label>
        <label>Agent<select value={agent} onChange={(event) => { setAgent(event.target.value); setOffset(0); }}>
          <option value="">All agents</option><option value="assistant">Assistant</option><option value="act">Act</option><option value="observer">Observer</option>
        </select></label>
      </div>
      {days.length < dayTotal && <div><button type="button" className="secondary" disabled={daysLoading} onClick={() => void loadOlderDays()}>Load older dates</button></div>}
      {error ? <LoadError error={error} onRetry={() => setRevision(revision + 1)} /> : !page ? <p className="muted" role="status">Loading messages…</p> : <>
        {!page.total && <p className="muted">No retained messages for this date and agent.</p>}
        {query.trim() && <p className="muted" role="status">{visibleMessages.length} matching {visibleMessages.length === 1 ? "message" : "messages"} on this page. Search covers the current date, agent, and page.</p>}
        <div className="memory-transcript">{visibleMessages.map((item) => <article className={`memory-transcript-message ${item.role === "user" ? "from-user" : "from-eidolon"}`} key={`${item.agent}-${item.source}-${item.source_id}-${item.role}`}>
          <header className="section-heading"><h3>{item.role === "user" ? "You" : "Eidolon"} <span className="muted">· {item.agent === "act" ? "Act" : item.agent === "observer" ? "Observer" : "Assistant"}</span></h3><time dateTime={item.timestamp} className="muted">{formatSystemDateTime(item.timestamp)}</time></header>
          <p className="preserve-whitespace">{item.text}</p>
        </article>)}</div>
        <MemoryPagination page={page} onChange={setOffset} label="messages" />
      </>}
    </>}
  </section>;
}

function MemoryPagination({ page, onChange, label }: { page: MemoryPageResult<unknown>; onChange: (offset: number) => void; label: string }) {
  if (!page.total) return null;
  return <div className="button-row memory-pagination" aria-label={`${label} pagination`}>
    <span className="muted">{Math.min(page.offset + 1, page.total)}–{Math.min(page.offset + page.items.length, page.total)} of {page.total} {label}</span>
    <button type="button" className="secondary" disabled={page.offset === 0} onClick={() => onChange(Math.max(0, page.offset - page.limit))}>Previous</button>
    <button type="button" className="secondary" disabled={page.offset + page.limit >= page.total} onClick={() => onChange(page.offset + page.limit)}>Next</button>
  </div>;
}

function LoadError({ error, onRetry }: { error: string; onRetry: () => void }) {
  return <div className="button-row"><p role="alert" className="error-text">{error}</p><button type="button" className="secondary" onClick={onRetry}>Retry</button></div>;
}

function message(reason: unknown) { return reason instanceof Error ? reason.message : "Could not load memory. Try again."; }
