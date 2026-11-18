import { Download } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { JobFailed, JobProgress } from "../components/JobProgress";
import { Alert, Button, CopyButton, Page, PageHeader, Spinner, StatusBadge, Stepper, Tabs, Tag, useToast } from "../components/ui";
import { ApiError, downloadFile } from "../lib/api";
import { formatDuration } from "../lib/format";
import { UsageSummary } from "../components/UsageSummary";
import { useJob } from "../lib/hooks";
import type { ExtractField, Job } from "../lib/types";
import { EXTRACT_STEPS } from "./ExtractNew";

type TabKey = "fields" | "json" | "map" | "activity" | "usage";

function show(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, Array.isArray(value) || typeof value === "object" ? 1 : 0);
}

function pagesLabel(pages: number[]) {
  if (!pages?.length) return "";
  return pages.length > 4 ? `${pages.slice(0, 4).join(", ")} and ${pages.length - 4} more` : pages.join(", ");
}

function Facts({ items }: { items: Array<[string, string | number | null | undefined]> }) {
  return (
    <dl className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-3 lg:grid-cols-6">
      {items
        .filter(([, v]) => v !== null && v !== undefined && v !== "")
        .map(([label, value]) => (
          <div key={label}>
            <dt className="text-[11px] font-semibold uppercase tracking-wider text-ink-400">{label}</dt>
            <dd className="mt-0.5 text-sm font-medium text-ink-900">{value}</dd>
          </div>
        ))}
    </dl>
  );
}

function FieldsTable({ fields }: { fields: ExtractField[] }) {
  return (
    <table className="table text-[13px]" data-testid="extract-fields">
      <thead>
        <tr>
          <th>Field</th>
          <th>Value</th>
          <th>Status</th>
          <th>Pages</th>
          <th>Evidence</th>
        </tr>
      </thead>
      <tbody>
        {fields.map((f) => (
          <tr key={f.path}>
            <td className="whitespace-nowrap align-top font-mono text-[12.5px]">{f.path}</td>
            <td className="align-top">
              <pre className="max-w-[460px] whitespace-pre-wrap break-words font-sans text-[13px] text-ink-900">{show(f.value) || <span className="text-ink-400">none</span>}</pre>
            </td>
            <td className="whitespace-nowrap align-top">
              {f.status === "found" ? <Tag tone="green">Found{f.confidence ? `, ${f.confidence}` : ""}</Tag> : <Tag tone="amber">Not found</Tag>}
            </td>
            <td className="whitespace-nowrap align-top tabular-nums">{pagesLabel(f.pages)}</td>
            <td className="align-top text-ink-600">{f.note}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function DocumentMap({ result }: { result: any }) {
  const doc = result.document ?? {};
  const toc = doc.toc;
  const sourceLabel: Record<string, string> = {
    bookmarks: "PDF bookmarks",
    toc_page_rules: "Contents page found by rules",
    toc_page_llm: "Contents page found by the language model",
    headings: "Headings in the text",
    none: "Not found",
  };
  return (
    <div className="space-y-5 p-6">
      <Facts
        items={[
          ["Pages", doc.pages],
          ["Read by OCR", doc.ocr_pages || null],
          ["Map source", toc ? sourceLabel[toc.source] ?? toc.source : "Not used (batch approach)"],
          ["Contents pages", toc?.toc_pages?.length ? toc.toc_pages.join(", ") : null],
          ["Page offset", toc?.page_offset ? `+${toc.page_offset}` : null],
          ["Chunking", doc.chunking ? `${doc.chunking.strategy}, ${doc.chunking.chunks} chunks` : null],
          ["Retrieval", doc.retrieval ? `${doc.retrieval.mode}${doc.retrieval.embedding_model ? ` (${doc.retrieval.embedding_model})` : ""}` : null],
        ]}
      />
      {toc?.document_type && (
        <p className="text-sm text-ink-700">
          <span className="font-medium">{toc.document_type}.</span> {toc.summary}
        </p>
      )}
      {doc.chunking?.reason && <p className="text-[13px] text-ink-600">Chunking strategy: {doc.chunking.reason}.</p>}
      {toc?.entries?.length ? (
        <table className="table text-[13px]">
          <thead>
            <tr>
              <th>Section</th>
              <th>Title</th>
              <th>Pages</th>
              <th>Printed page</th>
            </tr>
          </thead>
          <tbody>
            {toc.entries.map((e: any) => (
              <tr key={e.id}>
                <td className="font-mono text-[12.5px]">{e.id}</td>
                <td style={{ paddingLeft: `${(e.level - 1) * 16 + 12}px` }}>{e.title}</td>
                <td className="tabular-nums">{e.page ? (e.end_page && e.end_page !== e.page ? `${e.page} to ${e.end_page}` : e.page) : ""}</td>
                <td className="tabular-nums text-ink-500">{e.printed_page ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="text-[13px] text-ink-500">{toc?.notes?.join(" ") || "No document map for this run."}</p>
      )}
    </div>
  );
}

function Activity({ result }: { result: any }) {
  const agent = result.agent;
  const batch = result.batch;
  if (batch) {
    return (
      <div className="p-6">
        <p className="mb-3 text-[13px] text-ink-600">
          {batch.parts} part{batch.parts === 1 ? "" : "s"} of up to {batch.part_tokens.toLocaleString()} tokens, processed in order with the JSON state
          carried forward.
        </p>
        <table className="table text-[13px]">
          <thead>
            <tr>
              <th>Part</th>
              <th>Pages</th>
              <th>Tokens</th>
              <th>Time</th>
              <th>Fields updated</th>
            </tr>
          </thead>
          <tbody>
            {batch.log.map((row: any) => (
              <tr key={row.part}>
                <td>{row.part}</td>
                <td className="tabular-nums">{row.pages[0] === row.pages[1] ? row.pages[0] : `${row.pages[0]} to ${row.pages[1]}`}</td>
                <td className="tabular-nums">{row.approx_tokens.toLocaleString()}</td>
                <td className="tabular-nums">{formatDuration(row.ms)}</td>
                <td>{row.ok ? (row.fields_updated?.length ? row.fields_updated.join(", ") : "none") : <span className="text-red-700">{row.error}</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }
  if (!agent) return <p className="p-6 text-sm text-ink-500">No activity recorded.</p>;
  return (
    <div className="space-y-5 p-6">
      <table className="table text-[13px]" data-testid="agent-sessions">
        <thead>
          <tr>
            <th>Session</th>
            <th>Engine</th>
            <th>Turns</th>
            <th>Tool calls</th>
            <th>Peak context</th>
            <th>Compaction limit</th>
            <th>Fields resolved</th>
            <th>Ended</th>
          </tr>
        </thead>
        <tbody>
          {agent.sessions.map((s: any) => (
            <tr key={s.session}>
              <td>{s.session}</td>
              <td className="font-mono text-[12px]">{s.model}</td>
              <td className="tabular-nums">{s.turns}</td>
              <td className="tabular-nums">{s.tool_calls}</td>
              <td className="tabular-nums">{s.peak_context_tokens.toLocaleString()}</td>
              <td className="tabular-nums">{s.compact_at_tokens.toLocaleString()}</td>
              <td className="tabular-nums">
                {s.fields_resolved_at_start} to {s.fields_resolved_at_end}
              </td>
              <td>{s.ended.replace(/_/g, " ")}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {agent.handoff_notes?.length > 0 && (
        <div>
          <p className="mb-1.5 text-[13px] font-semibold text-ink-900">Handoff notes written at compaction</p>
          {agent.handoff_notes.map((n: string, i: number) => (
            <pre key={i} className="mb-2 whitespace-pre-wrap rounded-md bg-surface-strong p-3 font-sans text-[12.5px] text-ink-700">
              {n}
            </pre>
          ))}
        </div>
      )}
      <div>
        <p className="mb-1.5 text-[13px] font-semibold text-ink-900">Tool calls</p>
        <ol className="space-y-1 font-mono text-[12px]">
          {agent.trace.map((t: any, i: number) => (
            <li key={i} className={t.is_error ? "text-red-700" : "text-ink-700"}>
              <span className="text-ink-400">
                s{t.session} t{t.turn}
              </span>{" "}
              {t.tool === "(message)" ? <span className="italic">{t.result}</span> : `${t.tool}(${JSON.stringify(t.args)})`}
              {t.tool !== "(message)" && <span className="text-ink-400"> : {t.result_chars.toLocaleString()} chars</span>}
            </li>
          ))}
        </ol>
      </div>
    </div>
  );
}

function Review({ job }: { job: Job }) {
  const toast = useToast();
  const [tab, setTab] = useState<TabKey>("fields");
  const result = job.result_data ?? {};
  const json = useMemo(() => JSON.stringify(result.data ?? {}, null, 2), [result.data]);
  const usage = result.usage?.total ?? {};
  const agent = result.agent;
  const stem = (job.file_name ?? "document").replace(/\.[^.]+$/, "");
  return (
    <div className="space-y-5">
      <div className="card p-5">
        <Facts
          items={[
            ["Approach", result.approach === "agentic" ? "Agentic retrieval" : "Batch processing"],
            ["Fields found", `${result.summary?.found ?? 0} of ${result.summary?.total ?? 0}`],
            ["Engine", result.engine?.display_name],
            ["Usable window", result.engine?.usable_window ? `${result.engine.usable_window.toLocaleString()} tokens` : null],
            ["Sessions", agent ? `${agent.sessions.length} (${agent.compactions} compaction${agent.compactions === 1 ? "" : "s"})` : null],
            ["Model calls", usage.calls],
            ["Tokens in / out", usage.calls ? `${(usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens).toLocaleString()} / ${usage.output_tokens.toLocaleString()}` : null],
            ["Cost", usage.cost_usd !== null && usage.cost_usd !== undefined ? `$${usage.cost_usd.toFixed(4)}` : "not priced"],
            ["Duration", job.duration_ms ? formatDuration(job.duration_ms) : null],
            ["Pages", result.document?.pages],
          ]}
        />
      </div>
      {result.warnings?.length > 0 && <Alert tone="warning">{result.warnings.join(" ")}</Alert>}
      <div className="card overflow-hidden">
        <div className="flex flex-wrap items-end justify-between gap-3 border-b border-line px-5 pt-4">
          <Tabs
            value={tab}
            onChange={setTab}
            tabs={[
              { value: "fields", label: "Fields" },
              { value: "json", label: "JSON" },
              { value: "map", label: "Document map" },
              { value: "activity", label: result.batch ? "Parts" : "Agent activity" },
              { value: "usage", label: "Model usage" },
            ]}
          />
          <div className="flex gap-2 pb-3">
            <CopyButton value={json} label="Copy JSON" />
            <Button
              size="sm"
              onClick={async () => {
                try {
                  await downloadFile(`/jobs/${job.job_id}/file`, `${stem}.extracted.json`);
                } catch (err) {
                  toast("error", err instanceof ApiError ? err.message : "Download failed.");
                }
              }}
            >
              <Download className="h-3.5 w-3.5" aria-hidden />
              JSON
            </Button>
          </div>
        </div>
        <div className="max-h-[calc(100vh-380px)] min-h-[380px] overflow-auto" data-testid="extract-result">
          {tab === "fields" && <FieldsTable fields={result.fields ?? []} />}
          {tab === "json" && <pre className="whitespace-pre-wrap break-words p-6 font-mono text-[12.5px] leading-relaxed text-ink-800">{json}</pre>}
          {tab === "map" && <DocumentMap result={result} />}
          {tab === "activity" && <Activity result={result} />}
          {tab === "usage" && (
            <div className="p-6">
              <UsageSummary usage={job.usage ?? result.usage} />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default function ExtractJob() {
  const { jobId } = useParams();
  const { data: job, isLoading, error } = useJob(jobId);
  if (isLoading) {
    return (
      <Page>
        <Spinner />
      </Page>
    );
  }
  if (error || !job) {
    return (
      <Page>
        <PageHeader title="Data Extraction" />
        <Alert>{error instanceof ApiError ? error.message : "This job could not be loaded."}</Alert>
      </Page>
    );
  }
  const done = job.status === "COMPLETED";
  return (
    <Page>
      <PageHeader
        title={job.file_name || "Data Extraction"}
        subtitle={done ? "Structured data extracted with the requested spec" : "Extraction in progress"}
        actions={
          <>
            <StatusBadge status={job.status} />
            <Link to="/extract" className="btn-secondary">
              New extraction
            </Link>
          </>
        }
      />
      <Stepper steps={EXTRACT_STEPS} current={done ? 3 : 2} />
      {job.status === "FAILED" ? <JobFailed job={job} retryTo="/extract" /> : done ? <Review job={job} /> : <JobProgress job={job} />}
    </Page>
  );
}
