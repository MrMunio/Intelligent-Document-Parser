import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { Alert, Button, Field, Page, PageHeader, Section, Tag } from "../components/ui";
import { api, ApiError } from "../lib/api";
import { useAuth } from "../lib/auth";
import { formatDate } from "../lib/format";
import { ROLE_LABELS, SERVICE_LABELS, type TokenResponse } from "../lib/types";

export default function Account() {
  const { user, applySession } = useAuth();
  const navigate = useNavigate();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);
  if (!user) return null;
  const forced = user.must_change_password;
  const mismatch = confirm.length > 0 && next !== confirm;

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setDone(false);
    setBusy(true);
    try {
      const session = await api<TokenResponse>("/auth/change-password", { method: "POST", body: { current_password: current, new_password: next } });
      applySession(session);
      setCurrent("");
      setNext("");
      setConfirm("");
      if (forced) navigate("/", { replace: true });
      else setDone(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not change the password.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Page>
      <PageHeader title="Account" subtitle="Your profile, access and password." />
      {forced && (
        <div className="mb-5">
          <Alert tone="info">You are signed in with a temporary password. Choose a new password to continue.</Alert>
        </div>
      )}
      <div className="grid gap-5 lg:grid-cols-2">
        <Section title="Profile" subtitle="Managed by your administrator">
          <dl className="divide-y divide-line text-sm">
            <div className="flex justify-between gap-4 py-2.5">
              <dt className="text-ink-600">Name</dt>
              <dd className="font-medium">{user.full_name}</dd>
            </div>
            <div className="flex justify-between gap-4 py-2.5">
              <dt className="text-ink-600">Email</dt>
              <dd className="font-medium">{user.email}</dd>
            </div>
            <div className="flex justify-between gap-4 py-2.5">
              <dt className="text-ink-600">Roles</dt>
              <dd className="flex flex-wrap justify-end gap-1">
                {user.roles.map((r) => (
                  <Tag key={r} tone="blue">
                    {ROLE_LABELS[r]}
                  </Tag>
                ))}
              </dd>
            </div>
            <div className="flex justify-between gap-4 py-2.5">
              <dt className="text-ink-600">Services</dt>
              <dd className="text-right font-medium">{user.services.map((s) => SERVICE_LABELS[s]).join(", ") || "None"}</dd>
            </div>
            <div className="flex justify-between gap-4 py-2.5">
              <dt className="text-ink-600">Last sign-in</dt>
              <dd className="font-medium">{formatDate(user.last_login_at)}</dd>
            </div>
          </dl>
        </Section>

        <Section title="Change password" subtitle="At least 10 characters, with a letter and a number. Other sessions are signed out.">
          <form onSubmit={submit} className="space-y-4">
            {error && <Alert>{error}</Alert>}
            {done && <Alert tone="success">Your password was changed.</Alert>}
            <Field label={forced ? "Temporary password" : "Current password"}>
              <input className="input" type="password" name="current_password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} />
            </Field>
            <Field label="New password">
              <input className="input" type="password" name="new_password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} />
            </Field>
            <Field label="Confirm new password" error={mismatch ? "The passwords do not match." : undefined}>
              <input className="input" type="password" name="confirm_password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
            </Field>
            <Button type="submit" loading={busy} disabled={!current || !next || next !== confirm}>
              Change password
            </Button>
          </form>
        </Section>
      </div>
    </Page>
  );
}
