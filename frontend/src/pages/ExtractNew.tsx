import { useQuery } from "@tanstack/react-query";
import { CheckCircle2, CircleAlert } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { builderToSpec, countFields, newField, SpecBuilder, specToBuilder, type BuilderSpec } from "../components/SpecBuilder";
import { Alert, Button, Field, FileDrop, Page, PageHeader, Section, Segmented, Spinner, Stepper, Tag, cx } from "../components/ui";
import { api, ApiError } from "../lib/api";
import type { Approach, ExtractModel, ExtractOptions, SpecIssue, SpecReport } from "../lib/types";

export const EXTRACT_STEPS = ["Upload", "Define output", "Process", "Review"];
const SPEC_STORAGE_KEY = "idp.extract.spec";

const APPROACHES: { value: Approach; title: string; text: string; tag?: string }[] = [
  {
    value: "agentic",
    title: "Agentic retrieval",
    tag: "Recommended",
    text: "The document is mapped (table of contents, sections, page offsets), chunked by document type and indexed with local embeddings. A reasoning agent navigates it with retrieval tools and compacts its context when it grows, so very long documents are handled efficiently.",
  },
  {
    value: "batch",
    title: "Document batch processing",
    text: "The document is read in order, in parts sized to the model context. The JSON found so far is carried from one part to the next and updated. Simple and thorough, but every page is sent to the model.",
  },
];

function readStoredSpec(): string | null {
  try {
    return window.localStorage.getItem(SPEC_STORAGE_KEY);
  } catch {
    return null;
  }
}

function storeSpec(text: string) {
  try {
    window.localStorage.setItem(SPEC_STORAGE_KEY, text);
  } catch {
    /* storage unavailable: nothing to remember */
  }
}

function modelLabel(m: ExtractModel) {
  const window = m.context_window >= 1_000_000 ? `${(m.context_window / 1_000_000).toFixed(m.context_window % 1_000_000 ? 2 : 0)}M` : `${Math.round(m.context_window / 1000)}K`;
  const price = m.pricing.input ? `$${m.pricing.input} / $${m.pricing.output} per 1M tokens` : "local";
  return `${m.display_name}  (${window} context, ${price})${m.default ? "  [default]" : ""}`;
}

function SpecStatus({ report, checking, parseError }: { report: SpecReport | null; checking: boolean; parseError: string | null }) {
  if (parseError) {
    return (
      <div className="flex items-start gap-2 text-[13px] text-red-700">
        <CircleAlert className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
        <span>{parseError}</span>
      </div>
    );
  }
  if (checking && !report) return <Spinner label="Checking the spec" />;
  if (!report) return <p className="text-[13px] text-ink-500">Paste or write a spec to check it.</p>;
  if (!report.valid) {
    return (
      <div>
        <div className="mb-2 flex items-center gap-2 text-[13px] font-medium text-red-700">
          <CircleAlert className="h-4 w-4" aria-hidden />
          {report.errors.length} problem{report.errors.length === 1 ? "" : "s"} to fix before submitting
        </div>
        <ul className="space-y-1.5" data-testid="spec-errors">
          {report.errors.map((issue: SpecIssue, i: number) => (
            <li key={i} className="rounded-md bg-red-50 px-3 py-2 text-[12.5px] text-red-800">
              <span className="font-mono">{issue.path}</span>: {issue.message}
            </li>
          ))}
        </ul>
      </div>
    );
  }
  return (
    <div>
      <div className="mb-2 flex items-center gap-2 text-[13px] font-medium text-emerald-700" data-testid="spec-valid">
        <CheckCircle2 className="h-4 w-4" aria-hidden />
        Valid spec with {report.field_count} field{report.field_count === 1 ? "" : "s"}
      </div>
      <div className="max-h-[300px] overflow-auto rounded-md border border-line">
        <table className="table text-[12.5px]">
          <thead>
            <tr>
              <th>Field</th>
              <th>Type</th>
              <th>Description</th>
            </tr>
          </thead>
          <tbody>
            {report.fields.map((f) => (
              <tr key={f.path}>
                <td className="break-all font-mono">{f.path}</td>
                <td>{f.items ?? (f.format === "date" ? "date" : f.format ? `${f.type} (${f.format})` : f.type)}</td>
                <td className="text-ink-600">{f.description}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {report.warnings.length > 0 && <p className="mt-2 text-xs text-amber-700">{report.warnings.join("; ")}</p>}
    </div>
  );
}

export default function ExtractNew() {
  const navigate = useNavigate();
  const options = useQuery({ queryKey: ["extract-options"], queryFn: () => api<ExtractOptions>("/extract/options") });
  const [file, setFile] = useState<File | null>(null);
  const [specText, setSpecText] = useState<string>(() => readStoredSpec() ?? "");
  const [report, setReport] = useState<SpecReport | null>(null);
  const [parseError, setParseError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const [approach, setApproach] = useState<Approach>("agentic");
  const [model, setModel] = useState<string>("");
  const [instructions, setInstructions] = useState("");
  const [windowCap, setWindowCap] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const seq = useRef(0);

  // Two views of one spec: the form builder (default) and the JSON editor. specText is the source of truth.
  const [mode, setMode] = useState<"builder" | "json">("builder");
  const [builder, setBuilder] = useState<BuilderSpec>(() => specToBuilder(readStoredSpec() ?? "") ?? { title: "", description: "", fields: [] });
  const [modeError, setModeError] = useState<string | null>(null);

  function loadSpec(spec: unknown) {
    const text = JSON.stringify(spec, null, 2);
    setSpecText(text);
    setBuilder(specToBuilder(text) ?? { title: "", description: "", fields: [] });
    setModeError(null);
  }

  function editBuilder(next: BuilderSpec) {
    setBuilder(next);
    setSpecText(JSON.stringify(builderToSpec(next), null, 2));
  }

  function switchMode(next: "builder" | "json") {
    if (next === "builder") {
      const parsed = specToBuilder(specText || "{}");
      if (!parsed) {
        setModeError("The JSON cannot be opened in the form builder until it is valid JSON. Fix it here first.");
        return;
      }
      setBuilder(parsed);
    }
    setModeError(null);
    setMode(next);
  }

  // Start from the example spec the first time.
  useEffect(() => {
    if (!specText && options.data) loadSpec(options.data.example_spec);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [options.data, specText]);

  // Validate on the server as the spec changes (debounced), so the rules are exactly the API's.
  useEffect(() => {
    if (!specText.trim()) {
      setReport(null);
      setParseError(null);
      return;
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(specText);
      setParseError(null);
    } catch (err) {
      setReport(null);
      setParseError(`Not valid JSON: ${err instanceof Error ? err.message : "syntax error"}`);
      return;
    }
    const current = ++seq.current;
    setChecking(true);
    const timer = window.setTimeout(async () => {
      try {
        const res = await api<SpecReport>("/extract/validate-spec", { method: "POST", body: { spec: parsed } });
        if (current === seq.current) setReport(res);
      } catch (err) {
        if (current === seq.current) setParseError(err instanceof ApiError ? err.message : "The spec could not be checked.");
      } finally {
        if (current === seq.current) setChecking(false);
      }
    }, 450);
    storeSpec(specText);
    return () => window.clearTimeout(timer);
  }, [specText]);

  const groups = useMemo(() => {
    const out: Record<string, ExtractModel[]> = {};
    for (const m of options.data?.models ?? []) (out[m.provider_name] ??= []).push(m);
    return out;
  }, [options.data]);

  const ready = Boolean(file && report?.valid && !parseError);

  async function submit() {
    if (!file || !ready) return;
    setError(null);
    setSubmitting(true);
    try {
      const form = new FormData();
      form.append("file", file);
      form.append("spec", specText);
      form.append("approach", approach);
      if (model) form.append("model", model);
      if (instructions.trim()) form.append("instructions", instructions.trim());
      if (windowCap.trim()) form.append("context_window", windowCap.trim());
      const res = await api<{ job_id: string }>("/extract/jobs", { method: "POST", form });
      navigate(`/extract/jobs/${res.job_id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "The upload failed. Check your connection and try again.");
      setSubmitting(false);
    }
  }

  const step = !file ? 0 : 1;
  return (
    <Page>
      <PageHeader title="Data Extraction" subtitle="Turn a document of any length into JSON that follows your own spec." />
      <Stepper steps={EXTRACT_STEPS} current={step} />

      <div className="space-y-5">
        <Section title="Document" subtitle="The original is deleted from the server as soon as the job finishes">
          <FileDrop
            file={file}
            onFile={setFile}
            hint={`PDF, DOCX, images and text files. Up to ${options.data?.limits.max_upload_mb ?? 50} MB.`}
            accept=".pdf,.docx,.png,.jpg,.jpeg,.tif,.tiff,.bmp,.webp,.txt,.md,.csv,.tsv,.json,.xml,.html,.htm,.yaml,.yml,.log"
          />
        </Section>

        <Section
          title="Output fields"
          subtitle="Define the fields to extract: a name, a type and a description of each. Build them with the form or write the JSON spec directly."
          actions={
            <div className="flex flex-wrap items-center gap-2">
              <Segmented
                value={mode}
                onChange={switchMode}
                options={[
                  { value: "builder", label: "Form builder" },
                  { value: "json", label: "JSON" },
                ]}
              />
              <Button variant="secondary" size="sm" onClick={() => options.data && loadSpec(options.data.example_spec)}>
                Load example
              </Button>
              {mode === "builder" ? (
                <Button variant="secondary" size="sm" onClick={() => editBuilder({ title: "", description: "", fields: [newField()] })} data-testid="builder-clear">
                  Start empty
                </Button>
              ) : (
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => {
                    try {
                      setSpecText(JSON.stringify(JSON.parse(specText), null, 2));
                    } catch {
                      /* the status panel already shows the syntax error */
                    }
                  }}
                >
                  Format
                </Button>
              )}
            </div>
          }
        >
          {modeError && (
            <div className="mb-4">
              <Alert tone="warning">{modeError}</Alert>
            </div>
          )}
          <div className="grid gap-5 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
            <div className="min-w-0">
              {mode === "builder" ? (
                <>
                  <SpecBuilder spec={builder} onChange={editBuilder} maxDepth={options.data?.limits.max_depth ?? 6} />
                  <p className="mt-3 text-xs text-ink-500">
                    {countFields(builder.fields)} field{countFields(builder.fields) === 1 ? "" : "s"}. Use "Group of fields" to nest fields, and "List" for
                    values that repeat (choose "Group of fields" as the item type for a list of records, such as line items).
                  </p>
                </>
              ) : (
                <>
                  <textarea
                    className="textarea h-[420px] font-mono text-[12.5px] leading-relaxed"
                    spellCheck={false}
                    value={specText}
                    onChange={(e) => setSpecText(e.target.value)}
                    aria-label="Extraction spec JSON"
                    data-testid="spec-editor"
                  />
                  <p className="mt-2 text-xs text-ink-500">
                    Types: {(options.data?.supported_types ?? []).join(", ")}. Every field needs a description. Objects need properties, arrays need
                    items. {options.data ? `${options.data.limits.min_fields} to ${options.data.limits.max_fields} fields, nesting up to ${options.data.limits.max_depth} levels.` : ""}
                  </p>
                </>
              )}
            </div>
            <div className="min-w-0 lg:sticky lg:top-4 lg:self-start">
              <SpecStatus report={report} checking={checking} parseError={parseError} />
            </div>
          </div>
        </Section>

        <Section title="Approach" subtitle="How the document is read">
          <div className="grid gap-4 md:grid-cols-2">
            {APPROACHES.map((option) => (
              <button
                key={option.value}
                type="button"
                onClick={() => setApproach(option.value)}
                aria-pressed={approach === option.value}
                className={cx(
                  "rounded-lg border p-4 text-left transition-colors duration-150",
                  approach === option.value ? "border-brand-600 bg-brand-50 ring-1 ring-brand-600" : "border-line-strong bg-white hover:border-brand-400",
                )}
              >
                <div className="flex items-center gap-2">
                  <p className="text-sm font-semibold text-ink-900">{option.title}</p>
                  {option.tag && <Tag tone="blue">{option.tag}</Tag>}
                </div>
                <p className="mt-1.5 text-[13px] leading-relaxed text-ink-600">{option.text}</p>
              </button>
            ))}
          </div>
        </Section>

        <Section title="Engine" subtitle="Reasoning model and limits for this request">
          <div className="grid gap-5 md:grid-cols-2">
            <Field label="Model" hint="Prices are per million input and output tokens. The configured engine is used when none is chosen.">
              <select className="input" value={model} onChange={(e) => setModel(e.target.value)} aria-label="Model">
                <option value="">Configured engine ({options.data?.default_model ?? "loading"})</option>
                {Object.entries(groups).map(([provider, models]) => (
                  <optgroup key={provider} label={provider}>
                    {models.map((m) => (
                      <option key={m.key} value={m.key}>
                        {modelLabel(m)}
                      </option>
                    ))}
                  </optgroup>
                ))}
              </select>
            </Field>
            <Field
              label="Context window cap (optional)"
              hint={`Tokens the engine may use per request, at least 16000. Agent sessions are compacted at ${Math.round((options.data?.context_threshold ?? 0.7) * 100)}% of the usable window.`}
            >
              <input className="input" inputMode="numeric" placeholder={options.data?.context_cap_tokens ? `${options.data.context_cap_tokens}` : "model window"} value={windowCap} onChange={(e) => setWindowCap(e.target.value.replace(/[^0-9]/g, ""))} />
            </Field>
            <div className="md:col-span-2">
              <Field label="Instructions (optional)" hint="Extra guidance, for example how to treat amendments or which party is the provider.">
                <textarea className="textarea h-20" maxLength={4000} value={instructions} onChange={(e) => setInstructions(e.target.value)} />
              </Field>
            </div>
          </div>
        </Section>

        {error && <Alert>{error}</Alert>}
        <div className="flex items-center justify-end gap-3 pb-4">
          {!ready && <p className="text-[13px] text-ink-500">{!file ? "Choose a document to continue." : "Fix the spec to continue."}</p>}
          <Button onClick={submit} loading={submitting} disabled={!ready}>
            Start extraction
          </Button>
        </div>
      </div>
    </Page>
  );
}
