# Docker Skill Sandbox

This is a short compatibility page for the Milestone 8 Docker runner notes. The main sandbox documentation now lives in:

- [Sandbox Execution](security/sandbox_execution.md)
- [Permissions](security/permissions.md)

## Current Behavior

Installed function runs and persistent web-application instances use the selected runner mode:

```powershell
$env:PERSONAL_AGENT_RUNNER_MODE = "auto"   # auto, docker, local, or dev
$env:PERSONAL_AGENT_DOCKER_IMAGE = "personal-agent-skill-runner:latest"
```

`auto` and `docker` select the Docker sandbox. If Docker is unavailable, runs are blocked unless local/dev mode is explicitly selected.

The Docker runner uses the trusted image from `backend/docker/skill-runner.Dockerfile`. The backend can build that image automatically when it is missing or outdated. Generated skills cannot provide Dockerfiles, image names, build contexts, or build args.

## Restrictions

Each bounded function run uses a disposable container with:

- the skill folder mounted read-only at `/skill`;
- a per-skill cache mounted writable at `/skill/cache`;
- JSON input through stdin;
- stdout, stderr, exit-code, timeout, CPU, and memory capture;
- no project root, user home, or app secrets mounted.

Network mode is permission-dependent:

- no declared network domains: Docker runs with `--network none`;
- approved explicit network domains: Docker runs with container network enabled.

Important limitation: approved domains are currently a policy record, not a Docker egress firewall. Domain-level network enforcement is not implemented yet.

Web applications use the same trusted image but run in a version-pinned persistent container with a read-only root, tmpfs, PID/resource limits, dropped capabilities, loopback gateway ingress, and a separate application instance/session lifecycle. The application always starts on an internal network; a separate trusted relay container bridges loopback ingress and the single scoped backend capability without mounting skill code. Without approved domains the application has no other network. Approved domains attach bridge as a secondary egress network with the documented domain/direct-IP enforcement limitation. See [Sandboxed web applications](runtime/web_applications.md).

## Manual Verification

1. Start the backend with `PERSONAL_AGENT_RUNNER_MODE=auto` or `docker`.
2. Confirm Docker is running.
3. Open an installed, enabled skill with approved runtime permissions.
4. Run a function from Skill Detail or open a web application from Applications.
5. Inspect bounded run history or web-application lifecycle/audit diagnostics and sandbox state.
