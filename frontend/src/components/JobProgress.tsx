import { motion } from "framer-motion";
import { Link } from "react-router-dom";
import { titleCase } from "../lib/format";
import type { Job } from "../lib/types";
import { Alert, ProgressBar, Section } from "./ui";

const STAGE_LABELS: Record<string, string> = {
  queued: "Waiting in the queue",
  starting: "Starting",
  analyzing: "Analysing the document",
  loading: "Loading models",
  ocr: "Recognising text",
  detecting: "Detecting sensitive entities",
  redacting: "Applying redactions",
  verifying: "Verifying the output",
  parsing: "Parsing pages",
  describing: "Describing figures",
  reading: "Reading the document",
  indexing: "Mapping and indexing the document",
  agent: "Agent extracting fields",
  extracting: "Extracting fields part by part",
  finishing: "Writing the result",
};

export function JobProgress({ job }: { job: Job }) {
  const stage = job.progress?.stage ?? "queued";
  const percent = job.status === "PENDING" ? 0 : job.progress?.percent ?? 0;
  return (
    <Section title="Processing" subtitle="You can leave this page. The job keeps running and the result will be in Job History.">
      <div className="mx-auto max-w-2xl py-6">
        <div className="mb-2 flex items-baseline justify-between">
          <motion.p key={stage} initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} className="text-sm font-medium text-ink-900">
            {STAGE_LABELS[stage] ?? titleCase(stage)}
          </motion.p>
          <span className="text-sm tabular-nums text-ink-600">{percent}%</span>
        </div>
        <ProgressBar percent={percent} />
        <p className="mt-2 min-h-[18px] text-xs text-ink-500">{job.progress?.detail ?? ""}</p>
      </div>
    </Section>
  );
}

export function JobFailed({ job, retryTo }: { job: Job; retryTo: string }) {
  return (
    <Section title="This job did not complete">
      <Alert>{job.error ?? "Processing failed for an unknown reason."}</Alert>
      <p className="mt-3 text-sm text-ink-600">
        Nothing was produced for this document.{" "}
        <Link to={retryTo} className="link">
          Submit it again
        </Link>{" "}
        or contact an administrator if the problem continues.
      </p>
    </Section>
  );
}
