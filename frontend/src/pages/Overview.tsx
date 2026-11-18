import { motion } from "framer-motion";
import { ArrowRight } from "lucide-react";
import { Link } from "react-router-dom";
import { JobsTable } from "../components/JobsTable";
import { EmptyState, Page, PageHeader, Section, Spinner } from "../components/ui";
import { useAuth } from "../lib/auth";
import { useJobs } from "../lib/hooks";

const SERVICES = [
  {
    service: "phi" as const,
    to: "/phi",
    title: "PHI Redaction",
    text: "Detect and remove protected health information and your own custom identifiers. The redacted file keeps the format of the original, with a metadata report of every detection.",
    formats: "PDF, DOCX, images, TXT, MD and other text files",
    action: "Redact a document",
  },
  {
    service: "parse" as const,
    to: "/parse",
    title: "Document Parsing",
    text: "Convert scanned or digital documents into layout-aware markdown and structured JSON: headings, tables, figures and reading order.",
    formats: "PDF and images",
    action: "Parse a document",
  },
];

export default function Overview() {
  const { user, can } = useAuth();
  const jobs = useJobs({ page_size: 6 });
  const available = SERVICES.filter((s) => can(s.service));
  const firstName = (user?.full_name || "").split(" ")[0];

  return (
    <Page>
      <PageHeader
        title={firstName ? `Welcome back, ${firstName}` : "Overview"}
        subtitle="Start a new job or pick up where you left off. Jobs keep running if you leave the page."
      />

      <div className="grid gap-5 md:grid-cols-2">
        {available.map((item, i) => (
          <motion.div
            key={item.service}
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.3, delay: 0.05 * i }}
            whileHover={{ y: -2 }}
            className="card flex flex-col p-6 transition-shadow duration-200 hover:shadow-pop"
          >
            <h2 className="text-base font-semibold text-ink-900">{item.title}</h2>
            <p className="mt-2 flex-1 text-sm leading-relaxed text-ink-600">{item.text}</p>
            <p className="mt-4 text-xs text-ink-500">Accepts: {item.formats}</p>
            <Link to={item.to} className="btn-primary mt-5 self-start">
              {item.action}
              <ArrowRight className="h-4 w-4" aria-hidden />
            </Link>
          </motion.div>
        ))}
        {available.length === 0 && (
          <div className="card md:col-span-2">
            <EmptyState title="No services are assigned to your account" description="Ask an administrator to assign you a role." />
          </div>
        )}
      </div>

      <div className="mt-8">
        <Section
          title="Recent jobs"
          subtitle="Your latest submissions from the web app and the API"
          actions={
            <Link to="/jobs" className="link text-[13px]">
              View all
            </Link>
          }
          className="[&>div:last-child]:p-0"
        >
          {jobs.isLoading ? (
            <Spinner />
          ) : jobs.data && jobs.data.items.length ? (
            <JobsTable jobs={jobs.data.items} bare />
          ) : (
            <EmptyState title="No jobs yet" description="Jobs you submit will appear here with their status and results." />
          )}
        </Section>
      </div>
    </Page>
  );
}
