# Operation ID Plan: app_inspector_service

Canonical registry of `operation_id` values for every route in
`app_inspector_service`. These IDs feed the org-wide n8n custom-node
generator and the `ultrareview` cross-service catalogue, so they must be:

- **stable** — never rename without coordinating downstream,
- **`verb_noun` snake_case** — matches the org standard (nlp_service,
  worker_management, kag, analysis_service, video_compiler_service,
  video_analyzer_service),
- **globally unique** — disambiguated against the rest of the fleet's verbs.

This service hosts a single inspection domain plus a health surface.
Verbs are prefixed with `inspection_` / `app_` / `device_` where the generic
verb (`start_*`, `compare_*`, `list_*`, `get_*`, `download_*`, `export_*`)
would collide across the fleet.

## Files to edit

- `src/asgi.py` (root endpoint)
- `src/api/health.py`
- `src/api/routes/inspections.py`
- `src/api/routes/compare.py`
- `src/api/routes/farms.py`
- `src/api/routes/jobs.py`
- `src/api/routes/selection.py`
- `src/api/routes/export.py`

## Mapping Tables

### `src/asgi.py` (tag: `health`)

| Method | Function | operation_id   | Rationale                                  |
|--------|----------|----------------|--------------------------------------------|
| GET    | `root`   | `service_root` | Service banner; matches the fleet standard. |

### `src/api/health.py` (tag: `health`)

| Method | Function       | operation_id   | Rationale                                                  |
|--------|----------------|----------------|------------------------------------------------------------|
| GET    | `health_check` | `health_check` | Primary health probe; matches the fleet standard verb.     |
| GET    | `health_live`  | `health_live`  | Liveness sub-probe; one-to-one with path.                  |
| GET    | `health_ready` | `health_ready` | Readiness sub-probe; one-to-one with path.                 |

### `src/api/routes/inspections.py` (tag: `inspect`)

| Method | Function           | operation_id           | Rationale                                                                |
|--------|--------------------|------------------------|--------------------------------------------------------------------------|
| POST   | `start_inspection` | `start_app_inspection` | Generic `start_*` collides across the fleet; prefix with the resource verb. |

### `src/api/routes/compare.py` (tag: `inspect`)

| Method | Function                       | operation_id              | Rationale                                                          |
|--------|--------------------------------|---------------------------|--------------------------------------------------------------------|
| POST   | `compare_inspections_endpoint` | `compare_app_inspections` | Domain-prefix; `_endpoint` is implementation-leaking suffix. |

### `src/api/routes/farms.py` (tag: `inspect`)

| Method | Function            | operation_id         | Rationale                                                                      |
|--------|---------------------|----------------------|--------------------------------------------------------------------------------|
| GET    | `list_farms`        | `list_device_farms`  | Generic `list_farms` collides with non-device farm namespaces; prefix `device_`. |
| GET    | `list_farm_devices` | `list_farm_devices`  | One-to-one with path; verb_noun shape already canonical.                        |

### `src/api/routes/jobs.py` (tag: `inspect`)

| Method | Function                  | operation_id                    | Rationale                                                                       |
|--------|---------------------------|---------------------------------|---------------------------------------------------------------------------------|
| GET    | `get_job_status`          | `get_inspection_status`         | Domain-prefix to disambiguate from generic job-status verbs elsewhere.          |
| GET    | `get_inspection_result`   | `get_inspection_result`         | Already domain-prefixed; matches the fleet standard verb_noun.                  |
| GET    | `list_screens`            | `list_inspection_screens`       | Generic `list_screens` collides; prefix with the inspection domain.             |
| GET    | `get_job_video`           | `download_inspection_recording` | Surfacing the actual verb (download) and resource (recording) over `_job_video`. |

### `src/api/routes/selection.py` (tag: `inspect`)

| Method | Function              | operation_id                       | Rationale                                                                |
|--------|-----------------------|------------------------------------|--------------------------------------------------------------------------|
| GET    | `get_screens_preview` | `get_inspection_screens_preview`   | Domain-prefix to disambiguate from generic screens-preview namespaces.   |
| POST   | `select_screens`      | `select_inspection_screens`        | Domain-prefix; generic `select_*` collides across the fleet.             |

### `src/api/routes/export.py` (tag: `inspect`)

| Method | Function            | operation_id              | Rationale                                                              |
|--------|---------------------|---------------------------|------------------------------------------------------------------------|
| GET    | `export_inspection` | `export_inspection_report` | Generic `export_*` collides; the resource is a report (HTML / XML).    |

## Verification

```bash
cd app_inspector_service
uv run pytest tests/unit/test_openapi.py -x
```

`tests/unit/test_openapi.py` loads `app.openapi()` and asserts that the
set of `operationId` values equals the union of every right-hand column
above. Drift (rename, missed decorator, fallback) fails the test loudly.

## Disambiguation rationale

Concrete renames where the function name was generic enough to collide
across the fleet or within this service:

- `start_inspection` → `start_app_inspection` — `start_*` is generic at
  fleet scope; the op_id names both verb and domain.
- `compare_inspections_endpoint` → `compare_app_inspections` — strips the
  `_endpoint` implementation leak and matches the org's verb_noun shape.
- `list_farms` → `list_device_farms` — `farm` alone is ambiguous across
  the org's non-device farms (data, render, etc.).
- `get_job_status` → `get_inspection_status`, `list_screens` →
  `list_inspection_screens`, `get_screens_preview` →
  `get_inspection_screens_preview`, `select_screens` →
  `select_inspection_screens`, `export_inspection` →
  `export_inspection_report` — all domain-prefixed with `inspection_` to
  keep them out of the generic `*_job` / `*_screens` / `*_inspection`
  namespaces shared with other services.
- `get_job_video` → `download_inspection_recording` — the function name
  hides both the verb (download, not get-metadata) and the resource
  (recording, not the inspection object itself).

## Future routes

When adding a route in this service, **always** set `operation_id="…"`
explicitly. The `generate_unique_id_function` configured in `asgi.py` is
a safety net for forgotten kwargs, never the primary identifier. New IDs
go in the table above as part of the same PR (no out-of-band updates).
