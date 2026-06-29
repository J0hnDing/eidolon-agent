# Docker Skill Sandbox

Milestone 8 adds a Docker runner for installed automation and hybrid skills.

## Trusted Runtime Image

The backend automatically builds the trusted runner image when it is missing or when `backend/docker/skill-runner.Dockerfile` changes. The fixed build context is the project root. Generated skills cannot provide Dockerfiles, build contexts, image names, or build args.

Build metadata and the latest build log are stored in `runtime/docker_runner_build.json`.

The image is based on `python:3.12-slim` and includes `pytest` so the runner can test a skill before executing its entrypoint. The manual equivalent is:

```powershell
docker build -f backend/docker/skill-runner.Dockerfile -t personal-agent-skill-runner:latest .
```

## Runner Mode

The backend reads these environment variables:

```powershell
$env:PERSONAL_AGENT_RUNNER_MODE = "auto"   # auto, docker, local, or dev
$env:PERSONAL_AGENT_DOCKER_IMAGE = "personal-agent-skill-runner:latest"
$env:PERSONAL_AGENT_DOCKER_MEMORY = "256m"
$env:PERSONAL_AGENT_DOCKER_CPUS = "1.0"
$env:PERSONAL_AGENT_SKILL_TIMEOUT_SECONDS = "10"
```

`auto` and `docker` select the Docker sandbox. If Docker is unavailable, skill runs are blocked with a clear error. The local subprocess runner is only used when `PERSONAL_AGENT_RUNNER_MODE` is explicitly set to `local` or `dev`.

## Sandbox Restrictions

Each run uses a disposable container with:

- `--rm`
- `--network none`
- memory and CPU limits
- the skill folder mounted read-only at `/skill`
- a per-skill cache folder mounted writable at `/skill/cache`
- JSON input passed through stdin
- stdout/stderr/exit-code capture
- timeout enforcement

The runner does not mount the project root, user home, or application secrets.

## Runtime Permissions

The current runner only supports:

```json
{
  "network": [],
  "filesystem_read": [],
  "filesystem_write": [] ,
  "secrets": [],
  "shell": false
}
```

`filesystem_write` may also be `["./cache"]`.

Network domains, filesystem reads, secrets, shell access, absolute paths, and parent traversal are blocked. Runtime network domains may be approved as future design intent, but execution remains blocked until domain-level network sandboxing exists.

## Manual Verification

1. Start the backend with `PERSONAL_AGENT_RUNNER_MODE=auto` or `docker`.
2. Register or install a low-risk automation skill with runtime permissions approved.
3. Open the skill detail page and confirm the Runner / Sandbox panel shows Docker and image status.
4. Click Run. If the image is missing or outdated, the backend builds it automatically before tests run.
5. Inspect the run history, stdout, stderr, and output JSON.

For development only, set `PERSONAL_AGENT_RUNNER_MODE=local` to use the older subprocess runner.
