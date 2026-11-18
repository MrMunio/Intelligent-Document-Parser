import { ArrowDown, ArrowUp, Plus, Trash2 } from "lucide-react";
import { cx } from "./ui";

/**
 * Visual editor for an extraction spec. People who do not write JSON add fields one by one: a name, a type in
 * plain words, a description of what to find, and optionally a fixed list of allowed values. "Group of fields"
 * nests fields inside a field (a JSON object); "List" holds several values (a JSON array), and a list of groups
 * nests fields for each item. The builder and the JSON editor are two views of the same spec.
 */

export type FieldKind = "string" | "number" | "integer" | "boolean" | "date" | "object" | "array";
export type ItemKind = Exclude<FieldKind, "array">;

export interface BuilderField {
  id: string;
  name: string;
  type: FieldKind;
  description: string;
  required: boolean;
  allowed: string; // comma separated allowed values, for text and number fields
  itemType: ItemKind; // for lists
  children: BuilderField[]; // for groups, and for lists of groups
}

export interface BuilderSpec {
  title: string;
  description: string;
  fields: BuilderField[];
}

export const KIND_LABELS: Record<FieldKind, string> = {
  string: "Text",
  number: "Number (decimals allowed)",
  integer: "Whole number",
  boolean: "Yes / No",
  date: "Date",
  object: "Group of fields",
  array: "List",
};

const ITEM_LABELS: Record<ItemKind, string> = {
  string: "Text",
  number: "Number",
  integer: "Whole number",
  boolean: "Yes / No",
  date: "Date",
  object: "Group of fields",
};

const NAME_RE = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;
let counter = 0;
const nextId = () => `f${Date.now().toString(36)}${(counter++).toString(36)}`;

export function newField(partial: Partial<BuilderField> = {}): BuilderField {
  return { id: nextId(), name: "", type: "string", description: "", required: false, allowed: "", itemType: "string", children: [], ...partial };
}

/** A friendly name typed with spaces becomes a valid key: "Effective date" -> "effective_date". */
export function toKey(text: string): string {
  const key = text
    .trim()
    .replace(/[^A-Za-z0-9_]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .toLowerCase();
  return /^[0-9]/.test(key) ? `_${key}` : key;
}

function hasChildren(f: BuilderField) {
  return f.type === "object" || (f.type === "array" && f.itemType === "object");
}

// ---------------------------------------------------------------------------
// Builder <-> JSON spec
// ---------------------------------------------------------------------------

function scalarSchema(type: ItemKind, allowed: string): Record<string, unknown> {
  const out: Record<string, unknown> = { type };
  const values = allowed
    .split(",")
    .map((v) => v.trim())
    .filter(Boolean);
  if (values.length && (type === "string" || type === "number" || type === "integer")) {
    out.enum = type === "string" ? values : values.map(Number).filter((n) => !Number.isNaN(n));
  }
  return out;
}

function propertiesOf(fields: BuilderField[]) {
  const properties: Record<string, unknown> = {};
  const required: string[] = [];
  for (const f of fields) {
    const key = f.name || "";
    properties[key] = fieldSchema(f);
    if (f.required) required.push(key);
  }
  return { properties, required };
}

function fieldSchema(f: BuilderField): Record<string, unknown> {
  let schema: Record<string, unknown>;
  if (f.type === "object") {
    const { properties, required } = propertiesOf(f.children);
    schema = { type: "object", properties, ...(required.length ? { required } : {}) };
  } else if (f.type === "array") {
    let items: Record<string, unknown>;
    if (f.itemType === "object") {
      const { properties, required } = propertiesOf(f.children);
      items = { type: "object", properties, ...(required.length ? { required } : {}) };
    } else {
      items = scalarSchema(f.itemType, "");
    }
    schema = { type: "array", items };
  } else {
    schema = scalarSchema(f.type, f.allowed);
  }
  return { ...schema, description: f.description };
}

export function builderToSpec(spec: BuilderSpec): Record<string, unknown> {
  const { properties, required } = propertiesOf(spec.fields);
  return {
    ...(spec.title.trim() ? { title: spec.title.trim() } : {}),
    ...(spec.description.trim() ? { description: spec.description.trim() } : {}),
    type: "object",
    properties,
    ...(required.length ? { required } : {}),
  };
}

type Json = Record<string, any>;

function kindOf(node: Json): FieldKind {
  let t = node?.type;
  if (Array.isArray(t)) t = t.find((x: string) => x !== "null");
  if (typeof t === "string") t = t.toLowerCase();
  if (t === "date" || t === "datetime" || t === "date-time" || (t === "string" && typeof node.format === "string" && node.format.startsWith("date"))) return "date";
  if (t === "float") return "number";
  if (t === "int") return "integer";
  if (t === "bool") return "boolean";
  if (t === "str") return "string";
  if (!t && node?.properties) return "object";
  return ["string", "number", "integer", "boolean", "object", "array"].includes(t) ? (t as FieldKind) : "string";
}

function fieldsFrom(properties: Json | undefined, required: unknown): BuilderField[] {
  const req = new Set(Array.isArray(required) ? required : []);
  return Object.entries(properties ?? {}).map(([name, node]) => {
    const n = (node ?? {}) as Json;
    const type = kindOf(n);
    const f = newField({ name, type, description: typeof n.description === "string" ? n.description : "", required: req.has(name) });
    if (Array.isArray(n.enum)) f.allowed = n.enum.filter((v: unknown) => v !== null).join(", ");
    if (type === "object") f.children = fieldsFrom(n.properties, n.required);
    if (type === "array") {
      const items = (n.items ?? {}) as Json;
      const itemKind = kindOf(items);
      f.itemType = itemKind === "array" ? "string" : (itemKind as ItemKind);
      if (itemKind === "object") f.children = fieldsFrom(items.properties, items.required);
    }
    return f;
  });
}

/** Reads a JSON spec into the builder. Returns null when the text is not a JSON object with properties. */
export function specToBuilder(text: string): BuilderSpec | null {
  try {
    const raw = JSON.parse(text);
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
    return {
      title: typeof raw.title === "string" ? raw.title : "",
      description: typeof raw.description === "string" ? raw.description : "",
      fields: fieldsFrom(raw.properties, raw.required),
    };
  } catch {
    return null;
  }
}

export function countFields(fields: BuilderField[]): number {
  return fields.reduce((n, f) => n + (f.type === "object" ? countFields(f.children) : 1), 0);
}

// ---------------------------------------------------------------------------
// Editor
// ---------------------------------------------------------------------------

function nameProblem(f: BuilderField, siblings: BuilderField[]): string | null {
  if (!f.name) return "Name is required";
  if (!NAME_RE.test(f.name)) return "Use letters, digits and underscores; do not start with a digit";
  if (siblings.filter((s) => s.name === f.name).length > 1) return "Another field at this level has the same name";
  return null;
}

function FieldCard({
  field,
  siblings,
  index,
  depth,
  maxDepth,
  path,
  onChange,
  onRemove,
  onMove,
}: {
  field: BuilderField;
  siblings: BuilderField[];
  index: number;
  depth: number;
  maxDepth: number;
  path: string;
  onChange: (f: BuilderField) => void;
  onRemove: () => void;
  onMove: (delta: number) => void;
}) {
  const set = (patch: Partial<BuilderField>) => onChange({ ...field, ...patch });
  const problem = nameProblem(field, siblings);
  const nested = hasChildren(field);
  const descriptionShort = field.description.trim().length < 3;
  const fullPath = path ? `${path}.${field.name || "?"}` : field.name || "?";
  return (
    <div className="rounded-lg border border-line bg-white" data-testid="builder-field">
      <div className="grid gap-3 p-3 md:grid-cols-[minmax(160px,1fr)_200px_auto]">
        <label className="block">
          <span className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-ink-400">Field name</span>
          <input
            className={cx("input font-mono text-[13px]", problem && field.name && "border-red-400")}
            placeholder="for example effective_date"
            value={field.name}
            onChange={(e) => set({ name: e.target.value })}
            onBlur={(e) => {
              const key = toKey(e.target.value);
              if (key !== field.name) set({ name: key });
            }}
            aria-label="Field name"
          />
        </label>
        <label className="block">
          <span className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-ink-400">Type</span>
          <select
            className="input"
            value={field.type}
            aria-label="Field type"
            onChange={(e) => {
              const type = e.target.value as FieldKind;
              const keepChildren = type === "object" || (type === "array" && field.itemType === "object");
              set({ type, children: keepChildren ? field.children : [], allowed: type === "string" || type === "number" || type === "integer" ? field.allowed : "" });
            }}
          >
            {(Object.keys(KIND_LABELS) as FieldKind[])
              .filter((k) => depth < maxDepth || (k !== "object" && k !== "array"))
              .map((k) => (
                <option key={k} value={k}>
                  {KIND_LABELS[k]}
                </option>
              ))}
          </select>
        </label>
        <div className="flex items-end justify-between gap-1 md:justify-end">
          <label className="mr-2 flex items-center gap-1.5 pb-2 text-[13px] text-ink-700">
            <input type="checkbox" checked={field.required} onChange={(e) => set({ required: e.target.checked })} />
            Required
          </label>
          <button type="button" className="btn-ghost btn-sm" title="Move up" aria-label="Move up" disabled={index === 0} onClick={() => onMove(-1)}>
            <ArrowUp className="h-4 w-4" aria-hidden />
          </button>
          <button type="button" className="btn-ghost btn-sm" title="Move down" aria-label="Move down" disabled={index === siblings.length - 1} onClick={() => onMove(1)}>
            <ArrowDown className="h-4 w-4" aria-hidden />
          </button>
          <button type="button" className="btn-ghost btn-sm text-red-700" title="Remove field" aria-label="Remove field" onClick={onRemove}>
            <Trash2 className="h-4 w-4" aria-hidden />
          </button>
        </div>
        <label className="block md:col-span-3">
          <span className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-ink-400">Description</span>
          <input
            className="input"
            placeholder="What this field holds, in words the document is likely to use"
            value={field.description}
            onChange={(e) => set({ description: e.target.value })}
            aria-label="Field description"
          />
        </label>
        {(field.type === "string" || field.type === "number" || field.type === "integer") && (
          <label className="block md:col-span-2">
            <span className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-ink-400">Allowed values (optional)</span>
            <input className="input" placeholder="Comma separated, for example payer, provider" value={field.allowed} onChange={(e) => set({ allowed: e.target.value })} aria-label="Allowed values" />
          </label>
        )}
        {field.type === "array" && (
          <label className="block">
            <span className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-ink-400">Each item is</span>
            <select
              className="input"
              value={field.itemType}
              aria-label="List item type"
              onChange={(e) => {
                const itemType = e.target.value as ItemKind;
                set({ itemType, children: itemType === "object" ? (field.children.length ? field.children : [newField()]) : [] });
              }}
            >
              {(Object.keys(ITEM_LABELS) as ItemKind[])
                .filter((k) => depth < maxDepth || k !== "object")
                .map((k) => (
                  <option key={k} value={k}>
                    {ITEM_LABELS[k]}
                  </option>
                ))}
            </select>
          </label>
        )}
        {(problem || descriptionShort) && (
          <p className="text-xs text-amber-700 md:col-span-3">
            {[problem, descriptionShort ? "Add a description of at least 3 characters" : null].filter(Boolean).join(". ")}.
          </p>
        )}
      </div>
      {nested && (
        <div className="border-t border-line bg-surface-strong/40 px-3 pb-3 pt-2">
          <p className="mb-2 text-[12px] text-ink-600">
            {field.type === "object" ? "Fields in this group" : "Fields of each list item"} <span className="font-mono text-ink-500">({fullPath})</span>
          </p>
          <FieldList fields={field.children} onChange={(children) => set({ children })} depth={depth + 1} maxDepth={maxDepth} path={fullPath} />
        </div>
      )}
    </div>
  );
}

function FieldList({
  fields,
  onChange,
  depth,
  maxDepth,
  path,
}: {
  fields: BuilderField[];
  onChange: (fields: BuilderField[]) => void;
  depth: number;
  maxDepth: number;
  path: string;
}) {
  const replace = (i: number, f: BuilderField) => onChange(fields.map((x, j) => (j === i ? f : x)));
  const remove = (i: number) => onChange(fields.filter((_, j) => j !== i));
  const move = (i: number, delta: number) => {
    const j = i + delta;
    if (j < 0 || j >= fields.length) return;
    const copy = fields.slice();
    [copy[i], copy[j]] = [copy[j], copy[i]];
    onChange(copy);
  };
  return (
    <div className={cx("space-y-2.5", depth > 1 && "border-l-2 border-brand-100 pl-3")}>
      {fields.map((f, i) => (
        <FieldCard
          key={f.id}
          field={f}
          siblings={fields}
          index={i}
          depth={depth}
          maxDepth={maxDepth}
          path={path}
          onChange={(nf) => replace(i, nf)}
          onRemove={() => remove(i)}
          onMove={(d) => move(i, d)}
        />
      ))}
      <button type="button" className="btn-secondary btn-sm" onClick={() => onChange([...fields, newField()])} data-testid={depth === 1 ? "add-field" : "add-nested-field"}>
        <Plus className="h-3.5 w-3.5" aria-hidden />
        {depth === 1 ? "Add field" : "Add field to this group"}
      </button>
    </div>
  );
}

export function SpecBuilder({ spec, onChange, maxDepth = 6 }: { spec: BuilderSpec; onChange: (spec: BuilderSpec) => void; maxDepth?: number }) {
  return (
    <div className="space-y-4" data-testid="spec-builder">
      <div className="grid gap-3 md:grid-cols-2">
        <label className="block">
          <span className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-ink-400">Title (optional)</span>
          <input className="input" placeholder="for example Provider contract summary" value={spec.title} onChange={(e) => onChange({ ...spec, title: e.target.value })} />
        </label>
        <label className="block">
          <span className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-ink-400">What the document is (optional)</span>
          <input className="input" placeholder="Context that helps the extractor" value={spec.description} onChange={(e) => onChange({ ...spec, description: e.target.value })} />
        </label>
      </div>
      <FieldList fields={spec.fields} onChange={(fields) => onChange({ ...spec, fields })} depth={1} maxDepth={maxDepth} path="" />
    </div>
  );
}
