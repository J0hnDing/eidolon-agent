# Personal Agent

A local-first, single-user assistant that combines direct chat, explicit editable memory, and controlled reusable skill generation. Generated skills are validated, permission-reviewed, versioned, and run through the backend's sandbox and operation-lock boundaries.

Start with [AGENTS.md](AGENTS.md) for repository guardrails and [docs/README.md](docs/README.md) for the detailed architecture and behavior index.

## Development

Backend:

```powershell
cd backend
..\.venv\Scripts\python.exe -m pytest
```

Frontend:

```powershell
cd frontend
npm run build
```

The backend is FastAPI/SQLite/SQLAlchemy; the frontend is React/Vite/TypeScript. See the topic documentation before changing workflow, permission, lifecycle, runner, scheduling, or Codex integration behavior.
