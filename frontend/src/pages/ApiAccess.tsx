import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, Plus } from "lucide-react";
import { useMemo, useState } from "react";
import { Alert, Button, Checkbox, CopyButton, EmptyState, Field, Modal, Page, PageHeader, Section, Spinner, Tabs, Tag, useToast } from "../components/ui";
import { api, ApiError } from "../lib/api";
import { useAuth } from "../lib/auth";
import { formatDate, formatRelative } from "../lib/format";
import { SERVICE_LABELS, type ApiKey, type Service } from "../lib/types";

type Example = "redact" | "text" | "async" | "parse";

function examples(base: string): Record<Example, { title: string; note: string; code: string }> {
  return {
    redact: {
      title: "Redact a file",
      note: "Waits for the result and writes the redacted file, in the same format as the input. The job id is returned in the X-Job-Id response header.",
      code: `curl -X POST "${base}/api/v1/phi/redact" \\
  -H "X-API-Key: $IDP_API_KEY" \\
  -F "file=@discharge-summary.docx" \\
  -F 'custom_entities=[{"label":"EMPLOYEE_ID","description":"Internal employee numbers such as EMP-48213"}]' \\
  -F "response_format=file" \\
  -D headers.txt -o discharge-summary.redacted.docx

# Detection report for the same job
curl "${base}/api/v1/jobs/<job_id>/metadata" -H "X-API-Key: $IDP_API_KEY" -o metadata.json`,
    },
    text: {
      title: "Redact text",
      note: "JSON in, JSON out. Suited to chat messages, notes and other short text.",
      code: `curl -X POST "${base}/api/v1/phi/redact-text" \\
  -H "X-API-Key: $IDP_API_KEY" -H "Content-Type: application/json" \\
  -d '{
    "text": "Patient Sarah Mitchell (MRN 4481920) called from (415) 555-0134.",
    "entity_types": ["PATIENT_NAME", "MRN", "PHONE"],
    "custom_entities": [{"label": "CASE_NUMBER", "description": "Support case numbers starting with CS-"}]
  }'`,
    },
    async: {
      title: "Submit and poll",
      note: "For large documents. Submit returns immediately with a job id; poll until the status is COMPLETED or FAILED.",
      code: `# 1. Submit
curl -X POST "${base}/api/v1/phi/jobs" \\
  -H "X-API-Key: $IDP_API_KEY" -F "file=@scanned-records.pdf"
# -> {"job_id": "...", "status": "PENDING", "links": {"self": "/api/v1/jobs/..."}}

# 2. Poll
curl "${base}/api/v1/jobs/<job_id>" -H "X-API-Key: $IDP_API_KEY"

# 3. Download the redacted file and the metadata
curl "${base}/api/v1/jobs/<job_id>/file" -H "X-API-Key: $IDP_API_KEY" -OJ
curl "${base}/api/v1/jobs/<job_id>/metadata" -H "X-API-Key: $IDP_API_KEY" -OJ`,
    },
    parse: {
      title: "Parse a document",
      note: "Returns markdown and structured JSON chunks. Use mode=ocr for plain text with word coordinates.",
      code: `curl -X POST "${base}/api/v1/parse/sync" \\
  -H "X-API-Key: $IDP_API_KEY" \\
  -F "file=@report.pdf" -F "mode=structured"`,
    },
  };
}

export default function ApiAccess() {
  const { user, isAdmin } = useAuth();
  const toast = useToast();
  const queryClient = useQueryClient();
  const [showAll, setShowAll] = useState(false);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<Service[]>(user?.services ?? []);
  const [expiry, setExpiry] = useState("90");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<string | null>(null);
  const [revoking, setRevoking] = useState<ApiKey | null>(null);
  const [example, setExample] = useState<Example>("redact");

  const keys = useQuery({
    queryKey: ["api-keys", showAll],
    queryFn: () => api<ApiKey[]>(`/api-keys${showAll ? "?all_users=true" : ""}`),
  });
  const base = window.location.origin;
  const samples = useMemo(() => examples(base), [base]);
  const visibleExamples = (Object.keys(samples) as Example[]).filter((key) =>
    key === "parse" ? user?.services.includes("parse") : user?.services.includes("phi"),
  );
  const current = visibleExamples.includes(example) ? example : visibleExamples[0];

  async function create() {
    setBusy(true);
    setError(null);
    try {
      const res = await api<{ api_key: string }>("/api-keys", {
        method: "POST",
        body: { name: name.trim(), scopes, expires_in_days: expiry ? Number(expiry) : null },
      });
      setCreated(res.api_key);
      setCreating(false);
      setName("");
      queryClient.invalidateQueries({ queryKey: ["api-keys"] });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not create the key.");
    } finally {
      setBusy(false);
    }
  }

  async function revoke() {
    if (!revoking) return;
    try {
      await api(`/api-keys/${revoking.id}`, { method: "DELETE" });
      toast("success", `Key "${revoking.name}" was revoked.`);
      queryClient.invalidateQueries({ queryKey: ["api-keys"] });
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : "Could not revoke the key.");
    } finally {
      setRevoking(null);
    }
  }

  return (
    <Page>
      <PageHeader
        title="API Access"
        subtitle="Call PHI redaction and document parsing from your own systems. The API is self-contained and does not depend on this web app."
        actions={
          <a className="btn-secondary" href="/api/docs" target="_blank" rel="noreferrer">
            Interactive API reference
            <ExternalLink className="h-3.5 w-3.5" aria-hidden />
          </a>
        }
      />

      <div className="space-y-5">
        <Section
          title="API keys"
          subtitle="A key acts as you, limited to the services you select. Keys are shown once and stored only as a hash."
          actions={
            <div className="flex items-center gap-4">
              {isAdmin && <Checkbox checked={showAll} onChange={setShowAll} label="Show keys of all users" />}
              <Button
                size="sm"
                onClick={() => {
                  setScopes(user?.services ?? []);
                  setError(null);
                  setCreating(true);
                }}
              >
                <Plus className="h-3.5 w-3.5" aria-hidden />
                Create API key
              </Button>
            </div>
          }
          className="[&>div:last-child]:p-0"
        >
          {keys.isLoading ? (
            <Spinner />
          ) : keys.data && keys.data.length ? (
            <div className="overflow-x-auto">
              <table className="table" data-testid="api-keys-table">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Key</th>
                    <th>Services</th>
                    {showAll && <th>Owner</th>}
                    <th>Last used</th>
                    <th>Expires</th>
                    <th>Status</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {keys.data.map((key) => {
                    const expired = key.expires_at ? new Date(key.expires_at) < new Date() : false;
                    return (
                      <tr key={key.id}>
                        <td className="font-medium">{key.name}</td>
                        <td className="font-mono text-[12.5px] text-ink-600">{key.prefix}...</td>
                        <td>
                          <div className="flex flex-wrap gap-1">
                            {key.scopes.map((s) => (
                              <Tag key={s} tone="blue">
                                {SERVICE_LABELS[s]}
                              </Tag>
                            ))}
                          </div>
                        </td>
                        {showAll && <td className="text-ink-600">{key.owner_email}</td>}
                        <td className="whitespace-nowrap text-ink-600">{formatRelative(key.last_used_at)}</td>
                        <td className="whitespace-nowrap text-ink-600">{key.expires_at ? formatDate(key.expires_at) : "No expiry"}</td>
                        <td>{key.revoked_at ? <Tag tone="red">Revoked</Tag> : expired ? <Tag tone="amber">Expired</Tag> : <Tag tone="green">Active</Tag>}</td>
                        <td className="text-right">
                          {!key.revoked_at && (
                            <Button variant="danger" size="sm" onClick={() => setRevoking(key)}>
                              Revoke
                            </Button>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState title="No API keys yet" description="Create a key to call the API from a script, a backend service or an integration platform." />
          )}
        </Section>

        <Section title="Quick start" subtitle="Authenticate every request with the X-API-Key header">
          <div className="mb-4 grid gap-3 text-[13px] md:grid-cols-3">
            <div>
              <p className="text-ink-500">Base URL</p>
              <p className="font-mono text-ink-900">{base}/api/v1</p>
            </div>
            <div>
              <p className="text-ink-500">Authentication</p>
              <p className="font-mono text-ink-900">X-API-Key: idp_...</p>
            </div>
            <div>
              <p className="text-ink-500">OpenAPI specification</p>
              <a className="link font-mono" href="/api/openapi.json" target="_blank" rel="noreferrer">
                /api/openapi.json
              </a>
            </div>
          </div>
          {current && (
            <>
              <Tabs value={current} onChange={setExample} tabs={visibleExamples.map((key) => ({ value: key, label: samples[key].title }))} />
              <p className="mt-4 text-[13px] text-ink-600">{samples[current].note}</p>
              <div className="relative mt-3">
                <pre className="code-block pr-24">{samples[current].code}</pre>
                <div className="absolute right-3 top-3">
                  <CopyButton value={samples[current].code} />
                </div>
              </div>
            </>
          )}
        </Section>
      </div>

      <Modal
        open={creating}
        title="Create API key"
        subtitle="Choose a name that identifies the system that will use it"
        onClose={() => setCreating(false)}
        footer={
          <>
            <Button variant="secondary" onClick={() => setCreating(false)}>
              Cancel
            </Button>
            <Button loading={busy} disabled={!name.trim() || scopes.length === 0} onClick={create}>
              Create key
            </Button>
          </>
        }
      >
        <div className="space-y-4">
          {error && <Alert>{error}</Alert>}
          <Field label="Name">
            <input className="input" value={name} maxLength={120} placeholder="Claims intake service" onChange={(e) => setName(e.target.value)} autoFocus />
          </Field>
          <div>
            <span className="label">Services this key can call</span>
            <div className="space-y-2">
              {(user?.services ?? []).map((service) => (
                <Checkbox
                  key={service}
                  checked={scopes.includes(service)}
                  onChange={(on) => setScopes((list) => (on ? [...list, service] : list.filter((s) => s !== service)))}
                  label={SERVICE_LABELS[service]}
                />
              ))}
            </div>
          </div>
          <Field label="Expiry">
            <select className="input" value={expiry} onChange={(e) => setExpiry(e.target.value)}>
              <option value="30">30 days</option>
              <option value="90">90 days</option>
              <option value="365">1 year</option>
              <option value="">No expiry</option>
            </select>
          </Field>
        </div>
      </Modal>

      <Modal
        open={Boolean(created)}
        title="Copy your API key now"
        subtitle="For security it cannot be shown again"
        onClose={() => setCreated(null)}
        footer={<Button onClick={() => setCreated(null)}>Done</Button>}
      >
        <div className="flex items-center gap-2">
          <code className="min-w-0 flex-1 break-all rounded-md border border-line bg-surface px-3 py-2 font-mono text-[12.5px]" data-testid="new-api-key">
            {created}
          </code>
          <CopyButton value={created ?? ""} />
        </div>
        <p className="mt-3 text-[13px] text-ink-600">Store it in your secret manager. If it is ever exposed, revoke it here and create a new one.</p>
      </Modal>

      <Modal
        open={Boolean(revoking)}
        title="Revoke this API key?"
        subtitle={revoking?.name}
        onClose={() => setRevoking(null)}
        footer={
          <>
            <Button variant="secondary" onClick={() => setRevoking(null)}>
              Cancel
            </Button>
            <Button variant="danger" onClick={revoke}>
              Revoke key
            </Button>
          </>
        }
      >
        <p className="text-sm text-ink-700">Any system using this key will immediately lose access. This cannot be undone.</p>
      </Modal>
    </Page>
  );
}
