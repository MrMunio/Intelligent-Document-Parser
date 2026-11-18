import { useQuery } from "@tanstack/react-query";
import { AnimatePresence, motion } from "framer-motion";
import { Plus, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Alert, Button, Checkbox, Field, FileDrop, Page, PageHeader, Section, Segmented, Spinner, Stepper } from "../components/ui";
import { api, ApiError } from "../lib/api";
import { entityLabel, rememberOriginal } from "../lib/format";
import type { CustomEntity, EntityType, PhiCatalog } from "../lib/types";

export const PHI_STEPS = ["Upload", "Configure", "Process", "Review"];
const PREFS_KEY = "idp.phi.preferences.v1";

interface Preferences {
  entityTypes: string[] | null;
  custom: CustomEntity[];
  instructions: string;
  style: "label" | "blackout";
  ocrMode: "auto" | "always" | "never";
  patterns: boolean;
  scrub: boolean;
}

const DEFAULTS: Preferences = { entityTypes: null, custom: [], instructions: "", style: "label", ocrMode: "auto", patterns: true, scrub: true };

function loadPreferences(): Preferences {
  try {
    const raw = localStorage.getItem(PREFS_KEY);
    return raw ? { ...DEFAULTS, ...JSON.parse(raw) } : DEFAULTS;
  } catch {
    return DEFAULTS;
  }
}

function normalizedLabel(label: string): string {
  return label
    .trim()
    .replace(/[^A-Za-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .toUpperCase();
}

export default function PhiNew() {
  const navigate = useNavigate();
  const catalog = useQuery({ queryKey: ["phi-catalog"], queryFn: () => api<PhiCatalog>("/phi/entity-types"), staleTime: 600_000 });
  const [file, setFile] = useState<File | null>(null);
  const [prefs, setPrefs] = useState<Preferences>(loadPreferences);
  const [advanced, setAdvanced] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const allLabels = useMemo(() => catalog.data?.entity_types.map((e) => e.label) ?? [], [catalog.data]);
  const selected = useMemo(() => new Set(prefs.entityTypes ?? allLabels), [prefs.entityTypes, allLabels]);
  const groups = useMemo(() => {
    const out = new Map<string, EntityType[]>();
    for (const entity of catalog.data?.entity_types ?? []) {
      out.set(entity.group, [...(out.get(entity.group) ?? []), entity]);
    }
    return [...out.entries()];
  }, [catalog.data]);

  useEffect(() => {
    try {
      localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
    } catch {
      /* storage unavailable */
    }
  }, [prefs]);

  const update = (patch: Partial<Preferences>) => setPrefs((p) => ({ ...p, ...patch }));
  const toggleType = (label: string, on: boolean) => {
    const next = new Set(selected);
    if (on) next.add(label);
    else next.delete(label);
    update({ entityTypes: allLabels.filter((l) => next.has(l)) });
  };
  const setCustom = (index: number, patch: Partial<CustomEntity>) =>
    update({ custom: prefs.custom.map((c, i) => (i === index ? { ...c, ...patch } : c)) });

  const customValid = prefs.custom.filter((c) => c.label.trim() || c.description.trim());
  const customProblem = customValid.find((c) => !normalizedLabel(c.label) || !c.description.trim());
  const nothingSelected = selected.size === 0 && customValid.length === 0;
  const isPdf = file ? file.name.toLowerCase().endsWith(".pdf") : false;
  const step = file ? 1 : 0;

  async function submit() {
    if (!file) return;
    setError(null);
    setSubmitting(true);
    try {
      const form = new FormData();
      form.append("file", file);
      form.append("entity_types", JSON.stringify(allLabels.filter((l) => selected.has(l))));
      form.append("custom_entities", JSON.stringify(customValid.map((c) => ({ label: normalizedLabel(c.label), description: c.description.trim() }))));
      if (prefs.instructions.trim()) form.append("instructions", prefs.instructions.trim());
      form.append("redaction_style", prefs.style);
      form.append("ocr_mode", prefs.ocrMode);
      form.append("use_pattern_detectors", String(prefs.patterns));
      form.append("scrub_metadata", String(prefs.scrub));
      const res = await api<{ job_id: string }>("/phi/jobs", { method: "POST", form });
      rememberOriginal(res.job_id, file);
      navigate(`/phi/jobs/${res.job_id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "The upload failed. Check your connection and try again.");
      setSubmitting(false);
    }
  }

  return (
    <Page>
      <PageHeader
        title="PHI Redaction"
        subtitle="Upload a document, choose what counts as sensitive for this request, and get back a redacted file in the same format with a full detection report."
      />
      <Stepper steps={PHI_STEPS} current={step} />

      <div className="space-y-5">
        <Section title="Document" subtitle="The original is processed in memory and deleted from the server as soon as the job finishes">
          <FileDrop
            file={file}
            onFile={(f) => {
              setFile(f);
              setError(null);
            }}
            hint={`${catalog.data?.accepted_files ?? "PDF, DOCX, images and text files"}. Up to ${catalog.data?.max_upload_mb ?? 50} MB.`}
          />
        </Section>

        <AnimatePresence initial={false}>
          {file && (
            <motion.div
              key="configure"
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: "auto" }}
              exit={{ opacity: 0, height: 0 }}
              transition={{ duration: 0.25 }}
              className="space-y-5 overflow-hidden"
            >
              <Section
                title="Built-in entity types"
                subtitle="Standard HIPAA identifiers detected out of the box"
                actions={
                  <div className="flex gap-3 text-[13px]">
                    <button type="button" className="link" onClick={() => update({ entityTypes: null })}>
                      Select all
                    </button>
                    <button type="button" className="link" onClick={() => update({ entityTypes: [] })}>
                      Clear
                    </button>
                  </div>
                }
              >
                {catalog.isLoading ? (
                  <Spinner />
                ) : (
                  <div className="grid gap-x-8 gap-y-6 md:grid-cols-2 xl:grid-cols-4">
                    {groups.map(([group, items]) => (
                      <div key={group}>
                        <p className="mb-2.5 text-xs font-semibold uppercase tracking-wide text-ink-500">{group}</p>
                        <div className="space-y-2.5">
                          {items.map((entity) => (
                            <Checkbox
                              key={entity.label}
                              checked={selected.has(entity.label)}
                              onChange={(on) => toggleType(entity.label, on)}
                              label={entityLabel(entity.label)}
                              description={entity.description}
                            />
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </Section>

              <Section
                title="Custom entities"
                subtitle="Describe anything else that should be redacted in this request, in plain language"
                actions={
                  <Button
                    variant="secondary"
                    size="sm"
                    disabled={prefs.custom.length >= (catalog.data?.max_custom_entities ?? 25)}
                    onClick={() => update({ custom: [...prefs.custom, { label: "", description: "" }] })}
                  >
                    <Plus className="h-3.5 w-3.5" aria-hidden />
                    Add custom entity
                  </Button>
                }
              >
                {prefs.custom.length === 0 ? (
                  <p className="text-sm text-ink-600">
                    None defined. For example: label <span className="font-mono text-[13px]">EMPLOYEE_ID</span>, description{" "}
                    <span className="italic">"Internal employee numbers in the form EMP-12345"</span>.
                  </p>
                ) : (
                  <div className="space-y-3">
                    <AnimatePresence initial={false}>
                      {prefs.custom.map((entity, index) => (
                        <motion.div
                          key={index}
                          initial={{ opacity: 0, y: -6 }}
                          animate={{ opacity: 1, y: 0 }}
                          exit={{ opacity: 0 }}
                          className="grid gap-3 md:grid-cols-[220px_minmax(0,1fr)_auto]"
                        >
                          <div>
                            <input
                              className="input font-mono text-[13px]"
                              placeholder="LABEL"
                              aria-label={`Custom entity ${index + 1} label`}
                              value={entity.label}
                              maxLength={40}
                              onChange={(e) => setCustom(index, { label: e.target.value })}
                              onBlur={() => setCustom(index, { label: normalizedLabel(entity.label) })}
                            />
                          </div>
                          <input
                            className="input"
                            placeholder="What should be detected? Describe it the way you would to a colleague."
                            aria-label={`Custom entity ${index + 1} description`}
                            value={entity.description}
                            maxLength={600}
                            onChange={(e) => setCustom(index, { description: e.target.value })}
                          />
                          <Button
                            variant="ghost"
                            aria-label={`Remove custom entity ${index + 1}`}
                            onClick={() => update({ custom: prefs.custom.filter((_, i) => i !== index) })}
                          >
                            <Trash2 className="h-4 w-4" aria-hidden />
                          </Button>
                        </motion.div>
                      ))}
                    </AnimatePresence>
                    <p className="hint">Labels become the placeholder names in the output, for example [EMPLOYEE_ID_1].</p>
                  </div>
                )}
              </Section>

              <Section title="Output" subtitle="How redactions appear in the document">
                <div className="flex flex-wrap items-center gap-x-10 gap-y-4">
                  <div>
                    <span className="label">Redaction style</span>
                    <Segmented
                      value={prefs.style}
                      onChange={(style) => update({ style })}
                      options={[
                        { value: "label", label: "Labelled placeholders" },
                        { value: "blackout", label: "Blackout" },
                      ]}
                    />
                  </div>
                  <p className="max-w-md text-[13px] text-ink-600">
                    {prefs.style === "label"
                      ? "Each value is replaced with a consistent placeholder such as [PATIENT_NAME_1], so the document stays readable."
                      : "Each value is masked with a solid block and no label."}
                  </p>
                </div>

                <button type="button" className="link mt-5 text-[13px]" onClick={() => setAdvanced((v) => !v)}>
                  {advanced ? "Hide advanced options" : "Show advanced options"}
                </button>
                <AnimatePresence initial={false}>
                  {advanced && (
                    <motion.div
                      initial={{ opacity: 0, height: 0 }}
                      animate={{ opacity: 1, height: "auto" }}
                      exit={{ opacity: 0, height: 0 }}
                      className="overflow-hidden"
                    >
                      <div className="mt-4 grid gap-5 border-t border-line pt-5 md:grid-cols-2">
                        <Field label="Additional guidance" hint="Optional. Extra context for the detector, for example which names are staff and which are patients.">
                          <textarea
                            className="textarea"
                            rows={4}
                            maxLength={2000}
                            value={prefs.instructions}
                            onChange={(e) => update({ instructions: e.target.value })}
                          />
                        </Field>
                        <div className="space-y-4">
                          <Checkbox
                            checked={prefs.patterns}
                            onChange={(patterns) => update({ patterns })}
                            label="Pattern detectors"
                            description="Also match emails, phone numbers, SSNs, dates, URLs and IP addresses with deterministic rules."
                          />
                          <Checkbox
                            checked={prefs.scrub}
                            onChange={(scrub) => update({ scrub })}
                            label="Remove document metadata"
                            description="Clear author, title and similar properties from the output file."
                          />
                          {isPdf && (
                            <Field label="Text recognition for PDF" hint="Automatic reads the text layer when one exists and runs OCR only on scanned pages.">
                              <select className="input" value={prefs.ocrMode} onChange={(e) => update({ ocrMode: e.target.value as Preferences["ocrMode"] })}>
                                <option value="auto">Automatic (recommended)</option>
                                <option value="always">Always run OCR</option>
                                <option value="never">Text layer only</option>
                              </select>
                            </Field>
                          )}
                        </div>
                      </div>
                    </motion.div>
                  )}
                </AnimatePresence>
              </Section>

              {error && <Alert>{error}</Alert>}
              {nothingSelected && <Alert tone="warning">Select at least one built-in entity type or define a custom entity.</Alert>}
              {customProblem && <Alert tone="warning">Every custom entity needs both a label and a description.</Alert>}

              <div className="flex items-center justify-end gap-3 pb-4">
                <span className="text-[13px] text-ink-600">
                  {selected.size} built-in type{selected.size === 1 ? "" : "s"}
                  {customValid.length ? `, ${customValid.length} custom` : ""}
                </span>
                <Button onClick={submit} loading={submitting} disabled={nothingSelected || Boolean(customProblem)}>
                  Start redaction
                </Button>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </Page>
  );
}
