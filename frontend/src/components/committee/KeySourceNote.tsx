"use client";

// Tells you which API key the next run will actually use.
//
// There are two places a key can come from, and until now the UI showed neither:
//   1. the browser vault (Setup panel, session-scoped)
//   2. a Windows user environment variable read server-side — the backend falls
//      back to it whenever the request carries no key
//
// Naming the exact variable matters: with several accepted spellings per
// provider (ANTHROPIC_API_KEY / CLAUDE_API_KEY, MOONSHOT_API_KEY / KIMI_API_KEY),
// "a key was found" is not enough to debug a 401 — you need to know which one.

import { useEffect, useState } from "react";
import { KeyRound, RotateCw, ServerCog, TriangleAlert } from "lucide-react";
import { fetchProviders, type LlmSelection, type ProviderInfo } from "@/lib/llm";

export function KeySourceNote({ llm, className = "" }: { llm: LlmSelection; className?: string }) {
  const [providers, setProviders] = useState<ProviderInfo[] | null>(null);
  const [checking, setChecking] = useState(false);
  const [nonce, setNonce] = useState(0);

  // The registry answer is cached for the session, so `nonce` (bumped by the
  // recheck button) is what forces a fresh look. It has to be forced rather
  // than automatic: the backend reads HKCU\Environment on every call, so a
  // variable you add in Windows shows up without restarting anything — but
  // only if we actually ask again.
  useEffect(() => {
    let alive = true;
    setChecking(nonce > 0);
    fetchProviders(nonce > 0)
      .then((list) => alive && setProviders(list))
      .catch(() => alive && setProviders(null))
      .finally(() => alive && setChecking(false));
    return () => {
      alive = false;
    };
  }, [llm.provider, nonce]);

  const recheck = (
    <button
      onClick={() => setNonce((n) => n + 1)}
      disabled={checking}
      className="ml-1 inline-flex items-center gap-1 whitespace-nowrap rounded border border-border bg-white px-1.5 py-px text-[10px] font-semibold text-navy transition-colors hover:border-navy disabled:opacity-50"
      title="Re-read the Windows user environment variables"
    >
      <RotateCw className={`h-2.5 w-2.5 ${checking ? "animate-spin" : ""}`} />
      Check again
    </button>
  );

  const prov = providers?.find((p) => p.id === llm.provider);
  const label = prov?.label ?? llm.provider;
  const browserKey = Boolean(llm.apiKey.trim());

  // Browser key wins — api.ts only omits api_key when the vault has none.
  if (browserKey) {
    return (
      <Note tone="ok" icon={<KeyRound className="h-3 w-3" />} className={className}>
        Using your <b className="font-semibold">{label}</b> key from this browser session.
      </Note>
    );
  }

  if (prov?.has_server_key) {
    // `server_key_env` is absent on a backend older than this feature — say the
    // honest generic thing rather than rendering a gap where the name goes.
    const where =
      prov.server_key_origin === "windows-user"
        ? "your Windows user environment variables"
        : "the server environment";
    return (
      <Note tone="ok" icon={<ServerCog className="h-3 w-3" />} className={className}>
        No <b className="font-semibold">{label}</b> key in this browser — falling back to{" "}
        {prov.server_key_env ? (
          <>
            <code className="rounded bg-navy/10 px-1 py-px text-[10px] font-semibold text-navy">
              {prov.server_key_env}
            </code>{" "}
            from {where}.
          </>
        ) : (
          <>a key from {where}.</>
        )}
      </Note>
    );
  }

  // Nothing anywhere — say exactly which variables would be accepted.
  const names = prov?.env_names?.length ? prov.env_names : [];
  return (
    <Note tone="warn" icon={<TriangleAlert className="h-3 w-3" />} className={className}>
      No <b className="font-semibold">{label}</b> key found — not in this browser, and no{" "}
      {names.length ? (
        <>
          {names.map((n, i) => (
            <span key={n}>
              {i > 0 ? " or " : ""}
              <code className="rounded bg-amber/20 px-1 py-px text-[10px] font-semibold text-navy">{n}</code>
            </span>
          ))}{" "}
          in your Windows user environment variables
        </>
      ) : (
        "environment variable"
      )}
      . Add one under <b className="font-semibold">Setup</b>, or set the variable in Windows and
      {recheck}
    </Note>
  );
}

function Note({
  tone,
  icon,
  children,
  className = "",
}: {
  tone: "ok" | "warn";
  icon: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`flex items-start gap-1.5 rounded border px-2.5 py-1.5 text-[11px] leading-relaxed ${
        tone === "warn"
          ? "border-amber/40 bg-amber/10 text-navy"
          : "border-border bg-panel text-muted"
      } ${className}`}
    >
      <span className={`mt-[2px] shrink-0 ${tone === "warn" ? "text-amber" : "text-navy-3"}`}>{icon}</span>
      <span>{children}</span>
    </div>
  );
}
