const form = document.querySelector('#note-form');
const content = document.querySelector('#content');
const notes = document.querySelector('#notes');
const message = document.querySelector('#message');

function noteElement(note) {
  const article = document.createElement('article');
  article.className = 'note';
  const text = document.createElement('p');
  text.textContent = note.content;
  const footer = document.createElement('footer');
  const date = document.createElement('time');
  date.dateTime = note.created_at;
  date.textContent = new Date(note.created_at).toLocaleString();
  const remove = document.createElement('button');
  remove.className = 'delete'; remove.type = 'button'; remove.textContent = 'Delete';
  remove.addEventListener('click', async () => {
    if (!confirm('Delete this note?')) return;
    const response = await fetch(`/api/notes/${note.id}`, {method: 'DELETE'});
    if (response.ok) await loadNotes(); else message.textContent = 'Could not delete that note.';
  });
  footer.append(date, remove); article.append(text, footer); return article;
}

async function loadNotes() {
  const response = await fetch('/api/notes');
  if (!response.ok) { message.textContent = 'Could not load notes.'; return; }
  const data = await response.json();
  notes.replaceChildren(...(data.notes.length ? data.notes.map(noteElement) : [Object.assign(document.createElement('div'), {className: 'empty', textContent: 'No notes yet.'})]));
}

form.addEventListener('submit', async (event) => {
  event.preventDefault(); message.textContent = '';
  const response = await fetch('/api/notes', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({content: content.value})});
  const data = await response.json();
  if (!response.ok) { message.textContent = data.error || 'Could not save note.'; return; }
  form.reset(); message.textContent = 'Note saved.'; content.focus(); await loadNotes();
});

loadNotes();
