import { useQuery, useQueryClient } from "@tanstack/react-query";
import { UserPlus } from "lucide-react";
import { useState } from "react";
import { Alert, Button, Checkbox, CopyButton, Field, Modal, Page, PageHeader, Spinner, Tag, useToast } from "../components/ui";
import { api, ApiError } from "../lib/api";
import { useAuth } from "../lib/auth";
import { formatRelative } from "../lib/format";
import { ROLE_DESCRIPTIONS, ROLE_LABELS, type Role, type User } from "../lib/types";

const ALL_ROLES: Role[] = ["super_admin", "admin", "document_parser", "phi_redactor"];
const ADMIN_ROLES: Role[] = ["super_admin", "admin"];

function RolePicker({ value, onChange, allowed }: { value: Role[]; onChange: (roles: Role[]) => void; allowed: Role[] }) {
  return (
    <div className="space-y-2.5">
      {ALL_ROLES.map((role) => (
        <Checkbox
          key={role}
          checked={value.includes(role)}
          disabled={!allowed.includes(role)}
          onChange={(on) => onChange(on ? [...value, role] : value.filter((r) => r !== role))}
          label={ROLE_LABELS[role]}
          description={ROLE_DESCRIPTIONS[role]}
        />
      ))}
    </div>
  );
}

export default function AdminUsers() {
  const { user: me, isSuperAdmin } = useAuth();
  const toast = useToast();
  const queryClient = useQueryClient();
  const users = useQuery({ queryKey: ["users"], queryFn: () => api<User[]>("/users") });
  const allowed = isSuperAdmin ? ALL_ROLES : ALL_ROLES.filter((r) => !ADMIN_ROLES.includes(r));

  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({ full_name: "", email: "", roles: [] as Role[] });
  const [editing, setEditing] = useState<User | null>(null);
  const [edit, setEdit] = useState({ full_name: "", roles: [] as Role[], is_active: true });
  const [removing, setRemoving] = useState<User | null>(null);
  const [credential, setCredential] = useState<{ email: string; password: string; reset: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = () => queryClient.invalidateQueries({ queryKey: ["users"] });
  const fail = (err: unknown, fallback: string) => (err instanceof ApiError ? err.message : fallback);
  const canManage = (target: User) => isSuperAdmin || !target.roles.some((r) => ADMIN_ROLES.includes(r));

  async function create() {
    setBusy(true);
    setError(null);
    try {
      const res = await api<{ user: User; temporary_password: string | null }>("/users", {
        method: "POST",
        body: { full_name: form.full_name.trim(), email: form.email.trim(), roles: form.roles },
      });
      setCreating(false);
      setForm({ full_name: "", email: "", roles: [] });
      if (res.temporary_password) setCredential({ email: res.user.email, password: res.temporary_password, reset: false });
      refresh();
    } catch (err) {
      setError(fail(err, "Could not create the user."));
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    if (!editing) return;
    setBusy(true);
    setError(null);
    try {
      await api(`/users/${editing.id}`, { method: "PATCH", body: edit });
      toast("success", `${editing.email} was updated.`);
      setEditing(null);
      refresh();
    } catch (err) {
      setError(fail(err, "Could not update the user."));
    } finally {
      setBusy(false);
    }
  }

  async function resetPassword(target: User) {
    try {
      const res = await api<{ temporary_password: string }>(`/users/${target.id}/reset-password`, { method: "POST" });
      setCredential({ email: target.email, password: res.temporary_password, reset: true });
      setEditing(null);
    } catch (err) {
      toast("error", fail(err, "Could not reset the password."));
    }
  }

  async function remove() {
    if (!removing) return;
    try {
      await api(`/users/${removing.id}`, { method: "DELETE" });
      toast("success", `${removing.email} was deleted.`);
      refresh();
    } catch (err) {
      toast("error", fail(err, "Could not delete the user."));
    } finally {
      setRemoving(null);
    }
  }

  return (
    <Page>
      <PageHeader
        title="Users"
        subtitle="Create accounts and decide which services each person can use. Access is enforced by the server on every request."
        actions={
          <Button
            onClick={() => {
              setError(null);
              setCreating(true);
            }}
          >
            <UserPlus className="h-4 w-4" aria-hidden />
            Add user
          </Button>
        }
      />

      {users.isLoading ? (
        <Spinner />
      ) : (
        <div className="table-wrap">
          <table className="table" data-testid="users-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Email</th>
                <th>Roles</th>
                <th>Status</th>
                <th>Last sign-in</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {(users.data ?? []).map((u) => (
                <tr key={u.id}>
                  <td className="font-medium">
                    {u.full_name}
                    {u.id === me?.id && <span className="ml-2 text-xs font-normal text-ink-500">(you)</span>}
                  </td>
                  <td className="text-ink-600">{u.email}</td>
                  <td>
                    <div className="flex flex-wrap gap-1">
                      {u.roles.map((role) => (
                        <Tag key={role} tone={ADMIN_ROLES.includes(role) ? "blue" : "neutral"}>
                          {ROLE_LABELS[role]}
                        </Tag>
                      ))}
                    </div>
                  </td>
                  <td>
                    {!u.is_active ? <Tag tone="red">Disabled</Tag> : u.must_change_password ? <Tag tone="amber">Temporary password</Tag> : <Tag tone="green">Active</Tag>}
                  </td>
                  <td className="whitespace-nowrap text-ink-600">{formatRelative(u.last_login_at)}</td>
                  <td className="whitespace-nowrap text-right">
                    {canManage(u) && (
                      <div className="flex justify-end gap-2">
                        <Button
                          variant="secondary"
                          size="sm"
                          onClick={() => {
                            setError(null);
                            setEdit({ full_name: u.full_name, roles: u.roles, is_active: u.is_active });
                            setEditing(u);
                          }}
                        >
                          Edit
                        </Button>
                        {u.id !== me?.id && (
                          <Button variant="danger" size="sm" onClick={() => setRemoving(u)}>
                            Delete
                          </Button>
                        )}
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <Modal
        open={creating}
        title="Add user"
        subtitle="A temporary password is generated. The user must change it at first sign-in."
        onClose={() => setCreating(false)}
        footer={
          <>
            <Button variant="secondary" onClick={() => setCreating(false)}>
              Cancel
            </Button>
            <Button loading={busy} disabled={!form.full_name.trim() || !form.email.trim() || form.roles.length === 0} onClick={create}>
              Create user
            </Button>
          </>
        }
      >
        <div className="space-y-4">
          {error && <Alert>{error}</Alert>}
          <Field label="Full name">
            <input className="input" name="full_name" value={form.full_name} onChange={(e) => setForm({ ...form, full_name: e.target.value })} autoFocus />
          </Field>
          <Field label="Email">
            <input className="input" type="email" name="new_email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} placeholder="name@company.com" />
          </Field>
          <div>
            <span className="label">Roles</span>
            <RolePicker value={form.roles} onChange={(roles) => setForm({ ...form, roles })} allowed={allowed} />
            {!isSuperAdmin && <p className="hint">Only a super admin can assign administrator roles.</p>}
          </div>
        </div>
      </Modal>

      <Modal
        open={Boolean(editing)}
        title="Edit user"
        subtitle={editing?.email}
        onClose={() => setEditing(null)}
        footer={
          <>
            <Button variant="secondary" className="mr-auto" onClick={() => editing && resetPassword(editing)}>
              Reset password
            </Button>
            <Button variant="secondary" onClick={() => setEditing(null)}>
              Cancel
            </Button>
            <Button loading={busy} disabled={!edit.full_name.trim() || edit.roles.length === 0} onClick={save}>
              Save changes
            </Button>
          </>
        }
      >
        <div className="space-y-4">
          {error && <Alert>{error}</Alert>}
          <Field label="Full name">
            <input className="input" value={edit.full_name} onChange={(e) => setEdit({ ...edit, full_name: e.target.value })} />
          </Field>
          <div>
            <span className="label">Roles</span>
            <RolePicker value={edit.roles} onChange={(roles) => setEdit({ ...edit, roles })} allowed={allowed} />
          </div>
          <div className="border-t border-line pt-4">
            <Checkbox
              checked={edit.is_active}
              disabled={editing?.id === me?.id}
              onChange={(is_active) => setEdit({ ...edit, is_active })}
              label="Account is active"
              description="Disabled accounts cannot sign in and their API keys stop working immediately."
            />
          </div>
        </div>
      </Modal>

      <Modal
        open={Boolean(credential)}
        title={credential?.reset ? "Password reset" : "User created"}
        subtitle={credential?.email}
        onClose={() => setCredential(null)}
        footer={<Button onClick={() => setCredential(null)}>Done</Button>}
      >
        <p className="mb-3 text-sm text-ink-700">Share this temporary password with the user through a secure channel. It is shown only once.</p>
        <div className="flex items-center gap-2">
          <code className="min-w-0 flex-1 break-all rounded-md border border-line bg-surface px-3 py-2 font-mono text-sm" data-testid="temp-password">
            {credential?.password}
          </code>
          <CopyButton value={credential?.password ?? ""} />
        </div>
      </Modal>

      <Modal
        open={Boolean(removing)}
        title="Delete this user?"
        subtitle={removing?.email}
        onClose={() => setRemoving(null)}
        footer={
          <>
            <Button variant="secondary" onClick={() => setRemoving(null)}>
              Cancel
            </Button>
            <Button variant="danger" onClick={remove}>
              Delete user
            </Button>
          </>
        }
      >
        <p className="text-sm text-ink-700">The account and its API keys are removed. Jobs the user submitted stay in the history for audit purposes.</p>
      </Modal>
    </Page>
  );
}
