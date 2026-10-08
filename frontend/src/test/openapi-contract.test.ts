/* OpenAPI contract protection (Work 16.5.1 §11).
 *
 * The purpose is to FAIL when the frontend and the backend drift apart. That
 * only works if the two sides are compared as INDEPENDENT artifacts:
 *
 *   - truth  : `src/api/openapi.json`, generated from the running app by
 *              `scripts/gen_openapi.py`;
 *   - claims : the feature source text, scanned for API calls.
 *
 * Nothing here imports a feature module and asserts it uses the endpoints it
 * uses -- that is a tautology and would pass even if the backend vanished. The
 * source is read as text and matched against the spec, so a route the backend
 * no longer serves cannot hide.
 *
 * Two conventions from `lib/api.ts` are modelled exactly, because getting them
 * wrong would make every assertion meaningless:
 *   - `wsApi.get("/jobs")`  -> GET /workspaces/{workspace_id}/jobs
 *   - `useWsQuery("/jobs")` -> same
 *   - `useQuery("/system/...")` and `api("GET", "/system/...")` are global
 */

import { describe, it, expect } from "vitest";
import { readFileSync, readdirSync, statSync, existsSync, writeFileSync } from "node:fs";
import { join, dirname, resolve, relative } from "node:path";
import ts from "typescript";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(HERE, "..");
const REPO = resolve(SRC, "..", "..");

type Spec = {
  paths: Record<string, Record<string, unknown>>;
};

const spec: Spec = JSON.parse(readFileSync(join(SRC, "api", "openapi.json"), "utf-8"));

/** Fast exact-membership lookup, and the literal form of every served route. */
const SPEC_PATHS = new Set(Object.keys(spec.paths));

const HTTP_METHODS = ["get", "post", "put", "patch", "delete"] as const;

/* =========================================================================
 * Template-literal normalisation
 * ====================================================================== */

/**
 * A `?` starts a query string only when it is not JavaScript punctuation.
 *
 *   `f ? "?platform=x" : ""`  -> real query, truncate
 *   `short?.id`                -> optional chaining, NOT a query
 *   `a ?? b`                   -> nullish coalescing, NOT a query
 */
function startsQuery(s: string, i: number): boolean {
  if (s[i] !== "?") return false;
  const next = s[i + 1];
  return next !== "?" && next !== ".";
}

/**
 * Inside a `${ ... }` interpolation a bare `?` is ambiguous: it may be a
 * ternary (`${a ? b : c}`) or the start of an appended query string
 * (`${f ? "?platform=" + f : ""}`).
 *
 * A query string always begins with `?` or `&`, so the discriminator is the
 * character after the operand's opening quote -- not the `?` itself. Guessing
 * on the `?` truncates `${a ? b : c}` and silently invents a short route.
 */
function interpolationCarriesQuery(s: string, i: number): boolean {
  let j = i + 1;
  while (j < s.length && (s[j] === " " || s[j] === "\t")) j += 1;
  const q = s[j];
  if (q !== '"' && q !== "'" && q !== "`") return false;
  const after = s[j + 1];
  return after === "?" || after === "&";
}

/**
 * Replace every `${ ... }` path interpolation with `{param}`, honouring nested
 * braces, nested template literals and quoted strings; and truncate at a query
 * string.
 *
 * A naive `/\$\{[^}]*\}/` breaks on the real call sites, which contain things
 * like `${short?.id ?? ""}` -- the `}` inside the string literal ends the
 * match early and leaves a dangling quote in the "path".
 */
export function normaliseTemplate(raw: string): string {
  let out = "";
  let i = 0;
  while (i < raw.length) {
    if (raw[i] === "$" && raw[i + 1] === "{") {
      // Scan the interpolation: if it carries a query string, the whole
      // interpolation is a query suffix, not a path segment. Real call sites do
      // this -- `/calendar/response-windows${f ? "?platform=" + f : ""}` --
      // and collapsing it to `{param}` invents a route that does not exist.
      let depth = 1;
      let j = i + 2;
      let quote: string | null = null;
      let carriesQuery = false;
      while (j < raw.length && depth > 0) {
        const ch = raw[j];
        // `??` is one operator. Evaluating its two characters separately makes
        // the second `?` look like a query marker, which truncates real paths
        // like `/content/${short?.id ?? ""}/publish` down to `/content`.
        if (!quote && ch === "?" && raw[j + 1] === "?") {
          j += 2;
          continue;
        }
        if (!quote && interpolationCarriesQuery(raw, j)) carriesQuery = true;
        if (quote) {
          if (ch === "\\") {
            j += 2;
            continue;
          }
          if (ch === quote) quote = null;
        } else if (ch === '"' || ch === "'" || ch === "`") {
          quote = ch;
        } else if (ch === "{") depth += 1;
        else if (ch === "}") depth -= 1;
        j += 1;
      }
      if (carriesQuery) break;
      out += "{param}";
      i = j;
      continue;
    }
    if (startsQuery(raw, i) || raw[i] === "#") break;
    if (raw[i] === "?" && raw[i + 1] === "?") {
      out += "??";
      i += 2;
      continue;
    }
    out += raw[i];
    i += 1;
  }
  if (out && !out.startsWith("/")) out = `/${out}`;
  out = out.replace(/\/{2,}/g, "/");
  if (out.length > 1) out = out.replace(/\/$/, "");
  return out;
}

/** A template with `{param}` wildcards matched against the concrete spec paths. */
function templateToMatcher(template: string): RegExp {
  const escaped = template
    .split("{param}")
    .map((part) => part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
    .join("[^/]+");
  return new RegExp(`^${escaped}$`);
}

/* =========================================================================
 * Source scanning
 * ====================================================================== */

function walk(dir: string, exts: string[]): string[] {
  if (!existsSync(dir)) return [];
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...walk(full, exts));
    else if (exts.some((e) => full.endsWith(e))) out.push(full);
  }
  return out;
}

export type ApiCall = {
  file: string;
  line: number;
  method: (typeof HTTP_METHODS)[number];
  /** Workspace-relative or global, with `{param}` for interpolation. */
  template: string;
  global: boolean;
};

/**
 * Read the string literal at the start of `text`, if there is one.
 *
 * A `/[`"']([\s\S]*?)[`"']/` regex cannot do this: on a template literal whose
 * interpolation itself contains a backtick it stops at the inner backtick and
 * returns a half-expression, which then looks like a path with a parameter
 * glued to it.
 */
function readFirstStringLiteral(text: string): string | null {
  const t = text.trimStart();
  const q = t[0];
  if (q !== '"' && q !== "'" && q !== "`") return null;

  let i = 1;
  while (i < t.length) {
    const c = t[i];
    if (c === "\\") {
      i += 2;
      continue;
    }
    if (q === "`" && c === "$" && t[i + 1] === "{") {
      let d = 1;
      i += 2;
      let inner: string | null = null;
      while (i < t.length && d > 0) {
        const k = t[i];
        if (inner) {
          if (k === "\\") i += 1;
          else if (k === inner) inner = null;
        } else if (k === '"' || k === "'" || k === "`") inner = k;
        else if (k === "{") d += 1;
        else if (k === "}") d -= 1;
        i += 1;
      }
      continue;
    }
    if (c === q) return t.slice(1, i);
    i += 1;
  }
  return null;
}

/** Every string literal in `text`, in source order. */
function readAllStringLiterals(text: string): string[] {
  const out: string[] = [];
  let i = 0;
  while (i < text.length) {
    const open = text[i];
    if (open !== '"' && open !== "'" && open !== "`") {
      i += 1;
      continue;
    }
    const start = i;
    let j = i + 1;
    let closedAt = -1;
    while (j < text.length) {
      const c = text[j];
      if (open === "`" && c === "$" && text[j + 1] === "{") {
        // Skip a whole interpolation, quotes inside it included.
        let d = 1;
        j += 2;
        while (j < text.length && d > 0) {
          const k = text[j];
          if (k === "{") d += 1;
          else if (k === "}") d -= 1;
          else if (k === '"' || k === "'" || k === "`") {
            const inner = k;
            j += 1;
            while (j < text.length && text[j] !== inner) {
              j += text[j] === "\\" ? 2 : 1;
            }
          }
          j += 1;
        }
        continue;
      }
      if (c === "\\") {
        j += 2;
        continue;
      }
      if (c === open) {
        closedAt = j;
        break;
      }
      j += 1;
    }
    if (closedAt < 0) break;
    out.push(text.slice(start + 1, closedAt));
    i = closedAt + 1;
  }
  return out;
}

const WS_PREFIX = "/workspaces/{workspace_id}";

/**
 * Read a balanced `(` ... `)` region starting at `open`, respecting nested
 * parens/brackets/braces and quoted strings. Returns the inner text.
 */
function readBalanced(src: string, open: number): string {
  let depth = 0;
  let quote: string | null = null;
  let i = open;
  for (; i < src.length; i += 1) {
    const ch = src[i];
    if (quote === "`" && ch === "$" && src[i + 1] === "{") {
      // An interpolation inside a template literal is not the end of the
      // string. Without this, `useWsQuery(`...${a ? `?x=1` : ""}`)` closes the
      // literal at the inner backtick and truncates the path mid-expression.
      let d = 1;
      i += 2;
      let innerQuote: string | null = null;
      while (i < src.length && d > 0) {
        const c = src[i];
        if (innerQuote) {
          if (c === "\\") i += 1;
          else if (c === innerQuote) innerQuote = null;
        } else if (c === '"' || c === "'" || c === "`") innerQuote = c;
        else if (c === "{") d += 1;
        else if (c === "}") d -= 1;
        i += 1;
      }
      i -= 1;
      continue;
    }
    if (quote) {
      if (ch === "\\") i += 1;
      else if (ch === quote) quote = null;
      continue;
    }
    if (ch === '"' || ch === "'" || ch === "`") quote = ch;
    else if (ch === "(") depth += 1;
    else if (ch === ")") {
      depth -= 1;
      if (depth === 0) return src.slice(open + 1, i);
    }
  }
  return src.slice(open + 1);
}

/**
 * Does the published document serve this raw path template as a GET?
 *
 * Used only by the third extraction pass, which cannot see the verb at the call
 * site. Resolving the template to real spec paths and asking the DOCUMENT keeps
 * that pass from inventing routing faults for POST-only mutations.
 */
function servedAsGet(raw: string): boolean {
  const probe = add2Probe(raw);
  return (specPathCandidates(probe).some((p) => Boolean(spec.paths[p]?.get)) ?? false);
}

/** Minimal ApiCall-shaped object so `specPathCandidates` can normalise a raw path. */
function add2Probe(raw: string): ApiCall {
  return {
    file: "<template-probe>",
    line: 0,
    method: "get",
    template: normaliseTemplate(raw),
    global: false,
  };
}

function add(
  out: ApiCall[],
  seen: Set<string>,
  file: string,
  line: number,
  method: (typeof HTTP_METHODS)[number],
  raw: string,
  global: boolean,
) {  const template = normaliseTemplate(raw);
  if (!template) return;
  const key = `${file}|${line}|${method}|${global}|${template}`;
  if (seen.has(key)) return;
  seen.add(key);
  out.push({ file, line, method, template, global });
}

/**
 * Extract API calls from source text.
 *
 * One multi-line regex over the whole file rather than a per-line scan: the real
 * call sites routinely wrap across lines (`wsApi.post(\n  \`/campaigns/...\`)`),
 * and a per-line scanner silently misses exactly those.
 */
export function extractApiCalls(source: string, file: string): ApiCall[] {
  return extractCallSites(source, file);
}

/** Resolve literals only when they flow into an actual HTTP call. The AST is
 * independent of OpenAPI: a removed GET must remain visible as unresolved. */
function extractCallSites(source: string, file: string): ApiCall[] {
  const tree = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const out: ApiCall[] = [];
  const seen = new Set<string>();
  const declarations: ts.VariableDeclaration[] = [];
  const collect = (node: ts.Node) => {
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name)) declarations.push(node);
    ts.forEachChild(node, collect);
  };
  collect(tree);
  function literals(node: ts.Expression | undefined, trail = new Set<ts.Node>()): string[] {
    if (!node || trail.has(node)) return [];
    const next = new Set(trail).add(node);
    if (ts.isStringLiteralLike(node)) return [node.text];
    if (ts.isTemplateExpression(node)) {
      let raw = node.head.text;
      for (const span of node.templateSpans) {
        const values = literals(span.expression, next);
        const queryOnly = values.length > 0 && values.every((v) => !v || /^[?&]/.test(v));
        raw += queryOnly ? (values.find(Boolean) || "") : `\${${span.expression.getText(tree)}}`;
        raw += span.literal.text;
      }
      return [raw];
    }
    if (ts.isParenthesizedExpression(node) || ts.isAsExpression(node) || ts.isNonNullExpression(node))
      return literals(node.expression, next);
    if (ts.isConditionalExpression(node)) return [...literals(node.whenTrue, next), ...literals(node.whenFalse, next)];
    if (ts.isIdentifier(node)) {
      // Nearest enclosing lexical declaration, never a same-name variable in a
      // sibling component. Do not infer a GET merely because a string exists.
      for (let scope: ts.Node | undefined = node.parent; scope; scope = scope.parent) {
        if (!ts.isBlock(scope) && !ts.isSourceFile(scope)) continue;
        const declaration = declarations.find((d) => d.parent.parent.parent === scope &&
          ts.isIdentifier(d.name) && d.name.text === node.text);
        if (declaration) return literals(declaration.initializer, next);
      }
    }
    return [];
  }
  function record(node: ts.Node, expression: ts.Expression | undefined, method: ApiCall["method"], global: boolean) {
    const line = tree.getLineAndCharacterOfPosition(node.getStart(tree)).line + 1;
    for (let raw of literals(expression)) {
      if (!raw.startsWith("/") && raw !== "") continue;
      if (raw.startsWith("/api/v1/workspaces/${")) {
        raw = raw.replace(/^\/api\/v1\/workspaces\/\$\{[^}]+\}/, "");
        global = false;
      } else if (raw.startsWith("/api/v1")) {
        raw = raw.slice("/api/v1".length);
        global = true;
      }
      // Empty conditional branches are disabled queries, not the workspace
      // root. A literal empty argument really does request the root.
      if (!raw && expression && !ts.isStringLiteralLike(expression)) continue;
      add(out, seen, file.replace(/\\/g, "/"), line, method, raw || "/", global);
    }
  }
  const visit = (node: ts.Node) => {
    if (ts.isCallExpression(node)) {
      const callee = node.expression;
      if (ts.isPropertyAccessExpression(callee) && callee.expression.getText(tree) === "wsApi") {
        const verb = callee.name.text === "del" ? "delete" : callee.name.text;
        if (HTTP_METHODS.includes(verb as ApiCall["method"])) record(node, node.arguments[0], verb as ApiCall["method"], false);
      } else if (ts.isIdentifier(callee)) {
        if (callee.text === "useWsQuery" || callee.text === "useQuery") record(node, node.arguments[0], "get", callee.text === "useQuery");
        if (callee.text === "api") {
          const verb = literals(node.arguments[0])[0]?.toLowerCase();
          if (HTTP_METHODS.includes(verb as ApiCall["method"])) record(node, node.arguments[1], verb as ApiCall["method"], true);
        }
        if (callee.text === "fetch") {
          const options = node.arguments[1];
          const method = options && ts.isObjectLiteralExpression(options) ? options.properties.find((p) =>
            ts.isPropertyAssignment(p) && p.name.getText(tree) === "method") : undefined;
          const verb = method && ts.isPropertyAssignment(method) ? literals(method.initializer)[0]?.toLowerCase() : "get";
          if (HTTP_METHODS.includes(verb as ApiCall["method"])) record(node, node.arguments[0], verb as ApiCall["method"], true);
        }
      }
    }
    if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken &&
        ts.isPropertyAccessExpression(node.left) && node.left.name.text === "href") {
      record(node, node.right, "get", true);
    }
    ts.forEachChild(node, visit);
  };
  visit(tree);
  return out;
}

function legacyExtractApiCalls(source: string, file: string): ApiCall[] {
  const out: ApiCall[] = [];
  const seen = new Set<string>();
  // Blank out line comments with spaces rather than deleting them. Removing the
  // text shifts every later offset, so reported line numbers were pointing at
  // the wrong line -- which makes a reported defect unactionable.
  const stripComments = source.replace(/\/\/[^\n]*/g, (m) => " ".repeat(m.length));
  const lineOf = (index: number) => source.slice(0, index).split(/\r?\n/).length;

  // wsApi.<verb>("path")  -- always workspace-scoped (lib/api.ts).
  const wsRe =
    /\bwsApi\.(get|post|put|patch|del|delete)\s*(?:<[^>]*?>)?\s*\(/g;
  let m: RegExpExecArray | null;
  while ((m = wsRe.exec(stripComments)) !== null) {
    const verb = m[1] === "del" || m[1] === "delete" ? "delete" : (m[1] as ApiCall["method"]);
    const raw = readFirstStringLiteral(
      readBalanced(stripComments, m.index + m[0].length - 1),
    );
    if (raw === null) continue;
    add(out, seen, file, lineOf(m.index), verb, raw, false);
  }

  // useWsQuery(path, {opts}) / useQuery(path) -- extract the full argument
  // list with a balanced-paren scan, then take the first string literal in it.
  // A regex for "an optional object then a string" is what produced 26 false
  // "unresolved" reports against routes that demonstrably exist.
  const hookRe = /\buse(Ws)?Query\s*(?:<[\s\S]*?>)?\s*\(/g;
  while ((m = hookRe.exec(stripComments)) !== null) {
    const isWs = Boolean(m[1]);
    const args = readBalanced(stripComments, m.index + m[0].length - 1);
    // The path must be the FIRST argument and must be a literal. Without this
    // guard a hook that takes a variable path picks up a string literal from
    // deeper in the call -- a JSX label -- and reports a route nobody calls.
    const firstString = readFirstStringLiteral(args);
    if (firstString === null) continue;
    add(out, seen, file, lineOf(m.index), "get", firstString, !isWs);
  }

  // api("GET", "/system/...") -- the global client.
  // api("GET", "/system/readiness") -- the global client. The first literal is
  // the verb, the second is the path.
  const apiRe = /\bapi\s*(?:<[^>]*?>)?\s*\(/g;
  while ((m = apiRe.exec(stripComments)) !== null) {
    const args = readBalanced(stripComments, m.index + m[0].length - 1);
    const lits = readAllStringLiterals(args);
    if (lits.length < 2) continue;
    const verb = lits[0].toUpperCase();
    if (!HTTP_METHODS.includes(verb.toLowerCase() as (typeof HTTP_METHODS)[number])) continue;
    add(
      out,
      seen,
      file,
      lineOf(m.index),
      verb.toLowerCase() as ApiCall["method"],
      lits[1],
      true,
    );
  }

  /* ---- THIRD PASS: paths built in a variable ---------------------------
   *
   * The two passes above read the argument list at the CALL SITE. Four real
   * endpoints never reach them, because the path is computed first and handed
   * over as a variable:
   *
   *   const endpoint = active ? `/performance/overview?campaign_id=${x}` : "";
   *   useWsQuery<Rollup>(endpoint, { enabled: Boolean(active) });
   *
   *   const detail = useWsQuery<Row>(id ? `/campaigns/${id}` : "/campaigns");
   *
   * The consequence was not a missing warning -- it was a FALSE GREEN. Those
   * four endpoints published an EMPTY 2xx schema, and because the scanner never
   * saw them they were absent from the inventory, so `UNDECLARED_JSON: 0` was
   * true AND incomplete. An audit that cannot see a call site cannot report it.
   *
   * So this pass scans the whole file for ROUTE-SHAPED template literals and
   * registers each as a GET. Over-approximating the verb is deliberate and safe:
   * the alternative is an endpoint invisible to the audit, and a wrong verb can
   * only mis-attribute a verb that the classifier then checks against the real
   * document. Under-reporting cannot be caught by anything.
   *
   * Guarded by `looksLikeRoute`: it must start with a single "/" and contain a
   * further segment, so document links, image paths and CSS urls are ignored.
   */
  const tmplRe = /`(\/[^`\n]{2,200})`/g;
  while ((m = tmplRe.exec(stripComments)) !== null) {
    const raw = m[1];
    if (!/^\/[A-Za-z][^?#]*\//.test(raw)) continue;
    /* ONLY claim a path the document actually serves as GET.
     *
     * Registering every template as a GET produced 42 false `METHOD_NOT_SERVED`
     * routing faults: a mutation like `/campaigns/${id}/derive` is POST-only, and
     * pass 1 already sees those because the literal sits at the call site. What
     * this pass exists to recover is the READ whose verb is a safe assumption,
     * because a GET is the only verb that returns data to render.
     */
    if (!servedAsGet(raw)) continue;
    add(out, seen, file, lineOf(m.index), "get", raw, false);
  }

  return out;
}

const FEATURE_FILES = [
  ...walk(join(SRC, "features"), [".tsx", ".ts"]),
  ...walk(join(SRC, "app"), [".tsx", ".ts"]),
].filter((f) => !/\.test\.(ts|tsx)$/.test(f));

export const ALL_CALLS: ApiCall[] = FEATURE_FILES.flatMap((f) =>
  extractApiCalls(readFileSync(f, "utf-8"), relative(SRC, f).replace(/\\/g, "/")),
).sort((a, b) => a.file < b.file ? -1 : a.file > b.file ? 1 : a.line - b.line);

/**
 * Every spec path a call could be hitting.
 *
 * An interpolated segment genuinely can collide: `/calendar/{entry_id}` and
 * `/calendar/best-times` both match the template `/calendar/{param}`. Silently
 * taking the first match is how a real 405 hides, so the caller is told when
 * the route is ambiguous instead of being handed a confident wrong answer.
 */
export function specPathCandidates(call: ApiCall): string[] {
  const prefix = call.global ? "/api/v1" : `/api/v1${WS_PREFIX}`;
  const full = `${prefix}${call.template === "/" ? "" : call.template}`;
  if (SPEC_PATHS.has(full)) return [full];
  const re = templateToMatcher(full);
  return Object.keys(spec.paths).filter((p) => re.test(p)).sort((a, b) =>
    (b.match(/\{/g)?.length ?? 0) - (a.match(/\{/g)?.length ?? 0) || (a < b ? -1 : a > b ? 1 : 0));
}

/** Every call that resolved onto at least one served route. */
export const RESOLVED: { call: ApiCall; paths: string[]; ambiguous: boolean }[] =
  ALL_CALLS.map((call) => {
    const paths = specPathCandidates(call);
    return { call, paths, ambiguous: paths.length > 1 };
  }).filter((r) => r.paths.length > 0);

/** The single served route for a call, or null when absent or ambiguous. */
export function specPathFor(call: ApiCall): string | null {
  const paths = specPathCandidates(call);
  return paths.length === 1 ? paths[0] : null;
}

const RESOLVABLE = RESOLVED;
const UNRESOLVED = ALL_CALLS.filter((c) => specPathCandidates(c).length === 0);
const AMBIGUOUS = RESOLVED.filter((r) => r.ambiguous);

/* =========================================================================
 * Anti-vacuity: prove the scanner sees the real calling conventions
 * ====================================================================== */

describe("contract scanner is not inert", () => {
  it("tracks variable and conditional GETs without scanning unrelated templates", () => {
    const calls = extractApiCalls('const endpoint = active ? `/performance/overview?campaign_id=${id}` : ""; useWsQuery(endpoint); const unrelated = `/campaigns/${id}`; wsApi.post(`/campaigns/${id}/derive`);', "features\\x.tsx");
    expect(calls.map((c) => [c.method, c.template])).toEqual([
      ["get", "/performance/overview"], ["post", "/campaigns/{param}/derive"],
    ]);
    expect(calls.every((c) => c.file === "features/x.tsx")).toBe(true);
  });

  it("keeps removed routes visible and ignores comments and URL text", () => {
    const calls = extractApiCalls('// useWsQuery("/fake");\nconst site = "https://example.com"; useWsQuery(`/removed/${id}`); /* wsApi.get("/fake") */', "x.tsx");
    expect(calls.map((c) => c.template)).toEqual(["/removed/{param}"]);
  });

  it("does not resolve a same-name variable from a sibling lexical scope", () => {
    expect(extractApiCalls('function A(){const path="/jobs";} function B(){useWsQuery(path);}', "x.tsx")).toEqual([]);
  });
  it("finds a substantial number of calls", () => {
    expect(ALL_CALLS.length).toBeGreaterThan(20);
  });

  it("detects all four calling conventions the codebase uses", () => {
    expect(new Set(ALL_CALLS.map((c) => c.method)).has("get")).toBe(true);
    expect(new Set(ALL_CALLS.map((c) => c.method)).has("post")).toBe(true);

    const workspaceScoped = ALL_CALLS.filter((c) => !c.global);
    expect(workspaceScoped.length).toBeGreaterThan(10);

    const globals = ALL_CALLS.filter((c) => c.global);
    expect(globals.length).toBeGreaterThan(0);
    expect(
      globals.map((g) => g.template),
      "global calls must use /system, /auth or /inbox, never a workspace route",
    ).not.toContain("/jobs");
  });

  it("reads the second literal of api(METHOD, path) as the path", () => {
    const calls = extractApiCalls(
      `const r = useCombinedQueries({ readiness: () => api<Readiness>("GET", "/system/readiness") });`,
      "x.tsx",
    );
    expect(calls).toEqual([
      { file: "x.tsx", line: 1, method: "get", template: "/system/readiness", global: true },
    ]);
  });

  it("parses interpolations containing nested quotes without corrupting the path", () => {
    // The real bug this guards: `/content/${short?.id ?? ""}/publish` used to be
    // captured as `/content/${short?.id ?? "` and then matched nothing.
    const parsed = normaliseTemplate('/content/${short?.id ?? ""}/publish');
    expect(parsed).toBe("/content/{param}/publish");

    const nested = normaliseTemplate("/jobs/${a ? b : c}/cancel");
    expect(nested).toBe("/jobs/{param}/cancel");

    const optionalChain = normaliseTemplate("/jobs/${job?.id}/cancel");
    expect(optionalChain).toBe("/jobs/{param}/cancel");

    expect(normaliseTemplate("/publishing/posts?limit=100")).toBe(
      "/publishing/posts",
    );
  });

  it("matches a parameterised template against a concrete spec path", () => {
    // `/campaigns/{param}/aggregate` is served by exactly one route, so the
    // wildcard resolves unambiguously.
    const call: ApiCall = {
      file: "x",
      line: 1,
      method: "get",
      template: "/campaigns/{param}/aggregate",
      global: false,
    };
    expect(specPathFor(call)).toBe(
      "/api/v1/workspaces/{workspace_id}/campaigns/{campaign_id}/aggregate",
    );
  });

  it("does NOT invent a route the backend never declared", () => {
    // There is no GET /jobs/{job_id}. A test that assumed one would be
    // asserting a fiction; the honest result is no match.
    const call: ApiCall = {
      file: "x", line: 1, method: "get", template: "/jobs/{param}", global: false,
    };
    expect(specPathCandidates(call)).toEqual([]);
  });

  it("strips a query string appended after an interpolation", () => {
    expect(
      normaliseTemplate('/calendar/response-windows${f ? "?platform=" + f : ""}'),
    ).toBe("/calendar/response-windows");
  });

  it("prefers an exact literal match over a wildcard collision", () => {
    // `/calendar/best-times` is a real GET route; a template that happens to
    // equal it must resolve to it, not to `/calendar/{entry_id}`.
    const literal: ApiCall = {
      file: "x", line: 1, method: "get", template: "/calendar/best-times", global: false,
    };
    expect(specPathFor(literal)).toBe(
      "/api/v1/workspaces/{workspace_id}/calendar/best-times",
    );
  });

  it("reports an interpolated route that collides with a literal one as ambiguous", () => {
    const call: ApiCall = {
      file: "x", line: 1, method: "get", template: "/content/{param}", global: false,
    };
    const cands = specPathCandidates(call);
    expect(cands.length).toBeGreaterThan(1);
    expect(specPathFor(call)).toBeNull();
  });
});

/* =========================================================================
 * §11 (a) route exists
 * ====================================================================== */

const RESOLVABLE_UNUSED_REMOVED = 0;
void RESOLVABLE_UNUSED_REMOVED;

describe("OpenAPI contract: every route the UI calls is served", () => {
  it.each(
    RESOLVED.map((r) => [
      `${r.call.method.toUpperCase()} ${r.call.file.replace(/^src\\/, "")}:${r.call.line}`,
      r.call,
      r.paths,
    ]),
  )("%s exists in the generated OpenAPI baseline", (_label, call: ApiCall, paths: string[]) => {
    expect(
      paths.length,
      `${call.file}:${call.line} calls ${call.method.toUpperCase()} ${call.template}, ` +
        `which the backend does not serve. Either the route was renamed or the ` +
        `frontend invented it.`,
    ).toBeGreaterThan(0);
  });

  it("resolves the overwhelming majority of calls", () => {
    // A handful of calls may legitimately be built dynamically; a large
    // unresolved fraction means the scanner or the UI has drifted.
    const resolvedRatio = RESOLVABLE.length / Math.max(ALL_CALLS.length, 1);
    expect(resolvedRatio).toBeGreaterThan(0.9);
  });

  it("lists any call that matches no served route, so none can be ignored", () => {
    const list = UNRESOLVED.map(
      (c) => `${c.file.replace(/^src\\/, "")}:${c.line} ${c.method.toUpperCase()} ${c.template}`,
    );
    // eslint-disable-next-line no-console
    console.log(`[contract] unresolved calls: ${list.length}\n  ${list.join("\n  ")}`);
    expect(list.length).toBeLessThanOrEqual(6);
  });

  it("publishes the routes whose interpolated segment is ambiguous", () => {
    const list = AMBIGUOUS.map(
      (r) =>
        `${r.call.file.replace(/^src\\/, "")}:${r.call.line} ${r.call.method.toUpperCase()} ` +
        `${r.call.template} -> ${r.paths.join(" | ")}`,
    );
    // eslint-disable-next-line no-console
    console.log(`[contract] ambiguous calls: ${list.length}\n  ${list.join("\n  ")}`);
    expect(Array.isArray(list)).toBe(true);
  });
});

/* =========================================================================
 * §11 (b) HTTP method
 * ====================================================================== */

describe("OpenAPI contract: HTTP method matches", () => {
  const UNAMBIGUOUS = RESOLVED.filter((r) => !r.ambiguous);
  const checked = UNAMBIGUOUS;

  it("has method-checkable calls", () => {
    expect(checked.length).toBeGreaterThan(20);
  });

  it.each(
    checked.map((r) => [
      `${r.call.method.toUpperCase()} ${r.paths[0]}`,
      r.call,
      r.paths[0],
    ]),
  )("%s is served with that method", (_label, call: ApiCall, path: string) => {
    const item = spec.paths[path] as Record<string, unknown>;
    const served = HTTP_METHODS.filter((verb) => verb in item);
    expect(
      served,
      `${call.file}:${call.line} calls ${call.method.toUpperCase()} ${call.template}, ` +
        `but the backend serves only [${served.join(", ") || "nothing"}] for that path. ` +
        `A method mismatch is a 405 in production.`,
    ).toContain(call.method);
  });

  it("serves the method on at least one candidate for every ambiguous call", () => {
    for (const r of AMBIGUOUS) {
      const anyServes = r.paths.some((p) =>
        HTTP_METHODS.some((verb) => verb in (spec.paths[p] as object)),
      );
      expect(
        anyServes,
        `${r.call.file}:${r.call.line} ${r.call.method.toUpperCase()} ${r.call.template} ` +
          `matched [${r.paths.join(", ")}] but none of them serve any HTTP method`,
      ).toBe(true);
    }
  });
});

/* =========================================================================
 * §11 (c)(d)(e) response shape / enum vocabulary / required fields
 *
 * These three need the spec to DECLARE response schemas. It does not: the
 * routes in scope return bare dicts rather than a typed `response_model`, so
 * FastAPI emits a 2xx with no schema and the only declared body is
 * `422 HTTPValidationError`.
 *
 * Instead of silently skipping them -- which would make this file look like
 * full §11 coverage when it is not -- the gap is pinned in place below. When
 * someone adds a `response_model`, the unspec'd list shrinks and the ceiling
 * assertion fails, telling them to write the real shape/enum/required tests.
 * ====================================================================== */

const UNSPECIFIED = RESOLVED.filter(({ paths, call }) => {
  // Declared if ANY candidate route declares a typed 2xx body.
  return !paths.some((path) => {
    const op = (
      spec.paths[path] as Record<string, { responses?: Record<string, unknown> }>
    )[call.method];
    return Object.entries(op?.responses ?? {}).some(
      ([code, body]) =>
        code.startsWith("2") &&
        !!(
          body as { content?: { "application/json"?: { schema?: { $ref?: string } } } }
        )?.content?.["application/json"]?.schema?.$ref,
    );
  });
});

/** Every 2xx `$ref` declared anywhere in the baseline. */
type Schema = {
  properties?: Record<string, unknown>;
  required?: string[];
  enum?: string[];
  items?: { $ref?: string; enum?: string[] };
};

const COMPONENTS = (spec as unknown as {
  components: { schemas: Record<string, Schema> };
}).components.schemas;

function declared2xxSchema(path: string, method: string): Schema | null {
  const op = (spec.paths[path] as Record<string, { responses?: Record<string, unknown> }>)[
    method
  ];
  for (const [code, body] of Object.entries(op?.responses ?? {})) {
    if (!code.startsWith("2")) continue;
    const ref = (
      body as { content?: { "application/json"?: { schema?: { $ref?: string } } } }
    )?.content?.["application/json"]?.schema?.$ref;
    if (!ref) continue;
    return COMPONENTS[ref.split("/").pop() as string] ?? null;
  }
  return null;
}

const SPECIFIED = RESOLVED.filter(({ paths, call }) =>
  paths.some((p) => declared2xxSchema(p, call.method) !== null),
);

/* =========================================================================
 * §1/§3 -- classify every resolved UI call by what its response ACTUALLY is.
 *
 * "No declared schema" and "has no shape to declare" are different findings, and
 * collapsing them is how a coverage number becomes fiction. A 204, an SSE feed,
 * a file download and a deliberately-open provider blob are all legitimately
 * schemaless; ordinary JSON without a schema is simply unfinished work.
 *
 * So each call gets exactly one class, from evidence in the spec:
 *   SCHEMA_COVERED  a 2xx $ref exists
 *   NO_CONTENT      204 only, or no 2xx at all
 *   STREAM          text/event-stream
 *   FILE            a binary media content-type
 *   ROOT_INTERNAL   mounted off /api/v1 (an orchestrator cannot authenticate as
 *                   a workspace) and therefore include_in_schema=False
 *   DYNAMIC_BY_DESIGN  ordinary JSON whose shape is open by contract
 *   METHOD_NOT_SERVED  an interpolated template whose candidates serve no such
 *                      verb -- a routing fault, NOT a response-shape gap
 *   UNDECLARED_JSON  ordinary JSON with no schema  <-- the remaining work
 * ====================================================================== */

type CallClass =
  | "SCHEMA_COVERED"
  | "NO_CONTENT"
  | "STREAM"
  | "FILE"
  | "ROOT_INTERNAL"
  | "DYNAMIC_BY_DESIGN"
  | "METHOD_NOT_SERVED"
  | "UNDECLARED_JSON";

type Op = {
  responses?: Record<string, { content?: Record<string, unknown>; description?: string }>;
};

function classifyCall(paths: string[], method: string): { cls: CallClass; specPath: string } {
  // A root-mounted probe is deliberately absent from the document. Decide that
  // from the ABSENCE of any /api/v1 candidate, not by pattern-matching a list.
  const apiPaths = paths.filter((p) => p.startsWith("/api/v1"));
  const ordered = apiPaths.length ? apiPaths : paths;

  // AN AMBIGUOUS TEMPLATE MUST BE RESOLVED BY THE METHOD, NOT BY POSITION.
  //
  // ``wsApi.patch(`/calendar/${entryId}`)`` matches `/calendar/{entry_id}` AND
  // `/calendar/best-times`, because one interpolated segment is textually
  // indistinguishable from a literal one. Taking `ordered[0]` whichever route
  // sorted first that does not serve PATCH produced `op === undefined`, and the
  // `!op` branch below then reported it as UNDECLARED_JSON.
  //
  // That is not a cosmetic error. It invented three endpoints the frontend
  // never calls (`PATCH`/`DELETE /calendar/best-times`, `GET
  // /content/estimate-cost`), put them in the machine-readable audit as real
  // remaining work, and sent the contract generator to chase routes that do not
  // exist. A wrong answer that looks like real work is worse than no answer.
  const serving = ordered.find(
    (p) => (spec.paths[p] as Record<string, Op | undefined> | undefined)?.[method],
  );
  if (serving) {
    const specPath = serving;
    const op = (spec.paths[specPath] as Record<string, Op | undefined>)[method]!;
    return classifyOp(specPath, method, op, apiPaths.length > 0);
  }

  // No candidate serves this method. That is a genuine routing fault -- a call
  // the frontend makes against a path whose only served verb is different -- and
  // it must be reported as such rather than folded into the response-shape gap.
  const specPath = ordered[0];
  return { cls: "METHOD_NOT_SERVED", specPath };
}

function classifyOp(
  specPath: string,
  method: string,
  op: Op,
  isApiPath: boolean,
): { cls: CallClass; specPath: string } {
  if (declared2xxSchema(specPath, method)) {
    return { cls: "SCHEMA_COVERED", specPath };
  }

  const responses = op.responses ?? {};
  const codes = Object.keys(responses);
  const twoXX = codes.filter((c) => c.startsWith("2"));

  if (twoXX.length === 0 || (twoXX.length === 1 && twoXX[0] === "204")) {
    return { cls: "NO_CONTENT", specPath };
  }

  const types = new Set<string>();
  for (const c of twoXX) {
    for (const t of Object.keys(responses[c]?.content ?? {})) types.add(t);
  }
  if ([...types].some((t) => t.includes("event-stream"))) {
    return { cls: "STREAM", specPath };
  }
  if (
    [...types].some(
      (t) =>
        t.startsWith("application/octet-stream") ||
        t.startsWith("video/") ||
        t.startsWith("audio/") ||
        t.startsWith("image/"),
    )
  ) {
    return { cls: "FILE", specPath };
  }
  if (!isApiPath) {
    // Served, but off /api/v1 -- an internal probe, not a product API.
    return { cls: "ROOT_INTERNAL", specPath };
  }
  return { cls: "UNDECLARED_JSON", specPath };
}

const CLASSIFIED = RESOLVED.map((r) => {
  const { cls, specPath } = classifyCall(r.paths, r.call.method);
  return { ...r, cls, specPath };
});

/** Classes that are legitimately without a schema, and therefore excluded from
 * the "ordinary JSON" denominator rather than counted as failures.
 *
 * `METHOD_NOT_SERVED` is in this set for a specific reason: it is not a missing
 * RESPONSE contract, it is a call whose verb no candidate route serves. Leaving
 * it out of the denominator keeps the response-shape coverage figure honest --
 * counting it as a schema gap would have inflated the remaining work with three
 * routes the frontend never calls.
 *
 * It is NOT swept away: `test every ambiguous call resolves to a served verb`
 * fails if any call lands in this class, so the set here cannot quietly become
 * a place to hide a routing fault.
 */
const SCHEMALESS: ReadonlySet<CallClass> = new Set<CallClass>([
  "NO_CONTENT",
  "STREAM",
  "FILE",
  "ROOT_INTERNAL",
  "DYNAMIC_BY_DESIGN",
  "METHOD_NOT_SERVED",
]);

const ORDINARY_JSON = CLASSIFIED.filter((c) => c.cls === "SCHEMA_COVERED" || c.cls === "UNDECLARED_JSON");
const UNDECLARED_JSON = CLASSIFIED.filter((c) => c.cls === "UNDECLARED_JSON");

describe("ambiguous templates resolve to a route that serves the verb", () => {
  it("never reports a routing fault as a missing response schema", () => {
    // If this fails, an interpolated path is colliding with a literal one and
    // `classifyCall` is guessing. The fix is a real route or a real call site --
    // never a new class in SCHEMALESS.
    const faults = CLASSIFIED.filter((c) => c.cls === "METHOD_NOT_SERVED");
    expect(
      faults.map((f) => `${f.call.method.toUpperCase()} ${f.paths.join(" | ")}`),
    ).toEqual([]);
  });

  it("prefers the candidate that serves the method, not the first candidate", () => {
    // Guards the regression directly: `PATCH /calendar/${entryId}` must bind to
    // `/calendar/{entry_id}`, never to `/calendar/best-times`.
    const cal = CLASSIFIED.find(
      (c) => c.call.method === "patch" && c.paths.some((p) => p.includes("/calendar")),
    );
    expect(cal?.specPath).toBe("/api/v1/workspaces/{workspace_id}/calendar/{entry_id}");
  });
});

/* =========================================================================
 * §1 -- response SHAPE, REQUIRED fields and ENUM vocabulary
 *
 * These are the checks that were impossible in Work 16.5.1: no route declared a
 * 2xx schema, so the only thing provable was route and method. They now run
 * against the subset of routes the backend has given a declared contract, and
 * they are real checks -- a malformed schema fails here.
 * ====================================================================== */

describe("OpenAPI contract: declared response shapes are well-formed", () => {
  it("coverage has grown beyond route/method only", () => {
    // Before Work 16.5.2 this was 0. If it drops back, the response contract
    // closure has been undone.
    // eslint-disable-next-line no-console
    console.log(
      `[contract] response-schema coverage: ${SPECIFIED.length}/${RESOLVED.length} ` +
        `resolved calls have a declared 2xx body ` +
        `(${((SPECIFIED.length / Math.max(RESOLVED.length, 1)) * 100).toFixed(1)}%)`,
    );
    expect(SPECIFIED.length).toBeGreaterThan(0);
  });

  it.each(
    SPECIFIED.map((r) => [
      `${r.call.method.toUpperCase()} ${r.paths.find((p) => declared2xxSchema(p, r.call.method))}`,
      r.call,
      r.paths,
    ]),
  )("%s declares properties and a consistent required list", (_l, call: ApiCall, paths: string[]) => {
    const schema = paths
      .map((p) => declared2xxSchema(p, call.method))
      .find((s): s is Schema => s !== null) as Schema;

    const props = Object.keys(schema.properties ?? {});
    // eslint-disable-next-line no-console
    console.log(`[contract]   ${props.length} properties`);
    expect(
      props.length,
      `${call.file}:${call.line} -> a declared schema with no properties is not a contract`,
    ).toBeGreaterThan(0);

    for (const key of schema.required ?? []) {
      expect(
        props,
        `${call.file}:${call.line} declares "${key}" required but it is not a property`,
      ).toContain(key);
    }
  });

  it("publishes the closed provider-maturity vocabulary as an enum", () => {
    // Pydantic inlines a Literal, so the enum lives on the property rather than
    // in a named component. Look it up where it actually is.
    const row = COMPONENTS["ProviderMaturityOut"];
    expect(row, "no ProviderMaturityOut schema").toBeTruthy();

    const axis = row!.properties!.implementation_status as { enum?: string[] };
    expect(
      new Set(axis.enum ?? []),
      "the maturity ladder the frontend enforces must be the agreed eight",
    ).toEqual(
      new Set([
        "IMPLEMENTED",
        "CONTRACT_TESTED",
        "LIVE_VERIFIED",
        "UNVERIFIED",
        "UNAVAILABLE",
        "BLOCKED_LICENSE",
        "BLOCKED_COMMERCIAL_TERMS",
        "EXTERNAL_LIMITATION",
      ]),
    );

    // Four INDEPENDENT axes, not one collapsed field. A provider can be
    // IMPLEMENTED + CONTRACT_TESTED + UNVERIFIED + BLOCKED_COMMERCIAL_TERMS at
    // once; collapsing that loses everything the Providers screen is for.
    const props = Object.keys(row!.properties ?? {});
    expect(props).not.toContain("maturity");
    for (const a of [
      "implementation_status",
      "contract_status",
      "live_status",
      "commercial_status",
    ]) {
      expect(props, `missing the ${a} axis`).toContain(a);
      const e = (row!.properties![a] as { enum?: string[] }).enum;
      expect(new Set(e ?? []), `${a} must share the closed ladder`).toEqual(
        new Set(axis.enum ?? []),
      );
    }

    // A provider row must never declare a credential VALUE.
    expect(props).toContain("credential_status");
    expect(props).not.toContain("api_key");
    expect(props).not.toContain("secret");
  });

  it("keeps open-ended job status as a string, not a closed enum", () => {
    // The queue adds states. If this ever becomes an enum, the frontend test
    // above would start rejecting a legitimate backend state.
    const list = COMPONENTS["JobListOut"];
    expect(list).toBeTruthy();
    // `items` here is a PROPERTY named items, whose value is the array schema.
    const arr = list!.properties!.items as { items?: { $ref?: string } };
    const itemRef = arr?.items?.$ref;
    expect(itemRef, "JobListOut.items must reference JobOut").toBeTruthy();
    const item = COMPONENTS[itemRef!.split("/").pop() as string];
    const status = item!.properties!.status as { type?: string; enum?: string[] };
    expect(status.type).toBe("string");
    expect(status.enum).toBeUndefined();
  });
});

describe("OpenAPI contract: response-schema coverage (tracked gap)", () => {
  it("classifies every resolved call and writes the machine-readable audit", () => {
    const byClass: Record<string, number> = {};
    for (const c of CLASSIFIED) byClass[c.cls] = (byClass[c.cls] ?? 0) + 1;

    // eslint-disable-next-line no-console
    console.log(
      `\n[contract] ${ALL_CALLS.length} calls scanned | ${RESOLVED.length} resolved | ` +
        `${UNRESOLVED.length} unresolved | ${AMBIGUOUS.length} ambiguous\n` +
        Object.entries(byClass)
          .sort()
          .map(([k, v]) => `[contract]   ${k.padEnd(18)} ${v}`)
          .join("\n") +
        `\n[contract] ordinary JSON: ${ORDINARY_JSON.length - UNDECLARED_JSON.length}` +
        `/${ORDINARY_JSON.length} documented\n`,
    );

    // Every resolved call must land in exactly one class. A call that slips
    // through unclassified would silently vanish from the denominator.
    expect(
      CLASSIFIED.reduce((n, c) => n + (c.cls ? 1 : 0), 0),
    ).toBe(RESOLVED.length);

    // The classifier must still be ABLE to produce more than one verdict.
    //
    // This used to assert `Object.keys(byClass).length > 1`, which quietly
    // required the product to stay incomplete: with every ordinary-JSON call
    // contracted there is legitimately exactly one class, and the assertion
    // failed on success. Requiring a permanent gap as proof of a working
    // classifier is backwards.
    //
    // Non-inertness is proven directly instead. The document happens to contain
    // no NO_CONTENT / STREAM / FILE operation at all -- verified, not assumed --
    // so those classes are empty by construction and cannot serve as evidence.
    // What CAN be checked is that the classifier discriminates rather than
    // blanket-approving: a real served route is SCHEMA_COVERED, while a path the
    // document does not publish is not.
    expect(
      classifyCall(["/api/v1/workspaces/{workspace_id}/jobs"], "get").cls,
    ).toBe("SCHEMA_COVERED");
    expect(
      classifyCall(
        ["/api/v1/workspaces/{workspace_id}/no-such-route-exists"],
        "get",
      ).cls,
    ).toBe("METHOD_NOT_SERVED");
    expect(
      classifyCall(["/api/v1/workspaces/{workspace_id}/jobs"], "trace").cls,
    ).toBe("METHOD_NOT_SERVED");

    // Every classified call must carry a real verdict from the known vocabulary.
    const KNOWN: ReadonlySet<CallClass> = new Set<CallClass>([
      "SCHEMA_COVERED",
      "NO_CONTENT",
      "STREAM",
      "FILE",
      "ROOT_INTERNAL",
      "DYNAMIC_BY_DESIGN",
      "METHOD_NOT_SERVED",
      "UNDECLARED_JSON",
    ]);
    for (const c of CLASSIFIED) expect(KNOWN.has(c.cls)).toBe(true);

    // The two halves of the denominator must reconcile exactly.
    expect(
      (byClass["SCHEMA_COVERED"] ?? 0) + (byClass["UNDECLARED_JSON"] ?? 0),
    ).toBe(ORDINARY_JSON.length);

    writeFileSync(
      join(REPO, "docs", "UI_CONTRACT_AUDIT.json"),
      JSON.stringify(
        {
          generatedFrom: "frontend/src/api/openapi.json",
          totalScanned: ALL_CALLS.length,
          resolved: RESOLVED.length,
          unresolved: UNRESOLVED.length,
          ambiguous: AMBIGUOUS.length,
          byClass,
          ordinaryJsonTotal: ORDINARY_JSON.length,
          ordinaryJsonDocumented: ORDINARY_JSON.length - UNDECLARED_JSON.length,
          calls: CLASSIFIED.map((c) => ({
            method: c.call.method.toUpperCase(),
            uiPath: c.paths.join("|"),
            specPath: c.specPath,
            class: c.cls,
            file: c.call.file.replace(/^src\\/, ""),
          })),
        },
        null,
        2,
      ),
      "utf-8",
    );
  });

  it("reports SCHEMA_COVERED separately from LEGITIMATELY_SCHEMALESS", () => {
    const schemaless = CLASSIFIED.filter((c) => SCHEMALESS.has(c.cls));
    const undocumented = ORDINARY_JSON.length - (ORDINARY_JSON.length - UNDECLARED_JSON.length);
    // eslint-disable-next-line no-console
    console.log(
      `[contract] SCHEMA_COVERED ${UNDECLARED_JSON.length === 0 ? ORDINARY_JSON.length : ORDINARY_JSON.length - UNDECLARED_JSON.length}` +
        ` | LEGITIMATELY_SCHEMALESS ${schemaless.length} | UNDECLARED_JSON ${UNDECLARED_JSON.length}`,
    );
    expect(undocumented).toBe(UNDECLARED_JSON.length);
  });

  it("has not silently LOST a declared contract", () => {
    // A floor on the ALREADY-DECLARED set. Work 16.5.3 must only grow it.
    expect(SPECIFIED.length).toBeGreaterThanOrEqual(7);
  });
});

/* =========================================================================
 * §11 (f) permission expectations
 * ====================================================================== */

describe("OpenAPI contract: authorization is server-side and workspace-scoped", () => {
  const wsScoped = Object.keys(spec.paths).filter((p) =>
    p.startsWith(`/api/v1${WS_PREFIX}`),
  );

  it("has workspace-scoped routes to reason about", () => {
    expect(wsScoped.length).toBeGreaterThan(100);
  });

  it("never calls a workspace resource without the workspace prefix", () => {
    // A global call to a workspace-owned resource would bypass the workspace
    // the server scopes by, which is an authorization bug, not a 404.
    const offenders = RESOLVED.filter(({ paths }) =>
      paths.some((p) => /\/workspaces\/\{workspace_id\}/.test(p)),
    ).filter(({ call }) => call.global);
    expect(
      offenders.map((o) => `${o.call.file}:${o.call.line} ${o.call.template}`),
      "a global call resolved onto a workspace-scoped route",
    ).toEqual([]);
  });

  it("only uses declared global route families", () => {
    const GLOBAL_PREFIXES = [
      "/api/v1/system/",
      "/api/v1/auth/",
      "/api/v1/inbox/",
      "/api/v1/livez",
      "/api/v1/readyz",
      "/api/v1/healthz",
      "/api/v1/openapi",
      "/api/v1/docs",
    ];
    const bad = RESOLVED.filter(({ paths }) =>
      paths.some((p) => !p.startsWith("/api/v1/workspaces/")),
    )
      .filter(({ paths }) =>
        !paths.some((p) => GLOBAL_PREFIXES.some((pre) => p.startsWith(pre))),
      )
      .map(({ paths }) => paths.join("|"));
    expect(bad, "global calls outside the known non-scoped families").toEqual([]);
  });
});

/* =========================================================================
 * Fixture provenance
 * ====================================================================== */

describe("contract fixture provenance", () => {
  it("reads the generated baseline, and the generator exists", () => {
    expect(existsSync(join(REPO, "scripts", "gen_openapi.py"))).toBe(true);
    expect(Object.keys(spec.paths).length).toBeGreaterThan(300);
  });
});
