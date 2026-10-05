/* FedRAG pipeline explorer.
 *
 * A run's trace (planner -> specialist agents -> writer -> fact-checker -> answer) is reduced into a model of
 * nodes laid out in rows; the page draws it as a flow graph, a timeline and an inspector. The same reducer
 * serves live runs (NDJSON streamed by /api/run), saved runs and the evaluation runs, and their replays.
 */
"use strict";

// ================================================================ utilities
const $ = (s, r = document) => r.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const plural = (n, w, p) => `${n} ${n === 1 ? w : p || w + "s"}`;
const trunc = (s, n) => ((s = String(s ?? "")).length > n ? s.slice(0, n - 1) + "…" : s);
const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? u : null);
const plain = (s) => String(s || "").replace(/\[((?:[DWM]\d+)(?:\s*[,;]\s*[DWM]\d+)*)\]/g, "").replace(/[*_`#>|]+/g, "").replace(/\s+/g, " ").trim();
const clone = (o) => JSON.parse(JSON.stringify(o));

function fmtSec(s) {
  if (s == null || !isFinite(s)) return "";
  if (s < 0) s = 0;
  if (s < 10) return s.toFixed(1) + "s";
  if (s < 60) return Math.floor(s) + "s";
  const m = Math.floor(s / 60);
  return `${m}m ${String(Math.floor(s - m * 60)).padStart(2, "0")}s`;
}
function fmtNum(n) {
  if (n == null || !isFinite(n)) return "–";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e4) return Math.round(n / 1e3) + "k";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(Math.round(n));
}
function timeAgo(iso) {
  const t = Date.parse(iso);
  if (!t) return "";
  const s = (Date.now() - t) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + " min ago";
  if (s < 86400) return Math.floor(s / 3600) + " h ago";
  if (s < 7 * 86400) return Math.floor(s / 86400) + " d ago";
  return new Date(t).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

// ================================================================ icons (24px stroke icons)
const P = {
  planner: '<circle cx="12" cy="12" r="9"/><path d="m15.6 8.4-2.3 4.9-4.9 2.3 2.3-4.9z"/>',
  fed: '<path d="M3 8.5 12 3.5l9 5z"/><path d="M5.5 11v6.5M10 11v6.5M14 11v6.5M18.5 11v6.5"/><path d="M3.5 20.5h17"/>',
  db: '<ellipse cx="12" cy="5.8" rx="7.5" ry="2.8"/><path d="M4.5 5.8v12.4c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8V5.8"/><path d="M4.5 12c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3c2.4 2.5 3.7 5.5 3.7 9s-1.3 6.5-3.7 9c-2.4-2.5-3.7-5.5-3.7-9S9.6 5.5 12 3z"/>',
  trend: '<path d="m3 16.5 6-6 4 4 7.5-7.5"/><path d="M15 7h5.5v5.5"/>',
  pen: '<path d="M12 20h8.5"/><path d="M16.4 3.6a2.1 2.1 0 0 1 3 3L7.2 18.8l-4 1 1-4z"/>',
  shield: '<path d="M12 3 4.5 6v5.6c0 4.5 3.1 8.2 7.5 9.4 4.4-1.2 7.5-4.9 7.5-9.4V6z"/><path d="m8.8 12 2.2 2.2 4.3-4.3"/>',
  chat: '<path d="M20.5 11.5a8.4 8.4 0 0 1-12.1 7.6L3.5 20.5l1.5-4.8a8.4 8.4 0 1 1 15.5-4.2z"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4.5 20.5c.9-3.8 3.9-6 7.5-6s6.6 2.2 7.5 6"/>',
  answer: '<path d="M14 3H6.5A1.5 1.5 0 0 0 5 4.5v15A1.5 1.5 0 0 0 6.5 21h11a1.5 1.5 0 0 0 1.5-1.5V8z"/><path d="M14 3v5h5"/><path d="m8.8 14.6 2.1 2.1 4.3-4.3"/>',
  cog: '<circle cx="12" cy="12" r="3"/><path d="M12 2.8v2.4M12 18.8v2.4M4.9 4.9l1.7 1.7M17.4 17.4l1.7 1.7M2.8 12h2.4M18.8 12h2.4M4.9 19.1l1.7-1.7M17.4 6.6l1.7-1.7"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="m20 20-4.3-4.3"/>',
  list: '<path d="M9 6.5h11M9 12h11M9 17.5h11"/><path d="M4.5 6.5h.01M4.5 12h.01M4.5 17.5h.01"/>',
  file: '<path d="M14 3H6.5A1.5 1.5 0 0 0 5 4.5v15A1.5 1.5 0 0 0 6.5 21h11a1.5 1.5 0 0 0 1.5-1.5V8z"/><path d="M14 3v5h5M8.5 13h7M8.5 17h4.5"/>',
  table: '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="M3.5 9.5h17M3.5 14.5h17M9.5 4.5v15"/>',
  columns: '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="M3.5 9h17M9.2 9v10.5M14.8 9v10.5"/>',
  sql: '<ellipse cx="12" cy="5.8" rx="7.5" ry="2.8"/><path d="M4.5 5.8v12.4c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8V5.8"/><path d="m9.5 12.5-2 2 2 2M14.5 12.5l2 2-2 2"/>',
  link: '<path d="M10 14a4.5 4.5 0 0 0 6.4 0l3-3a4.5 4.5 0 0 0-6.4-6.4l-1 1"/><path d="M14 10a4.5 4.5 0 0 0-6.4 0l-3 3a4.5 4.5 0 0 0 6.4 6.4l1-1"/>',
  bars: '<path d="M4 20.5h16"/><path d="M7 16.5V11M12 16.5V6M17 16.5V8.5"/>',
  fx: '<path d="M4 8.5h14.5L15 5"/><path d="M20 15.5H5.5L9 19"/>',
  calc: '<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M8.5 7h7"/><path d="M8.5 11.5h.01M12 11.5h.01M15.5 11.5h.01M8.5 15h.01M12 15h.01M15.5 15h.01M8.5 18.2h.01M12 18.2h3.5"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  x: '<path d="M17.5 6.5l-11 11M6.5 6.5l11 11"/>',
  play: '<path d="M8 5.2v13.6a.8.8 0 0 0 1.2.7l10.7-6.8a.8.8 0 0 0 0-1.4L9.2 4.5a.8.8 0 0 0-1.2.7z" fill="currentColor" stroke="none"/>',
  stop: '<rect x="6.5" y="6.5" width="11" height="11" rx="2.2" fill="currentColor" stroke="none"/>',
  replay: '<path d="M3.5 12a8.5 8.5 0 1 0 2.6-6.1L3.5 8.5"/><path d="M3.5 3.8v4.7h4.7"/>',
  ext: '<path d="M14 4h6v6M20 4l-8.5 8.5"/><path d="M18 14v4.5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 18.5v-11A1.5 1.5 0 0 1 5.5 6H10"/>',
  copy: '<rect x="8.5" y="8.5" width="11.5" height="11.5" rx="2"/><path d="M15.5 8.5V5.5A1.5 1.5 0 0 0 14 4H5.5A1.5 1.5 0 0 0 4 5.5V14a1.5 1.5 0 0 0 1.5 1.5h3"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M2.5 12h2M19.5 12h2M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/>',
  moon: '<path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6"/><path d="M5.5 7l1 12.5A1.5 1.5 0 0 0 8 21h8a1.5 1.5 0 0 0 1.5-1.5L18.5 7M9 7V4.5A1.5 1.5 0 0 1 10.5 3h3A1.5 1.5 0 0 1 15 4.5V7"/>',
  chev: '<path d="m9 6 6 6-6 6"/>',
  left: '<path d="m15 6-6 6 6 6"/>',
  menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
  alert: '<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5M12 16.5h.01"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.5h.01"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.2 2"/>',
  spark: '<path d="M12 3.5l1.9 5.1 5.1 1.9-5.1 1.9L12 17.5l-1.9-5.1-5.1-1.9 5.1-1.9z"/><path d="M18.5 3v3.5M16.8 4.8h3.5"/>',
  loader: '<path d="M12 3a9 9 0 1 0 9 9"/>',
  layers: '<path d="m12 3 9 5-9 5-9-5z"/><path d="m3 13 9 5 9-5"/>',
};
function icon(name, size = 16, cls = "") {
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"${cls ? ` class="${cls}"` : ""}>${P[name] || P.cog}</svg>`;
}
const spinner = (size = 14) => icon("loader", size, "spin");

// ================================================================ agents and tools
const AGENTS = {
  question: { label: "Question", icon: "user", color: "--c-question" },
  planner: { label: "Planner", icon: "planner", color: "--c-planner" },
  fed_research: { label: "Fed research", long: "Fed document research", icon: "fed", color: "--c-fed" },
  data_analyst: { label: "Data analyst", long: "Data analyst (SQL)", icon: "db", color: "--c-data" },
  web_research: { label: "Web research", long: "Web research", icon: "globe", color: "--c-web" },
  market_data: { label: "Market data", long: "Market & economic data", icon: "trend", color: "--c-market" },
  synthesizer: { label: "Writer", icon: "pen", color: "--c-writer" },
  verifier: { label: "Fact-checker", icon: "shield", color: "--c-verifier" },
  direct: { label: "Direct answer", icon: "chat", color: "--c-direct" },
  answer: { label: "Answer", icon: "answer", color: "--c-answer" },
  orchestrator: { label: "Orchestrator", icon: "cog", color: "--c-orch" },
};
const SPECIALISTS = ["fed_research", "data_analyst", "web_research", "market_data"];
const ABOUT = {
  question: "Typed above or picked from the examples on the left.",
  planner: "Reads the question and decides which agents to summon, what each must find and in what order.",
  fed_research: "Searches and reads the collection's PDFs, web pages and Word files, citing pages.",
  data_analyst: "Finds the right table among 846, reads its schema and notes, answers with read-only SQL.",
  web_research: "Searches the web and reads pages: news and anything outside the collection.",
  market_data: "Live FRED series, ECB exchange rates and daily market prices.",
  synthesizer: "Writes the answer from the agents' findings, citing every claim.",
  verifier: "Checks each claim against the cited passages; can ask for a rewrite or more research.",
  direct: "Answers from general knowledge when no source is needed.",
  answer: "Cited answer, with links to the source pages, tables and data.",
};
const TOOLS = {
  search_fed_documents: { icon: "search", color: "--c-fed" },
  find_documents: { icon: "search", color: "--c-fed" },
  list_fed_documents: { icon: "list", color: "--c-fed" },
  get_document_outline: { icon: "list", color: "--c-fed" },
  read_document_pages: { icon: "file", color: "--c-fed" },
  expand_context: { icon: "file", color: "--c-fed" },
  search_tables: { icon: "table", color: "--c-data" },
  list_tables: { icon: "table", color: "--c-data" },
  describe_table: { icon: "columns", color: "--c-data" },
  query_data: { icon: "sql", color: "--c-data" },
  web_search: { icon: "globe", color: "--c-web" },
  fetch_webpage: { icon: "link", color: "--c-web" },
  get_market_prices: { icon: "trend", color: "--c-market" },
  get_economic_series: { icon: "bars", color: "--c-market" },
  search_economic_series: { icon: "search", color: "--c-market" },
  get_exchange_rate: { icon: "fx", color: "--c-market" },
  get_exchange_rate_history: { icon: "fx", color: "--c-market" },
  calculator: { icon: "calc", color: "--c-orch" },
};
// What actually executes behind each tool (shown by the Details switch)
const SEARCH_STACK = () => `BM25 + ${models().embedder} → ${models().reranker}`;
const TOOL_BACKEND = {
  search_fed_documents: SEARCH_STACK, find_documents: SEARCH_STACK, search_tables: SEARCH_STACK,
  list_fed_documents: () => "collection catalog", get_document_outline: () => "collection catalog",
  read_document_pages: () => "parsed page store", expand_context: () => "parsed page store",
  list_tables: () => "table catalog", describe_table: () => "DuckDB", query_data: () => "DuckDB (read-only SQL)",
  web_search: () => "DuckDuckGo search", fetch_webpage: () => "HTTP fetch + trafilatura",
  get_economic_series: () => "FRED API", search_economic_series: () => "FRED series catalog",
  get_exchange_rate: () => "ECB rates (Frankfurter)", get_exchange_rate_history: () => "ECB rates (Frankfurter)",
  get_market_prices: () => "Yahoo Finance daily bars", calculator: () => "Python arithmetic",
};
function models() {
  const d = (state.health && typeof state.health.detail === "object" && state.health.detail) || {};
  const short = (m, dflt) => String(m || dflt).split("/").pop();
  return { llm: (state.health && state.health.model) || (state.info && state.info.model) || "LLM",
    embedder: short(d.embedder, "Qwen3-Embedding-8B"), reranker: short(d.reranker, "Qwen3-Reranker-4B") };
}
const toolBackend = (t) => (TOOL_BACKEND[t] || (() => "tool"))();
const INTENTS = { fed_documents: "Fed documents", live_data: "Live data", web: "Web", general_knowledge: "General knowledge", mixed: "Mixed sources", conversational: "Conversational" };
const CATEGORIES = {
  fed_docs: "Fed documents", fed_docs_compare: "Comparisons across documents", fed_data: "Structured data (SQL)",
  word: "Word files", web_pages: "Web pages in the collection", cross_format: "Cross-format", live_data: "Live data",
  web: "Web", mixed: "Mixed sources", general: "General knowledge", out_of_collection: "Outside the collection",
};
const agentMeta = (a) => AGENTS[a] || { label: a, icon: "cog", color: "--c-orch" };
const toolMeta = (t) => TOOLS[t] || { icon: "cog", color: "--c-orch" };
const agentLabel = (n) => (n.type === "agent" ? agentMeta(n.agent).long || agentMeta(n.agent).label : agentMeta(n.agent).label);

// ================================================================ markdown (escape first, then format)
function evKind(id, ev) {
  if (ev && ev.kind === "doc" && ev.meta && ev.meta.unit === "query") return "k-query";
  if (ev && ev.kind) return "k-" + ev.kind;
  return { D: "k-doc", W: "k-web", M: "k-data" }[String(id)[0]] || "k-none";
}
function citeChip(id, evidence) {
  return `<button class="cite ${evKind(id, evidence && evidence[id])}" data-eid="${esc(id)}">${esc(id)}</button>`;
}
function inlineMd(text, evidence) {
  const slots = [];
  const keep = (html) => `\u0000${slots.push(html) - 1}\u0000`;
  let s = String(text ?? "");
  s = s.replace(/`([^`\n]+)`/g, (_, c) => keep(`<code>${esc(c)}</code>`));
  s = s.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, t, u) => keep(`<a href="${esc(u)}" target="_blank" rel="noopener">${inlineMd(t, evidence)}</a>`));
  s = s.replace(/\[((?:[DWM]\d+)(?:\s*[,;]\s*[DWM]\d+)*)\]/g, (_, ids) => keep(ids.split(/\s*[,;]\s*/).map((i) => citeChip(i, evidence)).join("")));
  s = s.replace(/\bhttps?:\/\/[^\s<>()\]]+[^\s<>()\].,;:!?'"]/g, (u) => keep(`<a href="${esc(u)}" target="_blank" rel="noopener">${esc(trunc(u, 70))}</a>`));
  s = esc(s);
  s = s.replace(/\*\*(?=\S)([\s\S]*?\S)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^\w])__(?=\S)([\s\S]*?\S)__(?!\w)/g, "$1<strong>$2</strong>");
  s = s.replace(/(^|[^\w*])\*(?=[^\s*])([^*\n]*?[^\s*])\*(?!\w)/g, "$1<em>$2</em>");
  s = s.replace(/(^|[^\w])_(?=[^\s_])([^_\n]*?[^\s_])_(?!\w)/g, "$1<em>$2</em>");
  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => slots[+i]);
}
const LIST_RE = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
const TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$/;
function splitRow(l) {
  let t = l.trim();
  if (t.startsWith("|")) t = t.slice(1);
  if (t.endsWith("|")) t = t.slice(0, -1);
  return t.split("|").map((c) => c.trim());
}
function parseList(lines, i, evidence) {
  const items = [];
  while (i < lines.length) {
    const l = lines[i];
    const m = l.match(LIST_RE);
    if (m) {
      items.push({ indent: m[1].replace(/\t/g, "    ").length, ordered: /\d/.test(m[2]), start: parseInt(m[2], 10) || 1, text: m[3] });
      i++;
    } else if (!l.trim()) {
      if (i + 1 < lines.length && LIST_RE.test(lines[i + 1])) i++;
      else break;
    } else if (/^\s{2,}\S/.test(l) && items.length) {
      items[items.length - 1].text += "\n" + l.trim();
      i++;
    } else break;
  }
  let html = "";
  const stack = [];
  for (const it of items) {
    while (stack.length && it.indent < stack[stack.length - 1].indent) html += `</li></${stack.pop().ordered ? "ol" : "ul"}>`;
    const top = stack[stack.length - 1];
    if (!top || it.indent > top.indent) {
      stack.push({ indent: it.indent, ordered: it.ordered });
      html += it.ordered ? `<ol${it.start !== 1 ? ` start="${it.start}"` : ""}>` : "<ul>";
    } else html += "</li>";
    html += `<li>${it.text.split("\n").map((t) => inlineMd(t, evidence)).join("<br>")}`;
  }
  while (stack.length) html += `</li></${stack.pop().ordered ? "ol" : "ul"}>`;
  return [html, i];
}
function md(src, evidence) {
  const lines = String(src || "").replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let i = 0;
  const startsBlock = (k) => /^(#{1,6})\s|^\s*```|^\s*>/.test(lines[k]) || LIST_RE.test(lines[k]) ||
    (lines[k].includes("|") && k + 1 < lines.length && TABLE_SEP.test(lines[k + 1]));
  while (i < lines.length) {
    const line = lines[i];
    let m;
    if (!line.trim()) { i++; continue; }
    if (/^\s*```/.test(line)) {
      const buf = [];
      i++;
      while (i < lines.length && !/^\s*```/.test(lines[i])) buf.push(lines[i++]);
      i++;
      out.push(`<pre><code>${esc(buf.join("\n"))}</code></pre>`);
    } else if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
      const lv = Math.min(m[1].length + 1, 4);
      out.push(`<h${lv}>${inlineMd(m[2].replace(/\s*#+\s*$/, ""), evidence)}</h${lv}>`);
      i++;
    } else if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      out.push("<hr>");
      i++;
    } else if (line.includes("|") && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1])) {
      const head = splitRow(line);
      const rows = [];
      i += 2;
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) rows.push(splitRow(lines[i++]));
      out.push(`<table><thead><tr>${head.map((h) => `<th>${inlineMd(h, evidence)}</th>`).join("")}</tr></thead><tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${inlineMd(c, evidence)}</td>`).join("")}</tr>`).join("")}</tbody></table>`);
    } else if (/^\s*>/.test(line)) {
      const buf = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ""));
      out.push(`<blockquote>${md(buf.join("\n"), evidence)}</blockquote>`);
    } else if (LIST_RE.test(line)) {
      const [html, next] = parseList(lines, i, evidence);
      out.push(html);
      i = next;
    } else {
      const buf = [line];
      i++;
      while (i < lines.length && lines[i].trim() && !startsBlock(i)) buf.push(lines[i++]);
      out.push(`<p>${buf.map((l) => inlineMd(l.trim(), evidence)).join("<br>")}</p>`);
    }
  }
  return out.join("\n");
}

// ================================================================ SQL
const SQL_KW = new Set(("SELECT FROM WHERE AND OR NOT IN IS NULL AS ON JOIN LEFT RIGHT INNER OUTER FULL CROSS GROUP BY ORDER " +
  "HAVING LIMIT OFFSET UNION ALL DISTINCT WITH CASE WHEN THEN ELSE END ASC DESC LIKE ILIKE BETWEEN OVER PARTITION " +
  "EXISTS USING QUALIFY WINDOW FILTER ROWS RANGE PRECEDING FOLLOWING CURRENT ROW INTERVAL TRUE FALSE NULLS FIRST LAST " +
  "EXCLUDE REPLACE SIMILAR TO ANY SOME").split(" "));
function formatSQL(sql) {
  sql = String(sql || "").trim();
  if (sql.includes("\n") || sql.length < 72) return sql;
  const re = /\s+(FROM|WHERE|GROUP BY|ORDER BY|HAVING|LIMIT|QUALIFY|(?:LEFT |RIGHT |INNER |FULL |CROSS )?JOIN|UNION(?: ALL)?)\b/gi;
  return sql.split(/('(?:[^']|'')*'|"(?:[^"]|"")*")/).map((seg, i) => (i % 2 ? seg : seg.replace(re, (_, kw) => "\n" + kw))).join("");
}
function highlightSQL(sql) {
  const re = /(--[^\n]*|\/\*[\s\S]*?\*\/)|('(?:[^']|'')*')|("(?:[^"]|"")*")|(\b\d+(?:\.\d+)?\b)|\b([A-Za-z_][A-Za-z0-9_]*)\b(\s*\()?/g;
  let out = "", last = 0, m;
  while ((m = re.exec(sql))) {
    out += esc(sql.slice(last, m.index));
    last = re.lastIndex;
    if (m[1]) out += `<span class="sql-c">${esc(m[1])}</span>`;
    else if (m[2]) out += `<span class="sql-s">${esc(m[2])}</span>`;
    else if (m[3]) out += esc(m[3]);
    else if (m[4]) out += `<span class="sql-n">${esc(m[4])}</span>`;
    else {
      const w = m[5], paren = m[6] || "";
      if (SQL_KW.has(w.toUpperCase())) out += `<span class="sql-k">${esc(w)}</span>${esc(paren)}`;
      else if (paren) out += `<span class="sql-f">${esc(w)}</span>${esc(paren)}`;
      else out += esc(w);
    }
  }
  return out + esc(sql.slice(last));
}
function resultTable(text, maxRows = 60) {
  const lines = String(text || "").split("\n").filter((l) => l.trim());
  if (!lines.length || /^\(no rows\)|^ERROR/.test(lines[0])) return lines.length ? `<div class="box">${esc(lines[0])}</div>` : "";
  const more = lines.length && /^\.\.\. \(more rows/.test(lines[lines.length - 1]) ? lines.pop() : "";
  const head = lines[0].split(" | ");
  const rows = lines.slice(1).map((l) => l.split(" | "));
  const isNum = (v) => /^-?[\d,]*\.?\d+(e[-+]?\d+)?%?$/i.test(v.trim());
  const body = rows.slice(0, maxRows).map((r) => `<tr>${r.map((c) => `<td class="${isNum(c) ? "num" : ""}">${esc(c)}</td>`).join("")}</tr>`).join("");
  const note = rows.length > maxRows ? `${rows.length - maxRows} more rows` : more ? "more rows not shown to the agent" : "";
  return `<div class="tbl-wrap"><table class="data"><thead><tr>${head.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead><tbody>${body}</tbody></table></div>` +
    (note ? `<div class="card-sub" style="margin-top:4px">${esc(note)}</div>` : "");
}
function splitQuery(text) {
  const t = String(text || "");
  const k = t.indexOf("\nResult:\n");
  return k < 0 ? [t.replace(/^SQL:\s*/, ""), ""] : [t.slice(0, k).replace(/^SQL:\s*/, ""), t.slice(k + 9)];
}

// ================================================================ run model: trace events -> nodes in rows
function newRun(question, opts) {
  const run = {
    id: null, question, opts: opts || {}, source: "live", status: "running", error: null, t: 0,
    clock0: performance.now(), synced: false, endT: null, nodes: new Map(), rows: [], plan: null,
    evidence: {}, result: null, info: [], eval: null, history: [], created: null,
    writers: 0, verifiers: 0, lastStarted: null,
  };
  addNode(run, "question", { type: "question", agent: "question", status: "done", t0: 0, t1: 0 });
  run.rows.push({ id: "question", nodes: ["question"] });
  return run;
}
function addNode(run, id, props) {
  const n = Object.assign({ id, type: "agent", agent: "orchestrator", status: "pending", t0: null, t1: null, calls: [], thoughts: [], v: 0 }, props);
  run.nodes.set(id, n);
  return n;
}
function addRow(run, id, nodes, before) {
  const i = run.rows.findIndex((r) => r.id === (before || "answer"));
  if (i >= 0) run.rows.splice(i, 0, { id, nodes });
  else run.rows.push({ id, nodes });
}
function ensureAnswer(run) {
  let a = run.nodes.get("answer");
  if (!a) {
    a = addNode(run, "answer", { type: "answer", agent: "answer" });
    run.rows.push({ id: "answer", nodes: ["answer"] });
  }
  return a;
}
function startNode(run, n, t) {
  n.status = "running";
  n.t0 = t;
  n.t1 = null;
  n.v++;
  run.lastStarted = n.id;
}
function taskNode(run, agent, tid) {
  if (tid != null) {
    const n = run.nodes.get("task:" + tid);
    if (n) return n;
  }
  return [...run.nodes.values()].reverse().find((n) => n.agent === agent && n.status === "running") || null;
}
function findCall(n, d) {
  if (d.call_id != null) {
    const c = n.calls.find((c) => c.callId === d.call_id && (d.step == null || c.step === d.step));
    if (c) return c;
  }
  return n.calls.find((c) => c.tool === d.tool && c.t1 == null) || null;
}
function onPlan(run, d, t) {
  run.plan = d;
  const p = run.nodes.get("planner");
  if (p) Object.assign(p, { status: "done", t1: t, plan: d, v: p.v + 1 });
  const tasks = d.tasks || [];
  if (!d.needs_tools || !tasks.length) {
    if (!run.nodes.has("direct")) {
      addNode(run, "direct", { type: "direct", agent: "direct" });
      addRow(run, "direct", ["direct"]);
    }
  } else {
    const byId = Object.fromEntries(tasks.map((x) => [x.id, x]));
    const wave = {};
    const w = (x, seen) => {
      if (wave[x.id] != null) return wave[x.id];
      if (seen.has(x.id)) return 0;
      seen.add(x.id);
      const deps = (x.depends_on || []).filter((id) => byId[id] && id !== x.id);
      return (wave[x.id] = deps.length ? 1 + Math.max(...deps.map((id) => w(byId[id], seen))) : 0);
    };
    tasks.forEach((x) => w(x, new Set()));
    const top = Math.max(0, ...Object.values(wave));
    for (let k = 0; k <= top; k++) {
      const ids = tasks.filter((x) => wave[x.id] === k).map((x) => {
        const id = "task:" + x.id;
        if (!run.nodes.has(id)) addNode(run, id, { type: "agent", agent: x.agent, taskId: x.id, instruction: x.instruction, deps: (x.depends_on || []).filter((dd) => byId[dd] && dd !== x.id) });
        return id;
      });
      if (ids.length) addRow(run, "wave" + k, ids);
    }
    if (!run.nodes.has("writer:1")) {
      addNode(run, "writer:1", { type: "writer", agent: "synthesizer", seq: 1 });
      addRow(run, "writer:1", ["writer:1"]);
    }
    if (run.opts.verify !== false && !run.nodes.has("verifier:1")) {
      addNode(run, "verifier:1", { type: "verifier", agent: "verifier", seq: 1 });
      addRow(run, "verifier:1", ["verifier:1"]);
    }
  }
  ensureAnswer(run);
}
function onStart(run, agent, d, t) {
  if (agent === "planner") {
    let n = run.nodes.get("planner");
    if (!n) {
      n = addNode(run, "planner", { type: "planner", agent: "planner" });
      addRow(run, "planner", ["planner"]);
    }
    startNode(run, n, t);
  } else if (agent === "synthesizer" || agent === "verifier") {
    const seq = agent === "synthesizer" ? ++run.writers : ++run.verifiers;
    const id = (agent === "synthesizer" ? "writer:" : "verifier:") + seq;
    let n = run.nodes.get(id);
    if (!n) {
      n = addNode(run, id, { type: agent === "synthesizer" ? "writer" : "verifier", agent, seq });
      addRow(run, id, [id]);
    }
    n.task = d.task;
    n.text = "";
    startNode(run, n, t);
  } else if (agent === "direct") {
    let n = run.nodes.get("direct");
    if (!n) {
      n = addNode(run, "direct", { type: "direct", agent: "direct" });
      addRow(run, "direct", ["direct"]);
    }
    n.text = "";
    startNode(run, n, t);
  } else if (SPECIALISTS.includes(agent)) {
    const id = "task:" + d.task_id;
    let n = run.nodes.get(id);
    if (!n) {
      n = addNode(run, id, { type: "agent", agent, taskId: d.task_id, instruction: d.task, deps: [] });
      if (d.task_id === "fallback") {
        n.fallback = true;
        addRow(run, "fallback", [id], run.nodes.get("writer:1") && run.nodes.get("writer:1").status === "pending" ? "writer:1" : "answer");
      } else {
        n.followUp = true;
        const rid = "round" + (run.verifiers + 1);
        const row = run.rows.find((r) => r.id === rid);
        if (row) row.nodes.push(id);
        else addRow(run, rid, [id]);
      }
    }
    n.instruction = d.task || n.instruction;
    startNode(run, n, t);
  }
}
function applyEvent(run, ev) {
  const { agent } = ev, t = ev.t || 0, d = ev.data || {};
  run.t = Math.max(run.t, t);
  let n;
  switch (ev.type) {
    case "agent_start": onStart(run, agent, d, t); break;
    case "plan": onPlan(run, d, t); break;
    case "thought":
      if ((n = taskNode(run, agent, d.task_id))) {
        n.thoughts.push({ step: d.step ?? ((n.calls.length ? n.calls[n.calls.length - 1].step : 0) + 1), text: d.text, t });
        n.v++;
      }
      break;
    case "tool_call":
      if ((n = taskNode(run, agent, d.task_id))) {
        const last = n.calls[n.calls.length - 1];
        const step = d.step ?? (!last ? 1 : t - last.t0 > 0.3 ? last.step + 1 : last.step);
        n.calls.push({ callId: d.call_id, step, tool: d.tool, args: d.args || {}, t0: t, t1: null, ids: [], preview: "", chars: 0, output: null });
        n.v++;
      }
      break;
    case "tool_result":
      if ((n = taskNode(run, agent, d.task_id))) {
        const c = findCall(n, d);
        if (c) Object.assign(c, { t1: t, ids: d.ids || [], preview: d.preview || "", chars: d.chars || 0 });
        n.v++;
      }
      break;
    case "tool_output":
      if ((n = taskNode(run, agent, d.task_id))) {
        const c = n.calls.find((c) => c.callId === d.call_id && (d.step == null || c.step === d.step));
        if (c) c.output = d.text;
        n.v++;
      }
      break;
    case "agent_finish":
      if ((n = taskNode(run, agent, d.task_id))) Object.assign(n, { status: "done", t1: t, finish: d, v: n.v + 1 });
      break;
    case "error":
      if (agent === "orchestrator") run.error = d.error;
      else if ((n = taskNode(run, agent, d.task_id))) Object.assign(n, { status: "error", t1: t, error: d.error, v: n.v + 1 });
      break;
    case "delta":
      n = agent === "direct" ? run.nodes.get("direct") : run.nodes.get("writer:" + run.writers);
      if (n) { n.text = (n.text || "") + (d.text || ""); n.v++; }
      break;
    case "synthesis":
      if ((n = run.nodes.get("writer:" + run.writers))) Object.assign(n, { status: "done", t1: t, text: d.answer || "", revised: !!d.revised, v: n.v + 1 });
      break;
    case "verification":
      if ((n = run.nodes.get("verifier:" + run.verifiers))) Object.assign(n, { status: "done", t1: t, verdict: d, v: n.v + 1 });
      break;
    case "final":
      if (agent === "direct" && (n = run.nodes.get("direct"))) Object.assign(n, { status: "done", t1: t, text: d.answer || "", v: n.v + 1 });
      if (agent === "orchestrator") {
        n = ensureAnswer(run);
        Object.assign(n, { status: "done", t0: t, t1: t, v: n.v + 1 });
        run.finalAnswer = d.answer;
      }
      break;
    case "info":
      run.info.push({ t, message: d.message || "" });
      break;
  }
}
function finishRun(run, result, status) {
  run.result = result || run.result;
  run.status = status;
  const res = run.result;
  if (res) {
    for (const s of res.sources || []) {
      const ev = run.evidence[s.id];
      if (!ev) run.evidence[s.id] = { id: s.id, kind: s.kind, title: s.title || s.source, source: s.source, url: s.url, text: s.excerpt || "", meta: s.meta || {} };
      else if (s.url) ev.url = s.url;
    }
  }
  run.endT = res && res.seconds != null ? Math.max(res.seconds, run.t) : run.t;
  for (const [id, n] of [...run.nodes]) {
    if (n.status === "pending" && id !== "answer") {
      run.nodes.delete(id);
      run.rows.forEach((r) => (r.nodes = r.nodes.filter((x) => x !== id)));
    } else if (n.status === "running") {
      n.status = status === "done" ? "done" : "stopped";
      n.t1 = n.t1 ?? run.endT;
      n.v++;
    }
  }
  run.rows = run.rows.filter((r) => r.nodes.length);
  const a = run.nodes.get("answer");
  if (a) {
    a.status = status === "done" ? "done" : "stopped";
    a.v++;
  }
}
function runFromSaved(saved) {
  const run = newRun(saved.question, saved.options || {});
  Object.assign(run, { id: saved.id, source: saved.source || "ui", eval: saved.eval || null, history: saved.history || [], created: saved.created });
  run.evidence = clone(saved.evidence || {});
  for (const ev of saved.events || []) applyEvent(run, ev);
  finishRun(run, saved.result, saved.status === "done" ? "done" : saved.status === "cancelled" ? "cancelled" : "error");
  run.error = saved.error || run.error;
  return run;
}
function computeEdges(run) {
  const edges = [];
  let leaves = [];
  for (const row of run.rows) {
    const start = leaves.slice();
    const used = new Set();
    for (const id of row.nodes) {
      const n = run.nodes.get(id);
      const deps = (n && n.deps ? n.deps : []).map((x) => "task:" + x).filter((x) => run.nodes.has(x) && x !== id);
      for (const f of deps.length ? deps : start) {
        edges.push([f, id]);
        used.add(f);
      }
    }
    leaves = leaves.filter((l) => !used.has(l)).concat(row.nodes);
  }
  return edges;
}
const orderedNodes = (run) => run.rows.flatMap((r) => r.nodes.map((id) => run.nodes.get(id)).filter(Boolean));

// ================================================================ state
const state = {
  info: null, health: null, runs: [], evalRuns: [], tab: "examples",
  run: null, live: null, replay: null, speed: 4,
  selected: null, evView: null, follow: true, demoSel: null,
  openDetails: new Set(),
  details: (() => { try { return localStorage.getItem("fedrag-details") === "1"; } catch (e) { return false; } })(),
};
function runNow(run) {
  if (!run) return 0;
  if (run.status !== "running") return run.endT ?? run.t;
  if (state.replay && state.replay.run === run) return state.replay.now || 0;
  return Math.max(run.t, (performance.now() - run.clock0) / 1000);
}

// ================================================================ api
const api = {
  async get(path) {
    const r = await fetch(path, { headers: { Accept: "application/json" } });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    return r.json();
  },
  async send(path, method, body) {
    const r = await fetch(path, { method, headers: { "Content-Type": "application/json", "X-FedRAG": "1" }, body: body ? JSON.stringify(body) : undefined });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    return r.json();
  },
};

// ================================================================ node cards
function nodeTitle(n) {
  if (n.type === "writer") return n.seq > 1 ? (/revise/.test(n.task || "") ? "Writer · revision" : "Writer · rewrite") : "Writer";
  if (n.type === "verifier") return n.seq > 1 ? `Fact-checker · round ${n.seq}` : "Fact-checker";
  if (n.type === "agent") return agentMeta(n.agent).label;
  return agentMeta(n.agent).label;
}
function stateIcon(n) {
  if (n.status === "running") return { cls: "", html: spinner(14) };
  if (n.status === "done" && n.finish) {
    const c = { high: "ok", medium: "warn", low: "bad" }[n.finish.confidence] || "ok";
    return { cls: c, html: icon("check", 15), title: `done · confidence ${n.finish.confidence || "?"}` };
  }
  if (n.status === "done") return { cls: "ok", html: icon("check", 15) };
  if (n.status === "error") return { cls: "bad", html: icon("x", 15) };
  if (n.status === "stopped") return { cls: "", html: icon("x", 14) };
  return { cls: "", html: "" };
}
function verdictBadge(v) {
  if (!v) return "";
  const map = { accept: ["ok", "accepted"], revise: ["warn", "revise"], research: ["info", "more research"] };
  const [c, l] = map[v.verdict] || ["muted", v.verdict];
  return `<span class="badge ${c}">${esc(l)}</span>`;
}
function confBadge(c) {
  const k = { high: "ok", medium: "warn", low: "bad" }[c] || "muted";
  return c ? `<span class="badge ${k}">${esc(c)}</span>` : "";
}
function toolDots(n, max = 9) {
  const dots = n.calls.slice(0, max).map((c) => {
    const tm = toolMeta(c.tool);
    return `<span class="t-dot${c.t1 == null && n.status === "running" ? " run" : ""}" style="--tc:var(${tm.color})" title="${esc(c.tool)}">${icon(tm.icon, 11)}</span>`;
  }).join("");
  return `<span class="n-tools">${dots}${n.calls.length > max ? `<span class="t-more">+${n.calls.length - max}</span>` : ""}</span>`;
}
function nodeTime(n) {
  if (n.t0 == null || n.type === "question" || n.type === "answer") return "";
  const live = n.status === "running";
  return `<span class="n-time${live ? " live" : ""}" data-node="${esc(n.id)}">${fmtSec((n.t1 ?? runNow(state.run)) - n.t0)}</span>`;
}
function nodeBody(run, n) {
  const idle = n.status === "idle";
  switch (n.type) {
    case "question":
      return idle ? esc(ABOUT.question) : esc(run.question);
    case "planner":
      if (n.plan) return esc(n.plan.reasoning || "");
      return n.status === "running" ? "Reading the question, choosing agents and writing their tasks…" : esc(ABOUT.planner);
    case "agent":
      return esc(n.instruction || ABOUT[n.agent] || "");
    case "writer":
    case "direct": {
      if (idle) return esc(ABOUT[n.agent]);
      if (n.status === "running" && n.text) return esc("…" + plain(n.text).slice(-150));
      if (n.text) return esc(trunc(plain(n.text), 150));
      return n.type === "writer" ? (n.task ? esc(n.task[0].toUpperCase() + n.task.slice(1)) : "Writes the cited answer from the findings") : "Answers from general knowledge";
    }
    case "verifier":
      if (idle) return esc(ABOUT.verifier);
      if (n.verdict) return n.verdict.issues && n.verdict.issues.length ? esc(n.verdict.issues[0]) : "No problems found in the draft.";
      return n.status === "running" ? "Checking every claim against the cited evidence…" : "Checks the draft against the cited evidence";
    case "answer": {
      if (idle) return esc(ABOUT.answer);
      const src = run.result ? run.result.sources || [] : [];
      if (n.status === "done") return src.length ? `Cites ${plural(src.length, "source")}` : "Answered without sources";
      if (n.status === "stopped") return "No answer: the run did not finish";
      return "Waiting for the writer…";
    }
  }
  return "";
}
function nodeFoot(run, n) {
  switch (n.type) {
    case "question": {
      const bits = [];
      if (run.history && run.history.length) bits.push('<span class="badge muted">follow-up</span>');
      if (run.opts && run.opts.thinking) bits.push('<span class="badge muted">thinking</span>');
      return bits.join("");
    }
    case "planner":
      return n.plan ? `<span class="badge intent">${esc(INTENTS[n.plan.intent] || n.plan.intent)}</span><span>${n.plan.needs_tools ? plural((n.plan.tasks || []).length, "task") : "no tools"}</span>` : "";
    case "agent": {
      if (n.status === "idle") {
        const tools = state.info ? (state.info.agents.find((a) => a.name === n.agent) || {}).tools || [] : [];
        return `<span class="card-sub">${plural(tools.length, "tool")}</span>`;
      }
      const extra = n.error ? '<span class="badge bad">failed</span>' : "";
      return (n.calls.length ? toolDots(n, 6) : n.status === "running" ? "<span>thinking…</span>" : "") + extra;
    }
    case "writer": {
      const words = n.text ? plain(n.text).split(" ").length : 0;
      return words ? `<span>${words} words</span>` : "";
    }
    case "verifier":
      return verdictBadge(n.verdict) + (n.verdict && n.verdict.issues && n.verdict.issues.length ? `<span>${plural(n.verdict.issues.length, "issue")}</span>` : "");
    case "answer": {
      if (!run.result) return "";
      const k = (kind) => (run.result.sources || []).filter((s) => s.kind === kind).length;
      return [["doc", "D"], ["web", "W"], ["data", "M"]].filter(([kind]) => k(kind)).map(([kind, p]) => `<span class="cite k-${kind}" style="cursor:default">${k(kind)} ${p}</span>`).join("");
    }
  }
  return "";
}
function nodeHTML(run, n) {
  const st = stateIcon(n);
  const foot = nodeFoot(run, n), time = nodeTime(n);
  const lines = n.type === "agent" ? 3 : n.type === "question" || n.type === "answer" ? 1 : 2;
  return `<div class="n-head"><span class="n-ico">${icon(agentMeta(n.agent).icon, 15)}</span>` +
    `<span class="n-name">${esc(nodeTitle(n))}</span>${n.taskId ? `<span class="n-tid">${esc(n.taskId)}</span>` : ""}` +
    `<span class="n-state ${st.cls}"${st.title ? ` title="${esc(st.title)}"` : ""}>${st.html}</span></div>` +
    `<div class="n-body${n.status === "running" && n.text ? " live" : ""}" style="-webkit-line-clamp:${lines}">${nodeBody(run, n)}</div>` +
    (foot || time ? `<div class="n-foot">${foot}<span class="grow"></span>${time}</div>` : "") +
    (state.details ? nodeDetails(run, n) : "");
}
function nodeDetails(run, n) {
  const llm = models().llm;
  const row = (live, ico, what, how) => `<div class="nd-row${live ? " live" : ""}" title="${esc(what + " · " + how)}">` +
    `${live ? '<span class="nd-dot"></span>' : `<span class="nd-ico">${icon(ico, 11)}</span>`}` +
    `<span class="nd-text"><span class="nd-what">${esc(what)}</span><span class="nd-how">${esc(how)}</span></span></div>`;
  const rows = [];
  const running = n.status === "running";
  const llmRole = { planner: "plans the tasks (JSON)", writer: "writes the answer (streamed)", verifier: "checks claims (JSON)", direct: "answers directly (streamed)" }[n.type];
  if (n.type === "question") return "";
  if (n.type === "answer") rows.push(row(false, "layers", "assembled", "citations linked to sources"));
  else if (n.type !== "agent") rows.push(row(running, "spark", llm, llmRole));
  else if (n.status === "idle") {
    const tools = state.info ? (state.info.agents.find((a) => a.name === n.agent) || {}).tools || [] : [];
    rows.push(row(false, "spark", llm, "ReAct loop: picks tools, then submits findings"));
    [...new Set(tools.map(toolBackend))].slice(0, 3).forEach((b) => rows.push(row(false, "cog", "runs on", b)));
  } else if (running) {
    const inflight = n.calls.filter((c) => c.t1 == null);
    if (inflight.length) inflight.slice(0, 3).forEach((c) => rows.push(row(true, "cog", c.tool, toolBackend(c.tool))));
    else rows.push(row(true, "spark", llm, n.calls.length ? (run.opts && run.opts.thinking ? "reasoning over the results" : "reading results, choosing the next step") : "choosing the first tools"));
  } else if (n.status === "pending") {
    rows.push(row(false, "spark", llm, "waits for its turn"));
  } else {
    const steps = n.finish ? n.finish.steps : null;
    rows.push(row(false, "spark", llm, steps ? `${plural(steps, "LLM step")}` : "LLM steps"));
    const used = [...new Set(n.calls.map((c) => toolBackend(c.tool)))];
    if (used.length) rows.push(row(false, "cog", "used", used.join(" · ")));
  }
  return `<div class="n-details">${rows.join("")}</div>`;
}

// ================================================================ graph
function renderGraph(run, host, selectedId) {
  if (!host || !run) return;
  const structure = run.rows.map((r) => r.id + ":" + r.nodes.join(",")).join("|");
  if (host._run !== run) {
    host._run = run;
    host._els = new Map();
    host._structure = null;
    host.innerHTML = '<svg class="edges"></svg>';
    if (!host._ro && window.ResizeObserver) {
      host._ro = new ResizeObserver(() => drawEdges(host._run, host));
      host._ro.observe(host);
    }
  }
  if (host._structure !== structure) {
    host._structure = structure;
    host.querySelectorAll(":scope > .g-row").forEach((e) => e.remove());
    for (const row of run.rows) {
      const r = document.createElement("div");
      const first = run.nodes.get(row.nodes[0]);
      const wide = row.nodes.length === 1 && first && ["question", "planner", "answer"].includes(first.type);
      r.className = `g-row${row.nodes.length === 1 ? " single" : ""}${wide ? " wide" : ""}`;
      r.style.setProperty("--k", Math.min(row.nodes.length, 4));
      r.dataset.row = row.id;
      for (const id of row.nodes) {
        let el = host._els.get(id);
        if (!el) {
          el = document.createElement("div");
          el.dataset.node = id;
          el.tabIndex = 0;
          el.setAttribute("role", "button");
          host._els.set(id, el);
        }
        r.appendChild(el);
      }
      host.appendChild(r);
    }
    for (const id of [...host._els.keys()]) if (!run.nodes.has(id)) host._els.delete(id);
  }
  for (const [id, el] of host._els) {
    const n = run.nodes.get(id);
    if (!n) continue;
    const cls = `node ${n.status}${id === selectedId ? " selected" : ""}`;
    if (el.className !== cls) el.className = cls;
    el.style.setProperty("--c", `var(${agentMeta(n.agent).color})`);
    const sig = `${n.v}|${n.status}|${run.result ? 1 : 0}|${state.info ? 1 : 0}|${state.details ? 1 : 0}|${models().llm}`;
    if (el._sig !== sig) {
      el._sig = sig;
      el.innerHTML = nodeHTML(run, n);
    }
  }
  requestAnimationFrame(() => drawEdges(run, host));
}
function drawEdges(run, host) {
  if (!run || !host || !host._els) return;
  const svg = host.querySelector("svg.edges");
  if (!svg) return;
  const box = host.getBoundingClientRect();
  svg.setAttribute("width", host.scrollWidth);
  svg.setAttribute("height", host.scrollHeight);
  const parts = [];
  for (const [a, b] of computeEdges(run)) {
    const ea = host._els.get(a), eb = host._els.get(b);
    const na = run.nodes.get(a), nb = run.nodes.get(b);
    if (!ea || !eb || !na || !nb || !ea.isConnected || !eb.isConnected) continue;
    const ra = ea.getBoundingClientRect(), rb = eb.getBoundingClientRect();
    const x1 = ra.left + ra.width / 2 - box.left, y1 = ra.bottom - box.top;
    const x2 = rb.left + rb.width / 2 - box.left, y2 = rb.top - box.top;
    const cls = nb.status === "pending" ? "pending" : nb.status === "running" ? "active" : nb.status === "idle" ? "" : "done";
    const color = `var(${agentMeta(nb.status === "running" ? nb.agent : na.agent).color})`;
    const dy = Math.max(14, (y2 - y1) / 2);
    parts.push(`<path class="edge ${cls}" style="--ec:${color}" d="M${x1.toFixed(1)},${y1.toFixed(1)} C${x1.toFixed(1)},${(y1 + dy).toFixed(1)} ${x2.toFixed(1)},${(y2 - dy).toFixed(1)} ${x2.toFixed(1)},${y2.toFixed(1)}"/>`);
    parts.push(`<circle class="port${cls === "active" ? " active" : ""}" style="--ec:${color}" cx="${x2.toFixed(1)}" cy="${y2.toFixed(1)}" r="3"/>`);
  }
  svg.innerHTML = parts.join("");
}

// ================================================================ timeline
function renderGantt(run) {
  const host = $("#gantt");
  if (!host || !run) return;
  const now = runNow(run);
  const items = orderedNodes(run).filter((n) => n.t0 != null && !["question", "answer"].includes(n.type));
  if (!items.length) {
    host.innerHTML = '<div class="empty">The timeline fills in as the agents start.</div>';
    return;
  }
  const total = Math.max(now, 1, ...items.map((n) => n.t1 ?? now));
  const step = [0.5, 1, 2, 5, 10, 15, 20, 30, 60, 120, 300, 600].find((s) => total / s <= 7) || 1200;
  const T = Math.max(step, Math.ceil(total / step) * step);
  const pct = (t) => `${((Math.max(0, t) / T) * 100).toFixed(3)}%`;
  let ticks = "", grid = "";
  for (let t = 0; t <= T + 1e-9; t += step) {
    ticks += `<span class="g-tick" style="left:${pct(t)}">${t >= 60 && step >= 60 ? t / 60 + "m" : t + "s"}</span>`;
    grid += `<i style="left:${pct(t)}"></i>`;
  }
  const lines = items.map((n) => {
    const m = agentMeta(n.agent);
    const end = n.t1 ?? now;
    const segs = n.calls.map((c) => {
      const ce = c.t1 ?? now;
      return `<span class="g-seg" style="left:${pct(c.t0)};width:${pct(Math.max(ce - c.t0, T * 0.004))}" title="${esc(c.tool)} · ${fmtSec(ce - c.t0)}"></span>`;
    }).join("");
    const endPct = (end / T) * 100;
    const dur = `<span class="g-dur" style="${endPct < 86 ? `left:calc(${endPct.toFixed(2)}% + 6px)` : `right:calc(${(100 - endPct).toFixed(2)}% + 6px);color:var(--text)`}">${fmtSec(end - n.t0)}</span>`;
    const name = `${nodeTitle(n)}${n.taskId ? " · " + n.taskId : ""}`;
    return `<div class="g-line${n.id === state.selected ? " selected" : ""}" data-node="${esc(n.id)}" style="--c:var(${m.color})">` +
      `<div class="g-name" title="${esc(name)}"><span class="n-ico">${icon(m.icon, 12)}</span>${esc(name)}</div>` +
      `<div class="g-track"><span class="g-bar${n.status === "running" ? " running" : ""}" style="left:${pct(n.t0)};width:${pct(Math.max(end - n.t0, T * 0.004))}"></span>${segs}${dur}</div></div>`;
  }).join("");
  host.innerHTML = `<div class="g-axis">${ticks}</div><div class="g-body"><div class="g-grid">${grid}</div>${lines}</div>` +
    `<div class="g-legend"><span><i style="background:color-mix(in srgb, var(--c-fed) 28%, transparent);border:1px solid color-mix(in srgb, var(--c-fed) 55%, transparent)"></i>agent working (LLM)</span><span><i style="background:var(--c-fed)"></i>tool call running</span></div>`;
}

// ================================================================ run view
function viewHTML() {
  return `
  <section class="card run-head" id="runHead"></section>
  <div id="runAlert"></div>
  <section class="card">
    <div class="card-head"><span class="card-title">Pipeline</span><span class="card-sub" id="graphSub"></span><span class="spacer"></span><span class="roster" id="roster"></span></div>
    <div class="graph-wrap scroll"><div class="graph" id="graph"></div></div>
    <div id="graphNotes" style="padding:0 16px 14px"></div>
  </section>
  <section class="card">
    <div class="card-head"><span class="card-title">Timeline</span><span class="card-sub" id="ganttSub"></span></div>
    <div class="gantt" id="gantt"></div>
  </section>
  <section class="card" id="answerCard">
    <div class="card-head"><span class="card-title">Answer</span><span class="card-sub" id="answerSub"></span><span class="spacer"></span>
      <button class="btn small" id="copyBtn" hidden>${icon("copy", 13)}Copy</button></div>
    <div id="evalBox"></div>
    <div class="answer-body"><div class="md" id="answer"></div></div>
  </section>
  <section class="card" id="sourcesCard" hidden>
    <div class="card-head"><span class="card-title">Sources</span><span class="card-sub" id="sourcesSub"></span></div>
    <div class="card-body" id="sources"></div>
  </section>
  <section class="card" id="statsCard" hidden>
    <div class="card-head"><span class="card-title">Run statistics</span></div>
    <div class="card-body" id="stats"></div>
  </section>`;
}
function renderRunHead(run) {
  const host = $("#runHead");
  if (!host) return;
  const nCalls = [...run.nodes.values()].reduce((s, n) => s + n.calls.length, 0);
  const nEv = Object.keys(run.evidence).length;
  const statusText = { running: "Running", done: "Done", error: "Failed", cancelled: "Stopped" }[run.status] || run.status;
  const label = run.source === "eval" ? `${icon("flask", 12)} Evaluation run${run.eval && run.eval.category ? " · " + esc(CATEGORIES[run.eval.category] || run.eval.category) : ""}`
    : state.replay && state.replay.run === run ? `${icon("replay", 12)} Replay at ${state.replay.speed}×`
      : run.source === "live" ? `${icon("spark", 12)} Live run${run.status === "running" ? "" : " · " + esc(timeAgo(run.created))}`
        : `${icon("clock", 12)} Saved run${run.created ? " · " + esc(timeAgo(run.created)) : ""}`;
  const followUp = run.history && run.history.length ? `<div class="card-sub" style="margin:-4px 0 10px">Follow-up to: “${esc(trunc(run.history[run.history.length - 1].question, 120))}”</div>` : "";
  const canReplay = run.id && run.status !== "running";
  const speeds = [1, 4, 16].map((s) => `<button data-speed="${s}" class="${state.speed === s ? "on" : ""}">${s}×</button>`).join("");
  host.innerHTML = `
    <div class="run-top"><div class="run-label">${label}</div>
      <span class="run-actions">
        ${state.replay && state.replay.run === run ? `<span class="seg" title="Replay speed">${speeds}</span><button class="btn small" data-act="stop-replay">${icon("stop", 12)}Stop replay</button>` : ""}
        ${canReplay ? `<span class="seg" title="Replay speed">${speeds}</span><button class="btn small" data-act="replay" title="Replay the run with its original timing">${icon("replay", 13)}Replay</button>` : ""}
      </span></div>
    <div class="run-q">${esc(run.question)}</div>${followUp}
    <div class="run-meta">
      <span class="chip status-${esc(run.status)}">${run.status === "running" ? spinner(12) : run.status === "done" ? icon("check", 12) : icon("x", 12)}<b id="runStatus">${statusText}</b>&nbsp;<span id="runElapsed">${fmtSec(runNow(run))}</span></span>
      <span class="chip"><b>${nCalls}</b> ${nCalls === 1 ? "tool call" : "tool calls"}</span>
      <span class="chip"><b>${nEv}</b> ${run.source === "eval" ? (nEv === 1 ? "source" : "sources") : nEv === 1 ? "passage" : "passages"}</span>
      ${run.eval && run.eval.score != null ? `<span class="chip" title="LLM judge's score against the reference answer">judge <span class="score s${run.eval.score}">${run.eval.score}/2</span></span>` : ""}
    </div>`;
  const roster = $("#roster");
  if (roster) {
    const tasks = [...run.nodes.values()].filter((n) => n.type === "agent");
    const list = run.nodes.has("direct") ? SPECIALISTS.concat("direct") : SPECIALISTS;
    roster.innerHTML = list.map((a) => {
      const k = a === "direct" ? 1 : tasks.filter((n) => n.agent === a).length;
      const m = agentMeta(a);
      const tip = a === "direct" ? "answered from general knowledge" : k ? `summoned for ${plural(k, "task")}` : run.plan ? "not summoned" : "waiting for the plan";
      return `<span class="r-agent${k ? " on" : ""}" style="--c:var(${m.color})" title="${esc((m.long || m.label) + ": " + tip)}">${icon(m.icon, 12)}${esc(m.label)}${k > 1 ? `<b>×${k}</b>` : ""}</span>`;
    }).join("");
  }
  const gs = $("#graphSub");
  if (gs) {
    const nAgents = [...run.nodes.values()].filter((n) => n.type === "agent").length;
    gs.textContent = run.plan ? (run.plan.needs_tools ? plural(nAgents, "agent task") : "no tools needed") : "";
    gs.title = "Click any step for its details";
  }
  const alert = $("#runAlert");
  if (alert) {
    alert.innerHTML = run.status === "cancelled"
      ? `<div class="alert warn"><span class="a-ico">${icon("alert", 16)}</span><div><b>Run stopped</b>${run.error && run.error !== "stopped by the user" ? `: ${esc(run.error)}` : " before it finished."}</div></div>`
      : run.error && run.status !== "running"
        ? `<div class="alert"><span class="a-ico">${icon("alert", 16)}</span><div><b>Run failed:</b> ${esc(run.error)}</div></div>`
        : "";
  }
}
function renderGraphNotes(run) {
  const host = $("#graphNotes");
  if (!host) return;
  host.innerHTML = run.info.map((i) => `<div class="info-note">${icon("info", 14)}${esc(i.message[0].toUpperCase() + i.message.slice(1))} (${fmtSec(i.t)})</div>`).join("");
}
function renderAnswer(run) {
  const host = $("#answer");
  if (!host) return;
  let text = "", streaming = false, sub = "";
  if (run.result && run.status !== "running") text = run.result.answer;
  else if (run.finalAnswer) text = run.finalAnswer;
  else {
    const w = [...run.nodes.values()].filter((n) => (n.type === "writer" || n.type === "direct") && n.text).pop();
    if (w) {
      text = w.text;
      streaming = w.status === "running";
      sub = streaming ? (w.type === "writer" && w.seq > 1 ? "rewriting…" : "writing…") : w.type === "writer" ? "draft, awaiting the fact-check" : "";
    }
  }
  if (run.result && run.status !== "running") {
    const n = (run.result.sources || []).length;
    sub = `${plural(n, "source")} cited${run.result.verifications && run.result.verifications.length ? " · fact-checked" : ""}`;
  }
  const key = `${run.id}|${text.length}|${streaming}|${Object.keys(run.evidence).length}`;
  if (host._key !== key) {
    host._key = key;
    host.innerHTML = text ? md(text, run.evidence) + (streaming ? '<span class="cursor"></span>' : "")
      : run.status === "running" ? '<div class="card-sub">The answer streams in here when the writer starts.</div>'
        : '<div class="card-sub">No answer.</div>';
  }
  $("#answerSub").textContent = sub;
  $("#copyBtn").hidden = !(run.result && run.result.answer);
  const evalBox = $("#evalBox");
  const e = run.eval;
  evalBox.innerHTML = e ? `<div class="eval-box">
      <div class="row"><span class="lbl">Judge score</span><span><span class="score s${e.score}">${e.score}/2</span> ${e.routing_ok === false ? '<span class="badge warn">unexpected routing</span>' : ""}</span></div>
      ${e.reference ? `<div class="row"><span class="lbl">Reference</span><span>${esc(e.reference)}</span></div>` : ""}
      ${e.judge ? `<div class="row"><span class="lbl">Judge's note</span><span style="color:var(--muted)">${esc(e.judge)}</span></div>` : ""}
    </div>` : "";
}
function renderSources(run) {
  const card = $("#sourcesCard"), host = $("#sources");
  if (!card) return;
  const src = run.result && run.status !== "running" ? run.result.sources || [] : [];
  card.hidden = !src.length;
  const key = `${run.id}|${src.length}|${run.status}`;
  if (!src.length || host._key === key) return;
  host._key = key;
  $("#sourcesSub").textContent = plural(src.length, "source");
  const groups = [["doc", "Federal Reserve documents and data tables"], ["web", "Web"], ["data", "Market and economic data"]];
  host.innerHTML = groups.map(([k, label]) => {
    const items = src.filter((s) => s.kind === k);
    if (!items.length) return "";
    return `<div class="src-group"><h4>${label}</h4>${items.map((s) => {
      const ev = run.evidence[s.id] || s;
      const isQuery = ev.meta && ev.meta.unit === "query";
      const url = safeUrl(s.url);
      return `<div class="src" data-eid="${esc(s.id)}">${citeChip(s.id, run.evidence)}<div class="s-main"><div class="s-title">${esc(s.source || s.title)}</div>` +
        `${isQuery ? `<div class="s-sub">SQL: ${esc(trunc((ev.meta.sql || "").replace(/\s+/g, " "), 140))}</div>` : url ? `<div class="s-sub">${esc(url)}</div>` : ""}</div>` +
        `${url ? `<a class="s-link" href="${esc(url)}" target="_blank" rel="noopener" title="Open the source">${icon("ext", 15)}</a>` : ""}</div>`;
    }).join("")}</div>`;
  }).join("");
}
function renderStats(run) {
  const card = $("#statsCard"), host = $("#stats");
  if (!card) return;
  const u = run.result && run.status !== "running" ? run.result.usage : null;
  card.hidden = !u;
  const key = `${run.id}|${run.status}|${u ? 1 : 0}`;
  if (!u || host._key === key) return;
  host._key = key;
  const nEv = Object.keys(run.evidence).length;
  const nCited = (run.result.sources || []).length;
  const tools = Object.entries(u.tool_calls || {}).sort((a, b) => b[1] - a[1]);
  const nTools = tools.reduce((s, [, c]) => s + c, 0);
  const by = Object.entries(u.by_agent || {}).sort((a, b) => b[1].seconds - a[1].seconds);
  const maxS = Math.max(1, ...by.map(([, v]) => v.seconds));
  host.innerHTML = `
    <div class="stats-grid">
      <div class="stat"><div class="v">${fmtSec(run.result.seconds)}</div><div class="l">wall time</div></div>
      <div class="stat"><div class="v">${u.llm_calls}</div><div class="l">LLM calls · ${fmtSec(u.llm_seconds)} total</div></div>
      <div class="stat"><div class="v">${fmtNum(u.prompt_tokens)}</div><div class="l">prompt tokens</div></div>
      <div class="stat"><div class="v">${fmtNum(u.completion_tokens)}</div><div class="l">generated tokens</div></div>
      <div class="stat"><div class="v">${nTools}</div><div class="l">tool calls</div></div>
      <div class="stat"><div class="v">${run.source === "eval" ? nCited : `${nCited}<span style="color:var(--faint);font-weight:500;font-size:13px"> / ${nEv}</span>`}</div><div class="l">${run.source === "eval" ? "sources cited" : "passages cited / gathered"}</div></div>
    </div>
    ${by.length ? `<div class="usage">${by.map(([a, v]) => `
      <div class="u-name" style="--c:var(${agentMeta(a).color})"><span class="n-ico" style="width:20px;height:20px;border-radius:6px">${icon(agentMeta(a).icon, 12)}</span>${esc(agentMeta(a).long || agentMeta(a).label)}</div>
      <div class="u-bar" style="--c:var(${agentMeta(a).color})"><i style="width:${((v.seconds / maxS) * 100).toFixed(1)}%"></i></div>
      <div class="u-num">${plural(v.calls, "call")} · ${fmtNum(v.prompt_tokens)} in · ${fmtNum(v.completion_tokens)} out · ${fmtSec(v.seconds)}</div>`).join("")}</div>` : ""}
    ${tools.length ? `<div class="tool-chips">${tools.map(([t, c]) => `<span class="tool-chip" style="--tc:var(${toolMeta(t).color})">${icon(toolMeta(t).icon, 12)}${esc(t)} ×${c}</span>`).join("")}</div>` : ""}`;
}

// ================================================================ inspector
function argChips(a, skip) {
  const chips = Object.entries(a || {}).filter(([k, v]) => !skip.includes(k) && v != null && v !== "" && !(Array.isArray(v) && !v.length))
    .map(([k, v]) => `<span class="argchip" title="${esc(typeof v === "object" ? JSON.stringify(v) : String(v))}"><b>${esc(k)}</b> ${esc(Array.isArray(v) ? v.join(", ") : typeof v === "object" ? JSON.stringify(v) : String(v))}</span>`).join("");
  return chips ? `<div class="argchips">${chips}</div>` : "";
}
function argsHTML(c) {
  const a = c.args || {};
  const q = (s) => `<span class="qtext">${esc(s || "")}</span>`;
  switch (c.tool) {
    case "query_data": return `<pre class="sql">${highlightSQL(formatSQL(a.sql || ""))}</pre>${argChips(a, ["sql"])}`;
    case "search_fed_documents": case "search_tables": case "web_search": return q(a.query) + argChips(a, ["query"]);
    case "find_documents": return q(a.topic) + argChips(a, ["topic"]);
    case "search_economic_series": return q(a.keywords);
    case "calculator": return `<code>${esc(a.expression || "")}</code>`;
    case "describe_table": return `<code>${esc(a.table || "")}</code>`;
    case "fetch_webpage": {
      const u = safeUrl(a.url);
      return (u ? `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(trunc(u, 90))}</a>` : esc(a.url || "")) + argChips(a, ["url"]);
    }
    case "read_document_pages":
      return `<code>${esc(a.doc_id || "")}</code> · ${a.end_page && a.end_page !== a.start_page ? `pages ${esc(a.start_page)}–${esc(a.end_page)}` : `page ${esc(a.start_page)}`}`;
    default: return argChips(a, []) || '<span class="card-sub">no arguments</span>';
  }
}
function evRow(run, id) {
  const ev = run.evidence[id];
  const label = ev ? ev.source || ev.title : "";
  const missing = run.source === "eval" ? "not cited (the evaluation file keeps cited passages only)"
    : run.status === "running" ? "details arrive with the result" : "no details recorded";
  return `<div class="ev-row" data-eid="${esc(id)}">${citeChip(id, run.evidence)}<span class="ev-t"${label ? "" : ' style="color:var(--faint)"'}>${esc(label || missing)}</span></div>`;
}
function linkIds(escaped, run) {
  return escaped.replace(/\[([DWM]\d+)\]/g, (_, id) => citeChip(id, run.evidence));
}
function resultHTML(run, n, c, key) {
  const out = c.output;
  const first = (out || c.preview || "").split("\n")[0];
  let html = "";
  if (/^ERROR/.test(first)) html = `<div style="color:var(--bad)">${esc(trunc(first, 300))}</div>`;
  else if (c.tool === "query_data" && c.ids.length) {
    const ev = run.evidence[c.ids[0]];
    const table = out ? out.split("\n").slice(1).join("\n") : ev && ev.text ? splitQuery(ev.text)[1] : "";
    html = `<div><span class="arrow">→</span>${citeChip(c.ids[0], run.evidence)} ${esc(first.replace(/^\[[DWM]\d+\]\s*/, ""))}</div>${table ? resultTable(table) : ""}`;
  } else if (c.tool === "calculator") html = `<div><span class="arrow">→</span><code>${esc(trunc(first, 200))}</code></div>`;
  else if (c.ids.length) {
    const ids = c.ids.slice(0, 8);
    html = `<div class="ev-list">${ids.map((id) => evRow(run, id)).join("")}${c.ids.length > 8 ? `<div class="card-sub">+${c.ids.length - 8} more</div>` : ""}</div>`;
  } else {
    // list-like results (documents, tables, outlines): the first few lines say what came back
    const lines = (out || c.preview || "").split("\n").filter((l) => l.trim()).slice(0, 4);
    html = lines.length ? `<div class="res-lines">${lines.map((l, i) => `<div>${i ? "" : '<span class="arrow">→</span>'}${esc(trunc(l.replace(/^- /, "· "), 160))}</div>`).join("")}</div>`
      : '<div style="color:var(--muted)"><span class="arrow">→</span>(empty)</div>';
  }
  const raw = out ?? c.preview ?? "";
  const missing = out == null && c.chars > raw.length ? `\n… ${fmtNum(c.chars)} characters in all; the full output is kept for runs made in the explorer` : "";
  const open = state.openDetails.has(key) ? " open" : "";
  html += `<details class="raw" data-key="${esc(key)}"${open}><summary>${icon("chev", 12)}Raw output · ${fmtNum(c.chars)} chars</summary><pre class="out">${linkIds(esc(raw), run)}${esc(missing)}</pre></details>`;
  return `<div class="call-res">${html}</div>`;
}
function callHTML(run, n, c, i) {
  const tm = toolMeta(c.tool);
  const time = c.t1 != null ? fmtSec(c.t1 - c.t0) : spinner(12);
  const key = `${n.id}#${i}`;
  return `<div class="call"><div class="call-head"><span class="t-dot" style="--tc:var(${tm.color})">${icon(tm.icon, 13)}</span>` +
    `<span class="call-name">${esc(c.tool)}</span>${state.details ? `<span class="call-backend">${esc(toolBackend(c.tool))}</span>` : ""}<span class="call-time">${time}</span></div>` +
    `<div class="call-args">${argsHTML(c)}</div>${c.t1 != null ? resultHTML(run, n, c, key) : ""}</div>`;
}
function agentInspector(run, n) {
  const steps = new Map();
  const get = (s) => steps.get(s) || steps.set(s, { thoughts: [], calls: [] }).get(s);
  n.thoughts.forEach((th) => get(th.step).thoughts.push(th));
  n.calls.forEach((c, i) => get(c.step).calls.push([c, i]));
  const order = [...steps.keys()].sort((a, b) => a - b);
  const deps = (n.deps || []).length ? `<div class="card-sub" style="margin-top:6px">Runs after ${n.deps.map((d) => `<code>${esc(d)}</code>`).join(", ")}, with their findings as context</div>` : "";
  let html = `<div class="sec"><div class="sec-title">Task${n.fallback ? " · web fallback" : n.followUp ? " · follow-up research" : ""}<span class="line"></span></div><div class="box quote" style="--c:var(${agentMeta(n.agent).color})">${esc(n.instruction || "")}</div>${deps}</div>`;
  html += `<div class="sec"><div class="sec-title">Trajectory · ${plural(n.calls.length, "tool call")}<span class="line"></span></div>`;
  if (!order.length) html += `<div class="card-sub">${n.status === "running" ? `${spinner(12)} Choosing the first tool…` : "No tool calls."}</div>`;
  else {
    html += `<div class="traj" style="--c:var(${agentMeta(n.agent).color})">`;
    for (const s of order) {
      const st = steps.get(s);
      const t = st.calls.length ? st.calls[0][0].t0 : st.thoughts[0].t;
      html += `<div class="step"><div class="step-title">Step ${s}${st.calls.length > 1 ? ` · ${st.calls.length} calls in parallel` : ""}<span class="t">+${fmtSec(t - n.t0)}</span></div>`;
      html += st.thoughts.map((th) => `<div class="thought">${esc(th.text)}</div>`).join("");
      html += st.calls.map(([c, i]) => callHTML(run, n, c, i)).join("");
      html += "</div>";
    }
    if (n.status === "running" && n.calls.length && n.calls.every((c) => c.t1 != null)) html += `<div class="step"><div class="step-title">${spinner(12)} Reading the results…</div></div>`;
    html += "</div>";
  }
  html += "</div>";
  if (n.finish) {
    const f = n.finish;
    html += `<div class="sec"><div class="sec-title">Findings<span class="line"></span></div><div class="findings" style="--c:var(${agentMeta(n.agent).color})">
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:6px">${confBadge(f.confidence)}<span class="card-sub">${plural(f.steps || 0, "step")} · ${plural(f.tool_calls || 0, "tool call")} · ${plural((f.evidence || []).length, "passage")} cited</span></div>
      <div class="md small">${md(f.answer || "", run.evidence)}</div>
      ${(f.key_facts || []).length ? `<ul>${f.key_facts.map((k) => `<li>${inlineMd(k, run.evidence)}</li>`).join("")}</ul>` : ""}
      ${f.gaps ? `<div class="gaps">${icon("alert", 14)}<span>${esc(f.gaps)}</span></div>` : ""}</div></div>`;
  }
  if (n.error) html += `<div class="alert"><span class="a-ico">${icon("alert", 16)}</span><div>${esc(n.error)}</div></div>`;
  return html;
}
function plannerInspector(run, n) {
  const p = n.plan;
  if (!p) return `<div class="card-sub">${n.status === "running" ? `${spinner(12)} The planner is reading the question…` : "No plan."}</div>`;
  const u = run.result && run.result.usage && run.result.usage.by_agent ? run.result.usage.by_agent.planner : null;
  let html = `<div class="sec"><div class="kv"><span class="k">Intent</span><span><span class="badge intent">${esc(INTENTS[p.intent] || p.intent)}</span></span>
    <span class="k">Tools needed</span><span>${p.needs_tools ? "yes" : "no: answer directly"}</span>
    ${u ? `<span class="k">LLM</span><span>${fmtNum(u.prompt_tokens)} prompt tokens · ${fmtNum(u.completion_tokens)} generated · ${fmtSec(u.seconds)}</span>` : ""}</div></div>`;
  html += `<div class="sec"><div class="sec-title">Reasoning<span class="line"></span></div><div class="box quote" style="--c:var(--c-planner)">${esc(p.reasoning || "")}</div></div>`;
  if (p.standalone_question && p.standalone_question.trim() !== run.question.trim()) html += `<div class="sec"><div class="sec-title">Question as the agents see it<span class="line"></span></div><div class="box">${esc(p.standalone_question)}</div></div>`;
  const tasks = p.tasks || [];
  if (tasks.length) {
    html += `<div class="sec"><div class="sec-title">${plural(tasks.length, "task")}<span class="line"></span></div><div class="task-list">${tasks.map((t) => {
      const m = agentMeta(t.agent);
      return `<div class="task-row" data-node="task:${esc(t.id)}" style="--c:var(${m.color})"><span class="n-ico">${icon(m.icon, 13)}</span><div class="t-main"><div class="t-top">${esc(m.long || m.label)}<span class="n-tid">${esc(t.id)}</span>${(t.depends_on || []).length ? `<span class="card-sub">after ${esc(t.depends_on.join(", "))}</span>` : ""}</div><div style="color:var(--muted)">${esc(t.instruction)}</div></div></div>`;
    }).join("")}</div></div>`;
  }
  return html;
}
function writerInspector(run, n) {
  const what = n.type === "direct" ? "Answer from general knowledge" : n.task ? n.task[0].toUpperCase() + n.task.slice(1) : "Write the cited answer";
  const note = n.type === "writer" && n.seq > 1 ? (/revise/.test(n.task || "") ? "Rewrites the draft to fix the fact-checker's issues." : "Rewrites the answer with the follow-up research.") : n.type === "writer" ? "Reads every agent's findings and the evidence they cite, then writes the answer." : "";
  return `<div class="sec"><div class="sec-title">${esc(what)}<span class="line"></span></div>${note ? `<div class="card-sub" style="margin-bottom:10px">${esc(note)}</div>` : ""}` +
    (n.text ? `<div class="box"><div class="md small">${md(n.text, run.evidence)}${n.status === "running" ? '<span class="cursor"></span>' : ""}</div></div>` : `<div class="card-sub">${n.status === "running" ? `${spinner(12)} Waiting for the first tokens…` : "Not started."}</div>`) + "</div>";
}
function verifierInspector(run, n) {
  const v = n.verdict;
  if (!v) return `<div class="card-sub">${n.status === "running" ? `${spinner(12)} Checking the draft against the cited evidence…` : "Not started."}</div>`;
  const what = { accept: "The draft is supported by the evidence and goes out as the answer.", revise: "The writer revises the draft to fix the issues below.", research: "The issues need more research: follow-up tasks go back to the agents, then the writer rewrites." }[v.verdict] || "";
  let html = `<div class="sec"><div style="display:flex;gap:10px;align-items:center">${verdictBadge(v)}<span class="card-sub">${esc(what)}</span></div></div>`;
  if ((v.issues || []).length) html += `<div class="sec"><div class="sec-title">${plural(v.issues.length, "issue")}<span class="line"></span></div>${v.issues.map((x, i) => `<div class="issue"><span class="n">${i + 1}</span><span>${inlineMd(x, run.evidence)}</span></div>`).join("")}</div>`;
  if ((v.follow_up_tasks || []).length) html += `<div class="sec"><div class="sec-title">Follow-up tasks<span class="line"></span></div><div class="task-list">${v.follow_up_tasks.map((t) => {
    const m = agentMeta(t.agent);
    return `<div class="task-row" style="--c:var(${m.color})"><span class="n-ico">${icon(m.icon, 13)}</span><div class="t-main"><div class="t-top">${esc(m.long || m.label)}</div><div style="color:var(--muted)">${esc(t.instruction)}</div></div></div>`;
  }).join("")}</div></div>`;
  if (v.error) html += `<div class="alert warn"><span class="a-ico">${icon("alert", 16)}</span><div>${esc(v.error)}</div></div>`;
  return html;
}
function questionInspector(run) {
  const o = run.opts || {};
  let html = `<div class="sec"><div class="box quote" style="--c:var(--c-question);font-size:14px">${esc(run.question)}</div></div>`;
  html += `<div class="sec"><div class="kv">
    <span class="k">Fact-check</span><span>${o.verify === false ? "off" : "on"}</span>
    <span class="k">Thinking mode</span><span>${o.thinking ? "on" : "off"}</span>
    ${run.created ? `<span class="k">Asked</span><span>${esc(new Date(run.created).toLocaleString())}</span>` : ""}
    ${run.id ? `<span class="k">Run id</span><span class="mono" style="font-size:12px">${esc(run.id)}</span>` : ""}</div></div>`;
  if (run.history && run.history.length) html += `<div class="sec"><div class="sec-title">Earlier turns sent to the planner<span class="line"></span></div>${run.history.map((h) => `<div class="box" style="margin-bottom:6px"><b>${esc(h.question)}</b><div class="card-sub" style="margin-top:4px">${esc(trunc(plain(h.answer), 300))}</div></div>`).join("")}</div>`;
  return html;
}
function answerInspector(run) {
  const src = run.result ? run.result.sources || [] : [];
  if (!src.length) return `<div class="card-sub">${run.status === "running" ? "The answer appears when the writer and the fact-checker are done." : "No sources cited."}</div>`;
  return `<div class="sec"><div class="sec-title">${plural(src.length, "cited source")}<span class="line"></span></div><div class="ev-list">${src.map((s) => evRow(run, s.id)).join("")}</div></div>`;
}
function evidenceInspector(run, id) {
  const ev = run.evidence[id] || { id, kind: { D: "doc", W: "web", M: "data" }[id[0]], title: "", source: "", text: "", meta: {} };
  const meta = ev.meta || {};
  const isQuery = meta.unit === "query";
  const kind = isQuery ? "SQL query result" : ev.kind === "doc" ? (meta.table ? "Data table (catalog card)" : "Federal Reserve document") : ev.kind === "web" ? (meta.type === "search" ? "Web search result" : "Web page") : "Market and economic data";
  const cited = run.result && (run.result.sources || []).some((s) => s.id === id);
  const by = [];
  for (const n of run.nodes.values()) n.calls.forEach((c) => { if ((c.ids || []).includes(id)) by.push([n, c]); });
  const url = safeUrl(ev.url);
  let html = `<div class="sec"><div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">${citeChip(id, run.evidence)}<span class="badge muted">${esc(kind)}</span>${run.result ? (cited ? '<span class="badge ok">cited in the answer</span>' : '<span class="badge muted">gathered, not cited</span>') : ""}</div>
    <div style="font-weight:650;margin-top:10px">${esc(ev.source || ev.title || id)}</div>
    ${meta.doc_type || meta.date ? `<div class="card-sub">${esc([meta.doc_type, meta.date, meta.section].filter(Boolean).join(" · "))}</div>` : ""}
    ${url ? `<div style="margin-top:8px"><a href="${esc(url)}" target="_blank" rel="noopener" class="btn small">${icon("ext", 13)}Open the source</a></div>` : ""}</div>`;
  if (isQuery) {
    const [sql, res] = splitQuery(ev.text);
    html += `<div class="sec"><div class="sec-title">Query<span class="line"></span></div><pre class="sql">${highlightSQL(formatSQL(sql || meta.sql || ""))}</pre></div>`;
    if (res) html += `<div class="sec"><div class="sec-title">Result<span class="line"></span></div>${resultTable(res, 200)}</div>`;
    if ((meta.tables || []).length) html += `<div class="sec"><div class="sec-title">Tables<span class="line"></span></div>${meta.tables.map((t) => `<code style="font-size:12px">${esc(t)}</code>`).join(" ")}</div>`;
  } else {
    html += `<div class="sec"><div class="sec-title">${ev.kind === "doc" ? "Passage" : "Text"}${ev.chars > (ev.text || "").length ? ` · first ${fmtNum((ev.text || "").length)} of ${fmtNum(ev.chars)} chars` : ""}<span class="line"></span></div>` +
      (ev.text ? `<div class="ev-text">${esc(ev.text)}</div>` : `<div class="box card-sub">The evaluation file keeps the source list but not the passage text. Runs made in the explorer keep every passage.</div>`) + "</div>";
  }
  if (by.length) html += `<div class="sec"><div class="sec-title">Retrieved by<span class="line"></span></div><div class="task-list">${by.map(([n, c]) => {
    const m = agentMeta(n.agent);
    return `<div class="task-row" data-node="${esc(n.id)}" style="--c:var(${m.color})"><span class="n-ico">${icon(m.icon, 13)}</span><div class="t-main"><div class="t-top">${esc(m.long || m.label)}${n.taskId ? `<span class="n-tid">${esc(n.taskId)}</span>` : ""}</div><div style="color:var(--muted)"><code>${esc(c.tool)}</code> · step ${c.step}</div></div></div>`;
  }).join("")}</div></div>`;
  return html;
}
function demoInspector(id) {
  const agent = id.replace(/^demo:/, "").replace(/:.*/, "").replace("writer", "synthesizer");
  const m = agentMeta(agent);
  const tools = state.info ? (state.info.agents.find((a) => a.name === agent) || {}).tools || [] : [];
  return [`<div class="sec"><div class="box quote" style="--c:var(${m.color})">${esc(ABOUT[agent] || "")}</div></div>`,
    tools.length ? `<div class="sec"><div class="sec-title">Tools<span class="line"></span></div><div class="tools" style="display:flex;flex-wrap:wrap;gap:6px">${tools.map((t) => `<span class="tool-chip" style="--tc:var(${toolMeta(t).color})">${icon(toolMeta(t).icon, 12)}${esc(t)}</span>`).join("")}</div></div>` : ""].join("");
}
function legendInspector() {
  const items = [["planner", "Planner", "routes the question and writes one task per agent."], ["fed_research", "Fed research", "searches and reads the 391 documents."],
    ["data_analyst", "Data analyst", "queries the 846 SQL tables."], ["web_research", "Web research", "searches and reads the open web."],
    ["market_data", "Market data", "FRED, ECB and market prices."], ["synthesizer", "Writer", "writes the cited answer."], ["verifier", "Fact-checker", "accepts, revises or asks for more research."]];
  return `<div class="insp-empty"><div class="big">${icon("layers", 30)}</div>Run a question or open a saved run, then click any step of the pipeline to see what it did.
    <div class="legend-list">${items.map(([a, l, d]) => `<div class="legend-item"><span class="n-ico" style="--c:var(${agentMeta(a).color});width:24px;height:24px;border-radius:7px;color:var(--c);background:color-mix(in srgb, var(--c) var(--mix), transparent);display:grid;place-items:center">${icon(agentMeta(a).icon, 13)}</span><span><b>${l}</b> ${d}</span></div>`).join("")}
    <div class="legend-item"><span style="display:flex;gap:3px;flex-direction:column">${citeChip("D1")}</span><span><b>D</b> Fed documents and SQL results, <b>W</b> web pages, <b>M</b> market data: click any citation to read the passage.</span></div></div></div>`;
}
function inspHead(iconName, color, title, sub) {
  return `<span class="n-ico" style="--c:var(${color});color:var(--c);background:color-mix(in srgb, var(--c) var(--mix), transparent);display:grid;place-items:center">${icon(iconName, 17)}</span>` +
    `<div style="min-width:0;flex:1"><div class="insp-title">${title}</div><div class="insp-sub">${sub}</div></div>` +
    `<button class="icon-btn insp-close" data-act="close-insp" title="Close">${icon("x", 16)}</button>`;
}
function renderInspector() {
  const head = $("#inspHead"), body = $("#inspBody");
  const run = state.run;
  let key, sig, h, b;
  if (!run) {
    if (state.demoSel) {
      const id = state.demoSel;
      const agent = id.replace(/^demo:/, "").replace(/:.*/, "").replace("writer", "synthesizer");
      const m = agentMeta(agent);
      key = sig = "demo|" + id;
      h = inspHead(m.icon, m.color, esc(m.long || m.label), "pipeline step");
      b = demoInspector(id);
    } else {
      key = sig = "legend|" + (state.info ? 1 : 0);
      h = inspHead("info", "--c-orch", "Inspector", "details of the selected step");
      b = legendInspector();
    }
  } else if (state.evView) {
    const id = state.evView;
    const ev = run.evidence[id];
    key = `${run.id}|ev|${id}`;
    sig = `${key}|${ev ? (ev.text || "").length : 0}|${run.status}|${[...run.nodes.values()].reduce((s, n) => s + n.calls.length, 0)}`;
    const back = state.selected && run.nodes.get(state.selected) ? `<button class="back" data-act="back">${icon("left", 13)}Back to ${esc(nodeTitle(run.nodes.get(state.selected)))}</button>` : "";
    h = inspHead("file", ev && ev.kind === "web" ? "--c-web" : ev && ev.kind === "data" ? "--c-market" : "--c-fed", `Evidence ${esc(id)}`, ev ? esc(trunc(ev.title || ev.source, 80)) : "");
    b = back + evidenceInspector(run, id);
  } else {
    const n = run.nodes.get(state.selected) || run.nodes.get("planner") || run.nodes.get("question");
    const m = agentMeta(n.agent);
    key = `${run.id}|${n.id}`;
    sig = `${key}|${n.v}|${n.status}|${Object.keys(run.evidence).length}|${run.result ? 1 : 0}|${state.follow}|${run.status}|${state.details}`;
    const status = { pending: "waiting", running: "running", done: "done", error: "failed", stopped: "stopped" }[n.status] || n.status;
    const dur = n.t0 != null && !["question", "answer"].includes(n.type) ? ` · ${fmtSec((n.t1 ?? runNow(run)) - n.t0)} · started +${fmtSec(n.t0)}` : "";
    const followBtn = run.status === "running" ? (state.follow ? '<span class="follow"><span class="live-dot"></span>following live</span>' : `<button class="follow" data-act="follow">${icon("play", 10)}follow live</button>`) : "";
    h = inspHead(m.icon, m.color, `${esc(n.type === "agent" ? m.long || m.label : nodeTitle(n))}${n.taskId ? ` <span class="n-tid">${esc(n.taskId)}</span>` : ""}`, `<span>${status}${dur}</span>${followBtn}`);
    b = n.type === "agent" ? agentInspector(run, n) : n.type === "planner" ? plannerInspector(run, n) : n.type === "writer" || n.type === "direct" ? writerInspector(run, n)
      : n.type === "verifier" ? verifierInspector(run, n) : n.type === "question" ? questionInspector(run) : answerInspector(run);
  }
  if (body._sig === sig) return;
  const sameKey = body._key === key;
  const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 60;
  const top = body.scrollTop;
  head.innerHTML = h;
  body.innerHTML = b;
  body._sig = sig;
  body._key = key;
  if (!sameKey) body.scrollTop = 0;
  else body.scrollTop = run && run.status === "running" && state.follow && atBottom ? body.scrollHeight : top;
}

// ================================================================ welcome view
function demoRun() {
  const run = newRun("", {});
  run.source = "demo";
  run.status = "idle";
  run.nodes.get("question").status = "idle";
  const add = (id, props, row) => { addNode(run, id, Object.assign({ status: "idle" }, props)); addRow(run, row || id, [id]); };
  add("demo:planner", { type: "planner", agent: "planner" });
  SPECIALISTS.forEach((a) => addNode(run, "demo:" + a, { type: "agent", agent: a, status: "idle", instruction: ABOUT[a] }));
  addRow(run, "agents", SPECIALISTS.map((a) => "demo:" + a));
  add("demo:writer", { type: "writer", agent: "synthesizer" });
  add("demo:verifier", { type: "verifier", agent: "verifier" });
  add("demo:answer", { type: "answer", agent: "answer" });
  return run;
}
function renderWelcome() {
  const info = state.info;
  const fmts = info ? Object.entries(info.formats).map(([f, c]) => `<span class="chip"><b>${c}</b> ${{ pdf: "PDF", html: "web pages", csv: "CSV", xlsx: "Excel", docx: "Word" }[f] || f}</span>`).join("") : "";
  $("#view").innerHTML = `
    <section class="card hero">
      <h1>Ask the Federal Reserve collection</h1>
      <p>A planner reads your question and summons specialist agents. They search ${info ? info.docs : 391} Fed documents, query ${info ? info.tables : 846} tables with SQL, read the web and pull live market data. You see every step as it happens, from the plan to the fact-checked answer with its citations.</p>
    </section>
    <section class="card">
      <div class="card-head"><span class="card-title">How a question flows</span><span class="card-sub">the planner summons only the agents a question needs; click a step</span></div>
      <div class="graph-wrap scroll"><div class="graph" id="demoGraph"></div></div>
    </section>
    <section class="agent-cards">${(info ? info.agents : []).map((a) => {
      const m = agentMeta(a.name);
      return `<div class="agent-card" style="--c:var(${m.color})"><div class="a-head"><span class="n-ico">${icon(m.icon, 15)}</span>${esc(a.label)}</div><p>${esc(a.about)}</p><div class="tools">${a.tools.map((t) => `<span class="tool-chip" style="--tc:var(${toolMeta(t).color})">${icon(toolMeta(t).icon, 11)}${esc(t)}</span>`).join("")}</div></div>`;
    }).join("")}</section>
    ${info ? `<section class="card"><div class="card-head"><span class="card-title">The collection</span></div><div class="card-body facts">
      <span class="chip"><b>${info.docs}</b> documents</span>${fmts}<span class="chip"><b>${info.tables}</b> SQL tables + <b>${info.views}</b> views</span>
      <span class="chip"><b>${fmtNum(info.chunks)}</b> indexed passages</span><span class="chip">model <b>${esc(info.model)}</b></span></div></section>` : ""}`;
  const host = $("#demoGraph");
  renderGraph(state.demo || (state.demo = demoRun()), host, state.demoSel);
}

// ================================================================ main render
let raf = 0;
function scheduleRender() {
  if (!raf) raf = requestAnimationFrame(() => { raf = 0; render(); });
}
function render() {
  const run = state.run;
  if (run && run.status === "running" && state.follow && run.lastStarted && run.nodes.has(run.lastStarted)) state.selected = run.lastStarted;
  if (run) {
    renderRunHead(run);
    renderGraph(run, $("#graph"), state.evView ? null : state.selected);
    renderGraphNotes(run);
    renderGantt(run);
    renderAnswer(run);
    renderSources(run);
    renderStats(run);
  } else if (state.demo && $("#demoGraph")) renderGraph(state.demo, $("#demoGraph"), state.demoSel);
  renderInspector();
  renderComposer();
}
function showRun(run, opts = {}) {
  state.run = run;
  state.evView = null;
  state.follow = run.status === "running";
  state.selected = opts.select || (run.status === "running" ? null : run.nodes.has("planner") ? "planner" : "question");
  state.openDetails.clear();
  $("#view").innerHTML = viewHTML();
  if (run.id && run.source !== "live") setRunHash(run.id);
  renderSidebar();
  render();
  $("#main").scrollTop = 0;
}
function showWelcome() {
  stopReplay();
  state.run = null;
  state.evView = null;
  state.demoSel = null;
  history.replaceState(null, "", location.pathname);
  renderWelcome();
  renderSidebar();
  render();
}
function updateTimes() {
  const run = state.run;
  if (!run || run.status !== "running") return;
  const now = runNow(run);
  const el = $("#runElapsed");
  if (el) el.textContent = fmtSec(now);
  document.querySelectorAll("#graph .n-time.live").forEach((s) => {
    const n = run.nodes.get(s.dataset.node);
    if (n && n.t0 != null) s.textContent = fmtSec(now - n.t0);
  });
}
setInterval(updateTimes, 200);
setInterval(() => { if (state.run && state.run.status === "running") renderGantt(state.run); }, 500);

// ================================================================ live runs
function renderComposer() {
  const btn = $("#runBtn");
  const running = !!state.live;
  btn.innerHTML = running ? `${icon("stop", 13)}Stop` : `${icon("play", 13)}Run`;
  btn.classList.toggle("danger", running);
  btn.classList.toggle("primary", !running);
  const run = state.run;
  const canFollow = run && run.status === "done" && run.result && run.result.answer && !state.live;
  $("#followWrap").hidden = !canFollow;
  if (!canFollow) $("#optFollow").checked = false;
}
function composerAlert(html, kind) {
  $("#composerAlert").innerHTML = html ? `<div class="alert${kind ? " " + kind : ""}"><span class="a-ico">${icon("alert", 16)}</span><div>${html}</div></div>` : "";
}
const START_HINT = 'Start it with <code>.venv/bin/python scripts/colab_up.py</code> (13–20 minutes on a fresh VM); the explorer picks up the new address from <code>.env</code> by itself. Saved runs and the evaluation runs open without it.';
async function startLive(question) {
  question = (question || "").trim();
  if (!question || state.live) return;
  stopReplay();
  composerAlert("");
  const opts = { verify: $("#optVerify").checked, thinking: $("#optThinking").checked };
  const prev = state.run;
  const turns = $("#optFollow").checked && prev && prev.result ? [...(prev.history || []), { question: prev.question, answer: prev.result.answer }].slice(-3) : [];
  const run = newRun(question, opts);
  run.history = turns;
  run.created = new Date().toISOString();
  const ctrl = new AbortController();
  state.live = { run, ctrl };
  showRun(run);
  let resp;
  try {
    resp = await fetch("/api/run", { method: "POST", headers: { "Content-Type": "application/json", "X-FedRAG": "1" }, body: JSON.stringify({ question, ...opts, history: turns }), signal: ctrl.signal });
  } catch (e) {
    return liveFailed(run, "The explorer server is not reachable: " + e.message);
  }
  if (!resp.ok) {
    const detail = (await resp.json().catch(() => ({}))).detail || resp.statusText;
    return liveFailed(run, resp.status === 503 ? `The GPU backend is not available (${esc(trunc(detail, 160))}). ${START_HINT}` : esc(detail), true);
  }
  const reader = resp.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let k;
      while ((k = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, k);
        buf = buf.slice(k + 1);
        if (line.trim()) onMessage(run, JSON.parse(line));
      }
    }
  } catch (e) {
    if (run.status === "running") { finishRun(run, null, "cancelled"); run.error = ctrl.signal.aborted ? "stopped by the user" : "connection lost: " + e.message; }
  }
  if (run.status === "running") { finishRun(run, null, "error"); run.error = run.error || "the connection closed before the run finished"; }
  state.live = null;
  if (state.run === run && run.id) setRunHash(run.id);
  loadRuns();
  scheduleRender();
}
function setRunHash(id) {
  if (location.hash !== "#run=" + id) history.replaceState(null, "", "#run=" + id);
}
function liveFailed(run, html, isHtml) {
  state.live = null;
  composerAlert(isHtml ? html : esc(html));
  showWelcome();
}
function onMessage(run, m) {
  if (m.type === "start") { run.id = m.id; run.created = m.created; renderSidebar(); }
  else if (m.type === "event") {
    if (!run.synced) { run.clock0 = performance.now() - m.t * 1000; run.synced = true; }
    applyEvent(run, { t: m.t, agent: m.agent, type: m.kind, data: m.data });
  } else if (m.type === "evidence") for (const it of m.items) run.evidence[it.id] = it;
  else if (m.type === "done") finishRun(run, m.result, "done");
  else if (m.type === "error") { finishRun(run, null, m.cancelled ? "cancelled" : "error"); run.error = m.error; }
  if (state.run === run) scheduleRender();
}
async function stopLive() {
  const L = state.live;
  if (!L) return;
  if (L.run.id) { try { await api.send(`/api/runs/${encodeURIComponent(L.run.id)}/cancel`, "POST"); } catch (e) { /* already finished */ } }
  setTimeout(() => { if (state.live === L) L.ctrl.abort(); }, 1500);
}

// ================================================================ saved runs and replay
async function openRun(id, opts = {}) {
  stopReplay();
  if (state.live && state.live.run.id === id) {
    showRun(state.live.run);
    closeDrawers();
    return;
  }
  try {
    const saved = await api.get(`/api/runs/${encodeURIComponent(id)}`);
    const run = runFromSaved(saved);
    composerAlert("");
    showRun(run);
    if (opts.replay) startReplay(saved);
  } catch (e) {
    toast("Could not open the run: " + e.message);
    showWelcome();
  }
  closeDrawers();
}
function startReplay(saved) {
  stopReplay();
  const run = newRun(saved.question, saved.options || {});
  Object.assign(run, { id: saved.id, source: saved.source || "ui", eval: saved.eval || null, history: saved.history || [], created: saved.created });
  run.evidence = clone(saved.evidence || {});
  const events = (saved.events || []).slice();
  const streams = [];
  events.forEach((ev, i) => {
    if (ev.type === "agent_start" && (ev.agent === "synthesizer" || ev.agent === "direct")) {
      const end = events.slice(i + 1).find((e) => e.agent === ev.agent && (e.type === "synthesis" || e.type === "final"));
      if (end) streams.push({ agent: ev.agent, t0: ev.t, t1: end.t, text: (end.data && end.data.answer) || "" });
    }
  });
  state.replay = { run, saved, events, streams, i: 0, start: performance.now(), speed: state.speed, now: 0 };
  showRun(run);
  state.replay.timer = setInterval(tickReplay, 40);
}
function tickReplay() {
  const R = state.replay;
  if (!R) return;
  R.now = ((performance.now() - R.start) / 1000) * R.speed;
  while (R.i < R.events.length && R.events[R.i].t <= R.now) applyEvent(R.run, R.events[R.i++]);
  for (const s of R.streams) {
    if (R.now < s.t0 || R.now >= s.t1) continue;
    const n = s.agent === "direct" ? R.run.nodes.get("direct") : R.run.nodes.get("writer:" + R.run.writers);
    if (n && n.status === "running") {
      const len = Math.floor(s.text.length * Math.min(1, (R.now - s.t0) / Math.max(0.01, s.t1 - s.t0)));
      if (len !== (n.text || "").length) { n.text = s.text.slice(0, len); n.v++; }
    }
  }
  if (R.i >= R.events.length) {
    clearInterval(R.timer);
    finishRun(R.run, R.saved.result, R.saved.status === "done" ? "done" : "error");
    R.run.error = R.saved.error;
    state.replay = null;
  }
  if (state.run === R.run) scheduleRender();
}
function stopReplay(restore = false) {
  const R = state.replay;
  if (!R) return;
  clearInterval(R.timer);
  state.replay = null;
  if (restore && state.run === R.run) showRun(runFromSaved(R.saved));
}
async function loadRuns() {
  try {
    const r = await api.get("/api/runs");
    state.runs = r.runs;
    state.evalRuns = r.eval;
    renderSidebar();
  } catch (e) { /* server restarting */ }
}

// ================================================================ sidebar
function agentDots(agents) {
  return `<span class="agent-dots">${(agents || []).map((a) => `<span class="agent-dot" style="--c:var(${agentMeta(a).color})" title="${esc(agentMeta(a).long || agentMeta(a).label)}">${icon(agentMeta(a).icon, 11)}</span>`).join("")}</span>`;
}
function runItem(r) {
  const active = state.run && state.run.id === r.id ? " active" : "";
  const st = r.status === "done" ? "" : `<span class="badge ${r.status === "running" ? "info" : "bad"}">${esc(r.status)}</span>`;
  const score = r.score != null ? `<span class="score s${r.score}">${r.score}/2</span>` : "";
  return `<div class="run-item${active}" data-run="${esc(r.id)}" role="button" tabindex="0"><div class="q">${esc(r.question)}</div>` +
    `<div class="meta">${score}${agentDots(r.agents)}${r.seconds != null ? `<span>${fmtSec(r.seconds)}</span>` : ""}${r.source === "ui" ? `<span>· ${esc(timeAgo(r.created))}</span>` : ""}${st}</div>` +
    (r.source === "ui" ? `<button class="del" data-del="${esc(r.id)}" title="Delete this run">${icon("trash", 13)}</button>` : "") + "</div>";
}
function recordedRun(question) {
  const q = question.trim().toLowerCase();
  return state.runs.find((r) => r.status === "done" && r.question.trim().toLowerCase() === q) ||
    state.evalRuns.find((r) => r.question.trim().toLowerCase() === q) || null;
}
function renderSidebar() {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === state.tab));
  $("#histCount").textContent = state.runs.length || "";
  $("#evalCount").textContent = state.evalRuns.length || "";
  const body = $("#sideBody");
  let html = "";
  if (state.tab === "examples") {
    const ex = state.info ? state.info.examples : [];
    const group = (lvl, title, sub) => `<div class="group-title"><h3>${title}</h3><span>${sub}</span></div>` +
      ex.filter((e) => e.level === lvl).map((e) => {
        const rec = recordedRun(e.question);
        return `<div class="ex ${lvl}" role="button" tabindex="0" data-q="${esc(e.question)}"><span class="tag">${esc(e.tag)}</span><div class="q">${esc(e.question)}</div>` +
          `<button class="go" data-go="${esc(e.question)}" title="Run it now">${icon("play", 12)}</button>` +
          (rec ? `<button class="go rec" data-run="${esc(rec.id)}" title="Open a recorded run of this question (no GPU needed)">${icon("clock", 13)}</button>` : "") + "</div>";
      }).join("");
    html = ex.length ? group("simple", "Simple", "one agent, one source") + group("complex", "Complex", "several agents, documents, data and live sources") : '<div class="empty">Loading…</div>';
  } else if (state.tab === "history") {
    const live = state.live && state.live.run.id ? [{ id: state.live.run.id, source: "ui", question: state.live.run.question, created: state.live.run.created, status: "running",
      agents: [...new Set([...state.live.run.nodes.values()].filter((n) => n.type === "agent" && n.status !== "pending").map((n) => n.agent))] }] : [];
    const items = live.concat(state.runs.filter((r) => !live.length || r.id !== live[0].id));
    html = items.length ? items.map(runItem).join("") : '<div class="empty">Runs you make here are saved and listed here. Open one to see its pipeline again or replay it.</div>';
  } else {
    const byCat = {};
    state.evalRuns.forEach((r) => (byCat[r.category] = byCat[r.category] || []).push(r));
    const n2 = state.evalRuns.filter((r) => r.score === 2).length;
    html = state.evalRuns.length ? `<div class="card-sub" style="margin:10px 4px 0">The ${state.evalRuns.length} benchmark questions of the final evaluation (run A): ${n2} answered fully correctly. Open one to see how the pipeline handled it.</div>` +
      Object.entries(byCat).map(([c, rs]) => `<div class="cat-title">${esc(CATEGORIES[c] || c)}</div>${rs.map(runItem).join("")}`).join("") : '<div class="empty">No evaluation results found.</div>';
  }
  if (body._html !== html) {
    body._html = html;
    body.innerHTML = html;
  }
}

// ================================================================ status, theme, toasts, tooltips
async function pollHealth() {
  try { state.health = await api.get("/api/health"); } catch (e) { state.health = { status: "unreachable", detail: "the explorer server is not responding" }; }
  const h = state.health;
  const pill = $("#statusPill");
  pill.dataset.status = h.status === "unreachable" ? "offline" : h.status;
  $("#statusText").textContent = { online: "GPU online", starting: "GPU starting", offline: "GPU offline", unconfigured: "No GPU configured", unreachable: "Server offline" }[h.status] || h.status;
}
function statusPopover() {
  closePopover();
  const h = state.health || { status: "checking" };
  const d = h.detail || {};
  let body;
  if (h.status === "online") body = `<h4>GPU backend online</h4><p>Model <b>${esc(h.model)}</b>, embedder and reranker on the Colab A100.</p>${d.idle_seconds != null ? `<p>Last request ${fmtSec(d.idle_seconds)} ago. The keep-alive lets the VM go after 60 idle minutes; <code>scripts/colab_up.py --stop</code> releases it now.</p>` : ""}`;
  else if (h.status === "starting") body = `<h4>GPU backend starting</h4><p>The gateway answers but the language model is still loading.</p>`;
  else body = `<h4>${h.status === "unreachable" ? "Explorer server offline" : "GPU backend offline"}</h4><p>${h.status === "unreachable" ? "Restart it with <code>.venv/bin/python -m fedrag.ui.app</code>." : START_HINT}</p>${typeof d === "string" ? `<p style="font-size:12px">${esc(trunc(d, 200))}</p>` : ""}`;
  const pop = document.createElement("div");
  pop.className = "popover";
  pop.id = "popover";
  pop.innerHTML = body;
  document.body.appendChild(pop);
  const r = $("#statusPill").getBoundingClientRect();
  pop.style.top = r.bottom + 8 + "px";
  pop.style.left = Math.max(12, Math.min(window.innerWidth - 372, r.right - 360)) + "px";
}
function closePopover() { const p = $("#popover"); if (p) p.remove(); }
function setTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("fedrag-theme", t); } catch (e) { /* private mode */ }
  $("#themeBtn").innerHTML = icon(t === "dark" ? "sun" : "moon", 16);
  requestAnimationFrame(() => { if (state.run) drawEdges(state.run, $("#graph")); });
}
let toastTimer = 0;
function toast(msg) {
  let t = $("#toast");
  if (!t) { t = document.createElement("div"); t.id = "toast"; t.className = "toast"; document.body.appendChild(t); }
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 2200);
}
let tipEl = null;
function showTip(target) {
  const run = state.run;
  const id = target.dataset.eid;
  if (!run || !id) return;
  const ev = run.evidence[id];
  if (!tipEl) { tipEl = document.createElement("div"); tipEl.className = "tip"; document.body.appendChild(tipEl); }
  const isQuery = ev && ev.meta && ev.meta.unit === "query";
  const kind = !ev ? "Evidence" : isQuery ? "SQL query result" : ev.kind === "doc" ? "Fed document" : ev.kind === "web" ? "Web" : "Market data";
  const text = ev ? (isQuery ? splitQuery(ev.text)[0] : ev.text) : "";
  tipEl.innerHTML = `<div class="tip-k">${esc(id)} · ${esc(kind)}</div><div class="tip-t">${esc(ev ? ev.source || ev.title : "Details arrive with the tool result")}</div>${text ? `<div class="tip-x">${esc(trunc(text, 420))}</div>` : ""}`;
  tipEl.hidden = false;
  const r = target.getBoundingClientRect();
  const w = Math.min(380, window.innerWidth - 24);
  tipEl.style.left = Math.max(12, Math.min(window.innerWidth - w - 12, r.left - 20)) + "px";
  const below = r.bottom + 8;
  tipEl.style.top = below + "px";
  const th = tipEl.getBoundingClientRect().height;
  if (below + th > window.innerHeight - 10) tipEl.style.top = Math.max(10, r.top - th - 8) + "px";
}
function hideTip() { if (tipEl) tipEl.hidden = true; }

// ================================================================ interactions
function openInspectorDrawer() { $("#inspector").classList.add("open"); }
function closeDrawers() {
  $("#inspector").classList.remove("open");
  $("#sidebar").classList.remove("open");
}
function selectNode(id) {
  if (!state.run) {
    state.demoSel = id;
    render();
    openInspectorDrawer();
    return;
  }
  if (id === "answer") $("#answerCard") && $("#answerCard").scrollIntoView({ behavior: "smooth", block: "start" });
  state.selected = id;
  state.evView = null;
  if (state.run.status === "running") state.follow = false;
  render();
  openInspectorDrawer();
}
function openEvidence(id) {
  if (!state.run) return;
  state.evView = id;
  if (state.run.status === "running") state.follow = false;
  hideTip();
  render();
  openInspectorDrawer();
}
function autoGrow() {
  const q = $("#q");
  q.style.height = "auto";
  q.style.height = Math.min(180, q.scrollHeight) + "px";
}
function fillQuestion(q) {
  const box = $("#q");
  box.value = q;
  autoGrow();
  box.focus();
  closeDrawers();
}

document.addEventListener("click", (e) => {
  const t = e.target;
  if (!t.closest("#popover") && !t.closest("#statusPill")) closePopover();
  let el;
  if ((el = t.closest("[data-go]"))) { e.stopPropagation(); fillQuestion(el.dataset.go); startLive(el.dataset.go); return; }
  if ((el = t.closest("[data-del]"))) {
    e.stopPropagation();
    const id = el.dataset.del;
    api.send(`/api/runs/${encodeURIComponent(id)}`, "DELETE").then(() => {
      if (state.run && state.run.id === id) showWelcome();
      loadRuns();
    }).catch((err) => toast(err.message));
    return;
  }
  if ((el = t.closest("[data-eid]")) && !t.closest("a")) { openEvidence(el.dataset.eid); return; }
  if ((el = t.closest("[data-node]")) && !t.closest("a")) { selectNode(el.dataset.node); return; }
  if ((el = t.closest("[data-run]"))) { openRun(el.dataset.run); return; }
  if ((el = t.closest("[data-q]"))) { fillQuestion(el.dataset.q); return; }
  if ((el = t.closest("[data-speed]"))) {
    state.speed = +el.dataset.speed;
    const R = state.replay;
    if (R) { R.start = performance.now() - (R.now * 1000) / state.speed; R.speed = state.speed; }
    scheduleRender();
    return;
  }
  if ((el = t.closest("[data-tab]"))) { state.tab = el.dataset.tab; renderSidebar(); return; }
  if ((el = t.closest("[data-act]"))) {
    const act = el.dataset.act;
    if (act === "stop") stopLive();
    else if (act === "replay" && state.run && state.run.id) {
      const id = state.run.id;
      api.get(`/api/runs/${encodeURIComponent(id)}`).then((saved) => startReplay(saved)).catch((err) => toast(err.message));
    } else if (act === "stop-replay") stopReplay(true);
    else if (act === "follow") { state.follow = true; state.evView = null; render(); }
    else if (act === "back") { state.evView = null; render(); }
    else if (act === "close-insp") closeDrawers();
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closeDrawers(); closePopover(); hideTip(); }
  if (e.key === "Enter" && e.target.matches("[data-node], [data-run], [data-q]")) e.target.click();
  if (e.key === "/" && document.activeElement && !/TEXTAREA|INPUT/.test(document.activeElement.tagName)) { e.preventDefault(); $("#q").focus(); }
});
document.addEventListener("mouseover", (e) => {
  const el = e.target.closest(".cite[data-eid], .ev-row[data-eid]");
  if (el) showTip(el.classList.contains("ev-row") ? el.querySelector(".cite") || el : el);
});
document.addEventListener("mouseout", (e) => {
  const el = e.target.closest(".cite[data-eid], .ev-row[data-eid]");
  if (el && !el.contains(e.relatedTarget)) hideTip();
});
document.addEventListener("toggle", (e) => {
  const d = e.target;
  if (d.matches && d.matches("details.raw")) (d.open ? state.openDetails.add(d.dataset.key) : state.openDetails.delete(d.dataset.key));
}, true);
$("#main").addEventListener("scroll", hideTip, { passive: true });
$("#q").addEventListener("input", autoGrow);
$("#q").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); startLive($("#q").value); }
});
$("#runBtn").addEventListener("click", () => (state.live ? stopLive() : startLive($("#q").value)));
$("#view").addEventListener("click", (e) => {
  if (e.target.closest("#copyBtn") && state.run && state.run.result) {
    navigator.clipboard.writeText(state.run.result.answer).then(() => toast("Answer copied"), () => toast("Copy failed"));
  }
});
$("#statusPill").addEventListener("click", (e) => { e.stopPropagation(); $("#popover") ? closePopover() : statusPopover(); });
$("#themeBtn").addEventListener("click", () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
$("#optDetails").addEventListener("change", (e) => {
  state.details = e.target.checked;
  try { localStorage.setItem("fedrag-details", state.details ? "1" : "0"); } catch (err) { /* private mode */ }
  $("#inspBody")._sig = null;
  render();
});
$("#menuBtn").addEventListener("click", () => $("#sidebar").classList.toggle("open"));
$(".brand").addEventListener("click", () => { if (!state.live) showWelcome(); });
$(".brand").style.cursor = "pointer";
window.addEventListener("resize", () => { if (state.run) drawEdges(state.run, $("#graph")); else if (state.demo) drawEdges(state.demo, $("#demoGraph")); });
window.addEventListener("hashchange", () => {
  const m = location.hash.match(/^#run=([\w-]+)$/);
  if (m && (!state.run || state.run.id !== m[1]) && !state.live) openRun(m[1]);
});

// ================================================================ start
(async function init() {
  $("#menuBtn").innerHTML = icon("menu", 17);
  $("#brandMark").innerHTML = icon("fed", 18);
  setTheme(document.documentElement.dataset.theme || "light");
  $("#optDetails").checked = state.details;
  renderComposer();
  try {
    state.info = await api.get("/api/info");
    const f = state.info.formats;
    $("#corpusPill").textContent = `${state.info.docs} documents · ${Object.keys(f).length} formats · ${state.info.tables} SQL tables`;
  } catch (e) {
    $("#corpusPill").textContent = "Collection not loaded";
  }
  pollHealth();
  setInterval(pollHealth, 15000);
  await loadRuns();
  const m = location.hash.match(/^#run=([\w-]+)$/);
  if (m) openRun(m[1]);
  else showWelcome();
})();
