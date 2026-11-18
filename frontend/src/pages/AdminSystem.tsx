import { useQuery } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { Alert, Button, Page, PageHeader, Section, Spinner, Tag } from "../components/ui";
import { api, ApiError } from "../lib/api";
import { formatDuration, formatRelative } from "../lib/format";
import type { JobType } from "../lib/types";

const IN_SENTENCE: Record<JobType, string> = { PHI: "PHI redaction", PARSE: "document parsing", OCR: "text extraction", EXTRACT: "data extraction" };

interface Engine {
  role: string;
  provider: string;
  model: string;
  url: string;
  key_configured: boolean;
  ok: boolean;
  detail: string;
}

interface SystemStatus {
  api: { ok: boolean };
  database: { ok: boolean };
  redis: { ok: boolean };
  worker: { online: boolean; detail?: string; device?: string; gpu_name?: string; last_seen?: string; started_at?: string; ocr_load_seconds?: number };
  queues: { priority: number | null; default: number | null };
  ocr: { detection_model: string; recognition_model: string; high_performance_inference: boolean; render_dpi: number; render_workers: number };
  llm: Record<"phi" | "doc" | "extract", { engines: Engine[]; active: string | null }>;
  extraction?: { context_threshold: number; context_cap_tokens: number; embedding: string; reasoning_effort: string | null };
  limits: { max_upload_mb: number; file_retention_days: number; storage_mode: string };
  jobs_last_24h: { by_status: Record<string, number>; average_duration_ms: Record<string, number>; completed: Record<string, number> };
  users: { total: number; active: number };
}

const PROVIDER_NAMES: Record<string, string> = {
  openai: "OpenAI",
  azure_openai: "Azure OpenAI",
  openai_compatible: "OpenAI compatible",
  anthropic: "Anthropic Claude",
  gemini: "Google Gemini",
  ollama: "Ollama (local)",
};

function Health({ ok, okText = "Operational", badText = "Unavailable" }: { ok: boolean; okText?: string; badText?: string }) {
  return <Tag tone={ok ? "green" : "red"}>{ok ? okText : badText}</Tag>;
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4 py-2.5 text-sm">
      <span className="text-ink-600">{label}</span>
      <span className="text-right font-medium text-ink-900">{children}</span>
    </div>
  );
}

function EngineTable({ title, subtitle, data }: { title: string; subtitle: string; data: { engines: Engine[]; active: string | null } }) {
  return (
    <Section title={title} subtitle={subtitle} className="[&>div:last-child]:p-0">
      <div className="overflow-x-auto">
        <table className="table">
          <thead>
            <tr>
              <th>Role</th>
              <th>Engine</th>
              <th>Model</th>
              <th>Status</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            {data.engines.map((engine) => (
              <tr key={engine.role}>
                <td className="whitespace-nowrap">
                  {engine.role === "primary" ? "Primary" : "Fallback"}
                  {data.active === engine.role && (
                    <span className="ml-2">
                      <Tag tone="blue">In use</Tag>
                    </span>
                  )}
                </td>
                <td className="whitespace-nowrap">{PROVIDER_NAMES[engine.provider] ?? engine.provider}</td>
                <td className="font-mono text-[12.5px]">{engine.model}</td>
                <td>
                  <Health ok={engine.ok} okText="Ready" badText="Not available" />
                </td>
                <td className="text-[13px] text-ink-600">{engine.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {data.active === null && (
        <div className="p-4">
          <Alert>No engine is available for this workload. Jobs that need it will fail until one is reachable.</Alert>
        </div>
      )}
    </Section>
  );
}

export default function AdminSystem() {
  const status = useQuery({ queryKey: ["system"], queryFn: () => api<SystemStatus>("/admin/system"), refetchInterval: 30_000 });
  const data = status.data;

  return (
    <Page>
      <PageHeader
        title="System Status"
        subtitle="Health of the platform components and the models behind each service."
        actions={
          <Button variant="secondary" onClick={() => status.refetch()} loading={status.isFetching}>
            {!status.isFetching && <RefreshCw className="h-4 w-4" aria-hidden />}
            Refresh
          </Button>
        }
      />
      {status.isLoading ? (
        <Spinner label="Checking components" />
      ) : status.isError || !data ? (
        <Alert>{status.error instanceof ApiError ? status.error.message : "Status could not be loaded."}</Alert>
      ) : (
        <div className="space-y-5">
          <div className="grid gap-5 lg:grid-cols-2">
            <Section title="Components" subtitle="Core services">
              <div className="divide-y divide-line">
                <Row label="API">
                  <Health ok={data.api.ok} />
                </Row>
                <Row label="Database">
                  <Health ok={data.database.ok} />
                </Row>
                <Row label="Queue">
                  <Health ok={data.redis.ok} />
                </Row>
                <Row label="Processing worker">
                  <Health ok={data.worker.online} okText="Online" badText="Offline" />
                </Row>
                {data.worker.online ? (
                  <>
                    <Row label="Compute">{data.worker.device === "gpu" ? data.worker.gpu_name ?? "GPU" : "CPU"}</Row>
                    <Row label="Last heartbeat">{formatRelative(data.worker.last_seen)}</Row>
                  </>
                ) : (
                  <p className="py-2.5 text-[13px] text-ink-600">{data.worker.detail}</p>
                )}
                <Row label="Jobs waiting">{(data.queues.priority ?? 0) + (data.queues.default ?? 0)}</Row>
              </div>
            </Section>

            <Section title="Last 24 hours" subtitle="Job activity across all users">
              <div className="divide-y divide-line">
                <Row label="Completed">{data.jobs_last_24h.by_status.COMPLETED ?? 0}</Row>
                <Row label="Failed">{data.jobs_last_24h.by_status.FAILED ?? 0}</Row>
                <Row label="In progress">{(data.jobs_last_24h.by_status.PROCESSING ?? 0) + (data.jobs_last_24h.by_status.PENDING ?? 0)}</Row>
                {Object.entries(data.jobs_last_24h.average_duration_ms).map(([type, ms]) => (
                  <Row key={type} label={`Average time, ${IN_SENTENCE[type as JobType] ?? type}`}>
                    {formatDuration(ms)}
                  </Row>
                ))}
                <Row label="Active users">
                  {data.users.active} of {data.users.total}
                </Row>
                <Row label="Output retention">{data.limits.file_retention_days} days</Row>
              </div>
            </Section>
          </div>

          <EngineTable title="PHI detection engines" subtitle="Language model that identifies sensitive entities. The fallback takes over automatically if the primary fails." data={data.llm.phi} />
          <EngineTable title="Document vision engines" subtitle="Vision model that describes figures and charts during document parsing." data={data.llm.doc} />
          {data.llm.extract && (
            <EngineTable
              title="Data extraction engines"
              subtitle={
                data.extraction
                  ? `Reasoning model for document to JSON extraction. Agent sessions are compacted at ${Math.round(data.extraction.context_threshold * 100)}% of the usable window (cap ${data.extraction.context_cap_tokens ? data.extraction.context_cap_tokens.toLocaleString() + " tokens" : "none"}); embeddings: ${data.extraction.embedding}.`
                  : "Reasoning model for document to JSON extraction."
              }
              data={data.llm.extract}
            />
          )}

          <Section title="Text recognition" subtitle="OCR models used for scanned pages and images">
            <div className="grid gap-x-10 md:grid-cols-2">
              <div className="divide-y divide-line">
                <Row label="Detection model">
                  <span className="font-mono text-[13px]">{data.ocr.detection_model}</span>
                </Row>
                <Row label="Recognition model">
                  <span className="font-mono text-[13px]">{data.ocr.recognition_model}</span>
                </Row>
              </div>
              <div className="divide-y divide-line">
                <Row label="Render resolution">{data.ocr.render_dpi} DPI</Row>
                <Row label="Parallel page renderers">{data.ocr.render_workers}</Row>
              </div>
            </div>
          </Section>
        </div>
      )}
    </Page>
  );
}
