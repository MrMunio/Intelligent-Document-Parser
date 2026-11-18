import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { EmptyState, Page, PageHeader, Pagination, Spinner } from "../components/ui";
import { api } from "../lib/api";
import { formatDate } from "../lib/format";

interface AuditEntry {
  id: number;
  created_at: string;
  actor_email: string | null;
  action: string;
  target_type: string | null;
  target_id: string | null;
  detail: Record<string, unknown> | null;
  ip_address: string | null;
}

const ACTIONS: Record<string, string> = {
  "auth.login": "Signed in",
  "auth.login_failed": "Failed sign-in attempt",
  "auth.password_changed": "Changed own password",
  "user.created": "Created user",
  "user.updated": "Updated user",
  "user.deleted": "Deleted user",
  "user.password_reset": "Reset a user's password",
  "api_key.created": "Created API key",
  "api_key.revoked": "Revoked API key",
  "job.submitted": "Submitted job",
  "job.deleted": "Deleted job",
  "job.file_downloaded": "Downloaded output file",
};

const FILTERS = [
  { value: "", label: "All activity" },
  { value: "auth.", label: "Sign-ins" },
  { value: "user.", label: "User management" },
  { value: "api_key.", label: "API keys" },
  { value: "job.", label: "Jobs" },
];

function describe(entry: AuditEntry): string {
  const d = entry.detail ?? {};
  const parts: string[] = [];
  if (typeof d.email === "string") parts.push(d.email);
  if (typeof d.name === "string") parts.push(d.name);
  if (typeof d.file === "string") parts.push(d.file);
  if (typeof d.job_type === "string") parts.push(String(d.job_type));
  if (typeof d.via === "string") parts.push(d.via === "api" ? "via API" : "via web app");
  if (Array.isArray(d.roles)) parts.push(`roles: ${d.roles.join(", ")}`);
  if (Array.isArray(d.scopes)) parts.push(`services: ${d.scopes.join(", ")}`);
  if (Array.isArray(d.custom_entities) && d.custom_entities.length) parts.push(`custom: ${d.custom_entities.join(", ")}`);
  return parts.join("  ·  ");
}

const PAGE_SIZE = 25;

export default function AdminAudit() {
  const [filter, setFilter] = useState("");
  const [page, setPage] = useState(1);
  const logs = useQuery({
    queryKey: ["audit", filter, page],
    queryFn: () => api<{ items: AuditEntry[]; total: number }>(`/admin/audit-logs?page=${page}&page_size=${PAGE_SIZE}${filter ? `&action=${filter}` : ""}`),
  });

  return (
    <Page>
      <PageHeader title="Audit Log" subtitle="A record of sign-ins, account changes, API key activity and job submissions." />
      <div className="mb-4">
        <select
          className="input w-56"
          aria-label="Filter activity"
          value={filter}
          onChange={(e) => {
            setFilter(e.target.value);
            setPage(1);
          }}
        >
          {FILTERS.map((f) => (
            <option key={f.value} value={f.value}>
              {f.label}
            </option>
          ))}
        </select>
      </div>
      {logs.isLoading ? (
        <Spinner />
      ) : logs.data && logs.data.items.length ? (
        <>
          <div className="table-wrap">
            <table className="table" data-testid="audit-table">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Who</th>
                  <th>Action</th>
                  <th>Details</th>
                  <th>IP address</th>
                </tr>
              </thead>
              <tbody>
                {logs.data.items.map((entry) => (
                  <tr key={entry.id}>
                    <td className="whitespace-nowrap text-ink-600">{formatDate(entry.created_at)}</td>
                    <td>{entry.actor_email ?? "Unknown"}</td>
                    <td className="whitespace-nowrap font-medium">{ACTIONS[entry.action] ?? entry.action}</td>
                    <td className="text-[13px] text-ink-600">{describe(entry)}</td>
                    <td className="font-mono text-[12.5px] text-ink-600">{entry.ip_address ?? ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pagination page={page} pageSize={PAGE_SIZE} total={logs.data.total} onPage={setPage} />
        </>
      ) : (
        <div className="card">
          <EmptyState title="No activity recorded for this filter" />
        </div>
      )}
    </Page>
  );
}
