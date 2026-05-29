/**
 * openrouter_custom dashboard plugin — UI for editing filter/ranking/refresh
 * knobs and watching the current candidate pool.
 *
 * Plain IIFE — no bundler. Uses globals exposed by the Hermes plugin SDK:
 *   window.__HERMES_PLUGIN_SDK__.{React, hooks, components, useI18n, ...}
 * Strings go through tx() with English fallbacks so the bundle still renders
 * against host SDKs that don't ship a translation namespace for this plugin
 * yet. When the host adds a t.openrouter_custom.<key> entry, the value
 * overrides automatically.
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const React = SDK.React;
  const { useState, useEffect, useCallback } = SDK.hooks;
  const C = SDK.components;
  const {
    Card, CardHeader, CardTitle, CardContent,
    Badge, Button, Input, Label,
  } = C;
  const h = React.createElement;

  // Locale shim — older host bundles may not expose useI18n yet.
  const useI18n = SDK.useI18n || function () {
    return { t: { openrouter_custom: null }, locale: "en" };
  };

  // tx(t, "section.key", "English fallback", optional vars)
  function tx(t, path, fallback, vars) {
    let node = t && t.openrouter_custom;
    if (node) {
      const parts = path.split(".");
      for (let i = 0; i < parts.length; i++) {
        if (node && typeof node === "object" && parts[i] in node) {
          node = node[parts[i]];
        } else { node = null; break; }
      }
    }
    let str = (typeof node === "string") ? node : fallback;
    if (vars) {
      for (const k in vars) {
        str = str.replace(new RegExp("\\{" + k + "\\}", "g"), vars[k]);
      }
    }
    return str;
  }

  const API_BASE = "/api/plugins/openrouter_custom";

  async function api(path, options) {
    const url = API_BASE + path;
    const token = window.__HERMES_SESSION_TOKEN__ || "";
    const headers = Object.assign({}, (options && options.headers) || {});
    if (token) headers["X-Hermes-Session-Token"] = token;
    const res = await fetch(url, Object.assign({}, options || {}, { headers: headers }));
    if (!res.ok) {
      const text = await res.text().catch(function () { return res.statusText; });
      throw new Error(res.status + ": " + text);
    }
    const text = await res.text();
    try { return JSON.parse(text); } catch (_) { return null; }
  }

  function deepClone(o) { return JSON.parse(JSON.stringify(o)); }

  function asNumber(v, dflt) {
    if (v === "" || v === null || v === undefined) return dflt;
    const n = Number(v);
    return isFinite(n) ? n : dflt;
  }

  function lines(arr) { return (arr || []).join("\n"); }
  function fromLines(s) {
    return String(s || "")
      .split("\n")
      .map(function (l) { return l.trim(); })
      .filter(Boolean);
  }

  // ── live re-rank — JS mirror of selector.py:rank() ──
  // Operates on state.candidates_top entries (already filtered) so the
  // table reflects the *current* form values, not whatever ranking was
  // active at the last cron tick. Diverges from selector.py in one place:
  // state only carries a boolean ``supports_tools`` (the original
  // ``supported_parameters`` array is discarded on serialization), so
  // ``tools_count`` collapses to 1 / 0 here.
  function compileRegex(pattern) {
    try { return new RegExp(pattern, "i"); }
    catch (_) { return null; }
  }
  function features(item, preferRxs) {
    const mid = String(item.id || "");
    const modality = String(item.modality || "").toLowerCase();
    return {
      prefer_match: preferRxs.reduce(function (n, rx) {
        return n + (rx && rx.test(mid) ? 1 : 0);
      }, 0),
      context_desc: Number(item.context_length) || 0,
      modality_pref: modality.indexOf("image") >= 0 ? 2 : 1,
      tools_count: item.supports_tools ? 1 : 0,
      params_desc: Number(item.params_billions) || 0,
      latency_p95: -1 * (Number(item._latency_p95) || 0),
    };
  }
  function rankLive(items, rankingCfg, preferPatterns) {
    if (!items || !items.length) return [];
    const primary = (rankingCfg && rankingCfg.rank_by) || "prefer_match";
    const tiebreakers = (rankingCfg && rankingCfg.tiebreakers) || [];
    const orderedKeys = [primary].concat(
      tiebreakers.filter(function (k) { return k !== primary; })
    );
    const preferRxs = (preferPatterns || []).map(compileRegex);
    // Decorate with sort tuple, sort stable, undecorate.
    const decorated = items.map(function (it, idx) {
      const f = features(it, preferRxs);
      const tuple = orderedKeys.map(function (k) { return -(f[k] || 0); });
      return { it: it, tuple: tuple, idx: idx };
    });
    decorated.sort(function (a, b) {
      for (let i = 0; i < a.tuple.length; i++) {
        if (a.tuple[i] !== b.tuple[i]) return a.tuple[i] - b.tuple[i];
      }
      return a.idx - b.idx; // stable
    });
    return decorated.map(function (d) { return d.it; });
  }

  function TextRow(props) {
    return h("div", { className: "flex flex-col gap-1" },
      h(Label, null, props.label),
      h(Input, {
        type: props.type || "text",
        value: props.value === null || props.value === undefined ? "" : String(props.value),
        onChange: function (e) { props.onChange(e.target.value); },
        placeholder: props.placeholder || "",
      }),
      props.hint && h("span", { className: "text-xs text-muted-foreground" }, props.hint),
    );
  }

  function NumberRow(props) {
    return TextRow(Object.assign({}, props, {
      type: "number",
      onChange: function (v) { props.onChange(asNumber(v, props.fallback)); },
    }));
  }

  function CheckRow(props) {
    return h("div", { className: "flex items-center gap-2" },
      h("input", {
        type: "checkbox",
        checked: !!props.value,
        onChange: function (e) { props.onChange(e.target.checked); },
        className: "h-4 w-4",
      }),
      h(Label, null, props.label),
    );
  }

  function SelectRow(props) {
    return h("div", { className: "flex flex-col gap-1" },
      h(Label, null, props.label),
      h("select", {
        value: props.value || "",
        onChange: function (e) { props.onChange(e.target.value); },
        className: "border border-border bg-background/40 px-3 py-2 text-sm",
      }, props.options.map(function (opt) {
        return h("option", { key: opt, value: opt }, opt);
      })),
    );
  }

  function TextAreaRow(props) {
    return h("div", { className: "flex flex-col gap-1" },
      h(Label, null, props.label),
      h("textarea", {
        value: props.value || "",
        onChange: function (e) { props.onChange(e.target.value); },
        rows: props.rows || 4,
        placeholder: props.placeholder || "",
        className: "border border-border bg-background/40 px-3 py-2 text-sm font-courier",
      }),
      props.hint && h("span", { className: "text-xs text-muted-foreground" }, props.hint),
    );
  }

  function OpenRouterCustomPage() {
    const { t } = useI18n();
    const [defaults, setDefaults] = useState(null);
    const [config, setConfig] = useState(null);
    const [state, setState] = useState(null);
    const [busy, setBusy] = useState(false);
    const [msg, setMsg] = useState(null);
    const [err, setErr] = useState(null);

    const reload = useCallback(function () {
      setBusy(true);
      Promise.all([api("/config"), api("/state")])
        .then(function (results) {
          setDefaults(results[0].defaults || {});
          setConfig(deepClone(results[0].config || {}));
          setState(results[1] || {});
          setErr(null);
        })
        .catch(function (e) { setErr(String(e && e.message || e)); })
        .finally(function () { setBusy(false); });
    }, []);

    useEffect(reload, [reload]);

    function patch(path, value) {
      const next = deepClone(config || {});
      let cur = next;
      for (let i = 0; i < path.length - 1; i++) {
        const k = path[i];
        cur[k] = cur[k] || {};
        cur = cur[k];
      }
      cur[path[path.length - 1]] = value;
      setConfig(next);
    }

    function get(path, dflt) {
      let cur = config;
      for (let i = 0; cur && i < path.length; i++) cur = cur[path[i]];
      return cur === undefined || cur === null ? dflt : cur;
    }

    function save() {
      setBusy(true); setMsg(null); setErr(null);
      api("/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ config: config }),
      })
        .then(function () {
          setMsg(tx(t, "msg.saved",
            "Saved — applies on the next cron tick, or press Refresh now."));
        })
        .catch(function (e) { setErr(String(e && e.message || e)); })
        .finally(function () { setBusy(false); });
    }

    function refreshNow() {
      setBusy(true); setMsg(null); setErr(null);
      api("/refresh", { method: "POST" })
        .then(function (st) {
          setState(st);
          setMsg(tx(t, "msg.refreshed",
            "Refreshed — picked: {model}",
            { model: st.real_model_id || "(none)" }));
        })
        .catch(function (e) { setErr(String(e && e.message || e)); })
        .finally(function () { setBusy(false); });
    }

    function resetDefaults() {
      if (!defaults) return;
      setConfig(deepClone(defaults));
      setMsg(tx(t, "msg.reset",
        "Reverted to defaults — don't forget to Save."));
    }

    if (!config) {
      return h(Card, null,
        h(CardHeader, null, h(CardTitle, null, tx(t, "title", "OpenRouter Custom"))),
        h(CardContent, null,
          err
            ? h("span", { className: "text-red-500" }, err)
            : tx(t, "loading", "Loading…")),
      );
    }

    const filters = config.filters || {};
    const price = filters.price || {};
    const ranking = config.ranking || {};
    const refresh = config.refresh || {};
    const internal = config.internal_fallback || {};

    return h("div", { className: "flex flex-col gap-6 max-w-4xl" },

      h(Card, null,
        h(CardHeader, null,
          h("div", { className: "flex items-center justify-between" },
            h("div", { className: "flex items-center gap-3" },
              h(CardTitle, null, tx(t, "title", "OpenRouter Custom")),
              h(Badge, { variant: "outline" }, "v0.4.5"),
            ),
            h("div", { className: "flex items-center gap-2" },
              h(Button, { onClick: refreshNow, disabled: busy },
                tx(t, "btn.refresh", "Refresh now")),
              h(Button, { onClick: save, disabled: busy },
                tx(t, "btn.save", "Save")),
              h(Button, { onClick: resetDefaults, disabled: busy, variant: "outline" },
                tx(t, "btn.reset", "Reset to defaults")),
            ),
          ),
        ),
        h(CardContent, { className: "flex flex-col gap-3" },
          msg && h("div", { className: "text-sm text-emerald-500" }, msg),
          err && h("div", { className: "text-sm text-red-500" }, err),
          h("div", { className: "grid grid-cols-2 gap-3 text-sm" },
            h("div", null,
              h("div", { className: "text-muted-foreground" }, tx(t, "summary.alias", "Pseudo alias")),
              h("div", { className: "font-courier" }, (state && state.pseudo_alias) || get(["pseudo_model_alias"], "best-free")),
            ),
            h("div", null,
              h("div", { className: "text-muted-foreground" }, tx(t, "summary.routes_to", "Currently routes to")),
              h("div", { className: "font-courier" }, (state && state.real_model_id) || tx(t, "summary.no_state", "(no state — run Refresh now)")),
            ),
            h("div", null,
              h("div", { className: "text-muted-foreground" }, tx(t, "summary.candidates", "Candidate count")),
              h("div", { className: "font-courier" }, state ? state.candidate_count : "—"),
            ),
            h("div", null,
              h("div", { className: "text-muted-foreground" }, tx(t, "summary.last_refresh", "Last refresh")),
              h("div", { className: "font-courier text-xs" }, (state && state.last_refresh_iso) || "—"),
            ),
          ),
        ),
      ),

      h(Card, null,
        h(CardHeader, null, h(CardTitle, { className: "text-base" }, tx(t, "section.alias", "Alias"))),
        h(CardContent, null,
          TextRow({
            label: tx(t, "alias.label", "Pseudo model alias (stable name shown in /model)"),
            value: get(["pseudo_model_alias"], "best-free"),
            onChange: function (v) { patch(["pseudo_model_alias"], v); },
            hint: tx(t, "alias.hint",
              "After Save, run '/model <new-alias> --provider openrouter_custom --global' to switch sessions to the new name."),
          }),
        ),
      ),

      h(Card, null,
        h(CardHeader, null, h(CardTitle, { className: "text-base" }, tx(t, "section.filters", "Filters"))),
        h(CardContent, { className: "grid grid-cols-2 gap-4" },
          NumberRow({
            label: tx(t, "filters.prompt_max", "Price: prompt_max (USD per million tokens)"),
            value: price.prompt_max,
            fallback: 0,
            onChange: function (v) { patch(["filters", "price", "prompt_max"], v); },
            hint: tx(t, "filters.prompt_max_hint",
              "0 = free-only. 0.5 surfaces near-free paid tiers."),
          }),
          NumberRow({
            label: tx(t, "filters.completion_max", "Price: completion_max (USD per million tokens)"),
            value: price.completion_max,
            fallback: 0,
            onChange: function (v) { patch(["filters", "price", "completion_max"], v); },
          }),
          NumberRow({
            label: tx(t, "filters.min_context", "Min context (tokens)"),
            value: filters.min_context,
            fallback: 65536,
            onChange: function (v) { patch(["filters", "min_context"], v); },
            hint: tx(t, "filters.min_context_hint",
              "Hermes system prompt + tools ≈ 35k; <64k will not fit."),
          }),
          SelectRow({
            label: tx(t, "filters.modality", "Modality"),
            value: filters.modality || "text",
            options: ["text", "text+image", "any"],
            onChange: function (v) { patch(["filters", "modality"], v); },
          }),
          CheckRow({
            label: tx(t, "filters.require_tools", "Require tools (function calling)"),
            value: !!filters.require_tools,
            onChange: function (v) { patch(["filters", "require_tools"], v); },
          }),
          CheckRow({
            label: tx(t, "filters.free_only", "Free-only shortcut (id ends with :free)"),
            value: !!filters.free_only,
            onChange: function (v) { patch(["filters", "free_only"], v); },
          }),
          h("div", { className: "col-span-2" },
            TextAreaRow({
              label: tx(t, "filters.exclude_patterns", "Exclude patterns (regex, one per line)"),
              value: lines(filters.exclude_patterns),
              onChange: function (v) { patch(["filters", "exclude_patterns"], fromLines(v)); },
              hint: tx(t, "filters.exclude_patterns_hint",
                "First match drops the candidate from the pool."),
              rows: 3,
            }),
          ),
          h("div", { className: "col-span-2" },
            TextAreaRow({
              label: tx(t, "filters.prefer_patterns", "Prefer patterns (regex, one per line)"),
              value: lines(filters.prefer_patterns),
              onChange: function (v) { patch(["filters", "prefer_patterns"], fromLines(v)); },
              placeholder: "qwen3\nllama-3\\.3\ndeepseek\nkimi-k2",
              hint: tx(t, "filters.prefer_patterns_hint",
                "More matches → higher rank when rank_by = prefer_match. " +
                "Empty pool → all candidates are tied on this key (the tiebreakers decide)."),
              rows: 4,
            }),
          ),
        ),
      ),

      h(Card, null,
        h(CardHeader, null, h(CardTitle, { className: "text-base" }, tx(t, "section.ranking", "Ranking"))),
        h(CardContent, { className: "flex flex-col gap-4" },
          h("div", { className: "grid grid-cols-2 gap-4" },
            SelectRow({
              label: tx(t, "ranking.primary", "Primary key"),
              value: ranking.rank_by || "prefer_match",
              options: ["prefer_match", "context_desc", "params_desc", "modality_pref", "tools_count", "latency_p95"],
              onChange: function (v) { patch(["ranking", "rank_by"], v); },
            }),
            h("div", { className: "col-span-2" },
              TextAreaRow({
                label: tx(t, "ranking.tiebreakers", "Tiebreakers (one per line, in order)"),
                value: lines(ranking.tiebreakers),
                onChange: function (v) { patch(["ranking", "tiebreakers"], fromLines(v)); },
                hint: tx(t, "ranking.tiebreakers_hint",
                  "Allowed: prefer_match, context_desc, params_desc, modality_pref, tools_count, latency_p95. " +
                  "The primary key is auto-skipped if you list it here too."),
                rows: 3,
              }),
            ),
          ),
          h("details", { className: "rounded border border-border/60 px-3 py-2 text-xs" },
            h("summary", { className: "cursor-pointer text-muted-foreground select-none" },
              tx(t, "ranking.help.summary", "How does ranking work? (key meanings + example)")),
            h("div", { className: "mt-2 space-y-3" },
              h("p", null,
                tx(t, "ranking.intro",
                  "Each surviving candidate gets a score per key. Sort is " +
                  "\"higher is better\" — the candidate at the top of the list " +
                  "becomes the alias target. The PRIMARY key decides first; " +
                  "TIEBREAKERS are applied left-to-right only when the primary " +
                  "score is tied.")),
              h("div", { className: "space-y-1" },
                h("div", { className: "uppercase tracking-wider text-muted-foreground" },
                  tx(t, "ranking.legend.title", "What each key means")),
                h("div", null,
                  h("span", { className: "font-courier text-emerald-500" }, "prefer_match"),
                  " — ",
                  tx(t, "ranking.legend.prefer_match",
                    "Count of PREFER_PATTERNS regexes matching the model id. " +
                    "More matches → higher. Useful to softly steer toward " +
                    "a model family (qwen3, llama-3.3, etc.) without hard-pinning.")),
                h("div", null,
                  h("span", { className: "font-courier text-emerald-500" }, "context_desc"),
                  " — ",
                  tx(t, "ranking.legend.context_desc",
                    "Raw context_length (tokens). Larger → higher. " +
                    "Useful when long prompts/transcripts are expected.")),
                h("div", null,
                  h("span", { className: "font-courier text-emerald-500" }, "modality_pref"),
                  " — ",
                  tx(t, "ranking.legend.modality_pref",
                    "text+image → 2; text → 1. Promotes multimodal models when " +
                    "your modality filter allows them.")),
                h("div", null,
                  h("span", { className: "font-courier text-emerald-500" }, "tools_count"),
                  " — ",
                  tx(t, "ranking.legend.tools_count",
                    "Length of supported_parameters array (tools, response_format, …). " +
                    "More native features → higher. Loose proxy for \"feature-richer\" model.")),
                h("div", null,
                  h("span", { className: "font-courier text-emerald-500" }, "params_desc"),
                  " — ",
                  tx(t, "ranking.legend.params_desc",
                    "Parameter count in billions, sniffed from the model description " +
                    "(\"Llama 3.3 70B\" → 70). Bigger model → higher. Missing/unparseable " +
                    "descriptions get 0 and rank last. The laguna and kimi families are " +
                    "hardcoded since their descriptions omit the count.")),
                h("div", null,
                  h("span", { className: "font-courier text-emerald-500" }, "latency_p95"),
                  " — ",
                  tx(t, "ranking.legend.latency_p95",
                    "Inverted p95 latency from runtime metrics. Currently 0 for all " +
                    "candidates — runtime metric injection is future work, so this " +
                    "key is a no-op until then.")),
              ),
              h("div", { className: "space-y-1" },
                h("div", { className: "uppercase tracking-wider text-muted-foreground" },
                  tx(t, "ranking.example.title", "Worked example")),
                h("p", null,
                  tx(t, "ranking.example.body",
                    "Primary = prefer_match; tiebreakers = [context_desc, modality_pref]. " +
                    "Three candidates pass the filter:")),
                h("ul", { className: "list-disc list-inside text-muted-foreground" },
                  h("li", null, tx(t, "ranking.example.a",
                    "qwen3-coder:free  → prefer_match=1, context=262144, modality=text")),
                  h("li", null, tx(t, "ranking.example.b",
                    "qwen3-next:free   → prefer_match=1, context=131072, modality=text")),
                  h("li", null, tx(t, "ranking.example.c",
                    "zzz-aurora:free   → prefer_match=0, context=200000, modality=text")),
                ),
                h("p", null,
                  tx(t, "ranking.example.result",
                    "Order: qwen3-coder (1, 262144) ▸ qwen3-next (1, 131072) ▸ zzz-aurora (0). " +
                    "qwen3-coder wins on primary tie via larger context.")),
              ),
            ),
          ),
        ),
      ),

      h(Card, null,
        h(CardHeader, null, h(CardTitle, { className: "text-base" }, tx(t, "section.refresh", "Refresh"))),
        h(CardContent, { className: "grid grid-cols-2 gap-4" },
          NumberRow({
            label: tx(t, "refresh.cron_minutes", "Cron interval (minutes)"),
            value: refresh.cron_minutes,
            fallback: 30,
            onChange: function (v) { patch(["refresh", "cron_minutes"], v); },
            hint: tx(t, "refresh.cron_minutes_hint",
              "The cron jobs.json entry is registered separately — see infra/hermes/cron-jobs.yaml."),
          }),
          SelectRow({
            label: tx(t, "refresh.on_failure", "On fetch failure"),
            value: refresh.on_failure || "keep_last",
            options: ["keep_last", "rotate", "fallback_static"],
            onChange: function (v) { patch(["refresh", "on_failure"], v); },
          }),
          TextRow({
            label: tx(t, "refresh.fallback_id", "Fallback static id (used when on_failure = fallback_static)"),
            value: refresh.fallback_static_id || "",
            onChange: function (v) { patch(["refresh", "fallback_static_id"], v); },
            placeholder: "qwen/qwen3-coder:free",
          }),
          NumberRow({
            label: tx(t, "refresh.max_candidates", "Max candidates kept"),
            value: config.max_candidates,
            fallback: 10,
            onChange: function (v) { patch(["max_candidates"], v); },
          }),
        ),
      ),

      h(Card, null,
        h(CardHeader, null,
          h(CardTitle, { className: "text-base" },
            tx(t, "section.internal_fallback", "Internal sequential fallback")),
        ),
        h(CardContent, { className: "flex flex-col gap-3" },
          h("p", { className: "text-xs text-muted-foreground" },
            tx(t, "internal_fallback.intro",
              "Applies only when the request uses the pseudo alias above " +
              "(direct picks of a concrete model are never wrapped). " +
              "Implemented via OpenRouter's native models[] request-body " +
              "parameter — OR walks the list server-side and only surfaces " +
              "a hard failure to Hermes when every candidate refuses.")),
          NumberRow({
            label: tx(t, "internal_fallback.sequential_count",
              "Sequential fallback depth"),
            value: internal.sequential_count,
            fallback: 1,
            onChange: function (v) {
              const n = Math.max(1, Math.floor(asNumber(v, 1)));
              patch(["internal_fallback", "sequential_count"], n);
            },
            hint: tx(t, "internal_fallback.sequential_count_hint",
              "1 = current behaviour (no fallback). N > 1 sends the top-N " +
              "candidate ids; OR falls through to the next on 4xx/5xx/timeout. " +
              "Capped by the current candidate pool size."),
          }),
          h("p", { className: "text-xs text-amber-500/80" },
            tx(t, "internal_fallback.caveat",
              "Caveat: OR's native fallback fires only on transport-level " +
              "failures. A 200 OK whose content is a refusal (\"I can't help " +
              "with that\") is success from OR's perspective and is NOT " +
              "retried.")),
        ),
      ),

      state && state.candidates_top && (function () {
        const liveRanked = rankLive(
          state.candidates_top, ranking, filters.prefer_patterns || []
        );
        // Detect divergence from server-side order (last cron tick).
        let diverged = false;
        for (let i = 0; i < liveRanked.length; i++) {
          if ((state.candidates_top[i] || {}).id !== liveRanked[i].id) {
            diverged = true; break;
          }
        }
        return h(Card, null,
          h(CardHeader, null,
            h("div", { className: "flex items-center justify-between gap-3" },
              h(CardTitle, { className: "text-base" },
                tx(t, "section.pool", "Current candidate pool")),
              diverged && h(Badge, { variant: "outline" },
                tx(t, "pool.live_rerank_badge", "live re-rank (unsaved)")),
            ),
          ),
          h(CardContent, { className: "flex flex-col gap-2" },
            h("p", { className: "text-xs text-muted-foreground" },
              tx(t, "pool.order_hint",
                "Sorted client-side using the Ranking section above — " +
                "changes update instantly. ★ marks the model currently " +
                "behind the alias in state.json; it only moves when you " +
                "press Save + Refresh now, so after editing Ranking the " +
                "star may not be at the top of this list until then.")),
            h("table", { className: "w-full text-sm font-courier" },
              h("thead", null, h("tr", { className: "text-muted-foreground" },
                h("th", { className: "text-left py-1" }, tx(t, "pool.rank", "#")),
                h("th", { className: "text-left py-1" }, tx(t, "pool.model", "Model")),
                h("th", { className: "text-right py-1" }, tx(t, "pool.params", "Params")),
                h("th", { className: "text-right py-1" }, tx(t, "pool.context", "Context")),
                h("th", { className: "text-right py-1" }, tx(t, "pool.tools", "Tools")),
                h("th", { className: "text-right py-1" }, tx(t, "pool.modality", "Modality")),
                h("th", { className: "text-right py-1" }, tx(t, "pool.price_in", "$/M in")),
                h("th", { className: "text-right py-1" }, tx(t, "pool.price_out", "$/M out")),
              )),
              h("tbody", null, liveRanked.map(function (c, idx) {
                const isPick = c.id === state.real_model_id;
                return h("tr", {
                  key: c.id,
                  className: isPick ? "bg-emerald-500/10" : "",
                },
                  h("td", { className: "py-1 text-muted-foreground" }, idx + 1),
                  h("td", { className: "py-1" }, (isPick ? "★ " : "  ") + c.id),
                  h("td", { className: "py-1 text-right" }, c.params_display || "—"),
                  h("td", { className: "py-1 text-right" }, c.context_length),
                  h("td", { className: "py-1 text-right" }, c.supports_tools ? "✓" : "—"),
                  h("td", { className: "py-1 text-right" }, c.modality || "—"),
                  h("td", { className: "py-1 text-right" }, c.prompt_per_million_usd != null ? c.prompt_per_million_usd.toFixed(3) : "—"),
                  h("td", { className: "py-1 text-right" }, c.completion_per_million_usd != null ? c.completion_per_million_usd.toFixed(3) : "—"),
                );
              })),
            ),
          ),
        );
      })(),
    );
  }

  window.__HERMES_PLUGINS__.register("openrouter_custom", OpenRouterCustomPage);
})();
