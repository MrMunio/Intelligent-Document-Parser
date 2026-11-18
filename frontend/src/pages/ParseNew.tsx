import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Alert, Button, Checkbox, FileDrop, Page, PageHeader, Section, Stepper } from "../components/ui";
import { api, ApiError } from "../lib/api";

export const PARSE_STEPS = ["Upload", "Configure", "Process", "Review"];

const MODES = [
  {
    value: "structured" as const,
    title: "Structured parsing",
    text: "Layout-aware markdown and JSON chunks: headings, paragraphs, tables and figures in reading order. Best for search, retrieval and downstream automation.",
  },
  {
    value: "ocr" as const,
    title: "Text extraction",
    text: "Plain text with line and word bounding boxes for every page. Fastest option when you only need the words and their positions.",
  },
];

export default function ParseNew() {
  const navigate = useNavigate();
  const [file, setFile] = useState<File | null>(null);
  const [mode, setMode] = useState<"structured" | "ocr">("structured");
  const [summaries, setSummaries] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function submit() {
    if (!file) return;
    setError(null);
    setSubmitting(true);
    try {
      const form = new FormData();
      form.append("file", file);
      form.append("mode", mode);
      form.append("ai_summaries", String(summaries));
      const res = await api<{ job_id: string }>("/parse/jobs", { method: "POST", form });
      navigate(`/parse/jobs/${res.job_id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "The upload failed. Check your connection and try again.");
      setSubmitting(false);
    }
  }

  return (
    <Page>
      <PageHeader title="Document Parsing" subtitle="Convert a scanned or digital document into structured, machine-readable content." />
      <Stepper steps={PARSE_STEPS} current={file ? 1 : 0} />

      <div className="space-y-5">
        <Section title="Document" subtitle="The original is deleted from the server as soon as the job finishes">
          <FileDrop file={file} onFile={setFile} hint="PDF and images (PNG, JPG, TIFF, BMP, WEBP). Up to 50 MB." accept=".pdf,.png,.jpg,.jpeg,.tif,.tiff,.bmp,.webp" />
        </Section>

        {file && (
          <>
            <Section title="Output type" subtitle="Choose the level of structure you need">
              <div className="grid gap-4 md:grid-cols-2">
                {MODES.map((option) => (
                  <button
                    key={option.value}
                    type="button"
                    onClick={() => setMode(option.value)}
                    aria-pressed={mode === option.value}
                    className={`rounded-lg border p-4 text-left transition-colors duration-150 ${
                      mode === option.value ? "border-brand-600 bg-brand-50 ring-1 ring-brand-600" : "border-line-strong bg-white hover:border-brand-400"
                    }`}
                  >
                    <p className="text-sm font-semibold text-ink-900">{option.title}</p>
                    <p className="mt-1.5 text-[13px] leading-relaxed text-ink-600">{option.text}</p>
                  </button>
                ))}
              </div>
              {mode === "structured" && (
                <div className="mt-5 border-t border-line pt-5">
                  <Checkbox
                    checked={summaries}
                    onChange={setSummaries}
                    label="Describe figures and charts"
                    description="A vision model writes a short description of each figure so the content is searchable. Adds processing time."
                  />
                </div>
              )}
            </Section>
            {error && <Alert>{error}</Alert>}
            <div className="flex justify-end pb-4">
              <Button onClick={submit} loading={submitting}>
                Start parsing
              </Button>
            </div>
          </>
        )}
      </div>
    </Page>
  );
}
