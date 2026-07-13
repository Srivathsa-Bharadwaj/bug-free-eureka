# 🤖 Multi-Agent AI Code Reviewer

A zero-cost, fully automated PR review pipeline that runs on GitHub Actions using a **locally hosted LLM** — no API keys, no rate limits, no cloud spend.

When a PR is opened, the system detects every language in the diff, runs the right static analysis tools for each language, feeds the results to a local LLM, posts a structured review comment, and opens a **draft auto-fix PR** with corrected files for you to review before merging.

---

## How it works

```
PR opened / updated
        │
        ▼
Detect languages in diff
(Python, JS, Go, Shell, YAML, SQL, Dockerfile, JSON, TS)
        │
        ▼
Run per-language static analysis tools
(ruff · bandit · radon · pytest · eslint · shellcheck
 yamllint · hadolint · sqlfluff · go vet)
        │
        ▼
Local LLM (Ollama / qwen2.5-coder) reviews diff + tool output
        │
        ├──► Post review comment on original PR
        │
        └──► For each file with findings:
                 Fetch file → LLM generates fix → commit to ai-fixes/pr-N
                 Open draft PR linked to original for human review
```

---

## Features

| Feature | Detail |
|---|---|
| **Zero cost** | Ollama runs `qwen2.5-coder:1.5b` directly on the GitHub Actions runner — no API key needed |
| **9 languages** | Python, JavaScript, TypeScript, Go, Shell, YAML, JSON, SQL, Dockerfile |
| **11 static analysis tools** | Per-language tools run before the LLM so findings are grounded in real output |
| **Auto-fixer** | Commits corrected files to a separate branch and opens a draft PR |
| **LLM fallback fixing** | If only the LLM (not static tools) found issues in a file, those findings are still used to drive auto-fix |
| **Hallucination suppression** | Prompt guardrails + post-processing deduplication cap findings at 5 per PR |
| **Self-modification prevention** | `.github/` files are excluded from both review and auto-fix |
| **Security hardened** | All subprocess calls validated, `shell=False` enforced, path traversal blocked |
| **Fully configurable** | All caps and endpoints controlled via environment variables |

---

## Setup

### 1. Copy the files into your repo

```
your-repo/
├── .github/
│   ├── workflows/
│   │   └── review.yml
│   └── scripts/
│       └── review_agent.py
```

### 2. Make your repo public *(recommended)*

Go to **Settings → Danger Zone → Change visibility → Public**

Public repos get **unlimited free GitHub Actions minutes**. Private repos are capped at 2,000 min/month (each review run costs ~3 min).

### 3. Set the workflow permissions

In your repo: **Settings → Actions → General → Workflow permissions**
Select **Read and write permissions** and save.

### 4. Open any PR

That's it. The reviewer triggers automatically on every PR open, update, or reopen.

No secrets to add — `GITHUB_TOKEN` is injected automatically by GitHub Actions.

---

## Configuration

All settings are optional environment variables. Add them to the `env:` block in `review.yml`:

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_HOST` | `http://localhost:11434` | Point at a remote Ollama instance |
| `OLLAMA_MODEL` | `qwen2.5-coder:1.5b` | Any model available in your Ollama install |
| `MAX_FILES_TO_FIX` | `10` | Max files the auto-fixer will touch per PR |
| `MAX_LINES_TO_FIX` | `500` | Files longer than this are skipped by auto-fixer |
| `MAX_FINDINGS_PER_REVIEW` | `5` | Max findings shown in the review comment |

Example — add to the `env:` block in `review.yml`:

```yaml
env:
  GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
  PR_NUMBER: ${{ github.event.pull_request.number }}
  REPO: ${{ github.repository }}
  OLLAMA_MODEL: "qwen2.5-coder:7b"   # use a larger model
  MAX_FILES_TO_FIX: "5"
  MAX_LINES_TO_FIX: "300"
  MAX_FINDINGS_PER_REVIEW: "3"
```

---

## Static analysis tools — what runs and when

| Language | Tools |
|---|---|
| Python | `ruff` (lint) · `bandit` (security) · `radon` (complexity) · `pytest` (tests) |
| JavaScript / TypeScript | `eslint` |
| Shell / Bash | `shellcheck` |
| YAML | `yamllint` |
| JSON | Python built-in `json.tool` |
| SQL | `sqlfluff` |
| Dockerfile | `hadolint` |
| Go | `go vet` |
| Everything else | LLM reviews raw diff only |

Tools that aren't installed are skipped gracefully with a log message — the LLM still reviews the diff.

---

## What you get on a PR

**Comment 1 — Review:**
```
🤖 AI Code Review
Languages detected: `python` `yaml`
Tools run: `Ruff` `Bandit` `Radon` `Pytest` `yamllint`

## Summary
This PR adds an API client with authentication and user management methods.

## Issues found
🔴 High · api_client.py:20 — No validation on `auth_type`...
🟡 Medium · api_client.py:30 — `make_request` has no exception handling...
🟢 Low · api_client.py:40 — `get_user` does not validate `user_id` type...

## Suggestions
...
```

**Comment 2 — Auto-fix link (if fixes were committed):**
```
🔧 Auto-fix PR ready
Applied fixes to 1 file(s). Draft PR for your review:
👉 https://github.com/you/repo/pull/42
Files fixed: `api_client.py`
```

---

## Safety design

- **Human in the loop always** — fixes go to a draft PR, never auto-merged
- **Self-modification blocked** — `.github/` and `review_agent.py` are excluded from both review and auto-fix
- **No shell injection** — all subprocess args validated against an allowlist regex, `shell=False` enforced everywhere
- **Path traversal blocked** — absolute paths and `..` segments rejected before any file operation
- **Sanity checks** — empty or identical LLM outputs are never committed
- **File and line caps** — bound runner cost and prevent runaway LLM usage on large PRs

---

## Stack

- **Runtime**: Python 3.11
- **LLM**: [Ollama](https://ollama.com) (`qwen2.5-coder:1.5b` by default)
- **CI**: GitHub Actions
- **Static analysis**: ruff, bandit, radon, pytest, eslint, shellcheck, yamllint, hadolint, sqlfluff, go vet
- **GitHub API**: Contents API (file read/write), Reviews API (comments), Git Refs API (branch creation)

---

## Project structure

```
.github/
├── workflows/
│   └── review.yml          # GitHub Actions workflow — triggers on PR events
└── scripts/
    └── review_agent.py     # Main agent — ~1000 lines, fully self-contained
```

`review_agent.py` is intentionally a single file with no external dependencies beyond
`requests` — easy to audit, easy to copy into any repo.

---

## Troubleshooting

**Workflow fails with exit code 1**
Check the Actions log. Common causes: Ollama didn't start in time (increase `sleep` in yml), or a required env var is missing.

**No review comment appears**
Check workflow permissions — the repo needs **Read and write permissions** under Settings → Actions → General.

**Auto-fix branch never created**
Either all changed files are in `.github/`, over the line limit, or the LLM produced no changes. Check the Actions log for `skipping` lines.

**Review flags issues that don't exist**
The small model (1.5b) occasionally hallucinates. Switch to `qwen2.5-coder:7b` via `OLLAMA_MODEL` for significantly better accuracy (slower first run — model is ~5GB).

---

## License

MIT — copy, modify, and use freely.