const state = {
  notes: [],
  selectedId: null,
};

const elements = {
  count: document.querySelector("#note-count"),
  list: document.querySelector("#notes-list"),
  form: document.querySelector("#note-form"),
  title: document.querySelector("#title"),
  body: document.querySelector("#body"),
  createdAt: document.querySelector("#created-at"),
  updatedAt: document.querySelector("#updated-at"),
  status: document.querySelector("#status"),
  newNote: document.querySelector("#new-note"),
  deleteNote: document.querySelector("#delete-note"),
};

function formatDate(value) {
  if (!value) return "-";
  return new Date(value).toLocaleString();
}

function setStatus(message) {
  elements.status.textContent = message;
}

async function requestJson(path, options = {}) {
  const response = await fetch(path, {
    headers: { "content-type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || "Request failed");
  }
  return payload;
}

function renderList() {
  elements.count.textContent = `${state.notes.length} ${
    state.notes.length === 1 ? "note" : "notes"
  }`;
  elements.list.innerHTML = "";

  if (state.notes.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "No notes yet.";
    elements.list.append(empty);
    return;
  }

  for (const note of state.notes) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = `note-row${note.id === state.selectedId ? " active" : ""}`;
    row.dataset.id = note.id;

    const title = document.createElement("strong");
    title.textContent = note.title || "Untitled note";
    const meta = document.createElement("span");
    meta.textContent = `Updated ${formatDate(note.updated_at)}`;

    row.append(title, meta);
    row.addEventListener("click", () => selectNote(note.id));
    elements.list.append(row);
  }
}

function showNote(note) {
  state.selectedId = note ? note.id : null;
  elements.title.value = note ? note.title : "";
  elements.body.value = note ? note.body : "";
  elements.createdAt.textContent = `Created: ${formatDate(note?.created_at)}`;
  elements.updatedAt.textContent = `Updated: ${formatDate(note?.updated_at)}`;
  elements.deleteNote.disabled = !note;
  renderList();
}

async function loadNotes() {
  const payload = await requestJson("/api/notes");
  state.notes = payload.notes;
  if (state.selectedId) {
    const selected = state.notes.find((note) => note.id === state.selectedId);
    if (selected) {
      showNote(selected);
      return;
    }
  }
  showNote(state.notes[0] || null);
}

async function selectNote(id) {
  const payload = await requestJson(`/api/notes/${id}`);
  showNote(payload.note);
  setStatus("");
}

elements.newNote.addEventListener("click", () => {
  showNote(null);
  elements.title.focus();
  setStatus("Drafting a new note.");
});

elements.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  setStatus("Saving...");

  const body = JSON.stringify({
    title: elements.title.value,
    body: elements.body.value,
  });
  const path = state.selectedId ? `/api/notes/${state.selectedId}` : "/api/notes";
  const method = state.selectedId ? "PUT" : "POST";

  try {
    const payload = await requestJson(path, { method, body });
    state.selectedId = payload.note.id;
    await loadNotes();
    setStatus("Saved.");
  } catch (error) {
    setStatus(error.message);
  }
});

elements.deleteNote.addEventListener("click", async () => {
  if (!state.selectedId) return;
  const confirmed = window.confirm("Delete this note?");
  if (!confirmed) return;

  try {
    await requestJson(`/api/notes/${state.selectedId}`, { method: "DELETE" });
    state.selectedId = null;
    await loadNotes();
    setStatus("Deleted.");
  } catch (error) {
    setStatus(error.message);
  }
});

loadNotes().catch((error) => setStatus(error.message));
