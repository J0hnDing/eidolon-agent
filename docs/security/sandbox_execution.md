# Sandbox Execution

Installed automation and hybrid skills execute through the selected skill runner.

## Runner Selection

`PERSONAL_AGENT_RUNNER_MODE` controls runner mode:

```text
auto, docker, local, dev
```

`auto` and `docker` prefer Docker. If Docker is unavailable, runs are blocked unless local/dev mode is explicitly configured.

## Docker Runner

The Docker runner:

- uses a trusted app Dockerfile: `backend/docker/skill-runner.Dockerfile`;
- auto-builds the trusted image when missing or outdated;
- runs each skill in a disposable container;
- mounts the skill folder read-only;
- mounts a per-skill cache folder writable at `/skill/cache`;
- enforces timeout, CPU, and memory limits;
- captures stdout, stderr, exit code, start/end time;
- requires JSON stdout for successful runs.

Generated skills cannot provide Dockerfiles, image names, build contexts, or build args.

## Network Limitation

Network domains may be approved, and Docker runtime can enable network for approved networked skills. Domain-level egress filtering is not implemented yet.

Wildcard or unrestricted network remains blocked.

## Local Runner

The local runner remains available only for explicit development fallback. It is less isolated and should not be treated as production-safe.

## Run Preconditions

Executable skill runs require:

- installed status;
- enabled skill;
- automation or hybrid type;
- runtime permissions approved;
- runtime permissions supported;
- operation lock available;
- tests pass before task execution.

Instruction-only and proposed skills cannot run.
