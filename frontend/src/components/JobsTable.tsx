import { Trash2 } from "lucide-react";
import { Link } from "react-router-dom";
import { formatBytes, formatDuration, formatRelative } from "../lib/format";
import { JOB_TYPE_LABELS, type Job } from "../lib/types";
import { cx, StatusBadge } from "./ui";

export function jobPath(job: Job): string {
  return job.job_type === "PHI" ? `/phi/jobs/${job.job_id}` : `/parse/jobs/${job.job_id}`;
}

function outcome(job: Job): string {
  if (job.status !== "COMPLETED" || !job.stats) return "";
  if (job.job_type === "PHI") {
    const n = job.stats.entities ?? 0;
    return `${n} redaction${n === 1 ? "" : "s"}`;
  }
  const pages = job.stats.pages;
  return pages ? `${pages} page${pages === 1 ? "" : "s"}` : "";
}

export function JobsTable({
  jobs,
  showOwner = false,
  onDelete,
  bare = false,
}: {
  jobs: Job[];
  showOwner?: boolean;
  onDelete?: (job: Job) => void;
  bare?: boolean;
}) {
  return (
    <div className={cx(!bare && "table-wrap", bare && "overflow-x-auto")}>
      <table className="table">
        <thead>
          <tr>
            <th>Document</th>
            <th>Service</th>
            <th>Status</th>
            <th>Result</th>
            <th>Duration</th>
            {showOwner && <th>Submitted by</th>}
            <th>Submitted</th>
            {onDelete && <th className="w-10" />}
          </tr>
        </thead>
        <tbody>
          {jobs.map((job) => (
            <tr key={job.job_id}>
              <td className="max-w-[320px]">
                <Link to={jobPath(job)} className="link block truncate" title={job.file_name ?? ""}>
                  {job.file_name || "Untitled"}
                </Link>
                <span className="text-xs text-ink-500">
                  {formatBytes(job.file_size)}
                  {job.source === "api" ? " · via API" : ""}
                </span>
              </td>
              <td className="whitespace-nowrap">{JOB_TYPE_LABELS[job.job_type]}</td>
              <td>
                <StatusBadge status={job.status} />
              </td>
              <td className="whitespace-nowrap text-ink-600">{outcome(job)}</td>
              <td className="whitespace-nowrap tabular-nums text-ink-600">{formatDuration(job.duration_ms)}</td>
              {showOwner && <td className="text-ink-600">{job.owner_email ?? ""}</td>}
              <td className="whitespace-nowrap text-ink-600">{formatRelative(job.created_at)}</td>
              {onDelete && (
                <td>
                  <button
                    type="button"
                    className="rounded p-1.5 text-ink-500 transition-colors hover:bg-red-50 hover:text-red-700 disabled:opacity-40"
                    aria-label={`Delete ${job.file_name ?? "job"}`}
                    disabled={job.status === "PROCESSING"}
                    onClick={() => onDelete(job)}
                  >
                    <Trash2 className="h-4 w-4" />
                  </button>
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
