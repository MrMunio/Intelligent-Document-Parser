import { lazy } from "react";
import { Navigate, Outlet, Route, Routes, useLocation } from "react-router-dom";
import Layout from "./components/Layout";
import { Spinner } from "./components/ui";
import { useAuth } from "./lib/auth";
import type { Service } from "./lib/types";
import Account from "./pages/Account";
import AdminAudit from "./pages/AdminAudit";
import AdminSystem from "./pages/AdminSystem";
import AdminUsers from "./pages/AdminUsers";
import ApiAccess from "./pages/ApiAccess";
import Jobs from "./pages/Jobs";
import Login from "./pages/Login";
import NotAllowed from "./pages/NotAllowed";
import Overview from "./pages/Overview";
import ParseNew from "./pages/ParseNew";
import PhiNew from "./pages/PhiNew";

// The review screens carry the PDF, Word and markdown renderers, so they load on first use.
const PhiJob = lazy(() => import("./pages/PhiJob"));
const ParseJob = lazy(() => import("./pages/ParseJob"));

function RequireAuth() {
  const { user, loading } = useAuth();
  const location = useLocation();
  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner label="Restoring your session" />
      </div>
    );
  }
  if (!user) return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  // A temporary password must be replaced before anything else is available.
  if (user.must_change_password && location.pathname !== "/account") return <Navigate to="/account" replace />;
  return <Outlet />;
}

// The server enforces access on every request. These guards only keep the navigation honest.
function RequireService({ service }: { service: Service }) {
  const { can } = useAuth();
  return can(service) ? <Outlet /> : <NotAllowed />;
}

function RequireAdmin() {
  const { isAdmin } = useAuth();
  return isAdmin ? <Outlet /> : <NotAllowed />;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<RequireAuth />}>
        <Route element={<Layout />}>
          <Route index element={<Overview />} />
          <Route element={<RequireService service="phi" />}>
            <Route path="phi" element={<PhiNew />} />
            <Route path="phi/jobs/:jobId" element={<PhiJob />} />
          </Route>
          <Route element={<RequireService service="parse" />}>
            <Route path="parse" element={<ParseNew />} />
            <Route path="parse/jobs/:jobId" element={<ParseJob />} />
          </Route>
          <Route path="jobs" element={<Jobs />} />
          <Route path="api-access" element={<ApiAccess />} />
          <Route path="account" element={<Account />} />
          <Route element={<RequireAdmin />}>
            <Route path="admin/users" element={<AdminUsers />} />
            <Route path="admin/system" element={<AdminSystem />} />
            <Route path="admin/audit" element={<AdminAudit />} />
          </Route>
          <Route path="*" element={<NotAllowed notFound />} />
        </Route>
      </Route>
    </Routes>
  );
}
