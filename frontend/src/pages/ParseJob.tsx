import { Download } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { JobFailed, JobProgress } from "../components/JobProgress";
import { MarkdownView } from "../components/viewers";
import { Alert, Button, CopyButton, Page, PageHeader, Spinner, StatusBadge, Stepper, Tabs, useToast } from "../components/ui";
import { ApiError, downloadFile } from "../lib/api";
import { formatDuration } from "../lib/format";
import { formatCost, UsageSummary } from "../components/UsageSummary";
import { useJob } from "../lib/hooks";
import type { Job } from "../lib/types";
import { PARSE_STEPS } from "./ParseNew";

function saveText(name: string, text: string, type: string) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

function Review({ job }: { job: Job }) {
  const toast = useToast();
  const structured = job.job_type === "PARSE";
  const [tab, setTab] = useState<"rendered" | "markdown" | "json" | "usage">(structured ? "rendered" : "markdown");
  const markdown = job.result_markdown ?? "";
  const json = useMemo(() => JSON.stringify(job.result_data ?? {}, null, 2), [job.result_data]);
  const stem = (job.file_name ?? "document").replace(/\.[^.]+$/, "");
  const stats = job.stats ?? {};
  const facts = [
    stats.pages ? `${stats.pages} page${stats.pages === 1 ? "" : "s"}` : null,
    stats.chunks ? `${stats.chunks} content blocks` : null,
    stats.lines ? `${stats.lines} text lines` : null,
    job.duration_ms ? formatDuration(job.duration_ms) : null,
    job.usage?.total.calls ? `${job.usage.total.calls} model call${job.usage.total.calls === 1 ? "" : "s"}, ${formatCost(job.usage.total)}` : null,
  ].filter(Boolean);

  const tabs = structured
    ? [
        { value: "rendered" as const, label: "Rendered" },
        { value: "markdown" as const, label: "Markdown" },
        { value: "json" as const, label: "JSON" },
        { value: "usage" as const, label: "Model usage" },
      ]
    : [
        { value: "markdown" as const, label: "Extracted text" },
        { value: "json" as const, label: "JSON with coordinates" },
        { value: "usage" as const, label: "Model usage" },
      ];

  return (
    <div className="card overflow-hidden">
      <div className="flex flex-wrap items-end justify-between gap-3 border-b border-line px-5 pt-4">
        <div>
          <p className="mb-3 text-[13px] text-ink-600">{facts.join("  ·  ")}</p>
          <Tabs value={tab} onChange={setTab} tabs={tabs} />
        </div>
        <div className="flex gap-2 pb-3">
          <CopyButton value={tab === "json" ? json : markdown} />
          <Button
            variant="secondary"
            size="sm"
            onClick={async () => {
              try {
                if (structured) await downloadFile(`/jobs/${job.job_id}/markdown`, `${stem}.md`);
                else saveText(`${stem}.txt`, markdown, "text/plain");
              } catch (err) {
                toast("error", err instanceof ApiError ? err.message : "Download failed.");
              }
            }}
          >
            <Download className="h-3.5 w-3.5" aria-hidden />
            {structured ? "Markdown" : "Text"}
          </Button>
          <Button size="sm" onClick={() => saveText(`${stem}.json`, json, "application/json")}>
            <Download className="h-3.5 w-3.5" aria-hidden />
            JSON
          </Button>
        </div>
      </div>
      <div className="max-h-[calc(100vh-330px)] min-h-[420px] overflow-auto" data-testid="parse-result">
        {tab === "rendered" && (
          <div className="mx-auto max-w-4xl px-8 py-6">
            <MarkdownView markdown={markdown} />
          </div>
        )}
        {tab === "markdown" && <pre className="whitespace-pre-wrap break-words p-6 font-mono text-[12.5px] leading-relaxed text-ink-800">{markdown}</pre>}
        {tab === "usage" && (
          <div className="p-6">
            <UsageSummary
              usage={job.usage}
              emptyText={structured ? "No figure descriptions were requested, so no language model was called." : "Text extraction uses OCR only: no language model calls and no token cost."}
            />
          </div>
        )}
        {tab === "json" && <pre className="whitespace-pre-wrap break-words p-6 font-mono text-[11.5px] leading-relaxed text-ink-800">{json}</pre>}
      </div>
    </div>
  );
}

export default function ParseJob() {
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
        <PageHeader title="Document Parsing" />
        <Alert>{error instanceof ApiError ? error.message : "This job could not be loaded."}</Alert>
      </Page>
    );
  }
  const done = job.status === "COMPLETED";
  return (
    <Page>
      <PageHeader
        title={job.file_name || "Document Parsing"}
        subtitle={done ? (job.job_type === "PARSE" ? "Structured content extracted from the document" : "Text extracted from the document") : "Parsing in progress"}
        actions={
          <>
            <StatusBadge status={job.status} />
            <Link to="/parse" className="btn-secondary">
              New document
            </Link>
          </>
        }
      />
      <Stepper steps={PARSE_STEPS} current={done ? 3 : 2} />
      {job.status === "FAILED" ? <JobFailed job={job} retryTo="/parse" /> : done ? <Review job={job} /> : <JobProgress job={job} />}
    </Page>
  );
}
