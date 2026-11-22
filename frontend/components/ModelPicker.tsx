"use client";

import { useEffect, useState } from "react";
import { api, type ProviderStatus } from "@/lib/api";

// Switch the language model at runtime. Providers without a key (or a running
// Ollama server) are shown but disabled, with the reason.
export default function ModelPicker({
  current,
  disabled,
  onChanged,
}: {
  current: string | null;
  disabled: boolean;
  onChanged: (name: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [providers, setProviders] = useState<ProviderStatus[]>([]);
  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!open) return;
    api
      .models()
      .then((r) => {
        setProviders(r.providers);
        const [cp, ...rest] = (r.current ?? "").split(":");
        setProvider(cp || r.providers.find((p) => p.ready)?.provider || "ollama");
        setModel(rest.join(":"));
      })
      .catch((e: Error) => setError(e.message));
  }, [open]);

  const selected = providers.find((p) => p.provider === provider);

  async function apply() {
    setSaving(true);
    setError(null);
    try {
      const r = await api.setModel(provider, model);
      onChanged(r.llm);
      setOpen(false);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        disabled={disabled}
        className="rounded border border-line bg-panel px-2 py-1 font-mono hover:border-accent disabled:opacity-50"
        title="Change the language model"
      >
        model: {current ?? "not set"} ▾
      </button>
      {open && (
        <div className="absolute left-0 z-20 mt-1 w-80 max-w-[calc(100vw-2rem)] rounded-xl border border-line bg-panel p-3 shadow-lg">
          <label className="block text-[11px] font-semibold text-muted">Provider</label>
          <select
            value={provider}
            onChange={(e) => {
              setProvider(e.target.value);
              setModel("");
            }}
            className="mt-1 w-full rounded border border-line bg-panel px-2 py-1 text-sm"
          >
            {providers.map((p) => (
              <option key={p.provider} value={p.provider} disabled={!p.ready}>
                {p.provider}
                {p.local ? " (local)" : ""}
                {p.ready ? "" : ` — ${p.why_not}`}
              </option>
            ))}
          </select>

          <label className="mt-3 block text-[11px] font-semibold text-muted">Model</label>
          {selected && selected.models.length > 0 ? (
            <select value={model || selected.default_model} onChange={(e) => setModel(e.target.value)} className="mt-1 w-full rounded border border-line bg-panel px-2 py-1 font-mono text-sm">
              {selected.models.map((m) => (
                <option key={m}>{m}</option>
              ))}
            </select>
          ) : (
            <input
              value={model}
              onChange={(e) => setModel(e.target.value)}
              placeholder={selected?.default_model || "model name"}
              className="mt-1 w-full rounded border border-line bg-panel px-2 py-1 font-mono text-sm"
            />
          )}
          <p className="mt-1 text-[11px] text-muted">
            {provider === "openrouter" ? "Any OpenRouter id, e.g. anthropic/…, openai/…, qwen/…. " : ""}
            Leave empty for the default. Keys live in .env, never in the browser.
          </p>

          {error && <p className="mt-2 text-xs text-bad">{error}</p>}
          <div className="mt-3 flex justify-end gap-2">
            <button onClick={() => setOpen(false)} className="rounded px-2 py-1 text-xs text-muted">
              Cancel
            </button>
            <button
              onClick={apply}
              disabled={saving || !selected?.ready}
              className="rounded bg-accent px-3 py-1 text-xs font-semibold text-on-accent disabled:opacity-40"
            >
              {saving ? "Switching…" : "Use this model"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
