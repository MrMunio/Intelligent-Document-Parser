import type { UsageReport, UsageRow } from "../lib/types";

/** "$0.0123", "$0.00" for local engines, "not priced" for models outside the catalog. */
export function formatCost(row?: UsageRow | null): string {
  if (!row) return "";
  if (row.cost_usd === null || row.cost_usd === undefined) return "not priced";
  return `$${row.cost_usd < 0.01 && row.cost_usd > 0 ? row.cost_usd.toFixed(4) : row.cost_usd.toFixed(2)}`;
}

function promptTokens(row: UsageRow) {
  return row.input_tokens + row.cache_read_tokens + row.cache_write_tokens;
}

/** Language model calls, tokens and cost for a job, total and per engine. */
export function UsageSummary({ usage, emptyText, compact = false }: { usage?: UsageReport | null; emptyText?: string; compact?: boolean }) {
  if (!usage || !usage.total || usage.total.calls === 0) {
    return <p className="text-[13px] text-ink-500">{emptyText ?? "No language model was used for this job."}</p>;
  }
  const models = Object.entries(usage.by_model ?? {});
  const t = usage.total;
  if (compact) {
    return (
      <div data-testid="usage-summary" className="space-y-1.5 text-[13px]">
        {models.map(([key, row]) => (
          <div key={key} className="flex items-baseline justify-between gap-4">
            <span className="truncate font-mono text-[12px] text-ink-700" title={key}>
              {key}
            </span>
            <span className="whitespace-nowrap text-right tabular-nums text-ink-900">
              {row.calls} call{row.calls === 1 ? "" : "s"}, {promptTokens(row).toLocaleString()} in, {row.output_tokens.toLocaleString()} out, {formatCost(row)}
            </span>
          </div>
        ))}
      </div>
    );
  }
  return (
    <div data-testid="usage-summary">
      <dl className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-4">
        {[
          ["Model calls", t.calls.toLocaleString()],
          ["Prompt tokens", promptTokens(t).toLocaleString()],
          ["Output tokens", t.output_tokens.toLocaleString()],
          ["Cost", formatCost(t)],
        ].map(([label, value]) => (
          <div key={label}>
            <dt className="text-[11px] font-semibold uppercase tracking-wider text-ink-400">{label}</dt>
            <dd className="mt-0.5 text-sm font-medium tabular-nums text-ink-900">{value}</dd>
          </div>
        ))}
      </dl>
      <table className="table mt-4 text-[12.5px]">
        <thead>
          <tr>
            <th>Engine</th>
            <th>Calls</th>
            <th>Uncached in</th>
            <th>Cache read</th>
            <th>Cache write</th>
            <th>Out</th>
            <th>Cost</th>
          </tr>
        </thead>
        <tbody>
          {models.map(([key, row]) => (
            <tr key={key}>
              <td className="font-mono">{key}</td>
              <td className="tabular-nums">{row.calls.toLocaleString()}</td>
              <td className="tabular-nums">{row.input_tokens.toLocaleString()}</td>
              <td className="tabular-nums">{row.cache_read_tokens.toLocaleString()}</td>
              <td className="tabular-nums">{row.cache_write_tokens.toLocaleString()}</td>
              <td className="tabular-nums">{row.output_tokens.toLocaleString()}</td>
              <td className="tabular-nums">{formatCost(row)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="mt-2 text-xs text-ink-500">Costs use the list prices in the model catalog and may differ from your provider invoice.</p>
    </div>
  );
}
