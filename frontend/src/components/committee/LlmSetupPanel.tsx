"use client";

// Provider + key + model setup. Replaces the single DeepSeek ApiKeyField.
//
// Keys for all five providers can be held at once so switching provider does not
// mean re-pasting; they live in sessionStorage (see lib/llm.ts) and the browser
// erases them when it closes. The provider/model choice lives in localStorage —
// selection only, never a secret.

import { useEffect, useMemo, useState } from "react";
import { Eye, EyeOff, RotateCw, Trash2 } from "lucide-react";
import {
  DEFAULT_IDLE_MINUTES,
  FALLBACK_PROVIDERS,
  IDLE_MINUTE_CHOICES,
  fetchModels,
  fetchProviders,
  isRunnable,
  type LlmVault,
  type ProviderInfo,
} from "@/lib/llm";

const OTHER = "__other__";

export function LlmSetupPanel({
  vault,
  onChange,
  onForget,
}: {
  vault: LlmVault;
  onChange: (next: LlmVault) => void;
  onForget: () => void;
}) {
  const [providers, setProviders] = useState<ProviderInfo[]>(FALLBACK_PROVIDERS);
  const [reveal, setReveal] = useState(false);
  const [catalogue, setCatalogue] = useState<Record<string, string[]>>({});
  const [modelNote, setModelNote] = useState<string | null>(null);
  const [loadingModels, setLoadingModels] = useState(false);

  // The backend registry is the source of truth for which providers are accepted.
  useEffect(() => {
    let alive = true;
    fetchProviders()
      .then((list) => alive && setProviders(list))
      .catch(() => {
        /* offline / backend down — the static fallback list still works */
      });
    return () => {
      alive = false;
    };
  }, []);

  const active = useMemo(
    () => providers.find((p) => p.id === vault.provider) || providers[0],
    [providers, vault.provider],
  );
  const key = vault.keys[vault.provider] || "";
  const models = catalogue[vault.provider] || active?.models || [];
  const isCustomModel = Boolean(vault.model) && !models.includes(vault.model as string);

  function setProvider(id: string) {
    setModelNote(null);
    setReveal(false);
    onChange({ ...vault, provider: id, model: null });
  }

  function setKey(value: string) {
    onChange({ ...vault, keys: { ...vault.keys, [vault.provider]: value } });
  }

  async function refreshModels() {
    if (!active) return;
    setLoadingModels(true);
    setModelNote(null);
    try {
      const res = await fetchModels(active.id, key);
      setCatalogue((c) => ({ ...c, [active.id]: res.models }));
      setModelNote(res.warning || (res.source === "provider" ? `${res.models.length} models available` : null));
    } catch (err) {
      setModelNote(`Could not reach the backend: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setLoadingModels(false);
    }
  }

  const keyedProviders = providers.filter((p) => (vault.keys[p.id] || "").trim());

  return (
    <section className="card p-4">
      <div className="label">AI model</div>
      <p className="mt-1 text-[11px] text-muted">
        Pick a provider and paste its key. Keys are held only in this browser tab and are erased when
        the browser closes{vault.idleMinutes ? `, or after ${vault.idleMinutes} minutes idle` : ""}.
        They are sent to the backend per request and never stored there.
      </p>

      {/* Provider */}
      <div className="mt-3">
        <label className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted">
          Provider
        </label>
        <div className="mt-1 flex flex-wrap gap-1.5">
          {providers.map((p) => {
            const hasKey = Boolean((vault.keys[p.id] || "").trim());
            const activeProvider = p.id === vault.provider;
            return (
              <button
                key={p.id}
                onClick={() => setProvider(p.id)}
                aria-pressed={activeProvider}
                title={
                  hasKey
                    ? `${p.label} — key set for this session`
                    : p.has_server_key
                      ? `${p.label} — no browser key; the server key will be used`
                      : `${p.label} — no key yet`
                }
                className={`flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-medium transition-colors ${
                  activeProvider
                    ? "border-navy bg-navy text-white"
                    : "border-border bg-white text-navy hover:border-navy/40"
                }`}
              >
                <span
                  className={`inline-block h-1.5 w-1.5 rounded-full ${
                    hasKey ? "bg-green" : p.has_server_key ? "bg-navy/40" : "bg-amber"
                  }`}
                />
                {p.label}
              </button>
            );
          })}
        </div>
      </div>

      {/* Key */}
      <div className="mt-3">
        <label className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted">
          {active?.label} API key
        </label>
        <div className="mt-1 flex gap-1.5">
          <input
            type={reveal ? "text" : "password"}
            value={key}
            onChange={(e) => setKey(e.target.value)}
            placeholder={active?.key_placeholder || "sk-…"}
            autoComplete="off"
            spellCheck={false}
            className="w-full rounded border border-border bg-white px-2 py-1.5 text-[13px] text-navy outline-none focus:border-navy"
          />
          <button
            onClick={() => setReveal((r) => !r)}
            title={reveal ? "Hide key" : "Show key"}
            aria-label={reveal ? "Hide key" : "Show key"}
            className="rounded border border-border px-2 text-muted transition-colors hover:border-navy/40 hover:text-navy"
          >
            {reveal ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
          </button>
        </div>
        <p className="mt-1 text-[10.5px] text-muted">
          {key.trim()
            ? "Key set for this session."
            : active?.has_server_key
              ? "No key pasted — the server's own key will be used."
              : "Optional, but this provider has no server key to fall back on."}
          {active?.docs_url ? (
            <>
              {" "}
              <a
                href={active.docs_url}
                target="_blank"
                rel="noreferrer noopener"
                className="underline decoration-dotted underline-offset-2 hover:text-navy"
              >
                Get a key
              </a>
            </>
          ) : null}
        </p>
      </div>

      {/* Model */}
      <div className="mt-3">
        <div className="flex items-center justify-between">
          <label className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted">
            Model
          </label>
          <button
            onClick={refreshModels}
            disabled={loadingModels}
            title="Ask the provider which models this key can call"
            className="flex items-center gap-1 text-[10px] text-muted transition-colors hover:text-navy disabled:opacity-50"
          >
            <RotateCw className={`h-3 w-3 ${loadingModels ? "animate-spin" : ""}`} />
            Refresh list
          </button>
        </div>
        <select
          value={isCustomModel ? OTHER : vault.model || ""}
          onChange={(e) =>
            onChange({ ...vault, model: e.target.value === OTHER ? "" : e.target.value || null })
          }
          className="mt-1 w-full rounded border border-border bg-white px-2 py-1.5 text-[13px] text-navy outline-none focus:border-navy"
        >
          <option value="">Default ({active?.chat_model})</option>
          {models.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
          <option value={OTHER}>Other…</option>
        </select>
        {isCustomModel || vault.model === "" ? (
          <input
            value={vault.model || ""}
            onChange={(e) => onChange({ ...vault, model: e.target.value || "" })}
            placeholder="exact model id"
            spellCheck={false}
            className="mt-1.5 w-full rounded border border-border bg-white px-2 py-1.5 text-[13px] text-navy outline-none focus:border-navy"
          />
        ) : null}
        {modelNote ? <p className="mt-1 text-[10.5px] text-muted">{modelNote}</p> : null}
      </div>

      {/* Compare against other models. The primary above always runs; these run
          alongside it on the same evidence, each in its own tab. */}
      <div className="mt-3 border-t border-border-soft pt-3">
        <label className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted">
          Also ask other models
        </label>
        <p className="mt-1 text-[11px] text-muted">
          Tick any you also want to run. They all study the same evidence, then answer separately — so
          you can see where they agree. Each one costs its own tokens.
        </p>
        <div className="mt-2 flex flex-col gap-1">
          {providers
            .filter((p) => p.id !== vault.provider)
            .map((p) => {
              const runnable = isRunnable(vault, p.id, providers);
              const ticked = (vault.alsoRun || []).includes(p.id);
              return (
                <label
                  key={p.id}
                  title={
                    runnable
                      ? undefined
                      : `${p.label} — add a key above (switch to it) or set one on the server first`
                  }
                  className={`flex items-center gap-2 rounded px-1.5 py-1 text-[12px] ${
                    runnable ? "cursor-pointer text-navy hover:bg-navy/5" : "cursor-not-allowed text-muted"
                  }`}
                >
                  <input
                    type="checkbox"
                    checked={ticked}
                    disabled={!runnable}
                    onChange={(e) =>
                      onChange({
                        ...vault,
                        alsoRun: e.target.checked
                          ? [...(vault.alsoRun || []), p.id]
                          : (vault.alsoRun || []).filter((x) => x !== p.id),
                      })
                    }
                    className="h-3.5 w-3.5 accent-navy"
                  />
                  <span className="font-medium">{p.label}</span>
                  <span className="text-[10px] text-muted">
                    {(vault.keys[p.id] || "").trim()
                      ? "key in this browser"
                      : p.has_server_key
                        ? "server key"
                        : "no key"}
                  </span>
                </label>
              );
            })}
        </div>
      </div>

      {/* Session hygiene */}
      <div className="mt-3 flex flex-wrap items-end justify-between gap-2 border-t border-border-soft pt-3">
        <div>
          <label className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted">
            Erase keys after
          </label>
          <select
            value={vault.idleMinutes ?? DEFAULT_IDLE_MINUTES}
            onChange={(e) => onChange({ ...vault, idleMinutes: Number(e.target.value) })}
            className="mt-1 block rounded border border-border bg-white px-2 py-1 text-[12px] text-navy outline-none focus:border-navy"
          >
            {IDLE_MINUTE_CHOICES.map((m) => (
              <option key={m} value={m}>
                {m === 0 ? "Never (until browser closes)" : `${m} min idle`}
              </option>
            ))}
          </select>
        </div>
        <button
          onClick={onForget}
          disabled={keyedProviders.length === 0}
          title="Erase every stored key immediately"
          className="flex items-center gap-1.5 rounded border border-border px-2.5 py-1.5 text-[11px] font-semibold text-navy transition-colors hover:border-red hover:text-red disabled:opacity-40"
        >
          <Trash2 className="h-3.5 w-3.5" />
          Forget {keyedProviders.length > 1 ? `${keyedProviders.length} keys` : "key"}
        </button>
      </div>
    </section>
  );
}
