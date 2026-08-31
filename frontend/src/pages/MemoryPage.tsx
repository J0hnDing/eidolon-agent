import { FormEvent, useEffect, useMemo, useState } from "react";

import { MemoryCategory, MemoryFact, MemoryFactInput, api } from "../api/client";

const categories: MemoryCategory[] = [
  "interests",
  "goals",
  "preferences",
  "routines",
  "trusted_sources",
  "blocked_sources",
  "writing_style",
  "risk_tolerance",
];

const emptyForm: MemoryFactInput = {
  key: "",
  value: "",
  category: "interests",
  source_message_id: null,
  sensitivity: "normal",
  expires_at: null,
  user_editable: true,
};

export default function MemoryPage() {
  const [facts, setFacts] = useState<MemoryFact[]>([]);
  const [form, setForm] = useState<MemoryFactInput>(emptyForm);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const editingFact = useMemo(
    () => facts.find((fact) => fact.id === editingId) ?? null,
    [facts, editingId],
  );

  useEffect(() => {
    loadFacts();
  }, []);

  async function loadFacts() {
    setIsLoading(true);
    setError(null);
    try {
      setFacts(await api.listMemoryFacts());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load memory facts");
    } finally {
      setIsLoading(false);
    }
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    try {
      if (editingId === null) {
        await api.createMemoryFact(form);
      } else {
        await api.updateMemoryFact(editingId, form);
      }
      setForm(emptyForm);
      setEditingId(null);
      await loadFacts();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save memory fact");
    }
  }

  async function handleDelete(id: number) {
    setError(null);
    try {
      await api.deleteMemoryFact(id);
      if (editingId === id) {
        setEditingId(null);
        setForm(emptyForm);
      }
      await loadFacts();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not delete memory fact");
    }
  }

  function startEdit(fact: MemoryFact) {
    setEditingId(fact.id);
    setForm({
      key: fact.key,
      value: fact.value,
      category: fact.category,
      source_message_id: fact.source_message_id,
      sensitivity: fact.sensitivity,
      expires_at: fact.expires_at,
      user_editable: fact.user_editable,
    });
  }

  return (
    <section className="page stack">
      <header className="page-header">
        <div>
          <h1>Memory</h1>
          <p className="muted">Keep the context Eidolon uses explicit, focused, and under your control.</p>
        </div>
      </header>

      <form className="form-panel" onSubmit={handleSubmit}>
        <h2>{editingFact ? `Editing ${editingFact.key}` : "Add Memory Fact"}</h2>
        <div className="form-grid">
          <label>
            Key
            <input
              value={form.key}
              onChange={(event) => setForm({ ...form, key: event.target.value })}
              required
              maxLength={128}
            />
          </label>
          <label>
            Category
            <select
              value={form.category}
              onChange={(event) =>
                setForm({ ...form, category: event.target.value as MemoryCategory })
              }
            >
              {categories.map((category) => (
                <option key={category} value={category}>
                  {category}
                </option>
              ))}
            </select>
          </label>
          <label>
            Sensitivity
            <input
              value={form.sensitivity}
              onChange={(event) => setForm({ ...form, sensitivity: event.target.value })}
              required
              maxLength={32}
            />
          </label>
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={form.user_editable}
              onChange={(event) => setForm({ ...form, user_editable: event.target.checked })}
            />
            User editable
          </label>
        </div>
        <label>
          Value
          <textarea
            value={form.value}
            onChange={(event) => setForm({ ...form, value: event.target.value })}
            required
            rows={4}
          />
        </label>
        <div className="button-row">
          <button type="submit">{editingId === null ? "Create" : "Save"}</button>
          {editingId !== null && (
            <button
              type="button"
              className="secondary"
              onClick={() => {
                setEditingId(null);
                setForm(emptyForm);
              }}
            >
              Cancel
            </button>
          )}
        </div>
      </form>

      {error && <p className="error-text">{error}</p>}
      {isLoading ? (
        <p className="muted">Loading memory facts...</p>
      ) : (
        <div className="item-grid">
          {facts.map((fact) => (
            <article key={fact.id} className="item-card">
              <div className="item-card-header">
                <div>
                  <h3>{fact.key}</h3>
                  <p>{fact.category}</p>
                </div>
                <span className="badge">{fact.sensitivity}</span>
              </div>
              <p>{fact.value}</p>
              <div className="button-row">
                <button type="button" className="secondary" onClick={() => startEdit(fact)}>
                  Edit
                </button>
                <button type="button" className="danger" onClick={() => handleDelete(fact.id)}>
                  Delete
                </button>
              </div>
            </article>
          ))}
          {facts.length === 0 && <p className="muted">No memory facts yet.</p>}
        </div>
      )}
    </section>
  );
}
