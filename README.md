# openrouter_custom — Hermes plugin

OpenRouter live-catalog filter with a **stable pseudo-model alias** and
adaptive request-time rotation backed by per-model health tracking.

> Target host: [NousResearch/hermes-agent](https://github.com/nousresearch/hermes-agent).
> Slots into Hermes's `plugins/model-providers/` discovery path; not standalone.

## What it does

OpenRouter's `:free` tier rotates rapidly — new models appear, old ones get
deprecated, daily rate-limits flap. Pinning a single `:free` id in
`config.yaml` means staleness; whitelisting everything floods the `/model`
picker with hundreds of entries.

This plugin gives the operator **one stable handle** — e.g. `best-free` —
that always points at "the best currently-available OR `:free` model that
satisfies my hard constraints". A cron job re-evaluates the OR catalog
every 30 minutes. At request time a rotation layer also reshuffles based
on per-model health so a dead upstream doesn't kill conversations.

## Quick start

```bash
# In Telegram:
/model best-free

# Or pin globally via Hermes CLI:
hermes config set model.default best-free
hermes config set model.provider openrouter_custom
```

All subsequent sessions transparently use whichever real OR id the cron
last picked. The `/model` picker also lists every other id that
currently passes the filter — pick a specific one if you want to pin
instead of ride the alias.

## Configuration

Lives in `plugin.yaml`, hot-reloaded on every cron tick and every
session start — no gateway restart required. Operator overrides go to
`state/openrouter_custom/config_overrides.yaml` (writable via dashboard
or directly).

```yaml
config:
  pseudo_model_alias: best-free
  filters:
    price:
      prompt_max: 0          # USD per million tokens (0 = free-only)
      completion_max: 0
    min_context: 65536
    require_tools: true
    modality: text
    exclude_patterns: []
    prefer_patterns: []
  ranking:
    rank_by: prefer_match    # prefer_match | context_desc | params_desc | params_asc | modality_pref | tools_count | latency_p95
    tiebreakers: [context_desc, params_desc, modality_pref, tools_count]
  refresh:
    cron_minutes: 30
    on_failure: keep_last    # keep_last | rotate | fallback_static
    fallback_static_id: ""
  max_candidates: 10
  rotation_mode: circuit_breaker          # static | failover_with_health | circuit_breaker | sticky_health_weighted
  internal_fallback:
    sequential_count: 4                   # OR-native fallback depth in the models:[...] array

  # Health-collection mode (v0.7.16+)
  health_mode: observe_outcome            # observe_outcome (passive, default) | observe_prob (passive + active probe cron)

  # Probe knobs (used only in observe_prob mode)
  probe_timeout_seconds: 8
  probe_max_retries: 0
  probe_jitter_min_seconds: 0.5
  probe_jitter_max_seconds: 1.5
  probe_auto_tune_top_n: 4
  probe_auto_tune_success_rate: 0.5
  probe_prompt: "ping"
  probe_max_tokens: 1
  probe_temperature: 0.0
  probe_base_url: null

  # Quarantine policy
  quarantine_consecutive_fail_threshold: 3
  quarantine_backoff_minutes: [5, 10, 20, 40, 80]

  alias_sessions_max: 500
  health_success_rate_smoothing: 1
```

Every numeric knob has a `DEFAULT_*` constant in the module that loads
it — nothing is silently hard-coded.

## Two health-collection modes

Picking which signals drive rotation:

| Mode               | What feeds health.json                                     | OR-quota cost           |
|--------------------|------------------------------------------------------------|-------------------------|
| `observe_outcome`  | **Passive** — only outcomes from real conversation turns   | **0 extra requests**    |
| `observe_prob`     | Passive **+** 10-minute probe cron (1-token ping per model)| ~6 × top-K req/hour     |

**Default is `observe_outcome`** because OpenRouter's free tier is
capped at **20 req/min and 50 req/day** for accounts without ≥$10 of
purchased credits (1000/day with credits). A naive 10m × 10-model probe
loop = 1440 requests/day and will trip the cap inside an hour.

Switch to `observe_prob` only when:
- you've topped up ≥$10 on OpenRouter, **and**
- you actively want synthetic recovery probes (e.g. low live traffic).

## Internal sequential fallback

OR accepts a `models: [id1, id2, ...]` array in the chat-completions
body. When the first id returns a transport-level failure, OR walks the
list server-side and only surfaces a hard failure once every candidate
refuses. The plugin uses this to express rotation order without writing
any proxy code:

- `internal_fallback.sequential_count: 1` — no array, single-model request.
- `internal_fallback.sequential_count: N` (default `4`) — when the
  session was resolved from the pseudo alias, the rotation layer emits
  an ordered length-N list of candidate ids and OR routes through them.

**Scope.** Rotation activates only when the session's marker is in
`alias_sessions.json` (set by `resolve_runtime_model` on alias match).
A direct `/model qwen3-coder:free` pick is never wrapped — the operator
chose a specific id and rotation deactivates for that session.

**Caveat.** OR's native fallback fires only on transport-level failures
(HTTP 4xx/5xx/timeout). A 200 OK whose content is a content-level
refusal is success from OR's perspective and is NOT retried.

## Rotation strategies

All strategies share the same input — the ranked `candidates_top` from
`state.json` and the per-model history in `health.json` — and emit an
ordered list of length up to `internal_fallback.sequential_count`.

| Mode                       | Behaviour |
|----------------------------|-----------|
| `static`                   | Strict ranking order. Health ignored. |
| `failover_with_health`     | Ranking order; quarantined models pushed to the END of the list. Probe (cooldown elapsed) returns to its ranking slot. |
| `circuit_breaker` (default)| Same layout as failover_with_health, plus exponential backoff on quarantine duration: 5 → 10 → 20 → 40 → 80 minutes per re-fail. |
| `sticky_health_weighted`   | Re-ranks every call by `rank_score × success_rate` (Beta-smoothed). Failing models drift down gradually without being hard-blocked. |

### Health states (UI labels)

| Label             | Underlying condition                                         |
|-------------------|--------------------------------------------------------------|
| `✓ ok`            | `circuit_state=closed`, `success>0`, `consecutive_fail=0`     |
| `△ degraded`      | `closed`, `success>0`, `consecutive_fail>0`                   |
| `△ no-success`    | `closed`, `success=0`, `fail>0`                               |
| `○ untested`      | No prior data (never pinged, never seen in live traffic)      |
| `⚠ probe`         | `open` with cooldown elapsed, OR `half_open` — gets its ranking slot back for a recovery attempt |
| `🚫 quarantined`  | `open` with cooldown still active — pushed to the tail of the request list |

### Persistent alias-session marker

`state/openrouter_custom/alias_sessions.json` stores the set of session
ids that have resolved through the alias. Survives gateway restart so
rotation kicks in immediately on the first turn after restart — without
this the in-process set would be empty until `resolve_runtime_model`
re-fires.

Concrete `/model <id>` picks call `_unmark_alias_session` to drop the
session id, so switching mid-session correctly disables rotation.

### Live observation (host hook)

`OpenRouterCustomProfile.observe_outcome(response, error, request_models)`
is called by the conversation loop after every chat completion (both
streaming and non-streaming paths). Attributions:

- `response.model` → success
- Models BEFORE `response.model` in `request_models` → `bypassed` failure
- Top-level errors (e.g. `RateLimitError`) → failure for every model in `request_models`

The hook is opt-in: providers without `observe_outcome` are skipped by
the host. **Requires the matching fork patch in
`hermes-agent/agent/chat_completion_helpers.py`** (`feat/mc-workflow-integration`
branch).

### Probe (active mode only)

Active in `health_mode: observe_prob`. The cron
`openrouter-custom-probe` issues a 1-token chat-completion ping to each
top-K candidate with random `0.5..1.5s` jitter between requests. Error
classification: `429`, `404`, `400`, `401`, `timeout`, `5xx`, `net`.

Auto-tune: when the top-N success rate drops below
`probe_auto_tune_success_rate` *and* at least one model succeeded,
probe triggers an inline `state.json` refresh — the next ranking can
incorporate freshly-promoted candidates from the OR catalog without
waiting for the regular 30-minute cron.

## Filter semantics

| Field                       | Behaviour |
|-----------------------------|-----------|
| `price.prompt_max`          | Maximum input price in USD per million tokens. `0` = free-only. |
| `price.completion_max`      | Maximum output price in USD per million tokens. `0` = free-only. |
| `min_context`               | `context_length >=` this many tokens. |
| `require_tools`             | `supported_parameters` must contain `"tools"`. |
| `modality`                  | `text` / `text+image` / `any`. Substring match against `architecture.modality`. |
| `exclude_patterns`          | Python regex blacklist on `id`. First match drops the model. |
| `prefer_patterns`           | Python regex used as ranking boost. More matches = higher `prefer_match` score. |

## Ranking semantics

Primary key (`rank_by`) computed per candidate, then tiebreakers
applied left-to-right. All keys are "higher is better".

| Key             | Feature |
|-----------------|---------|
| `prefer_match`  | Number of `prefer_patterns` regexes that match the id. |
| `context_desc`  | `context_length`. |
| `params_desc`   | Parameter count in billions, sniffed from the model description. |
| `params_asc`    | Inverse of params_desc — smaller model wins. Unparseable params rank last. |
| `modality_pref` | `text+image` → 2, `text` → 1. |
| `tools_count`   | Length of `supported_parameters`. |
| `latency_p95`   | Inverted p95 latency (future work). |

## State files

Under `$HERMES_HOME/state/openrouter_custom/` (typically
`/opt/data/state/openrouter_custom/`):

- `state.json` — `candidates_top` ranked list, `real_model_id`, refresh metadata.
- `health.json` — per-model counters and circuit state.
- `alias_sessions.json` — persistent set of session ids that came through the alias.
- `config_overrides.yaml` — operator-written deltas on top of `plugin.yaml`.

## How it plugs in

- `__init__.py` registers `provider=openrouter_custom` with a
  `fetch_models()` that surfaces both the pseudo alias and every
  candidate that passes the filter.
- `resolve_runtime_model` swaps the alias for `state.real_model_id` at
  the start of each turn AND marks the session as alias-resolved (or
  unmarks on a concrete pick).
- `build_extra_body` reads `alias_sessions.json` + `health.json` and
  emits the `models: [...]` array per the configured rotation strategy.
- `observe_outcome` is the host-side hook that records per-model
  outcomes after each request.
- `refresh.py` — 30-minute cron entry point (catalog re-rank).
- `probe_cron.py` — 10-minute cron entry point (active probe, gated by
  `health_mode`).

## Dashboard

REST API (`plugin_api.py`) used by the React UI shipped in
`dashboard/dist/index.js`. Endpoints:

| Method   | Path                       | Purpose |
|----------|----------------------------|---------|
| GET      | `/meta`                    | Plugin name + version (single source of truth from plugin.yaml). |
| GET      | `/config`                  | Defaults + effective config. |
| PUT      | `/config`                  | Write operator overrides. |
| GET      | `/defaults`                | Bundled defaults only. |
| POST     | `/refresh`                 | Force `state.json` re-pick. |
| GET      | `/state`                   | Latest `state.json` + computed `next_request_order` + `rotation_mode_effective`. |
| GET      | `/health`                  | `health.json` contents. |
| POST     | `/probe`                   | Probe every top-K candidate. Gated by `health_mode=observe_prob`. |
| POST     | `/probe/single`            | Probe ONE specific candidate. |
| POST     | `/health/reset`            | Wipe `health.json`. |
| POST     | `/health/reset/single`     | Zero ONE model's counters/quarantine without removing the row. |
| GET/PUT  | `/health-mode`             | Read/write `health_mode`. |

## Required fork patch

The host hook surface needs a small patch in `hermes-agent` to call
`observe_outcome` after each request. Branch
`feat/mc-workflow-integration`, file
`agent/chat_completion_helpers.py` — adds the call inside the `finally`
block of both `interruptible_api_call` (non-stream) and
`interruptible_streaming_api_call` (stream). The same branch also
pre-initialises `agent.session_id` before the `resolve_runtime_model`
hook so the alias-session marker keys off a real id rather than `""`.

## Running tests

```bash
python3 -m pytest tests/ -v
```

53 tests, all pure-function. Network is stubbed.

## Versioning

Plugin version lives **only** in `plugin.yaml`. The dashboard fetches
it via `/meta` (no hardcoded badge), and `manifest.json` is auto-synced
by `infra/hermes/scripts/sync-external-plugins.sh` in the parent
repo. Operators bump only `plugin.yaml`.

## License

MIT (see LICENSE).
