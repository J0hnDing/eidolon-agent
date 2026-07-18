# Sandbox Execution

Installed skills execute through a runtime-specific controlled runner. Function skills use bounded runs. Web applications use persistent version-pinned instances behind the trusted gateway.

## Runner Selection

`PERSONAL_AGENT_RUNNER_MODE` controls runner mode:

```text
auto, docker, local, dev
```

`auto` and `docker` prefer Docker. If Docker is unavailable, runs are blocked unless local/dev mode is explicitly configured.

## Docker Runner

The trusted Docker image supports both protocols. Bounded function runs use a disposable container. Web applications use a named persistent container until stop, failure, idle cleanup, version invalidation, or backend shutdown. The platform:

- uses a trusted app Dockerfile: `backend/docker/skill-runner.Dockerfile`;
- auto-builds the trusted image when missing or outdated;
- runs each skill in a disposable container;
- mounts the skill folder read-only;
- mounts a per-skill cache folder writable at `/skill/cache`;
- enforces timeout, CPU, and memory limits;
- uses a read-only root filesystem, bounded tmpfs, PID limit, dropped Linux capabilities, and `no-new-privileges` for web applications;
- captures stdout, stderr, exit code, start/end time;
- requires JSON stdout for successful bounded function runs; web applications use the declared ASGI protocol instead.
- exposes `PERSONAL_AGENT_SKILL_ID` and `PERSONAL_AGENT_BACKEND_URL` so approved skill code can call backend APIs.

Web applications additionally receive a platform-owned import entrypoint and an instance-scoped capability in the server process environment. Generated code uses the trusted capability helper; browser code never receives that token. The gateway reaches an ephemeral loopback-published port and waits for the trusted readiness endpoint before embedding.

Generated skills cannot provide Dockerfiles, image names, build contexts, or build args.

Skill entrypoints have a default 120-second execution timeout, configurable with `PERSONAL_AGENT_SKILL_TIMEOUT_SECONDS`. Generated network or backend-Codex workflows must budget retrieval, bounded Codex calls, cache writes, and graceful error output within that limit. Multi-item Codex work should use one batched backend request rather than sequential per-item calls. Individual backend-Codex caller timeouts should remain bounded below the full entrypoint timeout so the skill can persist partial results and exit cleanly.

## Network Limitation

Application ingress does not grant internet egress. Every Docker web application starts on a private internal network behind a platform-owned relay container, which has no package mount or capability token and exposes only the exact Codex-capability backend route to the application. Without approved domains the application remains internal-only. Approved domains attach Docker bridge as a secondary application egress network while ingress and capabilities remain relayed; common host aliases are suppressed, but domain-level and direct-IP/private-network filtering is not implemented yet.

Wildcard or unrestricted network remains blocked.

Backend-mediated Codex calls use the same runtime permission boundary: call/response is allowed by default, while Codex internet access is allowed only when the skill has approved runtime network domains.

Backend-mediated Function calls use ephemeral capabilities derived from the currently running function or current web-application instance. No-internet Docker functions use a transient internal network and an allowlisted relay that forwards only Function registry discovery/invocation requests; the relay does not grant general internet access. The entrypoint receives the token, but its pre-run tests do not.

Browser-side web-app traffic is a separate boundary and is blocked from external domains entirely. The gateway CSP permits same-origin application routes only; HTML/CSS/JavaScript literal absolute URLs also fail static validation.

## Local Runner

The local runner remains available only for explicit development fallback and is reported as `local_dev`. Web applications bind loopback and use the controlled cache parent as their working directory, but local mode does not enforce Docker filesystem, process, resource, or network isolation and should not be treated as production-safe.

## Run Preconditions

Bounded function runs and web-app opens require:

- installed status;
- enabled skill;
- runtime permissions approved;
- runtime permissions supported;
- operation lock available for the short transition;
- tests pass before task execution.

Proposed skills cannot run.

Persistent web applications also require an active version, bounded readiness, and the isolated gateway session. Idle service lifetime does not hold an operation lock and is not stored as a `SkillRun`. See [Sandboxed web applications](../runtime/web_applications.md).
