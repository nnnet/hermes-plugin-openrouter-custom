# openrouter_custom — Hermes plugin

OpenRouter live-catalog filter with a **stable pseudo-model alias**.

> Target host: [NousResearch/hermes-agent](https://github.com/nousresearch/hermes-agent) — the upstream Hermes Agent. This plugin slots into Hermes's `plugins/model-providers/` discovery path; it is not standalone.

## What it does

OpenRouter's `:free` tier rotates rapidly — new models appear, old ones get
deprecated, daily rate-limits flap. Pinning a single `:free` id in
`config.yaml` means staleness; whitelisting everything floods the `/model`
picker with hundreds of entries.

This plugin gives the operator **one stable handle** — e.g. `or-best-free` —
that always points at "the best currently-available OR `:free` model that
satisfies my hard constraints". A cron job re-evaluates the catalog every
30 minutes (configurable). The session bootstraps with the current best.

## Quick start

```bash
# /model <pseudo-alias> --provider openrouter_custom --global
/model best-free --provider openrouter_custom --global
```

All subsequent sessions on the chosen platform/profile transparently use
whichever real OR id the cron last picked. The `/model` picker also lists
every other id that currently passes the filter — pick a specific one if
you want to pin instead of ride the alias.

## Configuration

Lives in `plugin.yaml`, hot-reloaded on every cron tick and every session
start — no gateway restart required:

```yaml
config:
  pseudo_model_alias: best-free
  filters:
    price:
      prompt_max: 0          # USD per million tokens (default 0 = free)
      completion_max: 0      # USD per million tokens
    min_context: 65536
    require_tools: true
    modality: text
    exclude_patterns: []
    prefer_patterns: [qwen3, llama-3\.3, deepseek, kimi-k2]
  ranking:
    rank_by: prefer_match
    tiebreakers: [context_desc, params_desc, modality_pref, tools_count]
  refresh:
    cron_minutes: 30
    on_failure: keep_last        # keep_last | rotate | fallback_static
    fallback_static_id: qwen/qwen3-coder:free
  max_candidates: 10
  rotation_mode: circuit_breaker # static | failover_with_health | circuit_breaker | sticky_health_weighted
  internal_fallback:
    sequential_count: 4          # M — OR-native fallback depth (1 = off, see Rotation section below)
```

## Internal sequential fallback (M)

OpenRouter accepts a `models: [id1, id2, ...]` array in the chat-completions
request body. When the first id returns a transport-level failure
(HTTP 4xx/5xx, timeout, offline), OR walks the list server-side and only
surfaces a hard failure once every candidate refuses. We piggy-back on this
to make the alias more resilient without writing any proxy code:

* `internal_fallback.sequential_count: 1` (default) — current behaviour.
  Only the single top candidate is sent.
* `internal_fallback.sequential_count: N` (N > 1) — when the request
  resolves through the pseudo alias (`best-free`), the plugin attaches
  the top-N candidate ids from `state.candidates_top` as the request body's
  `models` field. Hermes's external fallback chain (e.g. `claude-haiku-4-5`)
  only triggers when the whole internal pool is exhausted.

**Scope.** The injection happens only when the session was resolved from
the pseudo alias. A direct pick (`/model qwen3-coder:free`) is never
wrapped — the operator chose a specific id and we respect that.

**Caveat.** OR's native fallback fires only on transport-level failures.
A 200 OK whose content is a content-level refusal ("I can't help with
that") is success from OR's perspective and is NOT retried. Detecting
that requires a separate in-plugin proxy and is out of scope.

## Rotation strategies (v0.5+)

The order in which candidates are sent in `models[...]` is decided by a
configurable rotation strategy. All strategies share the same input —
the ranked `candidates_top` from `state.json` and the per-model history
in `health.json` — and emit an ordered list of length up to
`internal_fallback.sequential_count`.

| Mode                       | Behaviour                                                                                                |
|----------------------------|----------------------------------------------------------------------------------------------------------|
| `static`                   | Strict ranking order. Health is ignored. Original behaviour.                                              |
| `failover_with_health`     | Rank order minus models whose circuit is OPEN with an unexpired cooldown. They rejoin once cooldown elapses. |
| `circuit_breaker` (default)| Same as failover but promotes a model to slot 1 (one-shot probe) when its cooldown expires. Success closes the circuit; failure re-opens with exponential backoff `5m → 10m → 20m → 40m → 80m`. |
| `sticky_health_weighted`   | Re-ranks every call by `rank_score × success_rate` (Beta-smoothed). Failing models drift downward without ever being binary-blocked. |

### Health tracking

`state/openrouter_custom/health.json` keeps per-model counters:

* `success` / `fail` — running totals from probe + live conversation observations.
* `consecutive_fail` — resets on success; circuit OPENS after 3 in a row.
* `circuit_state` — `closed` / `open` / `half_open`.
* `next_probe_iso` — when the next probe is allowed (during cooldown).
* `last_error_class` — short tag for UI (`429`, `404`, `400`, `timeout`, `5xx`, `net`).
* `last_success_iso` / `last_fail_iso` — for the dashboard's "Last ok / Last fail" columns.

### Probe cron

`openrouter-custom-probe` runs every 10 minutes (see
`infra/hermes/cron-jobs.yaml`). It issues a 1-token ping with `~1s` of
jitter between each candidate, recording the outcome. On a probe pass
where the top-4 success rate falls below 50% **and** at least one model
is healthy, the cron triggers an inline `state.json` refresh so the
rotation pool can be re-ranked with newly-promoted alternatives without
waiting for the slower 30-minute refresh tick.

### Live observation (host hook)

When configured for `provider=openrouter_custom`, the conversation loop
invokes `OpenRouterCustomProfile.observe_outcome(response, error,
request_models)` after every chat completion (streaming and non-stream).
The hook attributes:

* **Success** — to the model echoed back in `response.model` (the actual
  id that served the request).
* **`bypassed` failures** — to every model that appeared BEFORE the
  responder in `request_models` (i.e. OpenRouter walked past them).
* **Top-level errors** (RateLimitError, BadRequestError, etc.) — to
  every model in `request_models`.

The hook is opt-in: providers without an `observe_outcome` attribute
are skipped.

### Filter semantics

| Field                       | Behaviour                                                                  |
|-----------------------------|----------------------------------------------------------------------------|
| `price.prompt_max`          | Maximum input price in **USD per million tokens**. `0` = free-only.        |
| `price.completion_max`      | Maximum output price in **USD per million tokens**. `0` = free-only.       |
| `free_only` (back-compat)   | Model `id` must end with `:free`. Wins over the price budget when set.     |
| `min_context`               | Model `context_length` must be `>=` this number (tokens).                  |
| `require_tools`             | `supported_parameters` must contain `"tools"`.                             |
| `modality`                  | `text`, `text+image`, or `any`. Substring match against `architecture.modality`. |
| `exclude_patterns`          | Python regex blacklist on `id`. First match drops the model.               |
| `prefer_patterns`           | Python regex used for ranking boost. More matches = higher score.          |

### Ranking semantics

Primary key (`rank_by`) computed per candidate, then tiebreakers applied
left-to-right. All keys are "higher is better".

| Key             | Feature                                                                  |
|-----------------|--------------------------------------------------------------------------|
| `prefer_match`  | Number of `prefer_patterns` regexes that match the id.                   |
| `context_desc`  | `context_length`.                                                        |
| `modality_pref` | `text+image` → 2, `text` → 1.                                            |
| `tools_count`   | Length of `supported_parameters`.                                        |
| `latency_p95`   | Inverted p95 latency injected from runtime metrics (future work).        |

## State file

Written to `$HERMES_HOME/plugins/openrouter_custom/state.json` by the
cron job:

```json
{
  "last_refresh_iso": "2026-05-29T18:00:00Z",
  "pseudo_alias": "or-best-free",
  "real_model_id": "qwen/qwen3-coder:free",
  "candidate_count": 6,
  "candidates_top": [
    {"id": "qwen/qwen3-coder:free", "context_length": 262144, "modality": "text", "supports_tools": true}
  ],
  "reason": "ranked 6 candidates by prefer_match"
}
```

## How it plugs in

* `__init__.py` registers `provider: openrouter_custom` in Hermes' provider
  registry, with `fetch_models()` returning **only** the pseudo alias —
  picker always shows one stable entry.
* On every new session, the `on_session_start` hook checks if the current
  agent is configured for `openrouter_custom` + the pseudo alias; if yes,
  swaps `agent.model` to `state.real_model_id` before any LLM call goes
  out.
* `refresh.py` is the cron entry point — Hermes' cron system runs it every
  N minutes (created automatically at install time by the bootstrap
  script in the parent repo).

## Failure modes

* **Catalog fetch fails** → behaviour per `refresh.on_failure`:
  * `keep_last` (default) — leaves state.json untouched.
  * `rotate` — promotes the next-ranked candidate from the previous run.
  * `fallback_static` — pins `fallback_static_id`.
* **No candidates pass filters** → same strategies.
* **State file missing when session starts** → plugin warns, session
  proceeds with the pseudo alias as the model name (OpenRouter rejects
  it). Operators should run `refresh.py` once after install before the
  first session.

## Running tests

```bash
python3 -m pytest tests/ -v
```

All selector logic is pure-function. Network is stubbed in fixtures.

## License

MIT (see LICENSE).
