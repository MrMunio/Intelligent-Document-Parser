import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { JobsTable } from "../components/JobsTable";
import { Button, EmptyState, Modal, Page, PageHeader, Pagination, Segmented, Spinner, useToast } from "../components/ui";
import { api, ApiError } from "../lib/api";
import { useAuth } from "../lib/auth";
import { useJobs } from "../lib/hooks";
import type { Job } from "../lib/types";

const PAGE_SIZE = 15;

export default function Jobs() {
  const { isAdmin, can } = useAuth();
  const toast = useToast();
  const queryClient = useQueryClient();
  const [scope, setScope] = useState<"mine" | "all">("mine");
  const [type, setType] = useState("");
  const [status, setStatus] = useState("");
  const [page, setPage] = useState(1);
  const [pendingDelete, setPendingDelete] = useState<Job | null>(null);
  const [deleting, setDeleting] = useState(false);

  const jobs = useJobs({ scope, job_type: type, status, page, page_size: PAGE_SIZE });

  async function confirmDelete() {
    if (!pendingDelete) return;
    setDeleting(true);
    try {
      await api(`/jobs/${pendingDelete.job_id}`, { method: "DELETE" });
      toast("success", "Job and its output were deleted.");
      setPendingDelete(null);
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : "Could not delete the job.");
    } finally {
      setDeleting(false);
    }
  }

  return (
    <Page>
      <PageHeader title="Job History" subtitle="Every document submitted from the web app or through the API, with status and results." />

      <div className="mb-4 flex flex-wrap items-center gap-3">
        {isAdmin && (
          <Segmented
            value={scope}
            onChange={(value) => {
              setScope(value);
              setPage(1);
            }}
            options={[
              { value: "mine", label: "My jobs" },
              { value: "all", label: "All users" },
            ]}
          />
        )}
        <select
          className="input w-48"
          aria-label="Filter by service"
          value={type}
          onChange={(e) => {
            setType(e.target.value);
            setPage(1);
          }}
        >
          <option value="">All services</option>
          {can("phi") && <option value="PHI">PHI redaction</option>}
          {can("parse") && <option value="PARSE">Document parsing</option>}
          {can("parse") && <option value="OCR">Text extraction</option>}
        </select>
        <select
          className="input w-44"
          aria-label="Filter by status"
          value={status}
          onChange={(e) => {
            setStatus(e.target.value);
            setPage(1);
          }}
        >
          <option value="">All statuses</option>
          <option value="PENDING">Queued</option>
          <option value="PROCESSING">Processing</option>
          <option value="COMPLETED">Completed</option>
          <option value="FAILED">Failed</option>
        </select>
        {jobs.data && <span className="ml-auto text-[13px] text-ink-600">{jobs.data.total} job{jobs.data.total === 1 ? "" : "s"}</span>}
      </div>

      {jobs.isLoading ? (
        <Spinner />
      ) : jobs.data && jobs.data.items.length ? (
        <>
          <JobsTable jobs={jobs.data.items} showOwner={scope === "all"} onDelete={setPendingDelete} />
          <Pagination page={page} pageSize={PAGE_SIZE} total={jobs.data.total} onPage={setPage} />
        </>
      ) : (
        <div className="card">
          <EmptyState title="No jobs match these filters" description="Submit a document from PHI Redaction or Document Parsing to see it here." />
        </div>
      )}

      <Modal
        open={Boolean(pendingDelete)}
        title="Delete this job?"
        subtitle={pendingDelete?.file_name ?? undefined}
        onClose={() => setPendingDelete(null)}
        footer={
          <>
            <Button variant="secondary" onClick={() => setPendingDelete(null)}>
              Cancel
            </Button>
            <Button variant="danger" loading={deleting} onClick={confirmDelete}>
              Delete job
            </Button>
          </>
        }
      >
        <p className="text-sm text-ink-700">The job record, its output file and its detection report will be permanently removed. This cannot be undone.</p>
      </Modal>
    </Page>
  );
}
