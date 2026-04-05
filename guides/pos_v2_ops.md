# Bosco v2 Ops Guide

## Scope

This guide covers the server-side operational foundation already implemented in Bosco v2:

- ledger registry and version fencing
- system ledger account provisioning
- resilient POS sales with idempotency
- outbox processing and admin payment alerts
- cash closing safeguards for pending refunds
- operational preflight checks
- Django-side replay admission for `X-POS-Replay: 1`
- a dedicated replay gateway wrapper with total timeout, idle timeout, cold-lane slicing, and cooperative draining ahead of Django
- Python reference primitives for the offline JSONL journal, `.snapshot` sidecar, reseal, and valid-prefix recovery
- a local segmented journal runtime with limbo summary repair and size-based rotation
- organization-scoped ledger shards for open accounting adjustments

It does **not** cover the future Electron offline runtime integration or LAN sync. The replay gateway already enforces cold-lane slicing and cooperative draining, but only per gateway process, not via a distributed coordinator across multiple instances.

## Offline Journal Reference

Bosco now includes a Python reference implementation of the durable offline journal in:

- `pos/infrastructure/offline/journal.py`
- `pos/infrastructure/offline/runtime.py`

What is implemented there:

- JSONL append records with `payload_hash`, `record_crc32`, `prev_record_hash`, and `record_hash`
- `.snapshot` sidecar repair logic where the journal remains authoritative
- footer sealing with cumulative `segment_crc32`
- valid-prefix recovery for the active segment
- segment reseal from pending sidecar state after an interrupted footer write
- a segmented runtime that rolls to the next segment after the current one reaches the configured size threshold
- limbo summary repair from the journal when the `.snapshot` sidecar falls behind or loses its aggregates

What is still missing:

- Electron worker/process integration
- limbo UI driven by the `.snapshot` file
- disk-space enforcement and client read-only transitions

Operational command available now:

```powershell
python manage.py offline_journal path\\to\\segment.jsonl path\\to\\segment.snapshot.json --json
```

Reconcile the sidecar from the journal:

```powershell
python manage.py offline_journal path\\to\\segment.jsonl path\\to\\segment.snapshot.json --reconcile --json
```

Attempt footer reseal after an interrupted segment rotation:

```powershell
python manage.py offline_journal path\\to\\segment.jsonl path\\to\\segment.snapshot.json --reseal --json
```

Fail closed if the active segment has a truncated or corrupted tail:

```powershell
python manage.py offline_journal path\\to\\segment.jsonl path\\to\\segment.snapshot.json --strict
```

Inspect the current limbo summary for a journal directory:

```powershell
python manage.py offline_limbo path\\to\\offline-root --stream sales --json
```

## Daily Commands

Build the registry manifest:

```powershell
python scripts/build_ledger_registry_manifest.py --output build/ledger_registry_manifest.json --build-id local-dev
```

Provision or validate required ledger accounts:

```powershell
python manage.py provision_system_ledger_accounts
```

Target one organization:

```powershell
python manage.py provision_system_ledger_accounts --organization-slug legacy-default
```

Sync the active runtime registry:

```powershell
python manage.py sync_ledger_registry_activation
```

Enable maintenance mode before a ledger-affecting rollout:

```powershell
python manage.py sync_ledger_registry_activation --maintenance-mode on
```

Disable maintenance mode after activation:

```powershell
python manage.py sync_ledger_registry_activation --maintenance-mode off
```

Run operational checks:

```powershell
python manage.py ops_preflight
```

Strict mode:

```powershell
python manage.py ops_preflight --strict
```

Machine-readable output:

```powershell
python manage.py ops_preflight --json
```

Rebuild accounting shards for one organization:

```powershell
python manage.py reconcile_ledger_shards --organization-slug legacy-default --json
```

Rebuild accounting shards for every organization:

```powershell
python manage.py reconcile_ledger_shards --json
```

Enable the replay gateway wrapper in Railway/local env:

```powershell
REPLAY_GATEWAY_ENABLED=True
REPLAY_GATEWAY_TOTAL_TIMEOUT_SECONDS=10
REPLAY_GATEWAY_IDLE_TIMEOUT_SECONDS=5
REPLAY_GATEWAY_COLD_LANE_SLOTS=2
REPLAY_GATEWAY_COLD_SLICE_SECONDS=120
```

Enable offline journal runtime checks:

```powershell
OFFLINE_JOURNAL_ENABLED=True
OFFLINE_JOURNAL_ROOT=D:\\bosco-offline
OFFLINE_JOURNAL_STREAM_NAME=sales
```

Optional: enable server-side shadow capture so Django mirrors paid and failed sales into the same JSONL contract while Electron is not wired yet:

```powershell
OFFLINE_JOURNAL_CAPTURE_SERVER_EVENTS=True
```

With that flag enabled, Bosco now mirrors:

- POS sales confirmed as paid
- POS sales marked failed or voided
- web orders created as paid sales

Tune how far back `ops_preflight` scans recent journal origins:

```powershell
OPS_PREFLIGHT_OFFLINE_CAPTURE_LOOKBACK_HOURS=24
```

Append a canonical envelope to the offline journal using the shared writer harness:

```powershell
python manage.py offline_writer D:\\bosco-offline --stream sales --envelope-json "{\"event_id\":\"sale-001\",\"journal_event_type\":\"sale\",\"client_transaction_id\":\"sale-001\",\"payload\":{\"sale_total\":\"12.50\",\"payment_status\":\"PAID\",\"journal_capture_source\":\"client_runtime_harness\",\"sale_origin\":\"POS\"}}" --json
```

The same command also accepts `--envelope-file <path>` or JSON through `stdin`.

Inspect the current limbo directly from the app:

- URL: `/dashboard/limbo-offline/`
- Access: admin or superuser only
- Surface: current summary, active segment paths, tail health, recent events and bounded history of sealed segments
- JSON refresh endpoint: `/dashboard/limbo-offline/json/`
- Historical segment detail endpoint: `/dashboard/limbo-offline/segment/json/?segment_id=<segment_id>`
- Polling: the page refreshes automatically every 10 seconds and also supports manual refresh
- Operational actions:
  - `POST /dashboard/limbo-offline/reconcile/` repairs a lagging `.snapshot` sidecar from the active segment
  - `POST /dashboard/limbo-offline/reseal/` appends the pending footer when the sidecar already carries a valid seal request
  - `POST /dashboard/limbo-offline/seal-active/` seals the active segment only when the runtime reports `rotation_needed=true`
- Historical segment actions:
  - `POST /dashboard/limbo-offline/segment/revalidate/` revalidates footer state for a sealed historical segment and stores the result in `ops_metadata`
  - `POST /dashboard/limbo-offline/segment/review/` marks a sealed historical segment as operationally reviewed in `ops_metadata`
- when the segment exposes tenant scope (`organization_id/location_id`) or the acting user has a single active membership, both actions also write a centralized `AuditLog`; if scope cannot be resolved, the local action still succeeds and the response reports that the central log was skipped
- the expanded UI detail now shows the central logging result directly, including `audit_log_id` when recorded or the explicit skip reason when tenant scope could not be resolved
- the main analytics dashboard now includes an `ACCIONES OFFLINE AUDITADAS` table filtered by the active period so recent offline operations can be reviewed without opening each historical segment detail
- that table now supports operational ordering by newest first, missing-footer first, and unreviewed first so incident triage can be driven by severity instead of chronology alone
- Bosco also exposes `/dashboard/incidentes-offline/` as a dedicated critical-only view backed by the same filters and ordering rules, but restricted to segments still in incident state (`footer missing` or non-`sealed` status)
- that critical-only view also exposes `CSV` and `JSON` exports for the exact filtered subset currently under review, so incident response can work with the same operational ordering outside the browser
- that table now also supports operational filters by a secondary time window, action type, organization, location, actor, segment status, explicit footer presence, and the operational result recorded in `AuditLog` while preserving the active analytics period
- that same table now also supports quick partial lookup by `segment_id`, so operators can jump into an incident even when they only have a fragment of the segment identifier
- each row in that table now provides direct navigation to `/dashboard/limbo-offline/?segment_id=<segment_id>` and to `/dashboard/limbo-offline/segment/json/?segment_id=<segment_id>`
- the same row now also links to the Django admin change page for the corresponding `AuditLog`, so operators can move from dashboard analytics to central audit detail without manual lookup
- when that `AuditLog` targets `OfflineJournalSegment`, the admin change page also exposes reverse links back to `/dashboard/limbo-offline/?segment_id=<segment_id>` and `/dashboard/limbo-offline/segment/json/?segment_id=<segment_id>`
- the `Limbo Offline` page now also exposes a direct GET search by `segment_id`, so a sealed segment can be opened without first navigating from analytics or admin
- that same search now also rides on the periodic JSON refresh: the browser keeps the active `segment_id` in the URL, sends it to `/dashboard/limbo-offline/json/`, and expands the matching segment without a full page reload
- the same search box now also supports exact historical JSON jump with `Ctrl+Enter` or the explicit `Abrir JSON` button, so operators can open the segment detail payload directly from keyboard
- Both actions run under the same runtime file lock used by the writer, so they do not race appends from the shadow capture path
- Sealed history depth is controlled by `OFFLINE_JOURNAL_HISTORY_LIMIT` and defaults to `5`
- Historical segment detail is loaded on demand from the UI, so sealed-history inspection does not bloat the periodic limbo refresh payload

## Recommended Deploy Sequence

For deploys that touch ledger, accounting, idempotency, or POS mutation behavior:

1. Deploy code to the target environment.
2. Run migrations.
3. Run `python manage.py provision_system_ledger_accounts`.
4. Run `python manage.py sync_ledger_registry_activation --maintenance-mode on`.
5. Generate the current manifest with `scripts/build_ledger_registry_manifest.py`.
6. Verify `ops_preflight --strict`.
7. Run `python manage.py sync_ledger_registry_activation --maintenance-mode off`.

If `ops_preflight` reports a lockfile mismatch or activation mismatch, do **not** reopen mutations until that is resolved.

## What Ops Preflight Checks

`ops_preflight` now validates:

- database connectivity
- Celery execution mode
- Redis reachability
- ledger registry lockfile integrity
- active runtime registry activation
- version-fencing configuration
- replay gateway wrapper configuration and Procfile wiring
- offline journal root and current limbo summary health when the runtime is enabled
- server-side shadow capture status and recent offline journal origins (`POS` / `WEB`) when shadow capture is enabled
- required system ledger accounts per organization
- Telegram admin alert configuration
- WhatsApp environment settings
- stale pending sales
- stale idempotency rows
- outbox backlog and blocked critical events
- unresolved payment exceptions and open refund liabilities
- ledger shard state, missing shard rows, and counter drift against open accounting adjustments
- chronology-estimated replay sales and stale unresolved `sale.post_close_replay_alert`
- delivery pool availability
- pending delivery quote backlog
- print job backlog

## Interpreting Important Warnings

### Replay backpressure (`429 replay_backpressure`)

If a POS mutation arrives with `X-POS-Replay: 1`, Bosco may reject it with `429` when replay capacity is saturated.

Current response contract:

- `Retry-After`
- `X-Bosco-Replay-Lane`
- `X-Bosco-Replay-Scope`
- `X-Bosco-Replay-Reason`

Current lanes:

- `normal`
- `cold`

Current scopes:

- `global`
- `organization`
- `cold_lane`

Bosco now has two layers:

- Django-side admission control (`429 replay_backpressure`)
- an outer replay gateway wrapper that can cut replay requests on total timeout or idle timeout before they pin the web process indefinitely, classify cold replay, and drain a hot organization after its slice when another organization is already waiting

Gateway-level replay backpressure now also uses:

- `X-Bosco-Replay-Gateway: backpressure` for cold-lane capacity rejection
- `X-Bosco-Replay-Gateway: draining` when an organization already exhausted its cold slice and must yield after the current batch
- `X-Bosco-Replay-Lane`

Gateway timeout responses use:

- HTTP `504`
- `Retry-After`
- `X-POS-Replay: 1`
- `X-Bosco-Replay-Gateway`
- `X-Bosco-Replay-Scope: gateway`
- `X-Bosco-Replay-Reason`

Cold-lane fairness in the gateway relies on a stable organization hint. Current precedence is:

1. `X-Bosco-Replay-Organization`
2. `organization_id`
3. `organization_slug`
4. `location_uuid`
5. `queue_session_id`

If none are present, the gateway falls back to the client IP, which is acceptable only as a last resort.

### `offline_journal`

This check verifies:

- `OFFLINE_JOURNAL_ENABLED`
- `OFFLINE_JOURNAL_ROOT`
- current active segment recovery without truncated/corrupted tail
- limbo summary visibility (`total_sales`, `amount_total`) from the repaired sidecar/runtime view

If enabled but the root does not exist or the tail is corrupted, preflight fails closed.

### `sale.post_close_replay_alert`

These alerts mean a replayed sale landed in a different accounting day than its operational chronology.

Current operator workflow:

1. Review the alert in the analytics dashboard.
2. Confirm operational day versus accounting day.
3. Add a mandatory justification note.
4. Mark the alert as reviewed.

Important:

- this action does **not** reopen closed cash days
- this action does **not** rewrite `operated_at_normalized`
- this action only closes the operational alert trail in `AuditLog`

### `ledger_lockfile`

The generated lockfile does not match the code registry.

Action:

1. Regenerate the manifest and lock data from the current branch.
2. Confirm the deployed code hash matches the branch you expect.
3. Re-run `ops_preflight`.

### `ledger_activation`

The database activation does not match the currently deployed code hash/version, or maintenance mode is still enabled.

Action:

1. Run `python manage.py sync_ledger_registry_activation`.
2. If the deploy is complete, turn maintenance mode off.

### `system_ledger_accounts`

One or more organizations are missing required system accounts.

Action:

```powershell
python manage.py provision_system_ledger_accounts
```

### `pending_sales_backlog`

There are POS sales stuck in `PENDING` beyond the payment timeout.

Action:

1. Inspect payment provider behavior.
2. Run the stale-payment reaper task path.
3. Review analytics dashboard for orphan payments or refund liabilities.

### `outbox_backlog`

There are failed, blocked, or stale in-progress outbox events.

Action:

1. Confirm Redis/Celery are healthy.
2. Inspect `OutboxEvent` rows with `FAILED` or `BLOCKED`.
3. Prioritize `CRITICAL` events first.

### `payment_exceptions_backlog`

There are unresolved orphan-payment alerts, refund liabilities, or accounting adjustments pending identification.

Action:

1. Open the analytics dashboard.
2. Resolve the payment exception or accounting adjustment.
3. Do not close the operational loop by hand without an audit note.

### `ledger_shards`

This check verifies:

- every organization has `OrganizationLedgerState`
- shard rows match `shard_count`
- open `AccountingAdjustment` totals/counts match shard counters
- no open adjustment remains with null or out-of-range `contingency_shard_id`

If this check warns, run:

```powershell
python manage.py reconcile_ledger_shards --json
```

### `replay_gateway`

This check verifies:

- `REPLAY_GATEWAY_ENABLED`
- timeout ordering (`idle < total < upstream`)
- valid upstream port wiring
- cold-lane config (`hours`, `slots`, `slice`, `waiter_ttl`)
- `Procfile` still points `web` to `python scripts/start_web.py`

If this check fails, do not trust replay timeout enforcement at the edge even if Django admission is still active.

### `operational_drift`

This check tracks replay chronology risk:

- recent sales with `chronology_estimated=True`
- open `sale.post_close_replay_alert`
- stale unresolved replay alerts beyond `OPS_PREFLIGHT_REPLAY_ALERT_STALE_HOURS`

Stale replay alerts now fail preflight as errors because they imply unresolved accounting-day drift.

### `reconcile_ledger_shards`

This command recalculates `OrganizationLedgerCounterShard` from open `AccountingAdjustment` rows, organization by organization.

Use it when:

- a direct `QuerySet.update()` bypassed model counter sync
- an interrupted deploy left shard counters suspicious
- you need to re-tag historical adjustments after a shard drift incident

Current guarantees:

- deterministic `contingency_shard_id` from `adjustment_uid`
- sequential reconciliation ordered by `effective_at, id`
- best-effort advisory lock on PostgreSQL per organization
- no online shard rebalance in Fase 1

## Incident Notes

### Production deploy crashes during `pos.0016`

If you ever see a traceback around `pos.0016_printjob_uniqueness_and_tenant_guards`, verify the environment is running the latest hotfixes already merged on `main`.

### Cash closing blocked by refunds

This is intentional. Bosco now blocks closing when refund liabilities remain open unless the operator explicitly closes with a documented override note.

### Telegram admin alerts not arriving

Check:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ADMIN_ALERT_CHAT_ID`
- `REDIS_URL`
- `ops_preflight`

If Redis is unavailable, the circuit breaker fails safe and external Telegram sends are skipped.

## Validation Before Push

Use at least:

```powershell
python manage.py makemigrations --check --dry-run
python manage.py test pos.tests_registry --verbosity 1
python manage.py test pos.tests_v2 --verbosity 1
python manage.py ops_preflight --json
```
