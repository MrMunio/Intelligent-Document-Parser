import { useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, CircleAlert, RotateCcw } from "lucide-react";
import { useEffect, useState } from "react";
import { Alert, Button, Field, Page, PageHeader, Section, Segmented, Spinner, Tag, useToast } from "../components/ui";
import { api, ApiError } from "../lib/api";
import { formatDate } from "../lib/format";

type Engine = { provider: string; model: string; url?: string | null } | null;
type ServiceKey = "phi" | "doc" | "extract" | "extract_toc" | "extract_embed";

interface ServiceConfig {
  key: ServiceKey;
  title: string;
  description: string;
  value: any;
  env_defaults: any;
  source: string;
  updated_by: string | null;
  updated_at: string | null;
}

interface ProviderInfo {
  name: string;
  label: string;
  key_configured: boolean;
  needs_key: boolean;
  free_text_model: boolean;
  default_url: string | null;
  models: { id: string; display_name: string; context_window: number; tier: string; pricing: { input?: number; output?: number } }[];
}

interface ConfigResponse {
  services: ServiceConfig[];
  providers: ProviderInfo[];
  embedding_models: Record<string, { id: string; dimensions?: number; size_mb?: number; notes?: string }[]>;
  toc_modes: { value: "rules" | "llm" | "hybrid"; label: string; description: string }[];
  efforts: string[];
  storage: string;
}

interface TestResult {
  role: string;
  engine: string;
  ok: boolean;
  detail: string;
}

function engineLabel(engine: Engine, providers: ProviderInfo[]): string {
  if (!engine || !engine.provider) return "None";
  const p = providers.find((x) => x.name === engine.provider);
  const m = p?.models.find((x) => x.id === engine.model);
  return `${p?.label ?? engine.provider}: ${m?.display_name ?? engine.model}`;
}

function summary(s: ServiceConfig, providers: ProviderInfo[]): string {
  const v = s.value;
  if (s.key === "extract_toc") {
    if (v.mode === "rules") return "Rules only (no model)";
    return `${v.mode === "llm" ? "LLM" : "Hybrid (rules, then LLM)"} with ${v.engine ? engineLabel(v.engine, providers) : "the data extraction engine"}`;
  }
  if (s.key === "extract_embed") return v.provider === "none" ? "Off (keyword ranking only)" : `${v.provider}: ${v.model}`;
  return `${engineLabel(v.primary, providers)}${v.fallback ? `, fallback ${engineLabel(v.fallback, providers)}` : ""}`;
}

function SourceTag({ s }: { s: ServiceConfig }) {
  if (s.source === "admin")
    return (
      <Tag tone="blue">
        Changed by {s.updated_by ?? "an administrator"}
        {s.updated_at ? `, ${formatDate(s.updated_at)}` : ""}
      </Tag>
    );
  return <Tag>From .env{s.updated_by && s.updated_by !== "startup (.env)" ? `, reset by ${s.updated_by}` : " (first start)"}</Tag>;
}

// ---------------------------------------------------------------------------
// Engine picker: provider, then a model from the catalog (or a free name for local and self-hosted engines)
// ---------------------------------------------------------------------------

function EnginePicker({
  label,
  value,
  onChange,
  providers,
  optional,
  noneLabel = "None",
}: {
  label: string;
  value: Engine;
  onChange: (e: Engine) => void;
  providers: ProviderInfo[];
  optional?: boolean;
  noneLabel?: string;
}) {
  const provider = providers.find((p) => p.name === value?.provider);
  const custom = provider && (provider.free_text_model || !provider.models.some((m) => m.id === value?.model));
  return (
    <div className="grid gap-3 md:grid-cols-[240px_minmax(0,1fr)_minmax(0,1fr)]" data-testid={`picker-${label}`}>
      <Field label={label}>
        <select
          className="input"
          aria-label={`${label} provider`}
          value={value?.provider ?? ""}
          onChange={(e) => {
            const name = e.target.value;
            if (!name) return onChange(null);
            const p = providers.find((x) => x.name === name)!;
            onChange({ provider: name, model: p.models[0]?.id ?? "", url: null });
          }}
        >
          {optional && <option value="">{noneLabel}</option>}
          {providers.map((p) => (
            <option key={p.name} value={p.name} disabled={p.needs_key && !p.key_configured}>
              {p.label}
              {p.needs_key && !p.key_configured ? " (no API key in .env)" : ""}
            </option>
          ))}
        </select>
      </Field>
      {value?.provider && (
        <Field label="Model" hint={provider?.free_text_model ? "Model name or deployment, for example qwen3.5:4b" : undefined}>
          {provider && provider.models.length > 0 && !provider.free_text_model ? (
            <select className="input" aria-label={`${label} model`} value={value.model} onChange={(e) => onChange({ ...value, model: e.target.value })}>
              {custom && <option value={value.model}>{value.model} (configured)</option>}
              {provider.models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.display_name} ({m.context_window >= 1_000_000 ? `${(m.context_window / 1_000_000).toFixed(2).replace(/\.?0+$/, "")}M` : `${Math.round(m.context_window / 1000)}K`}
                  {m.pricing.input ? `, $${m.pricing.input} / $${m.pricing.output}` : ", local"})
                </option>
              ))}
            </select>
          ) : (
            <>
              <input className="input font-mono text-[13px]" aria-label={`${label} model`} list={`models-${label}`} value={value.model} onChange={(e) => onChange({ ...value, model: e.target.value })} />
              <datalist id={`models-${label}`}>
                {provider?.models.map((m) => (
                  <option key={m.id} value={m.id} />
                ))}
              </datalist>
            </>
          )}
        </Field>
      )}
      {value?.provider && (
        <Field label="URL" hint={provider?.default_url ? `Empty: ${provider.default_url}` : "Required for this provider"}>
          <input className="input font-mono text-[13px]" aria-label={`${label} URL`} value={value.url ?? ""} placeholder={provider?.default_url ?? "https://..."} onChange={(e) => onChange({ ...value, url: e.target.value || null })} />
        </Field>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// One card per service
// ---------------------------------------------------------------------------

function ServiceCard({ service, data }: { service: ServiceConfig; data: ConfigResponse }) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<any>(service.value);
  const [busy, setBusy] = useState<"" | "save" | "reset" | "test">("");
  const [error, setError] = useState<string | null>(null);
  const [tests, setTests] = useState<TestResult[] | null>(null);
  useEffect(() => setDraft(service.value), [service.value]);
  const changed = JSON.stringify(draft) !== JSON.stringify(service.value);
  const providers = data.providers;

  async function run(kind: "save" | "reset" | "test") {
    setBusy(kind);
    setError(null);
    try {
      if (kind === "test") {
        const res = await api<{ results: TestResult[] }>(`/admin/llm-config/${service.key}/test`, { method: "POST" });
        setTests(res.results);
      } else {
        await api(`/admin/llm-config/${service.key}${kind === "reset" ? "/reset" : ""}`, { method: kind === "reset" ? "POST" : "PUT", body: kind === "save" ? draft : undefined });
        setTests(null);
        await queryClient.invalidateQueries({ queryKey: ["llm-config"] });
        toast("success", kind === "save" ? `${service.title}: saved. New jobs use it within a few seconds.` : `${service.title}: restored the .env values.`);
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "The request failed.");
    } finally {
      setBusy("");
    }
  }

  return (
    <Section
      title={service.title}
      subtitle={service.description}
      actions={<SourceTag s={service} />}
    >
      <div className="space-y-4" data-testid={`service-${service.key}`}>
        {(service.key === "phi" || service.key === "doc" || service.key === "extract") && (
          <>
            <EnginePicker label="Primary engine" value={draft.primary} providers={providers} onChange={(primary) => setDraft({ ...draft, primary })} />
            <EnginePicker label="Fallback engine" value={draft.fallback} providers={providers} optional noneLabel="No fallback" onChange={(fallback) => setDraft({ ...draft, fallback })} />
            {service.key === "extract" && (
              <div className="max-w-xs">
                <Field label="Reasoning effort" hint="Higher effort: more thorough, more tokens">
                  <select className="input" value={draft.reasoning_effort ?? "medium"} onChange={(e) => setDraft({ ...draft, reasoning_effort: e.target.value })}>
                    {data.efforts.map((e) => (
                      <option key={e} value={e}>
                        {e}
                      </option>
                    ))}
                  </select>
                </Field>
              </div>
            )}
          </>
        )}
        {service.key === "extract_toc" && (
          <>
            <div>
              <p className="mb-1.5 text-[13px] font-medium text-ink-800">Detection mode</p>
              <Segmented value={draft.mode} onChange={(mode) => setDraft({ ...draft, mode })} options={data.toc_modes.map((m) => ({ value: m.value, label: m.label }))} />
              <p className="mt-2 text-[13px] text-ink-600">{data.toc_modes.find((m) => m.value === draft.mode)?.description} PDF bookmarks are always used first when a file has them.</p>
            </div>
            {draft.mode !== "rules" && (
              <EnginePicker
                label="Index detection engine"
                value={draft.engine}
                providers={providers}
                optional
                noneLabel="Same as data extraction engine"
                onChange={(engine) => setDraft({ ...draft, engine })}
              />
            )}
          </>
        )}
        {service.key === "extract_embed" && (
          <div className="grid gap-3 md:grid-cols-[240px_minmax(0,1fr)_minmax(0,1fr)]">
            <Field label="Provider">
              <select
                className="input"
                aria-label="Embedding provider"
                value={draft.provider}
                onChange={(e) => {
                  const provider = e.target.value;
                  setDraft({ ...draft, provider, model: data.embedding_models[provider]?.[0]?.id ?? draft.model });
                }}
              >
                <option value="ollama">Local (Ollama)</option>
                <option value="openai">OpenAI</option>
                <option value="none">Off: keyword ranking only</option>
              </select>
            </Field>
            {draft.provider !== "none" && (
              <Field label="Model" hint={data.embedding_models[draft.provider]?.find((m) => m.id === draft.model)?.notes}>
                <select className="input" aria-label="Embedding model" value={draft.model} onChange={(e) => setDraft({ ...draft, model: e.target.value })}>
                  {!data.embedding_models[draft.provider]?.some((m) => m.id === draft.model) && <option value={draft.model}>{draft.model} (configured)</option>}
                  {(data.embedding_models[draft.provider] ?? []).map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.id}
                      {m.size_mb ? ` (${m.size_mb} MB, ${m.dimensions} dimensions)` : m.dimensions ? ` (${m.dimensions} dimensions)` : ""}
                    </option>
                  ))}
                </select>
              </Field>
            )}
            {draft.provider === "ollama" && (
              <Field label="URL" hint="Empty: http://ollama:11434">
                <input className="input font-mono text-[13px]" value={draft.url ?? ""} onChange={(e) => setDraft({ ...draft, url: e.target.value || null })} />
              </Field>
            )}
          </div>
        )}

        {error && <Alert>{error}</Alert>}
        {tests && (
          <div className="space-y-1.5" data-testid={`test-${service.key}`}>
            {tests.map((t) => (
              <div key={t.role} className="flex items-start gap-2 text-[13px]">
                {t.ok ? <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600" aria-hidden /> : <CircleAlert className="mt-0.5 h-4 w-4 shrink-0 text-red-600" aria-hidden />}
                <span>
                  <span className="font-medium capitalize">{t.role}</span> <span className="font-mono text-[12px]">{t.engine}</span>: {t.detail}
                </span>
              </div>
            ))}
          </div>
        )}
        <div className="flex flex-wrap items-center justify-end gap-2 border-t border-line pt-4">
          {changed && <span className="mr-auto text-[13px] text-amber-700">Unsaved changes</span>}
          <Button variant="ghost" size="sm" onClick={() => run("reset")} loading={busy === "reset"} disabled={!!busy}>
            <RotateCcw className="h-3.5 w-3.5" aria-hidden />
            Reset to .env
          </Button>
          <Button variant="secondary" size="sm" onClick={() => run("test")} loading={busy === "test"} disabled={!!busy || changed} title={changed ? "Save first, then test" : undefined}>
            Test connection
          </Button>
          {changed && (
            <Button variant="secondary" size="sm" onClick={() => setDraft(service.value)} disabled={!!busy}>
              Discard
            </Button>
          )}
          <Button size="sm" onClick={() => run("save")} loading={busy === "save"} disabled={!changed || !!busy}>
            Save
          </Button>
        </div>
      </div>
    </Section>
  );
}

export default function AdminEngines() {
  const { data, isLoading, error } = useQuery({ queryKey: ["llm-config"], queryFn: () => api<ConfigResponse>("/admin/llm-config") });
  return (
    <Page>
      <PageHeader
        title="AI Engines"
        subtitle="Choose the language model engine for each service. Values come from .env at the first start; changes made here apply to new jobs within seconds."
      />
      {isLoading && <Spinner />}
      {error && <Alert>{error instanceof ApiError ? error.message : "The configuration could not be loaded."}</Alert>}
      {data && (
        <div className="space-y-5">
          <Section title="Current configuration" subtitle={`What every service uses now. Stored in: ${data.storage}. API keys stay in .env and are never shown.`}>
            <table className="table text-[13px]" data-testid="engines-summary">
              <thead>
                <tr>
                  <th>Service</th>
                  <th>Engine</th>
                  <th>Origin</th>
                </tr>
              </thead>
              <tbody>
                {data.services.map((s) => (
                  <tr key={s.key}>
                    <td className="font-medium">{s.title}</td>
                    <td>{summary(s, data.providers)}</td>
                    <td>
                      <SourceTag s={s} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="mt-4 flex flex-wrap gap-2 text-[12px] text-ink-600">
              {data.providers
                .filter((p) => p.needs_key)
                .map((p) => (
                  <span key={p.name} className="rounded border border-line px-2 py-1">
                    {p.label}: {p.key_configured ? "API key configured" : "no API key"}
                  </span>
                ))}
            </div>
          </Section>
          {data.services.map((s) => (
            <ServiceCard key={s.key} service={s} data={data} />
          ))}
        </div>
      )}
    </Page>
  );
}
