#!/usr/bin/env python3
"""
notate — Automatic technical documentation in Notion.
Analyzes Git commits and creates a new entry in a Notion database.

Configuration (~/.env.notate):
  ANTHROPIC_API_KEY=sk-ant-...
  NOTION_API_KEY=secret_...
  NOTION_DOCS_DATABASE_ID=your-database-id
  CLAUDE_MODEL=claude-sonnet-4-6   # optional, this is the default

Usage:
  notate                                 # document the current branch
  notate --branch feature/my-feature
  notate --from abc1234 --to def5678
  notate --commits abc1234 def5678
  notate --branch feature/x --type api
  notate --dry-run
"""

import argparse
import subprocess
import sys
import json
import os
import re
import socket
import threading
import itertools
import time
from datetime import datetime
from pathlib import Path

# ─── CONFIGURATION ────────────────────────────────────────────────────────────

ENV_FILE = Path.home() / ".env.notate"

REQUIRED_VARS = ("ANTHROPIC_API_KEY", "NOTION_API_KEY", "NOTION_DOCS_DATABASE_ID")

def load_env():
    """Load variables from ~/.env.notate. Falls back to the real environment (CI/tests)."""
    if not ENV_FILE.exists():
        if all(os.environ.get(k) for k in REQUIRED_VARS):
            return  # already provided via the environment
        print(f"⚠️  {ENV_FILE} not found")
        print("   Create it with:")
        print("   ANTHROPIC_API_KEY=sk-ant-...")
        print("   NOTION_API_KEY=secret_...")
        print("   NOTION_DOCS_DATABASE_ID=your-database-id")
        sys.exit(1)

    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

load_env()

ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
NOTION_API_KEY     = os.environ.get("NOTION_API_KEY", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DOCS_DATABASE_ID", "")
CLAUDE_MODEL       = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

MAX_DIFF_CHARS = 40_000

# Generated / lock / vendored files: noise that wastes the diff budget and
# teaches Claude nothing. Excluded from both the diff and the file content.
NOISE_PATHSPECS = [
    ":(exclude)**/package-lock.json",
    ":(exclude)**/pnpm-lock.yaml",
    ":(exclude)**/yarn.lock",
    ":(exclude)**/poetry.lock",
    ":(exclude)**/Pipfile.lock",
    ":(exclude)**/Cargo.lock",
    ":(exclude)**/composer.lock",
    ":(exclude)**/go.sum",
    ":(exclude)**/*.min.js",
    ":(exclude)**/*.min.css",
    ":(exclude)**/*.map",
    ":(exclude)**/*.snap",
    ":(exclude)**/__snapshots__/**",
    ":(exclude)**/dist/**",
    ":(exclude)**/build/**",
    ":(exclude)**/vendor/**",
    ":(exclude)**/*.lock",
]

# ─── GIT HELPERS ──────────────────────────────────────────────────────────────

def run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"❌ Error: {' '.join(cmd)}\n{result.stderr.strip()}")
        sys.exit(1)
    return result.stdout.strip()

def get_repo_name() -> str:
    """Get the name of the current repository."""
    try:
        remote = run(["git", "remote", "get-url", "origin"])
        name = re.split(r"[/:]", remote)[-1]
        return name.replace(".git", "")
    except SystemExit:
        return Path(run(["git", "rev-parse", "--show-toplevel"])).name

def ensure_git_repo():
    result = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        print("❌ Not inside a git repository. `cd` into the repo and try again.")
        sys.exit(1)

def get_current_branch() -> str:
    return run(["git", "rev-parse", "--abbrev-ref", "HEAD"])

def branch_exists(ref: str) -> bool:
    result = subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref],
                            capture_output=True, text=True)
    return result.returncode == 0

def find_merge_commit(base: str, branch_tip: str) -> str | None:
    """Find the merge commit in `base` whose second parent is the branch tip (already-merged branch)."""
    result = subprocess.run(
        ["git", "log", "--merges", "--format=%H %P", "-n", "500", base],
        capture_output=True, text=True)
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and branch_tip in parts[2:]:  # parents[1:] = merge parents except the mainline
            return parts[0]
    return None

def get_commits_from_branch(branch: str) -> list[str]:
    base = next((b for b in ("main", "master") if branch_exists(b)), None)
    if not base:
        print("❌ No local base branch (main/master). Use --from/--to or --commits.")
        sys.exit(1)

    commits = run(["git", "log", "--format=%H", f"{base}..{branch}"])
    if not commits:
        print(f"❌ Branch '{branch}' has no commits of its own vs '{base}'.")
        tip = run(["git", "rev-parse", branch])
        merge = find_merge_commit(base, tip)
        if merge:
            print(f"   Already merged (merge commit {merge[:8]}). Document it with:")
            print(f"     notate --commits {merge[:8]}")
        else:
            print("   Common cause: already merged. Document it with the merge SHA or its commits:")
            print( "     notate --commits <merge-sha>")
        sys.exit(1)
    return commits.splitlines()

def get_commits_in_range(from_sha: str, to_sha: str) -> list[str]:
    commits = run(["git", "log", "--format=%H", f"{from_sha}..{to_sha}"])
    return commits.splitlines() if commits else [to_sha]

def get_commit_info(sha: str) -> dict:
    return {
        "sha":     run(["git", "log", "--format=%H", "-1", sha])[:8],
        "message": run(["git", "log", "--format=%s",  "-1", sha]),
        "body":    run(["git", "log", "--format=%b",  "-1", sha]).strip(),
        "author":  run(["git", "log", "--format=%an", "-1", sha]),
        "date":    run(["git", "log", "--format=%ad", "--date=short", "-1", sha]),
    }

def git_show_file(ref: str, path: str) -> str | None:
    """Content of a file as it was at `ref` (not the working tree). None if absent/deleted."""
    r = subprocess.run(["git", "show", f"{ref}:{path}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None

def commit_parents(sha: str) -> list[str]:
    return run(["git", "rev-list", "--parents", "-n", "1", sha]).split()[1:]

def get_diff(commits: list[str]) -> str:
    if len(commits) == 1:
        sha = commits[0]
        if len(commit_parents(sha)) > 1:  # merge commit: `git show` emits no patch → diff vs first parent
            diff = run(["git", "diff", f"{sha}^1", sha, "--", *NOISE_PATHSPECS])
        else:
            diff = run(["git", "show", "--stat", "--patch", sha, "--", *NOISE_PATHSPECS])
    else:
        diff = run(["git", "diff", f"{commits[-1]}^", commits[0], "--", *NOISE_PATHSPECS])

    if len(diff) > MAX_DIFF_CHARS:
        cut = diff[:MAX_DIFF_CHARS]
        nl = cut.rfind("\n")            # cut on a line boundary, not mid-line
        if nl > 0:
            cut = cut[:nl]
        diff = cut + "\n\n[... diff truncated by size ...]"
    return diff

def get_modified_files_content(commits: list[str], max_chars: int = 30_000) -> str:
    """Read the content of the modified files as they were at the documented commit."""
    ref = commits[0]  # newest commit = the snapshot to read file contents from
    files_list = get_changed_files(commits)
    if not files_list:
        return ""

    extensions = (".ts", ".tsx", ".js", ".jsx", ".py", ".vue", ".rb", ".go",
                  ".java", ".kt", ".rs", ".php", ".cs", ".swift")
    files = [f for f in files_list if f.endswith(extensions)]

    if not files:
        return ""

    result = []
    total_chars = 0

    for filepath in files:
        # Content at the commit, not the working tree — matters for old/merged commits.
        file_content = git_show_file(ref, filepath)
        if file_content is None:  # deleted in this change, or unreadable
            continue

        lines = file_content.splitlines()
        if len(lines) > 300:
            file_content = "\n".join(lines[:300]) + "\n[... file truncated to 300 lines ...]"

        entry = f"=== FILE: {filepath} ===\n{file_content}\n"
        if total_chars + len(entry) > max_chars:
            result.append(f"=== FILE: {filepath} === [omitted due to size limit]\n")
            continue

        result.append(entry)
        total_chars += len(entry)

    return "\n".join(result)


def detect_doc_type(diff: str, forced_type: str | None) -> str:
    """Coarse hint that steers the prompt only (NOT a Notion property)."""
    if forced_type:
        return forced_type
    has_backend  = bool(re.search(r'\+\+\+ b/.*(route|controller|api|endpoint|view|serializer)', diff, re.I))
    has_frontend = bool(re.search(r'\+\+\+ b/.*(component|page|hook|\.tsx|\.vue|\.jsx)', diff, re.I))
    if has_backend and has_frontend:
        return "mixed"
    elif has_backend:
        return "api"
    elif has_frontend:
        return "frontend"
    return "mixed"

def get_changed_files(commits: list[str]) -> list[str]:
    """All paths touched by the change (noise excluded)."""
    if len(commits) == 1:
        sha = commits[0]
        if len(commit_parents(sha)) > 1:  # merge commit
            out = run(["git", "diff", "--name-only", f"{sha}^1", sha, "--", *NOISE_PATHSPECS])
        else:
            out = run(["git", "diff-tree", "--no-commit-id", "-r", "--name-only", sha, "--", *NOISE_PATHSPECS])
    else:
        out = run(["git", "diff", "--name-only", f"{commits[-1]}^", commits[0], "--", *NOISE_PATHSPECS])
    return out.splitlines() if out else []

# Canonical display order for the Areas multi-select.
AREA_ORDER = ["backend", "frontend", "db", "infra", "tests", "ci", "docs"]

def detect_areas(files: list[str]) -> list[str]:
    """Deterministic per-path area tagging — replaces the always-MIXED Type bucket."""
    found = set()
    for raw in files:
        p = raw.lower()
        base = p.rsplit("/", 1)[-1]
        if p.endswith((".tsx", ".jsx", ".vue", ".svelte")) or "/components/" in p or "/pages/" in p:
            found.add("frontend")
        if p.endswith(".py") or "/server/" in p or "/api/" in p or "/controllers/" in p or "/services/" in p:
            found.add("backend")
        if "/migrations/" in p or p.endswith(".sql") or "/models/" in p:
            found.add("db")
        if p.startswith("k8s/") or "/k8s/" in p or "/helm/" in p or "dockerfile" in base or p.endswith(".tf"):
            found.add("infra")
        if ".github/" in p or "gitlab-ci" in p:
            found.add("ci")
        if ("/tests/" in p or "__tests__" in p or base.startswith("test_")
                or base.endswith((".spec.ts", ".test.ts", ".spec.tsx", ".test.tsx"))
                or base.endswith(("_test.go", "_test.py"))):
            found.add("tests")
        if p.endswith((".md", ".mdx", ".rst")):
            found.add("docs")
    ordered = [a for a in AREA_ORDER if a in found]
    return ordered or (["other"] if files else [])

# ─── TRACEABILITY (repo slug, PR, ticket) ─────────────────────────────────────

def parse_repo_slug(remote: str) -> str | None:
    """owner/repo from an ssh or https remote URL (host aliases included)."""
    m = re.search(r"[:/]([^/]+/[^/]+?)(?:\.git)?$", remote)
    return m.group(1) if m else None

def get_repo_slug() -> str | None:
    try:
        return parse_repo_slug(run(["git", "remote", "get-url", "origin"]))
    except SystemExit:
        return None

def extract_pr_number(commit_infos: list[dict]) -> str | None:
    for c in commit_infos:
        m = re.search(r"#(\d+)", c.get("message", ""))
        if m:
            return m.group(1)
    return None

def extract_ticket(branch: str | None, commit_infos: list[dict]) -> str | None:
    """Ticket id (e.g. NIXON-305) from the branch name or commit messages/bodies."""
    pat = re.compile(r"\b([A-Z]{2,}-\d+)\b")
    if branch:
        m = pat.search(branch)
        if m:
            return m.group(1)
    for c in commit_infos:
        for field in (c.get("message", ""), c.get("body", "")):
            m = pat.search(field or "")
            if m:
                return m.group(1)
    return None

def build_pr_url(slug: str | None, pr: str | None, commit_infos: list[dict]) -> str | None:
    if not slug:
        return None
    if pr:
        return f"https://github.com/{slug}/pull/{pr}"
    if commit_infos:
        return f"https://github.com/{slug}/commit/{commit_infos[0]['sha']}"
    return None

# ─── CLAUDE ───────────────────────────────────────────────────────────────────

def build_prompt(diff: str, commit_infos: list[dict], doc_type: str,
                 file_contents: str = "", branch: str | None = None) -> str:
    def fmt_commit(c: dict) -> str:
        line = f"- [{c['date']}] {c['sha']} — {c['message']} ({c['author']})"
        body = c.get("body", "").strip()
        if body:  # the commit body often holds the rationale ("why")
            line += "\n  " + body.replace("\n", "\n  ")
        return line

    commits_summary = "\n".join(fmt_commit(c) for c in commit_infos)

    branch_line = (
        f"BRANCH NAME (often encodes the ticket id and intent): {branch}\n\n"
        if branch else ""
    )

    type_instructions = {
        "api": """
For each endpoint found, include:
- HTTP method and route
- Endpoint description
- Request parameters (headers, query params, body) with types
- Response structure with types and status codes
- Usage example (curl or code)
- Common errors
""",
        "frontend": """
For each component found, include:
- Description and purpose
- Props (name, type, required, default value)
- Emitted events (if any)
- Slots or children (if any)
- Visual states (loading, error, empty, etc.)
- Usage example with code
- External dependencies used
""",
        "mixed": """
Split clearly into two sections:
1. **Backend / API**: endpoints, routes, parameters, responses, examples
2. **Frontend / Components**: components, props, events, usage examples
"""
    }

    file_contents_section = file_contents if file_contents.strip() else "[Could not read the modified files]"

    return f"""You are a technical assistant specialized in software documentation with a teaching mindset.
Your task is to analyze a Git diff and generate accurate technical documentation for a development team.

TARGET AUDIENCE: JUNIOR developers getting familiar with the codebase.
So, beyond being accurate, the documentation must TEACH: explain the "why" behind the
"what", name the techniques, design patterns and theoretical concepts being applied, and include
concrete examples. When you use a technical term (e.g. "dependency injection", "memoization",
"idempotency", "guard clause"), define it briefly the first time in plain language.

{branch_line}INCLUDED COMMITS:
{commits_summary}

IMPLEMENTATION TYPE: {doc_type}

═══════════════════════════════════════════
STEP 1 — EXTRACT REAL CODE
Before writing anything, locate the following pieces of code in the diff.
Copy them LITERALLY as they appear in the diff (lines starting with +):

A) New or modified interfaces and types (interface, type, enum)
B) Function and hook signatures (export function, export const, const x = () =>)
C) React component props (interface XxxProps, type XxxProps)
D) Snippets showing how the implementation is used in real context
E) Any core logic that is key to understand (reducers, effects, callbacks)

CRITICAL RULE: If you can't find the snippet literally in the diff, do NOT include it.
Never invent, reconstruct or paraphrase code. Only copy real lines from the diff.
═══════════════════════════════════════════

STEP 2 — DOCUMENTATION

Using ONLY the code extracted in Step 1, generate the documentation with these sections:

1. **Title** — descriptive name of the feature (do not use the literal commit message)
2. **Overview** — what this implementation does, why it exists, context
3. **Data flow** — IMPORTANT: explain how all files/modules connect to each other.
   Describe the full flow step by step, from the initial event to the final result.
   Use a text diagram with arrows (→) showing what calls what and how data flows.
   Example: "User does X → ComponentA calls hookB → hookB updates storeC → ComponentD reacts"
   Include ALL modified files and how they relate to each other.
4. **Data contracts** — Extract from the diff ALL interfaces, types and enums of the Store and data models.
   Include each field with its type. If there are Store interfaces, props or relevant models, include them in full.
5. **Technical details** — depending on the type:
{type_instructions.get(doc_type, type_instructions['mixed'])}
   For each element, document its REAL signature/interface extracted from the diff.
6. **Error handling and edge cases** — Analyze the code and identify:
   - What happens when an operation fails (error callbacks, failure states)
   - Behavior on state loss (page reload, tab close)
   - Possible race conditions (multiple simultaneous operations)
   - If something is not implemented, state it explicitly as a gap
7. **Technical decisions** — why this approach, discarded alternatives, trade-offs
9. **Breaking changes** — Detailed list of changes that break compatibility:
    - What exactly changed (signature, behavior, contract)
    - Which files outside the diff could be affected
    - What needs to be reviewed/updated
11. **Usage examples** — REAL snippets from the diff showing usage
12. **TODOs / Future improvements** — technical debt found in the diff (TODO, FIXMEs, commented code)
13. **Notes** — anything relevant for the team
14. **Concepts and techniques (for juniors)** — TEACHING section. Identify the techniques, design patterns
    and theoretical concepts that appear in this code (e.g. pipeline, wrapper/DRY, guard clause, lookup
    table, dependency injection, error handling with try/except, serialization, idempotency,
    heuristic, etc.). For EACH one:
    - Name it and define it in 1-2 simple sentences, as if for someone seeing it for the first time.
    - Point out WHERE it appears in this change (file/function) citing a real line from the diff.
    - Explain WHY it is used here and what problem it solves.
    - If useful, add a mini-example or an analogy.
    End with 2-3 "topics to go deeper" (concepts the junior should study next).

GENERAL RULES:
- Every code block must be real code copied from the diff
- If you can't infer something from the diff, use [TODO]
- The reader is a JUNIOR developer: prioritize clarity, define terms and teach the why
- In "breaking_changes" be especially thorough — it is critical for the team

Respond ONLY with valid JSON, no markdown, no extra text:
{{
  "title": "string",
  "overview": "string (markdown allowed)",
  "data_flow": "string (markdown allowed, with a text flow diagram using →)",
  "data_contracts": "string (markdown allowed, with code blocks of real interfaces and types)",
  "technical_details": "string (markdown allowed, with real code blocks from the diff)",
  "error_handling": "string (markdown allowed)",
  "decisions": "string (markdown allowed)",
  "breaking_changes": "string (markdown allowed, detailed list with affected files)",
  "examples": "string (markdown allowed, with real code blocks from the diff)",
  "todos": ["string"],
  "notes": "string (markdown allowed)",
  "concepts": "string (markdown allowed, teaching section for juniors: techniques/patterns/concepts with definition, where they appear, why, example and topics to go deeper)"
}}

FULL MODIFIED FILES:
The following section contains the full current content of the modified files.
Use them to understand the full usage context — how the components/hooks are called,
where they are used from, what imports they have, etc. This context complements the diff.
{file_contents_section}

DIFF (concrete changes introduced):
{diff}
"""

# JSON Schema for structured outputs — guarantees Claude returns valid, parseable
# JSON instead of best-effort prose we have to clean up. All fields are required;
# the model fills empties with "" or "[TODO]" as instructed in the prompt.
DOC_SCHEMA = {
    "type": "object",
    "properties": {
        "title":             {"type": "string"},
        "overview":          {"type": "string"},
        "data_flow":         {"type": "string"},
        "data_contracts":    {"type": "string"},
        "technical_details": {"type": "string"},
        "error_handling":    {"type": "string"},
        "decisions":         {"type": "string"},
        "breaking_changes":  {"type": "string"},
        "examples":          {"type": "string"},
        "todos":             {"type": "array", "items": {"type": "string"}},
        "notes":             {"type": "string"},
        "concepts":          {"type": "string"},
    },
    "required": [
        "title", "overview", "data_flow", "data_contracts", "technical_details",
        "error_handling", "decisions", "breaking_changes", "examples",
        "todos", "notes", "concepts",
    ],
    "additionalProperties": False,
}

CLAUDE_TIMEOUT = 300  # seconds

def _spinner(stop: threading.Event, label: str):
    frames = itertools.cycle("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")
    start = time.time()
    while not stop.is_set():
        elapsed = int(time.time() - start)
        sys.stdout.write(f"\r{label} {next(frames)} {elapsed}s")
        sys.stdout.flush()
        time.sleep(0.1)
    sys.stdout.write("\r" + " " * (len(label) + 14) + "\r")
    sys.stdout.flush()

def call_claude(prompt: str) -> dict:
    import urllib.request, urllib.error

    payload = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": 16000,
        "messages": [{"role": "user", "content": prompt}],
        "output_config": {"format": {"type": "json_schema", "schema": DOC_SCHEMA}},
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01"
        }
    )

    label = "🤖 Analyzing commits with Claude..."
    stop = threading.Event()
    spinner = threading.Thread(target=_spinner, args=(stop, label), daemon=True)
    start = time.time()
    spinner.start()
    try:
        with urllib.request.urlopen(req, timeout=CLAUDE_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        stop.set(); spinner.join()
        print(f"❌ Claude API error: {e.code} {e.read().decode()}")
        sys.exit(1)
    except (socket.timeout, TimeoutError):
        stop.set(); spinner.join()
        print(f"❌ Timeout: Claude did not respond in {CLAUDE_TIMEOUT}s. Check your network/VPN and retry.")
        sys.exit(1)
    except urllib.error.URLError as e:
        stop.set(); spinner.join()
        print(f"❌ Connection to Claude failed: {e.reason}. Check your network/VPN and retry.")
        sys.exit(1)
    except KeyboardInterrupt:
        stop.set(); spinner.join()
        print("⛔ Interrupted by the user.")
        sys.exit(130)

    stop.set(); spinner.join()
    print(f"✅ Analysis complete in {int(time.time() - start)}s")

    raw = data["content"][0]["text"].strip()
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"❌ Claude did not return valid JSON: {e}\n{raw[:500]}")
        sys.exit(1)

# ─── NOTION ───────────────────────────────────────────────────────────────────

def notion_request(method: str, path: str, body: dict | None = None) -> dict:
    import urllib.request, urllib.error

    payload = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        f"https://api.notion.com/v1{path}",
        data=payload,
        method=method,
        headers={
            "Authorization": f"Bearer {NOTION_API_KEY}",
            "Content-Type": "application/json",
            "Notion-Version": "2022-06-28"
        }
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"❌ Notion API error: {e.code} {e.read().decode()}")
        sys.exit(1)

NOTION_LANGUAGE_MAP = {
    "tsx": "typescript",
    "jsx": "javascript",
    "sh": "shell",
    "zsh": "shell",
    "py": "python",
    "rb": "ruby",
    "rs": "rust",
    "kt": "kotlin",
    "tf": "hcl",
    "yml": "yaml",
    "md": "markdown",
    "vue": "javascript",
    "svelte": "javascript",
}

def normalize_language(lang: str) -> str:
    lang = lang.lower().strip()
    return NOTION_LANGUAGE_MAP.get(lang, lang) if lang else "plain text"

def parse_inline(text: str) -> list[dict]:
    """Parse inline bold (**text**), italic (*text*) and code (`text`)."""
    parts = []
    # Regex capturing **bold**, *italic* and `code`
    pattern = re.compile(r'\*\*(.+?)\*\*|\*(.+?)\*|`(.+?)`')
    last = 0
    for m in pattern.finditer(text):
        # Plain text before the match
        if m.start() > last:
            parts.append({"type": "text", "text": {"content": text[last:m.start()]}})
        if m.group(1) is not None:  # **bold**
            parts.append({"type": "text", "text": {"content": m.group(1)},
                          "annotations": {"bold": True, "italic": False, "strikethrough": False,
                                          "underline": False, "code": False, "color": "default"}})
        elif m.group(2) is not None:  # *italic*
            parts.append({"type": "text", "text": {"content": m.group(2)},
                          "annotations": {"bold": False, "italic": True, "strikethrough": False,
                                          "underline": False, "code": False, "color": "default"}})
        elif m.group(3) is not None:  # `code`
            parts.append({"type": "text", "text": {"content": m.group(3)},
                          "annotations": {"bold": False, "italic": False, "strikethrough": False,
                                          "underline": False, "code": True, "color": "default"}})
        last = m.end()
    # Remaining text
    if last < len(text):
        parts.append({"type": "text", "text": {"content": text[last:]}})
    return parts if parts else [{"type": "text", "text": {"content": text}}]


def md_to_notion_blocks(text: str) -> list[dict]:
    """Convert simple markdown into Notion blocks."""
    blocks = []
    if not text or not text.strip():
        return blocks

    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]

        # Detect start of a markdown table
        if line.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[-| :]+\|", lines[i + 1]):
            # Collect all table rows
            table_lines = []
            while i < len(lines) and lines[i].startswith("|"):
                table_lines.append(lines[i])
                i += 1

            # Parse header (first row) and separator (second row); the rest are data
            header_row = [c.strip() for c in table_lines[0].strip("|").split("|")]
            data_rows  = [
                [c.strip() for c in row.strip("|").split("|")]
                for row in table_lines[2:]  # skip the separator row
                if row.strip() and not re.match(r"^\|[-| :]+\|", row)
            ]

            # Data rows — pad if columns are missing
            rows_blocks = []
            for row in data_rows:
                while len(row) < len(header_row):
                    row.append("")
                rows_blocks.append({
                    "type": "table_row",
                    "table_row": {"cells": [[{"type": "text", "text": {"content": cell}}] for cell in row[:len(header_row)]]}
                })

            blocks.append({
                "object": "block",
                "type": "table",
                "table": {
                    "table_width": len(header_row),
                    "has_column_header": True,
                    "has_row_header": False,
                    "children": [
                        {
                            "type": "table_row",
                            "table_row": {"cells": [[{"type": "text", "text": {"content": h}, "annotations": {"bold": True, "italic": False, "strikethrough": False, "underline": False, "code": False, "color": "default"}}] for h in header_row]}
                        },
                        *rows_blocks
                    ]
                }
            })
            continue  # i already advanced in the inner while loop

        if line.startswith("```"):
            lang = normalize_language(line[3:].strip())
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code_lines.append(lines[i])
                i += 1
            blocks.append({
                "object": "block", "type": "code",
                "code": {
                    "rich_text": [{"type": "text", "text": {"content": "\n".join(code_lines)}}],
                    "language": lang
                }
            })
        elif line.startswith("### "):
            blocks.append({"object": "block", "type": "heading_3",
                "heading_3": {"rich_text": parse_inline(line[4:])}})
        elif line.startswith("## "):
            blocks.append({"object": "block", "type": "heading_2",
                "heading_2": {"rich_text": parse_inline(line[3:])}})
        elif line.startswith("# "):
            blocks.append({"object": "block", "type": "heading_1",
                "heading_1": {"rich_text": parse_inline(line[2:])}})
        elif re.match(r"^[-*] ", line):
            blocks.append({"object": "block", "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": parse_inline(line[2:])}})
        elif re.match(r"^\d+\. ", line):
            content = re.sub(r"^\d+\. ", "", line)
            blocks.append({"object": "block", "type": "numbered_list_item",
                "numbered_list_item": {"rich_text": parse_inline(content)}})
        elif line.strip():
            blocks.append({"object": "block", "type": "paragraph",
                "paragraph": {"rich_text": parse_inline(line)}})

        i += 1
    return blocks

def section_header(title: str, emoji: str) -> list[dict]:
    return [
        {"object": "block", "type": "divider", "divider": {}},
        {"object": "block", "type": "heading_2",
         "heading_2": {"rich_text": [{"type": "text", "text": {"content": f"{emoji} {title}"}}]}}
    ]

def build_page_blocks(doc: dict) -> list[dict]:
    blocks = []

    blocks += section_header("Overview", "📖")
    blocks += md_to_notion_blocks(doc.get("overview", ""))

    def optional_section(key, title, emoji):
        value = doc.get(key, "").strip()
        if value and value not in ("[TODO]", "—", ""):
            blocks.extend(section_header(title, emoji))
            blocks.extend(md_to_notion_blocks(value))

    optional_section("data_flow",       "Data flow",                      "🔄")
    optional_section("data_contracts",  "Data contracts",                 "📐")
    blocks += section_header("Technical details", "⚙️")
    blocks += md_to_notion_blocks(doc.get("technical_details", ""))
    optional_section("error_handling",  "Error handling and edge cases",  "⚠️")
    optional_section("decisions",       "Technical decisions",            "🧠")

    breaking = doc.get("breaking_changes", "").strip()
    if breaking and breaking not in ("[TODO]", "—", ""):
        blocks += section_header("Breaking Changes", "🚨")
        blocks += md_to_notion_blocks(breaking)

    optional_section("examples", "Usage examples", "💡")

    blocks += section_header("TODOs / Future improvements", "📋")
    todos = doc.get("todos", [])
    if todos:
        for todo in todos:
            blocks.append({
                "object": "block", "type": "to_do",
                "to_do": {
                    "rich_text": [{"type": "text", "text": {"content": todo}}],
                    "checked": False
                }
            })
    else:
        blocks.append({"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": "—"}}]}})

    optional_section("notes", "Notes", "📝")
    optional_section("concepts", "Concepts and techniques (for juniors)", "🎓")

    return blocks

def create_notion_entry(doc: dict, commit_infos: list[dict], areas: list[str],
                        ticket: str | None, pr_url: str | None, repo: str) -> str:
    """Create a new entry in the Notion database."""
    today        = datetime.now().strftime("%Y-%m-%d")
    commits_text = ", ".join(c["sha"] for c in commit_infos)

    properties = {
        "title": {
            "title": [{"type": "text", "text": {"content": doc["title"]}}]
        },
        "Areas": {
            "multi_select": [{"name": a} for a in areas]
        },
        "Date": {
            "date": {"start": today}
        },
        "Project": {
            "rich_text": [{"type": "text", "text": {"content": repo}}]
        },
        "Commits": {
            "rich_text": [{"type": "text", "text": {"content": commits_text}}]
        }
    }
    if ticket:
        properties["Ticket"] = {"select": {"name": ticket}}
    if pr_url:
        properties["PR"] = {"url": pr_url}

    blocks = build_page_blocks(doc)

    print("📝 Creating Notion entry...")
    data = notion_request("POST", "/pages", {
        "parent":     {"database_id": NOTION_DATABASE_ID},
        "icon":       {"type": "emoji", "emoji": "📄"},
        "properties": properties,
        "children":   blocks[:100]   # Notion: max 100 blocks per request
    })

    page_id = data["id"]

    # Extra blocks if there are more than 100
    remaining = blocks[100:]
    while remaining:
        notion_request("PATCH", f"/blocks/{page_id}/children", {"children": remaining[:100]})
        remaining = remaining[100:]

    return data.get("url", f"https://notion.so/{page_id.replace('-', '')}")

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def validate_env():
    missing = [k for k, v in {
        "ANTHROPIC_API_KEY":       ANTHROPIC_API_KEY,
        "NOTION_API_KEY":          NOTION_API_KEY,
        "NOTION_DOCS_DATABASE_ID": NOTION_DATABASE_ID
    }.items() if not v]

    if missing:
        print(f"❌ Missing variables in {ENV_FILE}:")
        for m in missing:
            print(f"   {m}=...")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(
        description="Generate technical documentation as an entry in a Notion database"
    )
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--branch",  help="Branch to document (default: current branch)")
    group.add_argument("--from",    dest="from_sha", help="Starting commit SHA")
    group.add_argument("--commits", nargs="+", help="Explicit list of commit SHAs")

    parser.add_argument("--to",      dest="to_sha", help="Ending SHA (use with --from)")
    parser.add_argument("--type",    choices=["api", "frontend", "mixed"],
                        help="Force the type (auto-detected from the diff by default)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the generated JSON without writing to Notion")

    args = parser.parse_args()

    ensure_git_repo()

    if not args.dry_run:
        validate_env()
    elif not ANTHROPIC_API_KEY:
        print(f"❌ Missing ANTHROPIC_API_KEY in {ENV_FILE}")
        sys.exit(1)

    # Resolve commits
    branch_name = None
    if args.from_sha:
        to = args.to_sha or "HEAD"
        print(f"🔍 Analyzing commits: {args.from_sha}..{to}")
        commits = get_commits_in_range(args.from_sha, to)
    elif args.commits:
        commits = args.commits
        print(f"🔍 Using {len(commits)} specified commit(s)")
    else:
        branch_name = args.branch or get_current_branch()
        if branch_name in ("main", "master", "HEAD"):
            print("❌ You are on the base branch (or detached HEAD). Switch to your feature branch, or use --commits/--from.")
            sys.exit(1)
        print(f"🔍 Analyzing branch: {branch_name}")
        commits = get_commits_from_branch(branch_name)

    if not commits:
        print("❌ No commits found")
        sys.exit(1)

    print(f"   {len(commits)} commit(s) found")

    commit_infos = [get_commit_info(sha) for sha in commits]
    diff         = get_diff(commits)
    doc_type     = detect_doc_type(diff, args.type)   # prompt hint only
    repo         = get_repo_name()

    areas  = detect_areas(get_changed_files(commits))
    ticket = extract_ticket(branch_name, commit_infos)
    pr_url = build_pr_url(get_repo_slug(), extract_pr_number(commit_infos), commit_infos)

    print(f"   Repository:    {repo}")
    print(f"   Areas:         {', '.join(areas) or '—'}")
    if ticket:
        print(f"   Ticket:        {ticket}")
    if pr_url:
        print(f"   PR:            {pr_url}")

    print("   Reading modified files...")
    file_contents = get_modified_files_content(commits)
    doc = call_claude(build_prompt(diff, commit_infos, doc_type, file_contents, branch=branch_name))

    if args.dry_run:
        print("\n📄 RESULT (dry-run, nothing written to Notion):\n")
        print(json.dumps(doc, indent=2, ensure_ascii=False))
        return

    url = create_notion_entry(doc, commit_infos, areas, ticket, pr_url, repo)

    print(f"\n✅ Documentation created in Notion")
    print(f"   🔗 {url}")

if __name__ == "__main__":
    main()
