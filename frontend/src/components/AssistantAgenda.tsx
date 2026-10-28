import { useState } from "react";

import { AssistantAgendaInput, AssistantAgendaItem, AssistantAgendaOccurrence, AssistantAgendaRecurrence, AssistantQuestionCategory, AssistantRuntime, api } from "../api/client";
import { parseBackendDateTime } from "../lib/dateTime";
import { Modal } from "./Modal";
import "./assistant-agenda.css";

export function AssistantAgenda({ items, questionAsking, dailyPlan, onChange, fixedKind, compact = false }: { items: AssistantAgendaItem[]; questionAsking?: AssistantRuntime["question_asking"]; dailyPlan?: AssistantRuntime["daily_plan"]; onChange: () => Promise<void>; fixedKind?: AssistantAgendaItem["kind"]; compact?: boolean }) {
  const [selectedKind, setKind] = useState<AssistantAgendaItem["kind"]>("task");
  const kind = fixedKind ?? selectedKind;
  const [view, setView] = useState<"current" | "past">("current");
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [category, setCategory] = useState("");
  const [schedule, setSchedule] = useState("");
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const selected = items.find((item) => item.id === selectedId);
  const [editing, setEditing] = useState<AssistantAgendaItem | "new" | null>(null);
  const [deleting, setDeleting] = useState<AssistantAgendaItem | null>(null);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function mutate(action: () => Promise<unknown>) {
    setWorking(true); setError(null);
    try { await action(); await onChange(); }
    catch (reason) { setError(errorMessage(reason, "Could not update the Assistant agenda")); }
    finally { setWorking(false); }
  }
  const collection = items.filter((item) => item.kind === kind && (view === "current" ? item.status === "open" || item.status === "waiting" : isPastAgendaItem(item)));
  const query = search.trim().toLocaleLowerCase();
  const visible = collection.filter((item) => (!query || `${item.title} ${item.notes}`.toLocaleLowerCase().includes(query))
    && (!status || item.status === status)
    && (kind !== "question" || !category || item.question_category === category)
    && (kind !== "task" || !schedule || Boolean(item.recurrence) === (schedule === "recurring")));
  const hasFilters = Boolean(query || status || (kind === "question" ? category : schedule));
  return <section className="detail-panel stack assistant-agenda">
    <div className="section-heading"><div><h2>{fixedKind === "question" ? "Questions" : fixedKind === "task" || compact ? "Agenda" : "Assistant agenda"}</h2>{!compact && <p className="muted">Responsibilities and questions the Assistant is keeping track of.</p>}</div><button type="button" onClick={() => { setView("current"); setEditing("new"); }} disabled={working || editing !== null}>Add {kind === "question" ? "question" : "task"}</button></div>
    <div className="agenda-list-navigation">
      {!fixedKind && <div className="button-row" role="group" aria-label="Agenda kind"><button type="button" className={kind === "task" ? "" : "secondary"} aria-pressed={kind === "task"} onClick={() => setKind("task")}>Tasks</button>
      <button type="button" className={kind === "question" ? "" : "secondary"} aria-pressed={kind === "question"} onClick={() => setKind("question")}>Questions</button></div>}
      <div className="button-row" role="group" aria-label="Agenda view">
        <button type="button" className={view === "current" ? "" : "secondary"} aria-pressed={view === "current"} onClick={() => { setView("current"); setStatus(""); }}>Current</button>
        <button type="button" className={view === "past" ? "" : "secondary"} aria-pressed={view === "past"} onClick={() => { setView("past"); setStatus(""); }}>Past</button>
      </div>
    </div>
    <div className="agenda-list-toolbar">
      <input type="search" aria-label={kind === "question" ? "Search questions" : "Search agenda"} placeholder={kind === "question" ? "Search questions and context…" : "Search tasks and notes…"} value={search} onChange={(event) => setSearch(event.target.value)} />
      <select aria-label="Filter by status" value={status} onChange={(event) => setStatus(event.target.value)}>
        <option value="">All statuses</option><option value="open">Open</option><option value="waiting">Waiting</option>{view === "past" && <><option value="done">Done</option><option value="dismissed">Dismissed</option></>}
      </select>
      {kind === "question" ? <select aria-label="Filter by question category" value={category} onChange={(event) => setCategory(event.target.value)}><option value="">All categories</option><option value="common">Common</option><option value="important">Important</option><option value="temporal">Temporal</option></select>
        : <select aria-label="Filter by schedule" value={schedule} onChange={(event) => setSchedule(event.target.value)}><option value="">All schedules</option><option value="recurring">Recurring</option><option value="one-time">One-time</option></select>}
    </div>
    {kind === "question" && <div className="agenda-question-context">{questionAsking && <span>Asked today: {questionAsking.asked_today} ({questionAsking.timezone}).</span>}<span>Important questions need prompt attention. Temporal questions follow their scheduled time; common questions wait for a suitable opening.</span></div>}
    {dailyPlan && <div className="agenda-question-context" role="status" aria-label="Today's Assistant plan">
      <span>Today · {dailyPlan.day} ({dailyPlan.timezone})</span>
      {dailyPlan.plan_turn_id === null ? <span>Morning plan not saved yet.</span> : ([['work', 'Work'], ['interaction', 'Interactions'], ['other', 'Other']] as const).map(([category, label]) => <span key={category}>{label}: {dailyPlan.completed[category]}/{dailyPlan.required[category]} {category === 'interaction' ? 'initiated' : 'completed'} · {dailyPlan.planned[category]} planned</span>)}
    </div>}
    {error && !selected && <p className="error-text" role="alert">{error}</p>}
    <div className="agenda-list-count">{visible.length} {kind === "question" ? (visible.length === 1 ? "question" : "questions") : (visible.length === 1 ? "task" : "tasks")}{hasFilters ? ` of ${collection.length}` : ""}</div>
    {visible.length > 0 ? <ul className="agenda-record-list" aria-label={kind === "question" ? "Questions" : "Agenda tasks"}>{visible.map((item) => <li key={item.id}>
      <button type="button" className="agenda-record-row" aria-label={`Open ${item.title}`} onClick={() => { setError(null); setSelectedId(item.id); }}>
        <span className="agenda-record-content"><strong>{item.title}</strong><span className="agenda-record-preview">{item.notes || (item.kind === "question" ? "No context added" : "No notes added")}</span><span className="agenda-record-meta">{item.kind === "question" ? `${questionCategoryLabel(item.question_category)} question` : item.recurrence ? formatRecurrence(item.recurrence) : "One-time task"}</span></span>
        <span className="agenda-record-timing">{item.due_at ? <><span>{item.kind === "question" ? "Next ask" : item.recurrence ? "Next occurrence" : "Next check"}</span><time dateTime={item.due_at}>{formatTimestamp(item.due_at)}</time></> : <span>No scheduled time</span>}</span>
        <span className={`badge status-${item.status}`}>{item.status}</span><span className="agenda-record-arrow" aria-hidden="true">›</span>
      </button>
    </li>)}</ul> : <div className="agenda-list-empty"><p className="muted">{hasFilters ? `No ${kind === "question" ? "questions" : "tasks"} match your filters.` : `No ${view === "past" ? "past" : "current"} ${kind === "question" ? "questions" : "tasks"}.`}</p>{hasFilters && <button type="button" className="secondary compact" onClick={() => { setSearch(""); setStatus(""); setCategory(""); setSchedule(""); }}>Clear filters</button>}</div>}
    {selected && editing === null && !deleting && <Modal title={selected.title} className="agenda-detail-modal" onClose={() => { if (!working) { setSelectedId(null); setError(null); } }}>
      <div className="agenda-detail-body">
        <div className="agenda-detail-labels"><span className={`badge status-${selected.status}`}>{selected.status}</span><span className="muted">{selected.kind === "question" ? `${questionCategoryLabel(selected.question_category)} question` : selected.recurrence ? "Recurring task" : "One-time task"}</span></div>
        <section className="agenda-detail-notes"><h3>{selected.kind === "question" ? "Context and why it matters" : "Notes and next step"}</h3><p className={selected.notes ? "preserve-whitespace" : "muted"}>{selected.notes || "No notes added."}</p></section>
        {(selected.recurrence || selected.due_at || selected.last_occurrence || selected.completion_requirement) && <div className="agenda-detail-schedule">
          {selected.completion_requirement === "interaction_today" && <p className="muted">Requires an interaction today</p>}
          {selected.completion_requirement === "morning_plan" && <p className="muted">Requires 2 work items, 2 interactions, 1 other item and the morning feed</p>}
          {selected.recurrence && <p className="muted">{formatRecurrence(selected.recurrence)} · {selected.status === "waiting" ? "Paused" : selected.status === "open" ? "Active" : "Stopped"}</p>}
          {selected.due_at && <p className="muted">{selected.kind === "question" ? "Next ask" : selected.recurrence ? "Next occurrence" : "Next check"}: {formatTimestamp(selected.due_at)}</p>}
          {selected.last_occurrence && <p className="muted">Last occurrence: {formatTimestamp(selected.last_occurrence.scheduled_at)} · {selected.last_occurrence.status}{selected.last_occurrence.retry_at ? ` · Retry: ${formatTimestamp(selected.last_occurrence.retry_at)}` : ""}</p>}
          {selected.recurrence && <AgendaOccurrenceHistory key={`${selected.id}:${selected.revision}`} item={selected} />}
        </div>}
        {error && <p className="error-text" role="alert">{error}</p>}
        <div className="agenda-detail-actions button-row">
          <button type="button" onClick={() => setEditing(selected)} disabled={working}>Edit {selected.kind}</button>
          {selected.recurrence && selected.status === "open" && <button className="secondary" type="button" disabled={working || selected.last_occurrence?.status === "queued"} onClick={() => mutate(async () => {
            const result = await api.runAssistantAgendaItem(selected.id, selected.revision);
            if (result.status === "busy") throw new Error("The Assistant is busy. Run this task again after the current turn finishes.");
            if (result.status === "disabled") throw new Error("Enable proactive Assistant in settings before running this task.");
          })}>Run now</button>}
          {selected.recurrence && (selected.status === "open" || selected.status === "waiting") && <button className="secondary" type="button" disabled={working} onClick={() => mutate(() => api.updateAssistantAgendaItem(selected.id, { expected_revision: selected.revision, status: selected.status === "open" ? "waiting" : "open" }))}>{selected.status === "open" ? "Pause schedule" : "Resume schedule"}</button>}
          <button className="secondary" type="button" disabled={working} onClick={() => mutate(() => api.updateAssistantAgendaItem(selected.id, { expected_revision: selected.revision, status: selected.status === "done" || selected.status === "dismissed" ? "open" : "done" }))}>{selected.status === "done" || selected.status === "dismissed" ? "Reopen" : selected.kind === "question" ? "Resolve" : selected.recurrence ? "End recurring task" : "Complete"}</button>
          {(selected.status === "open" || selected.status === "waiting") && <button className="secondary" type="button" disabled={working} onClick={() => mutate(() => api.updateAssistantAgendaItem(selected.id, { expected_revision: selected.revision, status: "dismissed" }))}>Dismiss</button>}
          <button className="secondary agenda-delete-action" type="button" disabled={working} onClick={() => setDeleting(selected)} aria-label={`Delete ${selected.kind}: ${selected.title}`}>Delete</button>
        </div>
      </div>
    </Modal>}
    {editing !== null && <Modal title={`${editing === "new" ? "New" : "Edit"} ${kind}`} className="agenda-detail-modal" onClose={() => { if (!working) setEditing(null); }}><AssistantAgendaEditor key={editing === "new" ? "new" : editing.id} item={editing === "new" ? null : editing} kind={kind} disabled={working} onCancel={() => setEditing(null)} onSave={async (payload) => {
      setWorking(true); setError(null);
      try {
        if (editing === "new") await api.createAssistantAgendaItem(payload);
        else await api.updateAssistantAgendaItem(editing.id, { ...payload, expected_revision: editing.revision });
        await onChange(); setEditing(null);
      } finally { setWorking(false); }
    }} /></Modal>}
    {deleting && <Modal title={`Delete this ${deleting.kind}?`} className="permission-modal" onClose={() => { if (!working) setDeleting(null); }}>
      <p>{deleting.title}</p><p className="muted">This removes the item from the Assistant agenda.</p>
      {error && <p className="error-text" role="alert">{error}</p>}
      <div className="button-row"><button type="button" disabled={working} onClick={() => mutate(async () => { await api.deleteAssistantAgendaItem(deleting.id, deleting.revision); setDeleting(null); setSelectedId(null); })}>Delete item</button><button className="secondary" type="button" disabled={working} onClick={() => setDeleting(null)}>Cancel</button></div>
    </Modal>}
  </section>;
}

function AgendaOccurrenceHistory({ item }: { item: AssistantAgendaItem }) {
  const [expanded, setExpanded] = useState(false);
  const [occurrences, setOccurrences] = useState<AssistantAgendaOccurrence[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function toggle() {
    if (expanded) {
      setExpanded(false);
      return;
    }
    setExpanded(true);
    if (loaded || loading) return;
    setLoading(true);
    setError(null);
    try {
      setOccurrences(await api.listAssistantAgendaOccurrences(item.id));
      setLoaded(true);
    } catch (reason) {
      setError(errorMessage(reason, "Could not load occurrence history"));
    } finally {
      setLoading(false);
    }
  }

  const pastOccurrences = occurrences.filter((occurrence) => occurrence.status !== "queued");
  return <div className="agenda-occurrence-history">
    <button className="secondary compact" type="button" aria-label={`${expanded ? "Hide" : "Show"} occurrence history for ${item.title}`} aria-expanded={expanded} disabled={loading} onClick={() => void toggle()}>
      {expanded ? "Hide occurrence history" : "Show occurrence history"}
    </button>
    {expanded && <div>
      {loading && <p className="muted" role="status">Loading occurrence history…</p>}
      {error && <p className="error-text" role="alert">{error}</p>}
      {loaded && !pastOccurrences.length && <p className="muted">No past occurrences yet.</p>}
      {pastOccurrences.length > 0 && <ul aria-label={`Past occurrences for ${item.title}`}>
        {pastOccurrences.map((occurrence) => <li key={occurrence.id}>
          Scheduled {formatTimestamp(occurrence.scheduled_at)} · {occurrence.status}
          {occurrence.completed_at ? ` · Completed ${formatTimestamp(occurrence.completed_at)}` : ""}
          {occurrence.retry_at ? ` · Retry ${formatTimestamp(occurrence.retry_at)}` : ""}
        </li>)}
      </ul>}
    </div>}
  </div>;
}

function isPastAgendaItem(item: AssistantAgendaItem) {
  return item.status === "done" || item.status === "dismissed" || Boolean(item.recurrence && item.last_occurrence);
}

function AssistantAgendaEditor({ item, kind, disabled, onSave, onCancel }: {
  item: AssistantAgendaItem | null;
  kind: AssistantAgendaItem["kind"];
  disabled: boolean;
  onSave: (payload: AssistantAgendaInput) => Promise<void>;
  onCancel: () => void;
}) {
  const [draft, setDraft] = useState<AssistantAgendaInput>(item ?? { kind, question_category: "common", title: "", notes: "", status: "open", due_at: null });
  const [due, setDue] = useState(item?.due_at ? localDateTime(item.due_at) : "");
  const [recurring, setRecurring] = useState(Boolean(item?.recurrence));
  const [recurrence, setRecurrence] = useState<AssistantAgendaRecurrence>(item?.recurrence ?? { frequency: "daily", time: "09:00", timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, weekdays: [0], day_of_month: 1 });
  const [error, setError] = useState<string | null>(null);
  const hasRecurrence = draft.kind === "task" && recurring;
  return <form className="form-panel stack" aria-label="Agenda item editor" onSubmit={async (event) => {
    event.preventDefault(); setError(null);
    try {
      if (hasRecurrence) {
        try { new Intl.DateTimeFormat(undefined, { timeZone: recurrence.timezone }); }
        catch { throw new Error("Enter a valid IANA timezone, such as America/Toronto."); }
        if (recurrence.frequency === "weekly" && !recurrence.weekdays?.length) throw new Error("Choose at least one weekday.");
      }
      const date = !hasRecurrence && due ? new Date(due) : null;
      if (date && (Number.isNaN(date.getTime()) || localDateTime(date.toISOString()) !== due)) throw new Error("Choose a valid local date and time.");
      await onSave({ kind: draft.kind, question_category: draft.kind === "question" ? draft.question_category : "common", title: draft.title.trim(), notes: draft.notes, status: draft.status, ...(hasRecurrence ? { recurrence: { frequency: recurrence.frequency, time: recurrence.time, timezone: recurrence.timezone.trim(), ...(recurrence.frequency === "weekly" ? { weekdays: recurrence.weekdays } : {}), ...(recurrence.frequency === "monthly" ? { day_of_month: recurrence.day_of_month ?? 1 } : {}) } } : { due_at: date?.toISOString() ?? null, ...(item?.recurrence ? { recurrence: null } : {}) }) });
    } catch (reason) { setError(errorMessage(reason, "Could not save this item. Your changes are preserved.")); }
  }}>
    <div className="form-grid">
      <label>Title<input required maxLength={200} value={draft.title} onChange={(event) => setDraft({ ...draft, title: event.target.value })} /></label>
      <label>Kind<select value={draft.kind} onChange={(event) => setDraft({ ...draft, kind: event.target.value as AssistantAgendaItem["kind"] })}><option value="task">Task</option><option value="question">Question</option></select></label>
      {draft.kind === "question" && <label>Question category<select value={draft.question_category} onChange={(event) => setDraft({ ...draft, question_category: event.target.value as AssistantQuestionCategory })}><option value="common">Common</option><option value="important">Important</option><option value="temporal">Temporal</option></select></label>}
      <label>Status<select value={draft.status} onChange={(event) => setDraft({ ...draft, status: event.target.value as AssistantAgendaItem["status"] })}><option value="open">Open</option><option value="waiting">Waiting</option><option value="done">Done</option><option value="dismissed">Dismissed</option></select></label>
      {draft.kind === "task" && <label className="checkbox-row"><input type="checkbox" checked={recurring} onChange={(event) => setRecurring(event.target.checked)} />Repeat this task</label>}
      {hasRecurrence ? <>
        <label>Repeat<select value={recurrence.frequency} onChange={(event) => setRecurrence({ ...recurrence, frequency: event.target.value as AssistantAgendaRecurrence["frequency"] })}><option value="daily">Daily</option><option value="weekly">Weekly</option><option value="monthly">Monthly</option></select></label>
        <label>Scheduled time<input type="time" required value={recurrence.time} onChange={(event) => setRecurrence({ ...recurrence, time: event.target.value })} /></label>
        <label>Schedule timezone<input required value={recurrence.timezone} onChange={(event) => setRecurrence({ ...recurrence, timezone: event.target.value })} /></label>
        {recurrence.frequency === "weekly" && <fieldset><legend>Weekdays</legend>{weekdays.map((day, index) => <label className="checkbox-row" key={day}><input type="checkbox" checked={recurrence.weekdays?.includes(index) ?? false} onChange={(event) => setRecurrence({ ...recurrence, weekdays: event.target.checked ? [...(recurrence.weekdays ?? []), index].sort() : recurrence.weekdays?.filter((value) => value !== index) })} />{day}</label>)}</fieldset>}
        {recurrence.frequency === "monthly" && <label>Day of month<input type="number" required min={1} max={31} step={1} value={recurrence.day_of_month ?? 1} onChange={(event) => setRecurrence({ ...recurrence, day_of_month: Number(event.target.value) })} /></label>}
      </> : <label>{draft.kind === "question" ? "Ask at" : "Next check"} ({Intl.DateTimeFormat().resolvedOptions().timeZone})<input type="datetime-local" value={due} onChange={(event) => setDue(event.target.value)} /></label>}
    </div>
    {hasRecurrence && recurrence.frequency === "monthly" && <p className="muted">Months without the selected day are skipped.</p>}
    {hasRecurrence && <p className="muted">Only the most recently missed occurrence is recovered after time offline. Waiting pauses the schedule; Done or Dismissed stops it.</p>}
    <label>{draft.kind === "question" ? "Context and why it matters" : "Notes and next step"}<textarea rows={3} maxLength={12000} value={draft.notes} onChange={(event) => setDraft({ ...draft, notes: event.target.value })} /></label>
    {error && <p className="error-text" role="alert">{error}</p>}
    <div className="button-row"><button type="submit" disabled={disabled || !draft.title.trim()}>Save item</button><button type="button" className="secondary" disabled={disabled} onClick={onCancel}>Cancel edit</button></div>
  </form>;
}

function localDateTime(value: string): string {
  const date = parseBackendDateTime(value);
  const pad = (part: number) => String(part).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function questionCategoryLabel(category: AssistantQuestionCategory) {
  return category.charAt(0).toUpperCase() + category.slice(1);
}

function errorMessage(reason: unknown, fallback: string) { return reason instanceof Error ? reason.message : fallback; }
function formatTimestamp(value: string) { return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(parseBackendDateTime(value)); }

const weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
function formatRecurrence(recurrence: AssistantAgendaRecurrence) {
  const frequency = recurrence.frequency === "weekly" ? `Weekly on ${(recurrence.weekdays ?? []).map((day) => weekdays[day]).join(", ")}` : recurrence.frequency === "monthly" ? `Monthly on day ${recurrence.day_of_month}` : "Daily";
  return `${frequency} at ${recurrence.time} (${recurrence.timezone})`;
}
