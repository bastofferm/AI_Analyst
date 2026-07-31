"use client";

// Browser-side LLM provider selection + API-key vault.
//
// Storage split, deliberately:
//   sessionStorage["mzqa_llm_keys"] -> { [providerId]: apiKey }
//     Secrets only. sessionStorage is per-tab and the browser erases it when the
//     tab (or the browser) closes, so keys never survive a session and never
//     touch disk the way localStorage would.
//   localStorage["mzqa_llm_pref"]  -> { provider, model, idleMinutes }
//     Selection only, never a secret — so the provider you picked is still there
//     tomorrow while the key is not.
//
// On top of the browser's own close-the-tab erasure there is an idle wipe
// (default 60 min) and a manual "Forget keys" action. Long committee runs count
// as activity (see touchLlmActivity) so a 3-6 minute run is never wiped
// mid-flight.

import { API_BASE } from "./api";

export type ProviderId = "deepseek" | "openai" | "anthropic" | "moonshot" | "gemini";

export type ProviderInfo = {
  id: string;
  label: string;
  dialect: string;
  base_url: string;
  chat_model: string;
  reasoner_model: string;
  models: string[];
  key_placeholder: string;
  docs_url: string;
  /** Env var names this provider accepts, canonical first. */
  env_names?: string[];
  has_server_key: boolean;
  /** Which env var the backend found a key in (never the key itself). */
  server_key_env?: string | null;
  /** "process" | "windows-user" — where that variable was read from. */
  server_key_origin?: string | null;
};

/** What a request needs to reach a provider. */
export type LlmSelection = {
  provider: string;
  model: string | null;
  apiKey: string;
};

export type LlmVault = {
  provider: string;
  model: string | null;
  idleMinutes: number;
  keys: Record<string, string>;
  /** Providers ticked for a parallel run, in addition to `provider`. `provider`
   *  stays the primary — it drives every single-provider surface and the shared
   *  preparation phase — so this holds only the *extra* providers. */
  alsoRun: string[];
  /** Per-provider model override for a parallel run. `provider`'s model stays in
   *  `model`; this covers the others. */
  models: Record<string, string>;
};

const KEYS_SLOT = "mzqa_llm_keys";        // sessionStorage — secrets
const PREF_SLOT = "mzqa_llm_pref";        // localStorage  — selection, no secrets
const ACTIVITY_SLOT = "mzqa_llm_seen";    // sessionStorage — last activity epoch ms
const LEGACY_KEY_SLOT = "mzqa_ai_key";    // pre-multi-provider single DeepSeek key

export const DEFAULT_PROVIDER: ProviderId = "deepseek";
export const DEFAULT_IDLE_MINUTES = 60;
export const IDLE_MINUTE_CHOICES = [15, 30, 60, 120, 0]; // 0 = never

/** Offline fallback; the live list comes from GET /api/llm/providers. */
export const FALLBACK_PROVIDERS: ProviderInfo[] = [
  {
    id: "deepseek", label: "DeepSeek", dialect: "openai",
    base_url: "https://api.deepseek.com/v1",
    chat_model: "deepseek-v4-flash", reasoner_model: "deepseek-v4-pro",
    models: ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"],
    key_placeholder: "sk-…", docs_url: "https://platform.deepseek.com/api_keys",
    has_server_key: false,
  },
  {
    id: "openai", label: "ChatGPT (OpenAI)", dialect: "openai",
    base_url: "https://api.openai.com/v1",
    chat_model: "gpt-5", reasoner_model: "gpt-5",
    models: ["gpt-5", "gpt-5-mini", "gpt-4.1"],
    key_placeholder: "sk-proj-…", docs_url: "https://platform.openai.com/api-keys",
    has_server_key: false,
  },
  {
    id: "anthropic", label: "Claude (Anthropic)", dialect: "anthropic",
    base_url: "https://api.anthropic.com",
    chat_model: "claude-opus-4-8", reasoner_model: "claude-opus-4-8",
    models: ["claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5"],
    key_placeholder: "sk-ant-…", docs_url: "https://platform.claude.com/settings/keys",
    has_server_key: false,
  },
  {
    id: "moonshot", label: "Moonshot (Kimi)", dialect: "openai",
    base_url: "https://api.moonshot.ai/v1",
    chat_model: "kimi-k2.6", reasoner_model: "kimi-k3",
    models: ["kimi-k2.6", "kimi-k3", "kimi-k2.7-code", "kimi-k2.7-code-highspeed"],
    key_placeholder: "sk-…", docs_url: "https://platform.moonshot.ai/console/api-keys",
    has_server_key: false,
  },
  {
    id: "gemini", label: "Gemini (Google)", dialect: "openai",
    base_url: "https://generativelanguage.googleapis.com/v1beta/openai",
    chat_model: "gemini-2.5-flash", reasoner_model: "gemini-2.5-pro",
    models: ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"],
    key_placeholder: "AIza…", docs_url: "https://aistudio.google.com/apikey",
    has_server_key: false,
  },
];

export const EMPTY_VAULT: LlmVault = {
  provider: DEFAULT_PROVIDER,
  model: null,
  idleMinutes: DEFAULT_IDLE_MINUTES,
  keys: {},
  alsoRun: [],
  models: {},
};

// ------------------------------------------------------------------ read/write

function browser(): boolean {
  return typeof window !== "undefined";
}

function readKeys(): Record<string, string> {
  if (!browser()) return {};
  let keys: Record<string, string> = {};
  try {
    const raw = window.sessionStorage.getItem(KEYS_SLOT);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === "object") {
        for (const [k, v] of Object.entries(parsed)) {
          if (typeof v === "string" && v.trim()) keys[k] = v.trim();
        }
      }
    }
  } catch {
    /* malformed vault — treat as empty rather than trapping the user */
  }
  // One-time migration off the single-key DeepSeek slot.
  try {
    const legacy = window.sessionStorage.getItem(LEGACY_KEY_SLOT)?.trim();
    if (legacy) {
      if (!keys.deepseek) keys = { ...keys, deepseek: legacy };
      window.sessionStorage.removeItem(LEGACY_KEY_SLOT);
      writeKeys(keys);
    }
  } catch {
    /* ignore */
  }
  return keys;
}

function writeKeys(keys: Record<string, string>): void {
  if (!browser()) return;
  try {
    const clean = Object.fromEntries(Object.entries(keys).filter(([, v]) => v && v.trim()));
    if (Object.keys(clean).length) window.sessionStorage.setItem(KEYS_SLOT, JSON.stringify(clean));
    else window.sessionStorage.removeItem(KEYS_SLOT);
  } catch {
    /* quota / privacy mode — the app still works, the key just won't persist */
  }
}

type StoredPref = Omit<LlmVault, "keys">;

const DEFAULT_PREF: StoredPref = {
  provider: DEFAULT_PROVIDER,
  model: null,
  idleMinutes: DEFAULT_IDLE_MINUTES,
  alsoRun: [],
  models: {},
};

function readPref(): StoredPref {
  if (!browser()) return { ...DEFAULT_PREF };
  try {
    const raw = window.localStorage.getItem(PREF_SLOT);
    if (raw) {
      const p = JSON.parse(raw) as Partial<LlmVault>;
      const models: Record<string, string> = {};
      if (p.models && typeof p.models === "object") {
        for (const [k, v] of Object.entries(p.models)) {
          if (typeof v === "string" && v.trim()) models[k] = v.trim();
        }
      }
      return {
        provider: typeof p.provider === "string" && p.provider ? p.provider : DEFAULT_PROVIDER,
        model: typeof p.model === "string" && p.model ? p.model : null,
        idleMinutes:
          typeof p.idleMinutes === "number" && p.idleMinutes >= 0 ? p.idleMinutes : DEFAULT_IDLE_MINUTES,
        alsoRun: Array.isArray(p.alsoRun) ? p.alsoRun.filter((x): x is string => typeof x === "string") : [],
        models,
      };
    }
  } catch {
    /* ignore malformed prefs */
  }
  return { ...DEFAULT_PREF };
}

function writePref(v: LlmVault): void {
  if (!browser()) return;
  try {
    // Never write `keys` here — localStorage survives the browser closing.
    window.localStorage.setItem(
      PREF_SLOT,
      JSON.stringify({
        provider: v.provider,
        model: v.model,
        idleMinutes: v.idleMinutes,
        alsoRun: v.alsoRun || [],
        models: v.models || {},
      }),
    );
  } catch {
    /* ignore quota errors */
  }
}

/** Full vault (selection + session keys), after wiping if the idle window lapsed. */
export function loadVault(): LlmVault {
  const pref = readPref();
  const vault: LlmVault = { ...pref, keys: readKeys() };
  if (isIdleExpired(vault.idleMinutes)) {
    forgetLlmKeys();
    return { ...vault, keys: {} };
  }
  return vault;
}

export function saveVault(v: LlmVault): LlmVault {
  writePref(v);
  writeKeys(v.keys);
  touchLlmActivity();
  return v;
}

/** Wipe every stored key. Selection and idle preference survive. */
export function forgetLlmKeys(): void {
  if (!browser()) return;
  try {
    window.sessionStorage.removeItem(KEYS_SLOT);
    window.sessionStorage.removeItem(LEGACY_KEY_SLOT);
    window.sessionStorage.removeItem(ACTIVITY_SLOT);
  } catch {
    /* ignore */
  }
}

// -------------------------------------------------------------------- idle wipe

/** Mark the session as active. Call around anything that uses a key. */
export function touchLlmActivity(): void {
  if (!browser()) return;
  try {
    window.sessionStorage.setItem(ACTIVITY_SLOT, String(Date.now()));
  } catch {
    /* ignore */
  }
}

export function isIdleExpired(idleMinutes: number): boolean {
  if (!browser() || !idleMinutes) return false; // 0 = never expire
  try {
    const raw = window.sessionStorage.getItem(ACTIVITY_SLOT);
    if (!raw) return false; // no activity recorded yet — nothing to expire
    const last = Number(raw);
    if (!Number.isFinite(last)) return false;
    return Date.now() - last > idleMinutes * 60_000;
  } catch {
    return false;
  }
}

// ------------------------------------------------------------------- selection

/** The provider/model/key a request should use right now. */
export function selection(v: LlmVault): LlmSelection {
  return { provider: v.provider, model: v.model, apiKey: v.keys[v.provider] || "" };
}

/** Can this provider actually run — either the browser holds a key, or the server
 *  has one in its environment? Used to stop the user ticking a provider that would
 *  only fail at request time. */
export function isRunnable(v: LlmVault, providerId: string, providers: ProviderInfo[]): boolean {
  if ((v.keys[providerId] || "").trim()) return true;
  return Boolean(providers.find((p) => p.id === providerId)?.has_server_key);
}

/** Every provider a run should fan out to: the primary first, then each ticked
 *  extra that has a usable key. Order matters — the first entry runs the shared
 *  preparation phase, and it is what a single-provider run collapses to. */
export function activeSelections(v: LlmVault, providers: ProviderInfo[] = []): LlmSelection[] {
  const out: LlmSelection[] = [selection(v)];
  for (const id of v.alsoRun || []) {
    if (id === v.provider) continue;
    // With no provider list loaded yet we cannot check server keys, so trust the
    // stored tick rather than silently dropping a provider the user chose.
    if (providers.length && !isRunnable(v, id, providers)) continue;
    out.push({ provider: id, model: v.models?.[id] || null, apiKey: v.keys[id] || "" });
  }
  return out;
}

/** Request-body fields for the backend. Omits the key when there is none, so
 *  the server can still fall back to its own env key. */
export function llmBody(v: LlmVault | LlmSelection): {
  provider: string;
  model?: string | null;
  api_key?: string | null;
} {
  const sel = "keys" in v ? selection(v) : v;
  const body: { provider: string; model?: string | null; api_key?: string | null } = {
    provider: sel.provider,
  };
  if (sel.model) body.model = sel.model;
  if (sel.apiKey) body.api_key = sel.apiKey;
  return body;
}

export function hasKey(v: LlmVault, provider?: string): boolean {
  return Boolean((v.keys[provider || v.provider] || "").trim());
}

export function providerLabel(providers: ProviderInfo[], id: string): string {
  return providers.find((p) => p.id === id)?.label || id;
}

// ---------------------------------------------------------------------- server

// The registry is static for the session and two components want it (app-shell
// for the nav pill, LlmSetupPanel for the picker), so share one request. A
// failure is not cached — the next caller retries.
let providersPromise: Promise<ProviderInfo[]> | null = null;

export function fetchProviders(force = false): Promise<ProviderInfo[]> {
  // `force` re-asks the backend: the answer includes whether a Windows user
  // environment variable holds a key, and that can be set while the app is
  // open. Without it the session cache would keep reporting "no key found".
  if (force) providersPromise = null;
  if (providersPromise) return providersPromise;
  providersPromise = (async () => {
    const res = await fetch(`${API_BASE}/api/llm/providers`);
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const data = (await res.json()) as { providers: ProviderInfo[] };
    return data.providers?.length ? data.providers : FALLBACK_PROVIDERS;
  })().catch((err) => {
    providersPromise = null;
    throw err;
  });
  return providersPromise;
}

/** Live model catalogue for one provider. The key is used for this one request
 *  and is never stored server-side. */
export async function fetchModels(
  provider: string,
  apiKey: string,
): Promise<{ models: string[]; source: string; warning?: string | null }> {
  const res = await fetch(`${API_BASE}/api/llm/models`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ provider, api_key: apiKey || null }),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as { models: string[]; source: string; warning?: string | null };
}
