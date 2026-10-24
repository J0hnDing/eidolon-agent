import { FormEvent, useEffect, useMemo, useState } from "react";

import { AssistantAgendaItem, AssistantRuntime, DailyMemorySummary, MemoryFact, MemoryFactInput, api } from "../api/client";
import { AppIcon } from "../components/AppIcon";
import { AssistantAgenda } from "../components/AssistantAgenda";
import { DeleteIconButton } from "../components/DeleteIconButton";
import { MemoryHistory, MemorySummaries, MemorySummaryDetail } from "../components/MemoryArchive";
import { Modal } from "../components/Modal";
import { formatSystemDateTime } from "../lib/dateTime";
import "./records.css";
import "./memory-details.css";

const sections = [
  ["overview", "Overview"], ["agenda", "Agenda"], ["questions", "Questions"],
  ["summaries", "Daily summaries"], ["facts", "Facts outside Atlas"], ["history", "Chat history"],
] as const;
type MemorySection = typeof sections[number][0];
const emptyForm: MemoryFactInput = {
  key: "", value: "", category: "facts", source_message_id: null,
  sensitivity: "normal", expires_at: null, user_editable: true,
};

export default function MemoryPage() {
  const [facts, setFacts] = useState<MemoryFact[]>([]);
  const [memoryRoot, setMemoryRoot] = useState<string | null>(null);
  const [section, setSection] = useState<MemorySection>("overview");
  const [agenda, setAgenda] = useState<AssistantAgendaItem[]>([]);
  const [runtime, setRuntime] = useState<AssistantRuntime | null>(null);
  const [summaries, setSummaries] = useState<DailyMemorySummary[]>([]);
  const [summaryTotal, setSummaryTotal] = useState<number | null>(null);
  const [loadErrors, setLoadErrors] = useState<string[]>([]);
  const [archiveRevision, setArchiveRevision] = useState(0);
  const [form, setForm] = useState<MemoryFactInput>(emptyForm);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [isSaving, setIsSaving] = useState(false);
  const [query, setQuery] = useState("");
  const [attributionFilter, setAttributionFilter] = useState("");
  const [selectedFactId, setSelectedFactId] = useState<number | null>(null);
  const [selectedSummary, setSelectedSummary] = useState<DailyMemorySummary | null>(null);
  const [deleteFact, setDeleteFact] = useState<MemoryFact | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);
  const editingFact = useMemo(() => facts.find((fact) => fact.id === editingId) ?? null, [facts, editingId]);
  const selectedFact = facts.find((fact) => fact.id === selectedFactId) ?? null;

  useEffect(() => { void loadFacts(); }, []);

  async function loadFacts() {
    setIsLoading(true); setError(null); setLoadErrors([]);
    const [factsResult, runtimeResult, agendaResult, summariesResult] = await Promise.allSettled([
      api.listMemoryFacts(), api.getAssistantRuntime(), api.listAssistantAgenda(), api.listDailyMemorySummaries(0, 3),
    ]);
    const failures: string[] = [];
    if (factsResult.status === "fulfilled") setFacts(factsResult.value);
    else failures.push("Facts outside Atlas could not be loaded.");
    if (runtimeResult.status === "fulfilled") {
      setRuntime(runtimeResult.value); setMemoryRoot(runtimeResult.value.memory_root ?? null);
    } else failures.push("Assistant memory settings could not be loaded.");
    if (agendaResult.status === "fulfilled") setAgenda(agendaResult.value);
    else failures.push("Agenda and questions could not be loaded.");
    if (summariesResult.status === "fulfilled") {
      setSummaries(summariesResult.value.items); setSummaryTotal(summariesResult.value.total);
    } else failures.push("Daily summaries could not be loaded.");
    setLoadErrors(failures); setArchiveRevision((value) => value + 1); setIsLoading(false);
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (isSaving) return;
    setFormError(null); setIsSaving(true);
    try {
      if (editingId === null) await api.createMemoryFact(form);
      else await api.updateMemoryFact(editingId, form);
      setForm(emptyForm); setEditingId(null); setEditorOpen(false);
      await loadFacts();
    } catch (err) { setFormError(err instanceof Error ? err.message : "Could not save memory fact"); }
    finally { setIsSaving(false); }
  }

  async function handleDelete(id: number) {
    if (isDeleting) return;
    setIsDeleting(true); setError(null);
    try {
      await api.deleteMemoryFact(id);
      if (editingId === id) { setEditingId(null); setForm(emptyForm); }
      if (selectedFactId === id) setSelectedFactId(null);
      setDeleteFact(null); await loadFacts();
    } catch (err) { setError(err instanceof Error ? err.message : "Could not delete memory fact"); }
    finally { setIsDeleting(false); }
  }

  async function openAgentFolder() {
    setError(null);
    try { await api.openActRoot(); }
    catch (err) { setError(err instanceof Error ? err.message : "Could not open the agent workspace"); }
  }

  function startEdit(fact: MemoryFact) {
    setEditingId(fact.id); setFormError(null); setEditorOpen(true);
    setForm({ key: fact.key, value: fact.value, category: fact.category, source_message_id: fact.source_message_id,
      sensitivity: fact.sensitivity, expires_at: fact.expires_at, user_editable: fact.user_editable });
  }

  const activeTasks = agenda.filter((item) => item.kind === "task" && ["open", "waiting"].includes(item.status));
  const activeQuestions = agenda.filter((item) => item.kind === "question" && ["open", "waiting"].includes(item.status));
  const visibleFacts = facts.filter((fact) =>
    `${fact.key} ${fact.value} ${fact.provenance_json?.evidence ?? ""}`.toLowerCase().includes(query.trim().toLowerCase())
    && (!attributionFilter || (fact.provenance_json?.attribution ?? "unknown") === attributionFilter));
  const counts: Partial<Record<MemorySection, number | null>> = {
    agenda: activeTasks.length, questions: activeQuestions.length, summaries: summaryTotal, facts: facts.length,
  };

  return <section className="page memory-page records-page">
    <header className="page-header"><div><h1>Memory</h1></div><div className="button-row">
      <button type="button" className="secondary" disabled={isLoading} onClick={() => void loadFacts()}>Refresh</button>
    </div></header>
    <div className="memory-workspace">
      <aside className="memory-collection-rail">
        <span className="record-rail-label">Your collections</span>
        <nav className="memory-collection-navigation" aria-label="Memory sections">
          {sections.map(([id, label]) => <button key={id} type="button" aria-label={label}
            className={section === id ? "active" : ""} aria-current={section === id ? "page" : undefined} onClick={() => setSection(id)}>
            <AppIcon name={id === "overview" ? "grid" : id === "agenda" ? "check" : id === "questions" ? "chat" : id === "summaries" ? "list" : id === "facts" ? "memory" : "clock"} size={16} />
            <span>{label}</span>{counts[id] != null && <small>{isLoading ? "—" : counts[id]}</small>}
          </button>)}
        </nav>
      </aside>
      <div className="memory-collection-content stack">
        {error && <p className="error-text" role="alert">{error}</p>}
        {loadErrors.length > 0 && <div role="alert" className="record-load-error">
          {loadErrors.map((failure) => <p key={failure}>{failure}</p>)}
          <button type="button" className="secondary" disabled={isLoading} onClick={() => void loadFacts()}>Retry loading memory</button>
        </div>}
        {isLoading && <p className="muted" role="status">Loading memory…</p>}
        {section === "overview" && <>
          <div className="memory-collection-grid">
            {([
              ["agenda", "Agenda", "Active responsibilities and next steps"], ["questions", "Questions", "Open questions to follow up"],
              ["summaries", "Daily summaries", "Actions and decisions by day"], ["facts", "Facts outside Atlas", "Knowledge you can review and edit"],
            ] as const).map(([id, label, description]) => <button type="button" className="memory-collection-card" key={id} onClick={() => setSection(id)}>
              <span className="memory-collection-card-top"><span>{label}</span><span aria-hidden="true">↗</span></span>
              <strong>{isLoading || loadErrors.some((failure) => failure.startsWith(label === "Questions" ? "Agenda" : label)) ? "—" : counts[id] ?? "—"}</strong>
              <small>{description}</small>
            </button>)}
          </div>
          <div className="memory-focus-grid">
            {(["task", "question"] as const).map((kind) => <section className="memory-focus-section" key={kind}>
              <div className="section-heading"><h2>{kind === "task" ? "Current agenda" : "Open questions"}</h2>
                <button type="button" className="secondary compact" onClick={() => setSection(kind === "task" ? "agenda" : "questions")}>View {kind === "task" ? "agenda" : "questions"}</button>
              </div>
              <div>{(kind === "task" ? activeTasks : activeQuestions).slice(0, 3).map((item) =>
                <button type="button" className="memory-focus-record memory-preview-link" key={item.id}
                  onClick={() => setSection(kind === "task" ? "agenda" : "questions")} aria-label={`View ${kind === "task" ? "agenda" : "questions"}: ${item.title}`}>
                  <span className={`record-state-dot status-${item.status}`} aria-hidden="true" />
                  <span><strong>{item.title}</strong>{item.notes && <span className="memory-row-preview">{item.notes}</span>}
                    <small className="muted">{item.status.replace(/_/g, " ")}{kind === "question" ? ` · ${item.question_category}` : ""}</small>
                  </span><span className="memory-row-arrow" aria-hidden="true">→</span>
                </button>)}</div>
              {!isLoading && !(kind === "task" ? activeTasks : activeQuestions).length && <div className="record-quiet-empty">
                <p>No active {kind === "task" ? "tasks" : "questions"}.</p><small>New {kind === "task" ? "responsibilities" : "questions"} will appear here.</small>
              </div>}
            </section>)}
          </div>
          <section className="memory-summary-preview"><div className="section-heading"><h2>Recent daily summaries</h2>
            <button type="button" className="secondary compact" onClick={() => setSection("summaries")}>View all summaries</button>
          </div><div className="memory-journal">{summaries.map((item) =>
            <button type="button" className="memory-summary-row" key={item.day} onClick={() => setSelectedSummary(item)} aria-label={`Read summary for ${formatDay(item.day)}`}>
              <time dateTime={item.day}>{formatDay(item.day)}</time><span className="memory-row-preview">{item.summary}</span><span className="memory-row-arrow" aria-hidden="true">→</span>
            </button>)}</div>
            {!isLoading && summaryTotal === 0 && <div className="record-quiet-empty"><p>No daily summaries saved yet.</p><small>Daily reflections will collect the decisions and actions that matter.</small></div>}
          </section>
          <div className="memory-archive-entry"><div><h3>Return to a conversation</h3><p className="muted">Browse retained Assistant, Act, and Observer messages.</p></div>
            <button type="button" className="secondary" onClick={() => setSection("history")}>Browse chat history <span aria-hidden="true">→</span></button>
          </div>
        </>}
        {(section === "agenda" || section === "questions") && <AssistantAgenda key={section} fixedKind={section === "agenda" ? "task" : "question"}
          items={agenda} questionAsking={runtime?.question_asking} onChange={async () => setAgenda(await api.listAssistantAgenda())} />}
        {section === "summaries" && <MemorySummaries key={archiveRevision} />}
        {section === "history" && <MemoryHistory key={archiveRevision} />}
        {section === "facts" && <>
          <div className="section-heading record-section-title"><div><h2>Facts outside Atlas</h2><p className="muted">Extra knowledge with its evidence, ready to review or correct.</p></div>
            <button type="button" onClick={() => { if (editingId !== null) { setEditingId(null); setForm(emptyForm); setFormError(null); } setEditorOpen(true); }}>Add fact</button>
          </div>
          <div className="records-toolbar">
            <label className="records-search"><AppIcon name="search" size={16} /><input aria-label="Search facts" placeholder="Search facts and evidence…" value={query} onChange={(event) => setQuery(event.target.value)} /></label>
            <label className="memory-inline-filter">Source<select aria-label="Filter facts by source" value={attributionFilter} onChange={(event) => setAttributionFilter(event.target.value)}>
              <option value="">All sources</option><option value="stated">Stated by you</option><option value="observed">Observed</option><option value="inferred">Inferred</option><option value="unknown">Unspecified</option>
            </select></label>
            <span className="muted">{visibleFacts.length} {visibleFacts.length === 1 ? "fact" : "facts"}</span>
            {(query || attributionFilter) && <button type="button" className="secondary compact" onClick={() => { setQuery(""); setAttributionFilter(""); }}>Clear filters</button>}
          </div>
          {!isLoading && <div className="memory-fact-list">{visibleFacts.map((fact) => <article key={fact.id} className="memory-fact-row">
            <button type="button" className="memory-fact-open" onClick={() => setSelectedFactId(fact.id)} aria-label={`View fact: ${fact.key}`}>
              <span className="memory-fact-heading"><strong>{fact.key}</strong><span className="memory-source-label">{attributionLabel(fact)}</span></span>
              <span className="memory-row-preview">{fact.value}</span>
            </button><span className="memory-row-arrow" aria-hidden="true">→</span>
          </article>)}
            {visibleFacts.length === 0 && <div className="record-quiet-empty"><h3>{query || attributionFilter ? "No matching facts" : "A place for the little things"}</h3>
              <p>{query || attributionFilter ? "Try another search or clear the filter." : "Add useful knowledge that does not belong in an Atlas record."}</p></div>}
          </div>}
        </>}
        <details className="memory-storage"><summary>Storage and memory folders</summary>
          <button type="button" className="secondary" onClick={() => void openAgentFolder()}>Open agent workspace</button>
          {memoryRoot && <p className="muted">Workspace memory folder: <code>{memoryRoot}</code></p>}
          <ul><li><code>memory/facts-outside-atlas/</code> — editable facts that are not stored in Atlas.</li>
            <li><code>memory/daily-summaries/</code> — daily reflections, up to 1,200 characters each.</li>
            <li><code>memory/chat-history/assistant/</code>, <code>memory/chat-history/act/</code>, and <code>memory/chat-history/observer/</code> — retained full chat transcripts.</li></ul>
          <p className="muted">Agenda and Questions use the Assistant's shared agenda. Atlas records remain in Atlas.</p>
        </details>
      </div>
    </div>
    {selectedSummary && <MemorySummaryDetail item={selectedSummary} onClose={() => setSelectedSummary(null)} />}
    {selectedFact && !editorOpen && !deleteFact && <Modal title={selectedFact.key} className="memory-detail-dialog" onClose={() => setSelectedFactId(null)}>
      <div className="memory-detail-body"><p className="memory-source-label">{attributionLabel(selectedFact)}</p><p className="preserve-whitespace memory-detail-text">{selectedFact.value}</p>
        {selectedFact.provenance_json?.evidence && <section className="memory-detail-section"><h3>Evidence</h3><p className="preserve-whitespace">{selectedFact.provenance_json.evidence}</p></section>}
        <dl className="memory-detail-metadata"><div><dt>Updated</dt><dd>{formatSystemDateTime(selectedFact.updated_at)}</dd></div><div><dt>Created</dt><dd>{formatSystemDateTime(selectedFact.created_at)}</dd></div>
          {selectedFact.expires_at && <div><dt>Expires</dt><dd>{formatSystemDateTime(selectedFact.expires_at)}</dd></div>}</dl>
      </div>
      <div className="button-row memory-detail-footer"><DeleteIconButton label={`Delete fact outside Atlas: ${selectedFact.key}`} onClick={() => setDeleteFact(selectedFact)} />
        <button type="button" className="secondary" aria-label={`Edit ${selectedFact.key}`} onClick={() => startEdit(selectedFact)}>Edit fact</button></div>
    </Modal>}
    {editorOpen && <Modal title={editingFact ? `Edit ${editingFact.key}` : "Add fact outside Atlas"} onClose={() => { if (!isSaving) setEditorOpen(false); }} className="memory-fact-editor">
      <form className="stack" onSubmit={handleSubmit}><p className="muted">Save a useful piece of knowledge in your local memory.</p>
        {formError && <p className="error-text" role="alert">{formError}</p>}
        <label>Key<input value={form.key} onChange={(event) => setForm({ ...form, key: event.target.value })} required maxLength={128} placeholder="For example, local library hours" /></label>
        <label>Value<textarea value={form.value} onChange={(event) => setForm({ ...form, value: event.target.value })} required rows={5} placeholder="What should Eidolon remember?" /></label>
        <div className="button-row memory-editor-footer"><button type="button" className="secondary" disabled={isSaving} onClick={() => setEditorOpen(false)}>Cancel</button>
          <button type="submit" disabled={isSaving}>{isSaving ? "Saving…" : editingId === null ? "Create" : "Save"}</button></div>
      </form>
    </Modal>}
    {deleteFact && <Modal title="Delete memory fact" onClose={() => { if (!isDeleting) setDeleteFact(null); }}><p>Delete <strong>{deleteFact.key}</strong> from Eidolon's memory?</p>
      {error && <p className="error-text" role="alert">{error}</p>}
      <div className="button-row"><button type="button" className="secondary" disabled={isDeleting} onClick={() => setDeleteFact(null)}>Keep fact</button>
        <button type="button" className="danger" disabled={isDeleting} onClick={() => void handleDelete(deleteFact.id)}>{isDeleting ? "Deleting…" : "Delete fact"}</button></div>
    </Modal>}
  </section>;
}

function formatDay(value: string) {
  return new Date(`${value}T12:00:00Z`).toLocaleDateString(undefined, { month: "long", day: "numeric", year: "numeric", timeZone: "UTC" });
}
function attributionLabel(fact: MemoryFact) {
  const attribution = fact.provenance_json?.attribution;
  return attribution === "inferred" ? "Hypothesis (inferred)" : attribution === "observed" ? "Observed" : attribution === "stated" ? "Stated by you" : "Source unspecified";
}
