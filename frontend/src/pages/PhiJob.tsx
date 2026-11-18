import { useQuery } from "@tanstack/react-query";
import { Download, Eye, EyeOff } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { JobFailed, JobProgress } from "../components/JobProgress";
import { DocumentViewer, kindOf } from "../components/viewers";
import { Alert, Button, EmptyState, Page, PageHeader, Segmented, Spinner, StatusBadge, Stepper, Tabs, Tag, useToast } from "../components/ui";
import { apiBlob, ApiError, downloadFile } from "../lib/api";
import { entityLabel, formatBytes, formatDate, formatDuration, maskValue, recallOriginal } from "../lib/format";
import { formatCost, UsageSummary } from "../components/UsageSummary";
import { useJob } from "../lib/hooks";
import type { Job } from "../lib/types";
import { PHI_STEPS } from "./PhiNew";

interface EntityRow {
  label: string;
  type: string;
  text: string;
  source: string;
  occurrences: number;
  pages?: number[];
}

const VERIFICATION: Record<string, { tone: "green" | "amber" | "red" | "neutral"; text: string }> = {
  passed: { tone: "green", text: "Verified: no redacted value remains in the output" },
  repaired: { tone: "green", text: "Verified after an automatic repair pass" },
  failed: { tone: "red", text: "Verification found values that may remain. Review before sharing." },
  not_applicable: { tone: "neutral", text: "Redactions are painted into the image pixels" },
};

function Fact({ label, value }: { label: string; value: string | number | null | undefined }) {
  if (value === null || value === undefined || value === "") return null;
  return (
    <div className="flex items-baseline justify-between gap-4 py-1.5 text-[13px]">
      <dt className="text-ink-600">{label}</dt>
      <dd className="text-right font-medium text-ink-900">{value}</dd>
    </div>
  );
}

function Review({ job }: { job: Job }) {
  const toast = useToast();
  const meta = job.result_data ?? {};
  const summary = meta.summary ?? {};
  const entities: EntityRow[] = meta.entities ?? [];
  const original = recallOriginal(job.job_id);
  const kind = kindOf(job.input_format, job.result_file_name ?? job.file_name);

  const [view, setView] = useState<"redacted" | "original">("redacted");
  const [panel, setPanel] = useState<"entities" | "details" | "json">("entities");
  const [reveal, setReveal] = useState(false);
  const [focus, setFocus] = useState<{ page: number; tick: number } | null>(null);
  const [typeFilter, setTypeFilter] = useState<string>("all");

  const file = useQuery({
    queryKey: ["job-file", job.job_id],
    queryFn: () => apiBlob(`/jobs/${job.job_id}/file?inline=true`),
    staleTime: Infinity,
    retry: false,
  });

  const byType = Object.entries((summary.by_type ?? {}) as Record<string, number>);
  const rows = useMemo(() => entities.filter((e) => typeFilter === "all" || e.type === typeFilter), [entities, typeFilter]);
  const verification = VERIFICATION[meta.verification?.status ?? ""] ?? null;
  const engines = Object.keys(meta.engine?.llm?.used ?? {});
  const metadataJson = useMemo(() => JSON.stringify(meta, null, 2), [meta]);

  async function download(path: string, name: string) {
    try {
      await downloadFile(path, name);
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : "Download failed.");
    }
  }

  return (
    <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_440px]">
      {/* Document */}
      <div className="card flex h-[calc(100vh-230px)] min-h-[560px] flex-col overflow-hidden">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-4 py-2.5">
          <div className="min-w-0">
            <p className="truncate text-sm font-semibold text-ink-900">{view === "redacted" ? job.result_file_name : job.file_name}</p>
            <p className="text-xs text-ink-500">{view === "redacted" ? "Redacted output" : "Original, shown from this browser only"}</p>
          </div>
          <div className="flex items-center gap-2">
            {original && (
              <Segmented
                value={view}
                onChange={setView}
                options={[
                  { value: "redacted", label: "Redacted" },
                  { value: "original", label: "Original" },
                ]}
              />
            )}
            <Button size="sm" onClick={() => download(`/jobs/${job.job_id}/file`, job.result_file_name ?? "redacted")}>
              <Download className="h-3.5 w-3.5" aria-hidden />
              Download redacted file
            </Button>
          </div>
        </div>
        <div className="min-h-0 flex-1">
          {view === "original" && original ? (
            <DocumentViewer blob={original} kind={kind} fileName={original.name} highlight={false} />
          ) : file.isLoading ? (
            <Spinner label="Loading the redacted document" />
          ) : file.isError || !file.data ? (
            <div className="p-5">
              <Alert>{file.error instanceof ApiError ? file.error.message : "The redacted file could not be loaded."}</Alert>
            </div>
          ) : (
            <DocumentViewer blob={file.data} kind={kind} fileName={job.result_file_name ?? "redacted"} focus={focus} />
          )}
        </div>
      </div>

      {/* Findings */}
      <div className="card flex h-[calc(100vh-230px)] min-h-[560px] flex-col overflow-hidden">
        <div className="border-b border-line px-5 pt-4">
          <div className="mb-3 flex items-end justify-between">
            <div>
              <p className="text-2xl font-semibold tabular-nums text-ink-900" data-testid="redaction-count">
                {summary.total_occurrences ?? 0}
              </p>
              <p className="text-[13px] text-ink-600">
                redaction{summary.total_occurrences === 1 ? "" : "s"} across {summary.unique_entities ?? 0} unique value
                {summary.unique_entities === 1 ? "" : "s"}
              </p>
            </div>
            <Button variant="secondary" size="sm" onClick={() => download(`/jobs/${job.job_id}/metadata`, "redaction-metadata.json")}>
              <Download className="h-3.5 w-3.5" aria-hidden />
              Metadata JSON
            </Button>
          </div>
          <Tabs
            value={panel}
            onChange={setPanel}
            tabs={[
              { value: "entities", label: "Detected entities" },
              { value: "details", label: "Processing details" },
              { value: "json", label: "Metadata JSON" },
            ]}
          />
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto">
          {panel === "entities" && (
            <div>
              {verification && (
                <div className="border-b border-line px-5 py-3">
                  <Tag tone={verification.tone}>{verification.text}</Tag>
                </div>
              )}
              {(meta.warnings ?? []).map((w: string) => (
                <div key={w} className="px-5 pt-3">
                  <Alert tone="warning">{w}</Alert>
                </div>
              ))}
              {entities.length === 0 ? (
                <EmptyState title="Nothing was redacted" description="No text in this document matched the selected entity types." />
              ) : (
                <>
                  <div className="flex flex-wrap items-center gap-1.5 border-b border-line px-5 py-3">
                    <button
                      type="button"
                      onClick={() => setTypeFilter("all")}
                      className={`rounded px-2 py-0.5 text-xs font-medium transition-colors ${typeFilter === "all" ? "bg-brand-600 text-white" : "bg-surface-strong text-ink-700 hover:bg-brand-50"}`}
                    >
                      All {summary.total_occurrences}
                    </button>
                    {byType.map(([type, count]) => (
                      <button
                        key={type}
                        type="button"
                        onClick={() => setTypeFilter(type === typeFilter ? "all" : type)}
                        className={`rounded px-2 py-0.5 text-xs font-medium transition-colors ${typeFilter === type ? "bg-brand-600 text-white" : "bg-surface-strong text-ink-700 hover:bg-brand-50"}`}
                      >
                        {entityLabel(type)} {count}
                      </button>
                    ))}
                  </div>
                  <div className="flex items-center justify-between px-5 py-2.5">
                    <p className="text-xs text-ink-500">Original values are hidden by default.</p>
                    <button type="button" className="link flex items-center gap-1.5 text-xs" onClick={() => setReveal((v) => !v)}>
                      {reveal ? <EyeOff className="h-3.5 w-3.5" aria-hidden /> : <Eye className="h-3.5 w-3.5" aria-hidden />}
                      {reveal ? "Hide values" : "Show values"}
                    </button>
                  </div>
                  <table className="table" data-testid="entities-table">
                    <thead>
                      <tr>
                        <th>Placeholder</th>
                        <th>Original value</th>
                        <th className="text-right">Count</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((entity) => {
                        const page = entity.pages?.[0];
                        return (
                          <tr
                            key={entity.label}
                            className={page ? "cursor-pointer" : undefined}
                            onClick={() => page && view === "redacted" && setFocus({ page, tick: Date.now() })}
                            title={page ? `Go to page ${page}` : undefined}
                          >
                            <td>
                              <span className="placeholder-chip">{entity.label}</span>
                              <span className="mt-1 block text-xs text-ink-500">
                                {entityLabel(entity.type)}
                                {entity.source === "pattern" ? " · pattern" : ""}
                                {entity.pages?.length ? ` · p. ${entity.pages.join(", ")}` : ""}
                              </span>
                            </td>
                            <td className="max-w-[170px] break-words font-mono text-[12.5px]">{reveal ? entity.text : maskValue(entity.text)}</td>
                            <td className="text-right tabular-nums">{entity.occurrences}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                  {(meta.unmatched ?? []).length > 0 && (
                    <p className="px-5 py-3 text-xs text-ink-500">
                      {meta.unmatched.length} value{meta.unmatched.length === 1 ? "" : "s"} reported by the detector could not be located in the document and
                      were ignored. They are listed in the metadata JSON.
                    </p>
                  )}
                </>
              )}
            </div>
          )}

          {panel === "details" && (
            <dl className="divide-y divide-line px-5 py-2">
              <Fact label="Source file" value={job.file_name} />
              <Fact label="Size" value={formatBytes(job.file_size)} />
              <Fact label="Format" value={(job.input_format ?? "").toUpperCase()} />
              <Fact label="Submitted" value={formatDate(job.created_at)} />
              <Fact label="Total time" value={formatDuration(job.duration_ms)} />
              <Fact label="Pages" value={summary.pages} />
              <Fact label="Pages read from text layer" value={summary.pages_text_layer} />
              <Fact label="Pages read with OCR" value={summary.pages_ocr} />
              <Fact label="Paragraphs" value={summary.paragraphs} />
              <Fact label="Lines" value={summary.lines} />
              <Fact label="Detection engine" value={engines.join(", ")} />
              <Fact label="Model calls" value={meta.engine?.llm?.calls} />
              <Fact label="Tokens in / out" value={meta.engine?.llm ? `${meta.engine.llm.input_tokens} / ${meta.engine.llm.output_tokens}` : null} />
              <Fact label="Language model cost" value={job.usage ? formatCost(job.usage.total) : null} />
              <Fact label="OCR models" value={meta.engine?.ocr ? `${meta.engine.ocr.detection_model}, ${meta.engine.ocr.recognition_model}` : null} />
              <Fact label="Text recognition time" value={meta.timings_ms?.ocr_ms ? formatDuration(meta.timings_ms.ocr_ms) : null} />
              <Fact label="Detection time" value={meta.timings_ms?.detect_ms ? formatDuration(meta.timings_ms.detect_ms) : null} />
              <Fact label="Redaction style" value={meta.options?.redaction_style === "blackout" ? "Blackout" : "Labelled placeholders"} />
              <Fact label="Custom entities" value={(meta.options?.custom_entities ?? []).map((c: { label: string }) => c.label).join(", ")} />
              <div className="py-2.5">
                <dt className="mb-1.5 text-[13px] text-ink-600">Usage by engine</dt>
                <dd>
                  <UsageSummary usage={job.usage} compact emptyText="Pattern detectors only: no language model calls." />
                </dd>
              </div>
            </dl>
          )}

          {panel === "json" && (
            <pre className="whitespace-pre-wrap break-words p-5 font-mono text-[11.5px] leading-relaxed text-ink-800" data-testid="metadata-json">
              {metadataJson}
            </pre>
          )}
        </div>
      </div>
    </div>
  );
}

export default function PhiJob() {
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
        <PageHeader title="PHI Redaction" />
        <Alert>{error instanceof ApiError ? error.message : "This job could not be loaded."}</Alert>
      </Page>
    );
  }

  const done = job.status === "COMPLETED";
  return (
    <Page>
      <PageHeader
        title={job.file_name || "PHI Redaction"}
        subtitle={done ? "Review the redacted document and the detection report, then download both." : "Redaction in progress"}
        actions={
          <>
            <StatusBadge status={job.status} />
            <Link to="/phi" className="btn-secondary">
              New redaction
            </Link>
          </>
        }
      />
      <Stepper steps={PHI_STEPS} current={done ? 3 : 2} />
      {job.status === "FAILED" ? <JobFailed job={job} retryTo="/phi" /> : done ? <Review job={job} /> : <JobProgress job={job} />}
    </Page>
  );
}
