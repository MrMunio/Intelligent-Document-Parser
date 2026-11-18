import { Activity, Braces, Cpu, FileText, History, KeyRound, LayoutGrid, LogOut, ScrollText, ShieldCheck, UserCog, Users } from "lucide-react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { Suspense, type ComponentType } from "react";
import { useAuth } from "../lib/auth";
import { ROLE_LABELS } from "../lib/types";
import { cx, Spinner } from "./ui";

type NavItem = { to: string; label: string; icon: ComponentType<{ className?: string }>; end?: boolean };

function NavGroup({ title, items }: { title: string; items: NavItem[] }) {
  if (!items.length) return null;
  return (
    <div className="mb-5">
      <p className="mb-1.5 px-3 text-[11px] font-semibold uppercase tracking-wider text-ink-400">{title}</p>
      <ul className="space-y-0.5">
        {items.map(({ to, label, icon: Icon, end }) => (
          <li key={to}>
            <NavLink
              to={to}
              end={end}
              className={({ isActive }) =>
                cx(
                  "group flex items-center gap-2.5 rounded-md px-3 py-2 text-sm transition-colors duration-150",
                  isActive ? "bg-brand-50 font-medium text-brand-700" : "text-ink-700 hover:bg-surface-strong hover:text-ink-900",
                )
              }
            >
              <Icon className="h-4 w-4 shrink-0" aria-hidden />
              {label}
            </NavLink>
          </li>
        ))}
      </ul>
    </div>
  );
}

export default function Layout() {
  const { user, logout, can, isAdmin } = useAuth();
  const navigate = useNavigate();
  if (!user) return null;

  const workspace: NavItem[] = [{ to: "/", label: "Overview", icon: LayoutGrid, end: true }];
  if (can("phi")) workspace.push({ to: "/phi", label: "PHI Redaction", icon: ShieldCheck });
  if (can("parse")) workspace.push({ to: "/parse", label: "Document Parsing", icon: FileText });
  if (can("extract")) workspace.push({ to: "/extract", label: "Data Extraction", icon: Braces });
  workspace.push({ to: "/jobs", label: "Job History", icon: History });

  const integrate: NavItem[] = [{ to: "/api-access", label: "API Access", icon: KeyRound }];
  const admin: NavItem[] = isAdmin
    ? [
        { to: "/admin/users", label: "Users", icon: Users },
        { to: "/admin/system", label: "System Status", icon: Activity },
        { to: "/admin/engines", label: "AI Engines", icon: Cpu },
        { to: "/admin/audit", label: "Audit Log", icon: ScrollText },
      ]
    : [];

  return (
    <div className="flex min-h-screen bg-white">
      <aside className="sticky top-0 flex h-screen w-60 shrink-0 flex-col border-r border-line bg-white">
        <div className="flex h-16 items-center gap-2.5 border-b border-line px-5">
          <span className="flex h-7 w-7 items-center justify-center rounded-md bg-brand-600 text-xs font-bold text-white">ID</span>
          <div className="leading-tight">
            <p className="text-sm font-semibold text-ink-900">IDP Platform</p>
            <p className="text-[11px] text-ink-500">Document intelligence</p>
          </div>
        </div>
        <nav className="flex-1 overflow-y-auto px-3 py-4" aria-label="Main">
          <NavGroup title="Workspace" items={workspace} />
          <NavGroup title="Integrate" items={integrate} />
          <NavGroup title="Administration" items={admin} />
        </nav>
        <div className="border-t border-line p-3">
          <NavLink
            to="/account"
            className={({ isActive }) =>
              cx("flex items-center gap-2.5 rounded-md px-3 py-2 transition-colors duration-150", isActive ? "bg-brand-50" : "hover:bg-surface-strong")
            }
          >
            <UserCog className="h-4 w-4 shrink-0 text-ink-600" aria-hidden />
            <span className="min-w-0 leading-tight">
              <span className="block truncate text-[13px] font-medium text-ink-900">{user.full_name || user.email}</span>
              <span className="block truncate text-[11px] text-ink-500">{user.roles.map((r) => ROLE_LABELS[r]).join(", ")}</span>
            </span>
          </NavLink>
          <button
            type="button"
            onClick={async () => {
              await logout();
              navigate("/login");
            }}
            className="mt-1 flex w-full items-center gap-2.5 rounded-md px-3 py-2 text-[13px] text-ink-700 transition-colors duration-150 hover:bg-surface-strong"
          >
            <LogOut className="h-4 w-4" aria-hidden />
            Sign out
          </button>
        </div>
      </aside>
      <main className="min-w-0 flex-1">
        <Suspense fallback={<Spinner />}>
          <Outlet />
        </Suspense>
      </main>
    </div>
  );
}
