import { useQuery } from "@tanstack/react-query";
import { api } from "./api";
import type { Job, JobList } from "./types";

const ACTIVE = new Set(["PENDING", "PROCESSING"]);

/** Polls a job while it is running. Progress is stored server side, so a refresh picks up where it was. */
export function useJob(jobId: string | undefined) {
  return useQuery({
    queryKey: ["job", jobId],
    queryFn: () => api<Job>(`/jobs/${jobId}`),
    enabled: Boolean(jobId),
    refetchInterval: (query) => (query.state.data && !ACTIVE.has(query.state.data.status) ? false : 1000),
  });
}

export function useJobs(params: Record<string, string | number | undefined>) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== "") search.set(key, String(value));
  });
  return useQuery({
    queryKey: ["jobs", search.toString()],
    queryFn: () => api<JobList>(`/jobs?${search.toString()}`),
    refetchInterval: (query) => (query.state.data?.items.some((j) => ACTIVE.has(j.status)) ? 2500 : false),
  });
}
