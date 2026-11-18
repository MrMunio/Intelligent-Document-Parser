import { motion } from "framer-motion";
import { useState, type FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { Alert, Button, Field } from "../components/ui";
import { ApiError } from "../lib/api";
import { useAuth } from "../lib/auth";

const POINTS = [
  { title: "PHI redaction", text: "Remove patient and personal identifiers from PDF, Word, image and text files, in the original format." },
  { title: "Document parsing", text: "Turn scanned and digital documents into structured markdown and JSON for downstream systems." },
  { title: "Governed access", text: "Role-based access, API keys for integrations, and a full audit trail of every action." },
];

export default function Login() {
  const { user, login, loading } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (!loading && user) return <Navigate to="/" replace />;

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await login(email.trim(), password);
      const from = (location.state as { from?: string } | null)?.from;
      navigate(from && from !== "/login" ? from : "/", { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reach the server. Check your connection and try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid min-h-screen lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
      <div className="flex items-center justify-center px-6 py-12">
        <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.35 }} className="w-full max-w-sm">
          <div className="mb-10 flex items-center gap-2.5">
            <span className="flex h-8 w-8 items-center justify-center rounded-md bg-brand-600 text-xs font-bold text-white">ID</span>
            <span className="text-base font-semibold text-ink-900">IDP Platform</span>
          </div>
          <h1 className="text-2xl font-semibold tracking-tight text-ink-900">Sign in</h1>
          <p className="mt-1 text-sm text-ink-600">Use the email and password issued by your administrator.</p>

          <form onSubmit={submit} className="mt-8 space-y-4" noValidate>
            {error && <Alert>{error}</Alert>}
            <Field label="Email">
              <input
                className="input"
                type="email"
                name="email"
                autoComplete="username"
                required
                autoFocus
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="name@company.com"
              />
            </Field>
            <Field label="Password">
              <input
                className="input"
                type="password"
                name="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </Field>
            <Button type="submit" className="w-full" loading={busy} disabled={!email || !password}>
              Sign in
            </Button>
          </form>
          <p className="mt-8 text-xs text-ink-500">
            Accounts are created by an administrator. Contact yours if you need access or a password reset.
          </p>
        </motion.div>
      </div>

      <div className="hidden bg-brand-600 lg:flex lg:items-center lg:justify-center lg:px-14">
        <div className="max-w-md text-white">
          <motion.h2
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.4, delay: 0.1 }}
            className="text-3xl font-semibold leading-tight tracking-tight"
          >
            Intelligent document processing for regulated data
          </motion.h2>
          <ul className="mt-10 space-y-6">
            {POINTS.map((point, i) => (
              <motion.li
                key={point.title}
                initial={{ opacity: 0, x: 12 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ duration: 0.35, delay: 0.2 + i * 0.1 }}
                className="border-l-2 border-white/40 pl-4"
              >
                <p className="text-sm font-semibold">{point.title}</p>
                <p className="mt-1 text-sm leading-relaxed text-white/80">{point.text}</p>
              </motion.li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}
