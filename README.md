# App Inspector Service

Drives a mobile app on a real or emulated device and emits a structured map of its screens, UI elements, and inferred purpose. Port 6311.

## What App Inspector Service Does

When an upstream caller submits `POST /api/inspect` with an app binary reference (a local path, a URL, or a store ref), a target platform, a device-farm provider, and an automation backend, the service acquires the binary and starts an asynchronous inspection job. Acquisition is SSRF-guarded and path-traversal-checked against an allowlist of local directories, and the `app_path` is validated *before* any database row is written so a malformed request never leaves a job record behind. The caller gets back a `job_id` and a poll URL immediately; the actual crawl runs in the background.

Behind that job, the service provisions a device session and connects an automation driver to it. The device layer is pluggable: the default `local` provider spins up a Dockerized Android emulator (`budtmo/docker-android`), while `browserstack` targets real hosted devices. On top of the session, an `AutomationClient` (Appium WebDriver or Maestro, async-wrapped) installs and launches the app, then a breadth-first navigator walks the screens up to a configured maximum. At each screen it captures the XML page-source, a screenshot, and the set of clickable elements, taps into reachable screens, and records the navigation edges as a graph. Screenshots and any session recording are streamed to a MinIO bucket (`app-inspector-service`); the screen graph, UI components, and per-job usage are persisted to PostgreSQL.

Once the crawl is complete the service classifies each screen by type and infers the app's overall purpose and domain — social media, e-commerce, productivity, and so on. This analysis is deterministic: it runs on keyword and structural signals, not an LLM. An `llm` mode exists as a configuration knob but no worker_management client is wired into the shipped path, so callers should treat the classification as fully heuristic. The finished map is available through the result, screens, and export endpoints, and two inspections can be diffed via the compare endpoint.

The job model supports an optional human-in-the-loop pause: a caller can preview the discovered screens and select which ones to keep before the service finalizes the report. Every request is tenant-isolated through license-token authentication, and a per-tenant sliding-window rate limiter caps how many inspections a tenant can start. A background cleanup loop runs every five minutes, reaping jobs stuck in `processing` past a timeout and deleting completed or failed jobs older than the retention window. The primary upstream consumers are the w2v_orchestrator `app_review` product family (mobile-app review runs) and `bdd_playwright_service`, which calls the inspect endpoint to discover mobile screens that feed demo-video generation.

## Features

- **Device-driven crawl** — installs and launches the app on a live emulator or real device and walks its screens breadth-first, so the resulting map reflects what the app actually renders rather than static manifest metadata
- **Pluggable device farms** — a `DeviceFarmClient` ABC exposes `local` (Dockerized Android emulator) and `browserstack` (real hosted devices) backends behind one interface; BrowserStack credentials travel per request in `device_farm` (never env, never persisted)
- **Pluggable automation backends** — an `AutomationClient` protocol wraps Appium WebDriver and Maestro (plus a stub for CI), so page-source, screenshots, element discovery, tap, and back work identically regardless of driver
- **SSRF-guarded acquisition** — fetches app binaries from path, URL, or store ref with size and timeout limits and validates `app_path` before any DB write, so untrusted references can't reach internal hosts or escape the allowed directories
- **Heuristic screen classification** — infers screen type and app domain from keyword and structural signals with zero LLM dependency, so inspection is deterministic, cheap, and reproducible
- **Accessibility findings** — analyzes each screen's UI tree for accessibility issues, so downstream reviews can surface them without re-crawling
- **Human-in-the-loop screen selection** — lets a caller preview discovered screens and select which to keep before finalizing, so noisy or irrelevant screens are excluded from the report
- **Inspection diffing** — compares two inspections to highlight added, removed, and changed screens, so callers can track how an app evolves between builds
- **MinIO artifact storage** — streams screenshots and session recordings to object storage rather than holding them in-pod, so large binaries never blow the pod memory budget
- **Per-tenant rate limiting** — enforces a sliding-window request cap and concurrent-job ceiling keyed per tenant, so a single tenant can't saturate the shared emulator pool
- **Tenant-scoped status cache** — caches job status under a tenant-qualified Redis key read before the RLS-scoped DB lookup, so a cache hit can never leak another tenant's job status
- **Background job reaping** — a cleanup loop marks stuck jobs failed and deletes aged jobs on a fixed interval, so the job table and emulator sessions don't accumulate indefinitely

## Authentication

Auth enabled by default (`AUTH_ENABLED=true`). The `AuthMiddleware` (delegated to `w2v_common.auth.middleware`) reads the license token from `X-Tenant-Id: Bearer <license_token>` — **not** the `Authorization` header, which is reserved for Keycloak JWTs from the SPA. Tokens are introspected against the licenses service and the result is cached (~5 minutes) so per-request latency stays flat.

| Priority | Method | Details |
|----------|--------|---------|
| 1 | License token | `X-Tenant-Id: Bearer <token>` introspected against the licenses service, ~5-min cache |
| 2 | Trusted network | Requests from trusted CIDRs resolve to the default tenant without a token (synthetic-identity path, off unless `trust_incluster_identity` is enabled) |

For service-to-service calls (device farms, storage), the identity is forwarded as plain `X-Tenant-Id`, `X-License-Id`, and `X-Subject-Id` headers (no `Bearer` prefix).

Exempt paths (no token required): `/`, `/health`, `/health/live`, `/health/ready`, `/healthz`, `/readyz`, `/ready`, `/docs`, `/redoc`, `/openapi.json`, `/favicon.ico`, `/metrics` (plus the `/docs`, `/redoc`, `/health/` prefixes).

Row-level security is enforced: migration `20260617_0000_rls` ENABLEs `tenant_isolation`, and an `after_begin` listener re-binds the `app.current_tenant` GUC after each commit so multi-commit background jobs keep their tenant scope. RLS is inert until the app connects as the restricted `w2v_app_inspector_app` role with `TENANT_RLS_MODE=enforce`.

## Quick Start

```bash
uv sync
uv run uvicorn src.asgi:app --host 0.0.0.0 --port 6311
uv run pytest tests/ -v --cov=src
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/
```

## API Endpoints

### Inspections `/api/inspect`

| Method | Path | Description |
|--------|------|-------------|
| POST | `/` | Start a mobile app inspection job; returns `job_id` + poll URL |
| GET | `/{job_id}` | Poll inspection job status |
| GET | `/{job_id}/result` | Retrieve the full inspection result |
| GET | `/{job_id}/screens` | List discovered screens |
| GET | `/{job_id}/video` | Download the session recording |
| GET | `/{job_id}/screens/preview` | Preview screens available for selection |
| POST | `/{job_id}/screens/select` | Select which screens to keep (HITL pause) |
| GET | `/{job_id}/export/{format}` | Export an inspection report in the given format |
| POST | `/compare` | Diff two inspections |
| GET | `/farms` | List available device farms |
| GET | `/farms/{provider}/devices` | List devices for a device-farm provider |

### Health

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Service info and endpoint listing |
| GET | `/health` | Combined health check (database, Redis, storage) |
| GET | `/health/live` | Liveness probe |
| GET | `/health/ready` | Readiness probe (checks DB) |
| GET | `/healthz` | Liveness probe (modern K8s alias) |
| GET | `/readyz` | Readiness probe (modern K8s alias) |

## Configuration

All configuration via environment variables. Service-specific groups use the prefixes shown.

| Variable | Default | Description |
|----------|---------|-------------|
| `API_HOST` | `0.0.0.0` | Listen host |
| `API_PORT` | `6311` | Listen port (base default 8080 overridden to 6311) |
| `API_DEBUG` | `false` | Enable /docs debug mode |
| `API_RELOAD` | `false` | Auto-reload on code changes |
| `AUTH_ENABLED` | `true` | Toggle authentication middleware |
| `AUTH_DEFAULT_TENANT_ID` | nil-UUID sentinel | Fallback tenant for the trusted-network path (unattributed, billing-excluded) |
| `LOG_LEVEL` | `INFO` | Logging level |
| `LOG_JSON_LOGS` | `false` | Structured JSON logging |
| `DATABASE_URL` | -- (required) | PostgreSQL connection string (psycopg 3) |
| `DATABASE_POOL_SIZE` | `10` | Connection pool size |
| `DATABASE_POOL_MIN_SIZE` | `2` | Minimum connections in pool |
| `DATABASE_MAX_OVERFLOW` | `20` | Max connections beyond pool size |
| `REDIS_URL` | -- (required) | Redis connection string (status cache + rate limiting) |
| `REDIS_MAX_CONNECTIONS` | `10` | Max Redis connections in pool |
| `STORAGE_MINIO_BUCKET` | `app-inspector-service` | MinIO bucket for screenshots/recordings |
| `ACQUISITION_DOWNLOAD_TIMEOUT` | `300` | App download timeout (seconds) |
| `ACQUISITION_MAX_FILE_SIZE_MB` | `500` | Max app binary size (MB) |
| `ACQUISITION_TEMP_DIR` | `/tmp/app_inspector` | Temp directory for downloads |
| `ACQUISITION_ALLOWED_LOCAL_PATHS` | `/app/data,/tmp/app_inspector` | Allowed base dirs for local app paths |
| `ANALYSIS_ENABLE_PURPOSE_INFERENCE` | `true` | Enable purpose/domain inference |
| `ANALYSIS_PURPOSE_INFERENCE_MODE` | `heuristic` | Inference mode: `heuristic` or `llm` (llm not wired) |
| `RATE_LIMIT_REQUESTS_PER_MINUTE` | `30` | Max inspection requests per minute per tenant |
| `RATE_LIMIT_CONCURRENT_JOBS_PER_TENANT` | `5` | Max concurrent inspection jobs per tenant |
| `CLEANUP_RETENTION_DAYS` | `30` | Days to retain completed/failed jobs |
| `CLEANUP_STUCK_JOB_TIMEOUT_MINUTES` | `60` | Minutes before a processing job is deemed stuck |

## Architecture

```
src/
├── asgi.py                          # FastAPI app, lifespan, middleware, health filter, OpenAPI auth
├── config.py                        # Pydantic settings (extends w2v_common; port 6311)
├── api/
│   ├── health.py                    # Health/liveness/readiness endpoints + aliases
│   ├── constants.py                 # API constants
│   ├── schemas.py                   # Request/response DTOs
│   └── routes/
│       ├── __init__.py              # Aggregate router (/api/inspect mount, ordering)
│       ├── inspections.py           # POST /api/inspect (start job)
│       ├── jobs.py                  # Status / result / screens / video
│       ├── selection.py             # Screen preview + select (HITL)
│       ├── compare.py               # POST /api/inspect/compare
│       ├── export.py                # Export report by format
│       ├── farms.py                 # List device farms + devices
│       └── _helpers.py              # Shared route helpers
├── auth/
│   ├── middleware.py                # AuthMiddleware (delegates to w2v_common)
│   └── models.py                    # Auth context models
├── middleware/
│   ├── rate_limiter.py              # Per-tenant sliding-window rate limiter
│   └── request_id.py                # Request-ID ContextVar binding
├── cache/
│   ├── redis_client.py              # Redis connection lifecycle
│   └── cache_keys.py                # Tenant-scoped cache key builders
├── database/
│   ├── connection.py                # Async engine + pool
│   ├── session.py                   # Session factory
│   ├── models.py                    # SQLAlchemy models (jobs, screens, components, usage)
│   ├── rls_events.py                # after_begin GUC re-bind listener
│   ├── rls_resolvers.py             # Tenant GUC resolvers
│   └── migrations/                  # Alembic (6 migrations incl. 20260617_0000_rls)
├── services/
│   ├── inspector_service.py         # Top-level orchestrator wiring the pipeline
│   ├── job_manager.py               # DB job state transitions
│   ├── app_acquisition.py           # Fetch + validate app binary (SSRF/path guards)
│   ├── device_farm.py               # DeviceFarmClient ABC (local / browserstack)
│   ├── automation_client.py         # AutomationClient protocol
│   ├── appium_client.py             # Appium WebDriver backend
│   ├── maestro_client.py            # Maestro backend
│   ├── stub_automation_client.py    # No-device stub for CI
│   ├── screen_navigator.py          # BFS screen crawl + edges
│   ├── xml_parser.py                # Page-source XML parsing
│   ├── ui_component_detector.py     # UI component extraction
│   ├── screen_classifier.py         # Heuristic screen-type classification
│   ├── purpose_inferrer.py          # Heuristic app purpose/domain inference
│   ├── accessibility_analyzer.py    # Accessibility findings
│   ├── inspection_diff.py           # Two-inspection diff
│   ├── export_service.py            # Report export
│   ├── storage_service.py           # MinIO screenshot/recording persistence
│   └── cleanup_service.py           # Stuck/old job reaping
├── helpers/                         # retry, URL validation
└── utils/                           # retry utility
```

## Testing

```bash
uv run pytest tests/ -v --cov=src          # All tests with coverage
uv run pytest tests/unit/ -v               # Unit tests
uv run pytest tests/integration/ -v        # Integration tests
uv run ruff check src/ tests/              # Lint
uv run ruff format --check src/ tests/     # Format check
```

Coverage threshold: 80% (`fail_under` in pyproject.toml). Run `bash scripts/ci-local.sh` before pushing — it mirrors the archetype `Lint & Unit Tests` job exactly (uv sync, ruff, ty, pytest, detect-secrets, pip-audit, bandit).

## CI/CD

`.github/workflows/ci-public.yml` is self-contained and runs on GitHub-hosted runners: a betterleaks secret scan, the `no-private-refs` guard, then `ruff check`, `ruff format --check`, `pytest` against a Postgres 18 service container, and `scripts/pip-audit.sh`.

No image is published from this repository; build it locally with the Dockerfile.

## Docker

```bash
docker build -t app-inspector-service .
docker run -p 6311:6311 app-inspector-service
docker run -p 6311:6311 -e LOG_LEVEL=DEBUG -e AUTH_ENABLED=false -e DATABASE_URL=postgresql+psycopg://host/db -e REDIS_URL=redis://host:6379/0 app-inspector-service
```

## Dependencies

- **licenses service** — validates license tokens via introspection (auth)
- **device farm** — local Dockerized Android emulator (`budtmo/docker-android`) or BrowserStack real devices (Appium hub)
- **MinIO** — object storage for screenshots and session recordings (bucket `app-inspector-service`)
- **w2v_orchestrator (app_review)** and **bdd_playwright_service** — upstream callers of `POST /api/inspect`
- **PostgreSQL 18** — job/screen/component persistence via PgBouncer (`pgbouncer.pgbouncer:6432`), psycopg 3
- **Redis** — job status cache and per-tenant rate limiting
- **FastAPI** — web framework
- **Uvicorn** — ASGI server
- **Pydantic v2 / pydantic-settings** — schema and settings validation
- **SQLAlchemy (asyncio)** — async ORM
- **psycopg 3** — PostgreSQL driver
- **alembic** — schema migrations
- **Appium-Python-Client** — mobile automation driver
- **lxml** — page-source XML parsing
- **httpx** — async HTTP client
- **cachetools** — in-process caching
- **w2v-common** — shared config, auth middleware, error envelope, RLS helpers

## Known Issues

- **LLM purpose-inference mode is not wired** — `ANALYSIS_PURPOSE_INFERENCE_MODE` accepts `llm`, but no worker_management client is connected; the shipped path is always heuristic. Setting `llm` does not change behavior.

## License

Apache 2.0 — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).

## Licensing

This service contains no licence enforcement of its own and runs no licence
server. Tenant and licence identity is forwarded from the caller. It runs with
no licence switch set; unset means off.
