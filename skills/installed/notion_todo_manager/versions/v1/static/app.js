(() => {
  "use strict";

  const list = document.getElementById("todo-list");
  const status = document.getElementById("status");
  const count = document.getElementById("count");
  const nextButton = document.getElementById("next-button");
  let nextCursor = null;
  let todos = [];

  function setStatus(message, isError = false) {
    status.textContent = message;
    status.className = isError ? "status error-text" : "status";
  }

  function valueOrNull(form, name) {
    const value = new FormData(form).get(name);
    return value === "" || value === null ? null : value;
  }

  function todoPayload(form) {
    const estimated = valueOrNull(form, "estimated_minutes");
    return { title: valueOrNull(form, "title"), priority: valueOrNull(form, "priority"), start_at: valueOrNull(form, "start_at"), due_at: valueOrNull(form, "due_at"), estimated_minutes: estimated === null ? null : Number(estimated), atlas_goal_id: valueOrNull(form, "atlas_goal_id"), notes: valueOrNull(form, "notes") };
  }

  async function request(url, options = {}) {
    const response = await fetch(url, { headers: { "content-type": "application/json" }, ...options });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.message || "The request could not be completed.");
    return body;
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[character]));
  }

  function field(label, name, value, type = "text", wide = false) {
    const safeValue = value === null || value === undefined ? "" : String(value);
    const wideClass = wide ? " wide" : "";
    if (name === "priority") return `<label class="field${wideClass}">${label}<select name="priority"><option value="">None</option><option value="low" ${safeValue === "low" ? "selected" : ""}>Low</option><option value="medium" ${safeValue === "medium" ? "selected" : ""}>Medium</option><option value="high" ${safeValue === "high" ? "selected" : ""}>High</option></select></label>`;
    if (name === "notes") return `<label class="field${wideClass}">${label}<textarea name="notes" maxlength="2000" rows="2">${escapeHtml(safeValue)}</textarea></label>`;
    return `<label class="field${wideClass}">${label}<input name="${name}" type="${type}" value="${escapeHtml(safeValue)}" ${name === "title" ? "required maxlength=\"2000\"" : ""}></label>`;
  }

  function renderTodos() {
    if (!todos.length) {
      list.innerHTML = '<div class="empty">No todos yet. Add the first one above.</div>';
      count.textContent = "0 todos";
      return;
    }
    count.textContent = `${todos.length} shown`;
    list.innerHTML = todos.map((todo) => {
      const priority = todo.priority ? `<span class="badge ${todo.priority === "high" ? "high" : ""}">${escapeHtml(todo.priority)}</span>` : "";
      const details = [todo.due_at ? `Due ${escapeHtml(todo.due_at)}` : null, todo.estimated_minutes ? `${todo.estimated_minutes} min` : null, todo.created_at ? `Created ${escapeHtml(todo.created_at)}` : null].filter(Boolean).join(" · ");
      return `<article class="todo-card" data-id="${escapeHtml(todo.id)}"><div class="todo-summary"><div><h3 class="todo-title">${escapeHtml(todo.title)}</h3><div class="meta">${priority}<span>${details}</span></div></div><div class="actions"><button class="text-button edit-button" type="button">Edit</button><button class="text-button danger delete-button" type="button">Delete</button></div></div></article>`;
    }).join("");
    list.querySelectorAll(".edit-button").forEach((button, index) => button.addEventListener("click", () => openEditor(todos[index])));
    list.querySelectorAll(".delete-button").forEach((button, index) => button.addEventListener("click", () => deleteTodo(todos[index])));
  }

  function openEditor(todo) {
    const card = list.querySelector(`[data-id="${CSS.escape(todo.id)}"]`);
    card.classList.add("editing");
    card.insertAdjacentHTML("beforeend", `<form class="edit-form">${field("Title", "title", todo.title, "text", true)}${field("Priority", "priority", todo.priority)}${field("Start date/time", "start_at", todo.start_at)}${field("Due date/time", "due_at", todo.due_at)}${field("Estimate (minutes)", "estimated_minutes", todo.estimated_minutes, "number")}${field("Atlas goal reference", "atlas_goal_id", todo.atlas_goal_id, "text", true)}${field("Notes", "notes", todo.notes, "text", true)}<div class="actions"><button class="button primary" type="submit">Save changes</button><button class="button secondary cancel-button" type="button">Cancel</button></div></form>`);
    const form = card.querySelector(".edit-form");
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      await mutate(`Updating “${todo.title}”…`, `/api/todos/${encodeURIComponent(todo.id)}`, { method: "PATCH", body: JSON.stringify(todoPayload(form)) }, "Todo updated.");
    });
    form.querySelector(".cancel-button").addEventListener("click", () => form.remove());
  }

  async function deleteTodo(todo) {
    if (!window.confirm("Delete this todo? It will be moved to Notion trash, not permanently deleted.")) return;
    await mutate("Deleting todo…", `/api/todos/${encodeURIComponent(todo.id)}`, { method: "DELETE" }, "Todo moved to Notion trash.");
  }

  async function mutate(progress, url, options, success) {
    setStatus(progress);
    try {
      await request(url, options);
      setStatus(success);
      await loadTodos();
      return true;
    } catch (error) {
      setStatus(error.message, true);
      return false;
    }
  }

  async function loadTodos(cursor = null) {
    if (!cursor) {
      list.innerHTML = '<div class="loading">Loading todos…</div>';
      todos = [];
    }
    const query = new URLSearchParams({ page_size: "25" });
    if (cursor) query.set("start_cursor", cursor);
    try {
      const body = await request(`/api/todos?${query.toString()}`);
      todos = cursor ? todos.concat(body.todos || []) : (body.todos || []);
      renderTodos();
      nextCursor = body.has_more ? body.next_cursor : null;
      nextButton.classList.toggle("hidden", !nextCursor);
    } catch (error) {
      list.innerHTML = `<div class="error">${escapeHtml(error.message)} <button class="text-button" id="retry-button" type="button">Try again</button></div>`;
      document.getElementById("retry-button").addEventListener("click", () => loadTodos());
      setStatus("Could not load todos.", true);
    }
  }

  document.getElementById("create-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    if (await mutate("Adding todo…", "/api/todos", { method: "POST", body: JSON.stringify(todoPayload(form)) }, "Todo added.")) form.reset();
  });
  document.getElementById("refresh-button").addEventListener("click", () => loadTodos());
  nextButton.addEventListener("click", () => loadTodos(nextCursor));
  loadTodos();
})();
