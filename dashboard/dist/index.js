/**
 * openrouter_custom dashboard plugin — UI for editing filter/ranking/refresh
 * knobs and watching the current candidate pool.
 *
 * Plain IIFE — no bundler. Uses globals exposed by the Hermes plugin SDK:
 *   window.__HERMES_PLUGIN_SDK__.{React, hooks, components, fetchJSON, ...}
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const { React } = SDK;
  const { useState, useEffect, useCallback } = SDK.hooks;
  const {
    Card, CardHeader, CardTitle, CardContent,
    Badge, Button, Input, Label, Separator,
  } = SDK.components;
  const h = React.createElement;

  const API = "/api/plugins/openrouter_custom";

  // ── helpers ──────────────────────────────────────────────────────────────

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

  // ── small input components ───────────────────────────────────────────────

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
        className: "border border-border bg-background/40 px-3 py-2 text-sm font-courier",
      }),
      props.hint && h("span", { className: "text-xs text-muted-foreground" }, props.hint),
    );
  }

  // ── main page ────────────────────────────────────────────────────────────

  function OpenRouterCustomPage() {
    const [defaults, setDefaults] = useState(null);
    const [config, setConfig] = useState(null);
    const [state, setState] = useState(null);
    const [busy, setBusy] = useState(false);
    const [msg, setMsg] = useState(null);
    const [err, setErr] = useState(null);

    const reload = useCallback(function () {
      setBusy(true);
      Promise.all([
        SDK.fetchJSON(API + "/config"),
        SDK.fetchJSON(API + "/state"),
      ])
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
      SDK.fetchJSON(API + "/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ config: config }),
      })
        .then(function () { setMsg("Saved — будет применено на следующем cron tick'е или нажми Refresh now."); })
        .catch(function (e) { setErr(String(e && e.message || e)); })
        .finally(function () { setBusy(false); });
    }

    function refreshNow() {
      setBusy(true); setMsg(null); setErr(null);
      SDK.fetchJSON(API + "/refresh", { method: "POST" })
        .then(function (st) {
          setState(st);
          setMsg("Refreshed — выбрано: " + (st.real_model_id || "(none)"));
        })
        .catch(function (e) { setErr(String(e && e.message || e)); })
        .finally(function () { setBusy(false); });
    }

    function resetDefaults() {
      if (!defaults) return;
      setConfig(deepClone(defaults));
      setMsg("Сброшено к defaults — не забудь Save.");
    }

    if (!config) {
      return h(Card, null,
        h(CardHeader, null, h(CardTitle, null, "OpenRouter Custom")),
        h(CardContent, null,
          err
            ? h("span", { className: "text-red-500" }, err)
            : "Loading…"),
      );
    }

    const filters = config.filters || {};
    const price = filters.price || {};
    const ranking = config.ranking || {};
    const refresh = config.refresh || {};

    return h("div", { className: "flex flex-col gap-6 max-w-4xl" },

      // ── Top: current pick + actions ────────────────────────────────────
      h(Card, null,
        h(CardHeader, null,
          h("div", { className: "flex items-center justify-between" },
            h("div", { className: "flex items-center gap-3" },
              h(CardTitle, null, "OpenRouter Custom"),
              h(Badge, { variant: "outline" }, "v0.3.0"),
            ),
            h("div", { className: "flex items-center gap-2" },
              h(Button, { onClick: refreshNow, disabled: busy }, "Refresh now"),
              h(Button, { onClick: save, disabled: busy }, "Save"),
              h(Button, { onClick: resetDefaults, disabled: busy, variant: "outline" }, "Reset to defaults"),
            ),
          ),
        ),
        h(CardContent, { className: "flex flex-col gap-3" },
          msg && h("div", { className: "text-sm text-emerald-500" }, msg),
          err && h("div", { className: "text-sm text-red-500" }, err),
          h("div", { className: "grid grid-cols-2 gap-3 text-sm" },
            h("div", null,
              h("div", { className: "text-muted-foreground" }, "Pseudo alias"),
              h("div", { className: "font-courier" }, (state && state.pseudo_alias) || get(["pseudo_model_alias"], "best-free")),
            ),
            h("div", null,
              h("div", { className: "text-muted-foreground" }, "Currently routes to"),
              h("div", { className: "font-courier" }, (state && state.real_model_id) || "(no state — run Refresh now)"),
            ),
            h("div", null,
              h("div", { className: "text-muted-foreground" }, "Candidate count"),
              h("div", { className: "font-courier" }, state ? state.candidate_count : "—"),
            ),
            h("div", null,
              h("div", { className: "text-muted-foreground" }, "Last refresh"),
              h("div", { className: "font-courier text-xs" }, (state && state.last_refresh_iso) || "—"),
            ),
          ),
        ),
      ),

      // ── Pseudo alias ───────────────────────────────────────────────────
      h(Card, null,
        h(CardHeader, null, h(CardTitle, { className: "text-base" }, "Alias")),
        h(CardContent, null,
          TextRow({
            label: "Pseudo model alias (stable name shown in /model)",
            value: get(["pseudo_model_alias"], "best-free"),
            onChange: function (v) { patch(["pseudo_model_alias"], v); },
            hint: "Smena imeni unaslediut posle Save + ' /model <new-alias> --provider openrouter_custom --global '.",
          }),
        ),
      ),

      // ── Filters ────────────────────────────────────────────────────────
      h(Card, null,
        h(CardHeader, null, h(CardTitle, { className: "text-base" }, "Filters")),
        h(CardContent, { className: "grid grid-cols-2 gap-4" },
          NumberRow({
            label: "Price: prompt_max (USD per million tokens)",
            value: price.prompt_max,
            fallback: 0,
            onChange: function (v) { patch(["filters", "price", "prompt_max"], v); },
            hint: "0 = только полностью бесплатные. 0.5 — включит почти-бесплатные.",
          }),
          NumberRow({
            label: "Price: completion_max (USD per million tokens)",
            value: price.completion_max,
            fallback: 0,
            onChange: function (v) { patch(["filters", "price", "completion_max"], v); },
          }),
          NumberRow({
            label: "Min context (tokens)",
            value: filters.min_context,
            fallback: 65536,
            onChange: function (v) { patch(["filters", "min_context"], v); },
            hint: "Hermes system prompt + tools ≈ 35k; <64k не подойдёт.",
          }),
          SelectRow({
            label: "Modality",
            value: filters.modality || "text",
            options: ["text", "text+image", "any"],
            onChange: function (v) { patch(["filters", "modality"], v); },
          }),
          CheckRow({
            label: "Require tools (function calling)",
            value: !!filters.require_tools,
            onChange: function (v) { patch(["filters", "require_tools"], v); },
          }),
          CheckRow({
            label: "Free-only shortcut (id ends with :free)",
            value: !!filters.free_only,
            onChange: function (v) { patch(["filters", "free_only"], v); },
          }),
          h("div", { className: "col-span-2" },
            TextAreaRow({
              label: "Exclude patterns (regex, one per line)",
              value: lines(filters.exclude_patterns),
              onChange: function (v) { patch(["filters", "exclude_patterns"], fromLines(v)); },
              hint: "Первое совпадение выкидывает модель из пула.",
              rows: 3,
            }),
          ),
          h("div", { className: "col-span-2" },
            TextAreaRow({
              label: "Prefer patterns (regex, one per line)",
              value: lines(filters.prefer_patterns),
              onChange: function (v) { patch(["filters", "prefer_patterns"], fromLines(v)); },
              hint: "Чем больше совпадений — тем выше rank при rank_by=prefer_match.",
              rows: 4,
            }),
          ),
        ),
      ),

      // ── Ranking ────────────────────────────────────────────────────────
      h(Card, null,
        h(CardHeader, null, h(CardTitle, { className: "text-base" }, "Ranking")),
        h(CardContent, { className: "grid grid-cols-2 gap-4" },
          SelectRow({
            label: "Primary key",
            value: ranking.rank_by || "prefer_match",
            options: ["prefer_match", "context_desc", "modality_pref", "tools_count", "latency_p95"],
            onChange: function (v) { patch(["ranking", "rank_by"], v); },
          }),
          h("div", { className: "col-span-2" },
            TextAreaRow({
              label: "Tiebreakers (one per line, in order)",
              value: lines(ranking.tiebreakers),
              onChange: function (v) { patch(["ranking", "tiebreakers"], fromLines(v)); },
              hint: "Допустимы: prefer_match, context_desc, modality_pref, tools_count, latency_p95.",
              rows: 3,
            }),
          ),
        ),
      ),

      // ── Refresh ────────────────────────────────────────────────────────
      h(Card, null,
        h(CardHeader, null, h(CardTitle, { className: "text-base" }, "Refresh")),
        h(CardContent, { className: "grid grid-cols-2 gap-4" },
          NumberRow({
            label: "Cron interval (minutes)",
            value: refresh.cron_minutes,
            fallback: 30,
            onChange: function (v) { patch(["refresh", "cron_minutes"], v); },
            hint: "Cron jobs.json обновляется отдельно — см. infra/hermes/cron-jobs.yaml.",
          }),
          SelectRow({
            label: "On fetch failure",
            value: refresh.on_failure || "keep_last",
            options: ["keep_last", "rotate", "fallback_static"],
            onChange: function (v) { patch(["refresh", "on_failure"], v); },
          }),
          TextRow({
            label: "Fallback static id (когда on_failure = fallback_static)",
            value: refresh.fallback_static_id || "",
            onChange: function (v) { patch(["refresh", "fallback_static_id"], v); },
          }),
          NumberRow({
            label: "Max candidates kept",
            value: config.max_candidates,
            fallback: 10,
            onChange: function (v) { patch(["max_candidates"], v); },
          }),
        ),
      ),

      // ── State viewer ───────────────────────────────────────────────────
      state && state.candidates_top && h(Card, null,
        h(CardHeader, null, h(CardTitle, { className: "text-base" }, "Current candidate pool")),
        h(CardContent, null,
          h("table", { className: "w-full text-sm font-courier" },
            h("thead", null, h("tr", { className: "text-muted-foreground" },
              h("th", { className: "text-left py-1" }, "Model"),
              h("th", { className: "text-right py-1" }, "Context"),
              h("th", { className: "text-right py-1" }, "Tools"),
              h("th", { className: "text-right py-1" }, "Modality"),
              h("th", { className: "text-right py-1" }, "$/M in"),
              h("th", { className: "text-right py-1" }, "$/M out"),
            )),
            h("tbody", null, state.candidates_top.map(function (c, i) {
              const isPick = c.id === state.real_model_id;
              return h("tr", {
                key: c.id,
                className: isPick ? "bg-emerald-500/10" : "",
              },
                h("td", { className: "py-1" }, (isPick ? "★ " : "  ") + c.id),
                h("td", { className: "py-1 text-right" }, c.context_length),
                h("td", { className: "py-1 text-right" }, c.supports_tools ? "✓" : "—"),
                h("td", { className: "py-1 text-right" }, c.modality || "—"),
                h("td", { className: "py-1 text-right" }, c.prompt_per_million_usd != null ? c.prompt_per_million_usd.toFixed(3) : "—"),
                h("td", { className: "py-1 text-right" }, c.completion_per_million_usd != null ? c.completion_per_million_usd.toFixed(3) : "—"),
              );
            })),
          ),
        ),
      ),
    );
  }

  SDK.registerPlugin({
    name: "openrouter_custom",
    component: OpenRouterCustomPage,
  });
})();
