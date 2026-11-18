import { renderAsync } from "docx-preview";
import { Minus, Plus } from "lucide-react";
import * as pdfjs from "pdfjs-dist";
import pdfWorkerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import rehypeRaw from "rehype-raw";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import { Alert, Spinner } from "./ui";

pdfjs.GlobalWorkerOptions.workerSrc = pdfWorkerUrl;

const PLACEHOLDER = /(\[[A-Z0-9_]+_\d+\]|█{3,})/g;

/* Splits text on redaction placeholders and renders them as highlighted chips. */
export function highlightPlaceholders(text: string): ReactNode[] {
  return text.split(PLACEHOLDER).map((part, i) =>
    i % 2 === 1 ? (
      <mark key={i} className="placeholder-chip">
        {part}
      </mark>
    ) : (
      <span key={i}>{part}</span>
    ),
  );
}

/* ---------- PDF ---------- */

function PdfPage({ doc, pageNumber, scale }: { doc: pdfjs.PDFDocumentProxy; pageNumber: number; scale: number }) {
  const holder = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const [visible, setVisible] = useState(pageNumber <= 2);
  const [size, setSize] = useState<{ w: number; h: number } | null>(null);

  useEffect(() => {
    const el = holder.current;
    if (!el || visible) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) setVisible(true);
      },
      { rootMargin: "600px" },
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [visible]);

  useEffect(() => {
    let cancelled = false;
    let task: pdfjs.RenderTask | null = null;
    (async () => {
      const page = await doc.getPage(pageNumber);
      if (cancelled) return;
      const viewport = page.getViewport({ scale });
      setSize({ w: viewport.width, h: viewport.height });
      if (!visible || !canvas.current) return;
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      const el = canvas.current;
      el.width = Math.floor(viewport.width * ratio);
      el.height = Math.floor(viewport.height * ratio);
      const ctx = el.getContext("2d");
      if (!ctx) return;
      task = page.render({ canvasContext: ctx, viewport, transform: ratio !== 1 ? [ratio, 0, 0, ratio, 0, 0] : undefined });
      try {
        await task.promise;
      } catch {
        /* render cancelled */
      }
    })();
    return () => {
      cancelled = true;
      task?.cancel();
    };
  }, [doc, pageNumber, scale, visible]);

  return (
    <div
      ref={holder}
      data-page={pageNumber}
      className="relative mx-auto mb-4 bg-white shadow-panel ring-1 ring-line"
      style={{ width: size?.w ?? 612 * scale, height: size?.h ?? 792 * scale }}
    >
      <canvas ref={canvas} style={{ width: "100%", height: "100%" }} />
      <span className="absolute right-2 top-2 rounded bg-white/90 px-1.5 py-0.5 text-[11px] text-ink-500 ring-1 ring-line">
        Page {pageNumber}
      </span>
    </div>
  );
}

export type PageFocus = { page: number; tick: number } | null;

export function PdfViewer({ data, focus }: { data: ArrayBuffer; focus?: PageFocus }) {
  const [doc, setDoc] = useState<pdfjs.PDFDocumentProxy | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [zoom, setZoom] = useState(1);
  const [baseScale, setBaseScale] = useState(1);
  const scroller = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    setDoc(null);
    setError(null);
    const task = pdfjs.getDocument({ data: data.slice(0) });
    task.promise
      .then(async (loaded) => {
        if (cancelled) return;
        const first = await loaded.getPage(1);
        const width = first.getViewport({ scale: 1 }).width;
        const available = (scroller.current?.clientWidth ?? 800) - 40;
        setBaseScale(Math.max(0.5, Math.min(1.6, available / width)));
        setDoc(loaded);
      })
      .catch((e) => !cancelled && setError(String(e?.message ?? e)));
    return () => {
      cancelled = true;
      task.destroy();
    };
  }, [data]);

  useEffect(() => {
    if (!focus || !doc || !scroller.current) return;
    scroller.current.querySelector(`[data-page="${focus.page}"]`)?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [focus, doc]);

  if (error) return <Alert>Could not display this PDF: {error}</Alert>;
  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-line bg-white px-4 py-2 text-[13px] text-ink-600">
        <span>{doc ? `${doc.numPages} page${doc.numPages === 1 ? "" : "s"}` : "Loading"}</span>
        <div className="flex items-center gap-1">
          <button type="button" aria-label="Zoom out" className="rounded p-1 hover:bg-surface-strong" onClick={() => setZoom((z) => Math.max(0.5, +(z - 0.15).toFixed(2)))}>
            <Minus className="h-4 w-4" />
          </button>
          <span className="w-12 text-center tabular-nums">{Math.round(zoom * 100)}%</span>
          <button type="button" aria-label="Zoom in" className="rounded p-1 hover:bg-surface-strong" onClick={() => setZoom((z) => Math.min(2.5, +(z + 0.15).toFixed(2)))}>
            <Plus className="h-4 w-4" />
          </button>
        </div>
      </div>
      <div ref={scroller} className="flex-1 overflow-auto bg-surface p-5" data-testid="pdf-viewer">
        {!doc ? (
          <Spinner label="Rendering document" />
        ) : (
          Array.from({ length: doc.numPages }, (_, i) => <PdfPage key={i} doc={doc} pageNumber={i + 1} scale={baseScale * zoom} />)
        )}
      </div>
    </div>
  );
}

/* ---------- DOCX ---------- */

function markPlaceholders(root: HTMLElement) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const targets: Text[] = [];
  while (walker.nextNode()) {
    const node = walker.currentNode as Text;
    PLACEHOLDER.lastIndex = 0;
    if (node.nodeValue && PLACEHOLDER.test(node.nodeValue)) targets.push(node);
  }
  for (const node of targets) {
    const fragment = document.createDocumentFragment();
    (node.nodeValue ?? "").split(PLACEHOLDER).forEach((part, i) => {
      if (!part) return;
      if (i % 2 === 1) {
        const mark = document.createElement("mark");
        mark.className = "placeholder-chip";
        mark.textContent = part;
        fragment.appendChild(mark);
      } else {
        fragment.appendChild(document.createTextNode(part));
      }
    });
    node.parentNode?.replaceChild(fragment, node);
  }
}

export function DocxViewer({ data, highlight = true }: { data: ArrayBuffer; highlight?: boolean }) {
  const host = useRef<HTMLDivElement>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");

  useEffect(() => {
    let cancelled = false;
    const el = host.current;
    if (!el) return;
    el.innerHTML = "";
    setState("loading");
    renderAsync(data.slice(0), el, undefined, { className: "docx", inWrapper: true, breakPages: true, ignoreLastRenderedPageBreak: true })
      .then(() => {
        if (cancelled) return;
        if (highlight) markPlaceholders(el);
        setState("ready");
      })
      .catch(() => !cancelled && setState("error"));
    return () => {
      cancelled = true;
    };
  }, [data, highlight]);

  return (
    <div className="h-full overflow-auto bg-surface" data-testid="docx-viewer">
      {state === "loading" && <Spinner label="Rendering document" />}
      {state === "error" && (
        <div className="p-5">
          <Alert>This document could not be displayed in the browser. Download it to open it in Word.</Alert>
        </div>
      )}
      <div ref={host} className="docx-host" />
    </div>
  );
}

/* ---------- Text and markdown ---------- */

export function TextViewer({ text, wrap = true }: { text: string; wrap?: boolean }) {
  const content = useMemo(() => highlightPlaceholders(text), [text]);
  return (
    <div className="h-full overflow-auto bg-surface p-5" data-testid="text-viewer">
      <pre
        className={`min-h-full rounded-md bg-white p-5 font-mono text-[12.5px] leading-relaxed text-ink-800 shadow-panel ring-1 ring-line ${
          wrap ? "whitespace-pre-wrap break-words" : "whitespace-pre"
        }`}
      >
        {content}
      </pre>
    </div>
  );
}

export function MarkdownView({ markdown }: { markdown: string }) {
  return (
    <div className="md-body">
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeRaw, rehypeSanitize]}>
        {markdown}
      </ReactMarkdown>
    </div>
  );
}

export function ImageViewer({ blob, alt }: { blob: Blob; alt: string }) {
  const url = useMemo(() => URL.createObjectURL(blob), [blob]);
  useEffect(() => () => URL.revokeObjectURL(url), [url]);
  return (
    <div className="flex h-full items-start justify-center overflow-auto bg-surface p-5" data-testid="image-viewer">
      <img src={url} alt={alt} className="max-w-full bg-white shadow-panel ring-1 ring-line" />
    </div>
  );
}

/* ---------- Format dispatch ---------- */

export type DocKind = "pdf" | "docx" | "image" | "text";

export function kindOf(format?: string | null, fileName?: string | null): DocKind {
  const name = (fileName ?? "").toLowerCase();
  if (format === "pdf" || name.endsWith(".pdf")) return "pdf";
  if (format === "docx" || name.endsWith(".docx")) return "docx";
  if (format === "image" || /\.(png|jpe?g|bmp|webp|tiff?)$/.test(name)) return "image";
  return "text";
}

export function DocumentViewer({
  blob,
  kind,
  fileName,
  focus,
  highlight = true,
}: {
  blob: Blob;
  kind: DocKind;
  fileName: string;
  focus?: PageFocus;
  highlight?: boolean;
}) {
  const [buffer, setBuffer] = useState<ArrayBuffer | null>(null);
  const [text, setText] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setBuffer(null);
    setText(null);
    if (kind === "pdf" || kind === "docx") blob.arrayBuffer().then((b) => !cancelled && setBuffer(b));
    else if (kind === "text") blob.text().then((t) => !cancelled && setText(t));
    return () => {
      cancelled = true;
    };
  }, [blob, kind]);

  if (kind === "image") return <ImageViewer blob={blob} alt={fileName} />;
  if (kind === "text") return text === null ? <Spinner /> : <TextViewer text={text} />;
  if (!buffer) return <Spinner label="Loading document" />;
  return kind === "pdf" ? <PdfViewer data={buffer} focus={focus} /> : <DocxViewer data={buffer} highlight={highlight} />;
}
