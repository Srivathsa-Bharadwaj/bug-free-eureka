import os, json, requests, subprocess, time, base64
from collections import defaultdict

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
PR_NUMBER = os.environ["PR_NUMBER"]
REPO = os.environ["REPO"]

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "qwen2.5-coder:1.5b"

# ── Auto-fix safety caps ───────────────────────────────────────────────────────
MAX_FILES_TO_FIX = 10        # never auto-fix more than 10 files per PR
MAX_LINES_TO_FIX = 500       # skip files longer than 500 lines

GH_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28"
}

# ── File type detection ────────────────────────────────────────────────────────

EXTENSION_MAP = {
    ".py":         "python",
    ".js":         "javascript",
    ".ts":         "typescript",
    ".jsx":        "javascript",
    ".tsx":        "typescript",
    ".java":       "java",
    ".go":         "go",
    ".sh":         "shell",
    ".bash":       "shell",
    ".yml":        "yaml",
    ".yaml":       "yaml",
    ".json":       "json",
    ".sql":        "sql",
    ".tf":         "terraform",
    ".dockerfile": "docker",
}

SPECIAL_FILENAMES = {
    "dockerfile": "docker",
    "makefile":   "shell",
}

def detect_language(filename):
    base = os.path.basename(filename).lower()
    if base in SPECIAL_FILENAMES:
        return SPECIAL_FILENAMES[base]
    ext = os.path.splitext(filename)[1].lower()
    return EXTENSION_MAP.get(ext, "unknown")

def group_files_by_language(files):
    groups = defaultdict(list)
    for f in files:
        lang = detect_language(f["filename"])
        groups[lang].append(f)
    return dict(groups)

# ── GitHub helpers ─────────────────────────────────────────────────────────────

def get_pr_info():
    url = f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}"
    resp = requests.get(url, headers=GH_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()

def get_pr_files():
    url = f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}/files"
    resp = requests.get(url, headers=GH_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()

def get_file_content(filepath, ref):
    url = f"https://api.github.com/repos/{REPO}/contents/{filepath}?ref={ref}"
    resp = requests.get(url, headers=GH_HEADERS, timeout=30)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return base64.b64decode(resp.json()["content"]).decode("utf-8", errors="replace")

def get_diff_text(files):
    chunks = []
    for f in files:
        if f.get("patch"):
            lang = detect_language(f["filename"])
            chunks.append(f"File: {f['filename']} (language: {lang})\n{f['patch']}")
    full = "\n\n---\n\n".join(chunks[:8])
    return full[:8000]

def get_branch_sha(branch):
    url = f"https://api.github.com/repos/{REPO}/git/ref/heads/{branch}"
    resp = requests.get(url, headers=GH_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()["object"]["sha"]

def create_branch(branch_name, sha):
    url = f"https://api.github.com/repos/{REPO}/git/refs"
    payload = {"ref": f"refs/heads/{branch_name}", "sha": sha}
    resp = requests.post(url, headers=GH_HEADERS, json=payload, timeout=30)
    if resp.status_code == 422:
        print(f"  Branch {branch_name} already exists, reusing.")
        return
    resp.raise_for_status()

def get_file_sha(filepath, branch):
    url = f"https://api.github.com/repos/{REPO}/contents/{filepath}?ref={branch}"
    resp = requests.get(url, headers=GH_HEADERS, timeout=30)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()["sha"]

def commit_file(filepath, content, branch, message):
    url = f"https://api.github.com/repos/{REPO}/contents/{filepath}"
    file_sha = get_file_sha(filepath, branch)
    payload = {
        "message": message,
        "content": base64.b64encode(content.encode()).decode(),
        "branch": branch,
    }
    if file_sha:
        payload["sha"] = file_sha
    resp = requests.put(url, headers=GH_HEADERS, json=payload, timeout=30)
    resp.raise_for_status()

def create_draft_pr(head_branch, base_branch, pr_title, pr_body):
    url = f"https://api.github.com/repos/{REPO}/pulls"
    payload = {
        "title": pr_title,
        "body": pr_body,
        "head": head_branch,
        "base": base_branch,
        "draft": True
    }
    resp = requests.post(url, headers=GH_HEADERS, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()["html_url"]

def post_comment(body, languages_found, tool_names):
    lang_badges = " ".join(f"`{l}`" for l in sorted(languages_found))
    tools_used = " ".join(f"`{t}`" for t in tool_names) or "LLM only"
    header = (
        f"## 🤖 AI Code Review\n\n"
        f"**Languages detected:** {lang_badges}  \n"
        f"**Tools run:** {tools_used}\n\n---\n\n"
    )
    url = f"https://api.github.com/repos/{REPO}/issues/{PR_NUMBER}/comments"
    resp = requests.post(url, headers=GH_HEADERS, json={"body": header + body}, timeout=30)
    resp.raise_for_status()
    print(f"  Review comment posted: {resp.json()['html_url']}")

# ── Static analysis tools ──────────────────────────────────────────────────────

def tool_exists(name):
    return subprocess.run(["which", name], capture_output=True).returncode == 0

def run_ruff(files):
    targets = [f["filename"] for f in files if os.path.exists(f["filename"])]
    if not targets:
        return None
    result = subprocess.run(
        ["ruff", "check", "--output-format=json"] + targets,
        capture_output=True, text=True
    )
    try:
        issues = json.loads(result.stdout)
        if not issues:
            return "Ruff: no issues found."
        lines = [
            f"- {i['filename']}:{i['location']['row']} [{i['code']}] {i['message']}"
            for i in issues[:20]
        ]
        return "Ruff (Python lint):\n" + "\n".join(lines)
    except Exception:
        return result.stdout[:500] or None

def run_bandit(files):
    targets = [f["filename"] for f in files if os.path.exists(f["filename"])]
    if not targets:
        return None
    result = subprocess.run(
        ["bandit", "-r", "-f", "json"] + targets,
        capture_output=True, text=True
    )
    try:
        data = json.loads(result.stdout)
        issues = data.get("results", [])
        if not issues:
            return "Bandit: no security issues found."
        lines = [
            f"- {i['filename']}:{i['line_number']} [{i['issue_severity']}] {i['issue_text']}"
            for i in issues[:15]
        ]
        return "Bandit (Python security):\n" + "\n".join(lines)
    except Exception:
        return result.stdout[:500] or None

def run_radon(files):
    targets = [f["filename"] for f in files if os.path.exists(f["filename"])]
    if not targets:
        return None
    result = subprocess.run(
        ["radon", "cc", "--min", "B", "--show-complexity", "-s"] + targets,
        capture_output=True, text=True
    )
    out = result.stdout.strip()
    return f"Radon (Python complexity):\n{out[:800]}" if out else "Radon: no complex functions found."

def run_pytest():
    if not tool_exists("pytest"):
        return None
    result = subprocess.run(
        ["pytest", "--tb=short", "-q"],
        capture_output=True, text=True
    )
    output = (result.stdout + result.stderr).strip()
    return f"Pytest:\n{output[:800]}" if output else None

def run_eslint(files):
    if not tool_exists("eslint"):
        return "ESLint not installed — skipped. Add `npm install -g eslint` to workflow to enable."
    targets = [f["filename"] for f in files if os.path.exists(f["filename"])]
    if not targets:
        return None
    result = subprocess.run(
        ["eslint", "--format=compact"] + targets,
        capture_output=True, text=True
    )
    out = (result.stdout + result.stderr).strip()
    return f"ESLint (JS/TS lint):\n{out[:800]}" if out else "ESLint: no issues found."

def run_shellcheck(files):
    if not tool_exists("shellcheck"):
        return "ShellCheck not installed — skipped."
    targets = [f["filename"] for f in files if os.path.exists(f["filename"])]
    if not targets:
        return None
    result = subprocess.run(
        ["shellcheck", "--format=gcc"] + targets,
        capture_output=True, text=True
    )
    out = (result.stdout + result.stderr).strip()
    return f"ShellCheck (shell lint):\n{out[:800]}" if out else "ShellCheck: no issues found."

def run_yamllint(files):
    if not tool_exists("yamllint"):
        return "yamllint not installed — skipped."
    targets = [f["filename"] for f in files if os.path.exists(f["filename"])]
    if not targets:
        return None
    result = subprocess.run(
        ["yamllint", "-f", "parsable"] + targets,
        capture_output=True, text=True
    )
    out = (result.stdout + result.stderr).strip()
    return f"yamllint (YAML lint):\n{out[:600]}" if out else "yamllint: no issues found."

def run_json_check(files):
    results = []
    for f in files:
        fname = f["filename"]
        if not os.path.exists(fname):
            continue
        result = subprocess.run(
            ["python3", "-m", "json.tool", fname],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            results.append(f"- {fname}: {result.stderr.strip()}")
    return "JSON validation errors:\n" + "\n".join(results) if results else "JSON validation: all files valid."

def run_hadolint(files):
    if not tool_exists("hadolint"):
        return "Hadolint not installed — skipped."
    targets = [f["filename"] for f in files if os.path.exists(f["filename"])]
    if not targets:
        return None
    result = subprocess.run(
        ["hadolint"] + targets,
        capture_output=True, text=True
    )
    out = (result.stdout + result.stderr).strip()
    return f"Hadolint (Dockerfile lint):\n{out[:600]}" if out else "Hadolint: no issues found."

def run_sqlfluff(files):
    if not tool_exists("sqlfluff"):
        return "sqlfluff not installed — skipped."
    targets = [f["filename"] for f in files if os.path.exists(f["filename"])]
    if not targets:
        return None
    result = subprocess.run(
        ["sqlfluff", "lint", "--format", "json"] + targets,
        capture_output=True, text=True
    )
    try:
        data = json.loads(result.stdout)
        issues = []
        for file_result in data:
            for v in file_result.get("violations", []):
                issues.append(
                    f"- {file_result['filepath']}:{v['line_no']} [{v['code']}] {v['description']}"
                )
        return "sqlfluff (SQL lint):\n" + "\n".join(issues[:15]) if issues else "sqlfluff: no issues found."
    except Exception:
        return result.stdout[:500] or None

def run_go_vet(files):
    if not tool_exists("go"):
        return "Go not installed — skipped."
    dirs = set(os.path.dirname(f["filename"]) or "." for f in files)
    results = []
    for d in dirs:
        result = subprocess.run(
            ["go", "vet", f"./{d}/..."],
            capture_output=True, text=True
        )
        out = (result.stdout + result.stderr).strip()
        if out:
            results.append(out)
    return "go vet:\n" + "\n".join(results)[:600] if results else "go vet: no issues found."

# ── Dynamic tool runner ────────────────────────────────────────────────────────

def run_tools_for_language(lang, files):
    """Returns list of (tool_name, output) tuples for a given language."""
    results = []

    dispatch = {
        "python":     [("Ruff", run_ruff), ("Bandit", run_bandit), ("Radon", run_radon)],
        "javascript": [("ESLint", run_eslint)],
        "typescript": [("ESLint", run_eslint)],
        "shell":      [("ShellCheck", run_shellcheck)],
        "yaml":       [("yamllint", run_yamllint)],
        "json":       [("JSON check", run_json_check)],
        "docker":     [("Hadolint", run_hadolint)],
        "sql":        [("sqlfluff", run_sqlfluff)],
        "go":         [("go vet", run_go_vet)],
    }

    for name, fn in dispatch.get(lang, []):
        print(f"  Running {name}...")
        out = fn(files)
        if out:
            results.append((name, out))

    # Pytest is Python-only and doesn't take a files arg
    if lang == "python":
        print("  Running Pytest...")
        out = run_pytest()
        if out:
            results.append(("Pytest", out))

    if lang not in dispatch:
        print(f"  No static analysis tool available for '{lang}' — LLM will review diff only.")

    return results

# ── Ollama ─────────────────────────────────────────────────────────────────────

def wait_for_ollama(retries=10, delay=3):
    for i in range(retries):
        try:
            resp = requests.get("http://localhost:11434/api/tags", timeout=3)
            if resp.status_code == 200:
                print("Ollama is ready.")
                return
        except Exception:
            pass
        print(f"Waiting for Ollama... ({i+1}/{retries})")
        time.sleep(delay)
    raise RuntimeError("Ollama did not start in time.")

def ollama_call(prompt):
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 2048}
    }
    resp = requests.post(OLLAMA_URL, json=payload, timeout=300)
    resp.raise_for_status()
    return resp.json()["response"]

def ask_ollama_review(diff, tool_outputs, languages_found):
    tools_section = (
        "\n\n".join(f"### {name}\n{output}" for name, output in tool_outputs)
        or "No static analysis tools ran for these file types."
    )
    lang_list = ", ".join(sorted(languages_found)) or "unknown"
    prompt = f"""You are an expert code reviewer. This PR contains changes in: {lang_list}.

Review the diff and static analysis results below. Tailor your review to the languages present.

Focus on:
1. Security issues (hardcoded secrets, injection risks, missing auth)
2. Performance problems (inefficient loops, blocking I/O, N+1 queries)
3. Code quality (unclear naming, missing error handling, no documentation)
4. Language-specific best practices for: {lang_list}

## Static Analysis Results
{tools_section}

## PR Diff
{diff}

Respond using this exact format:

## Summary
One paragraph overview of what this PR changes.

## Issues found
For each issue: severity (🔴 High / 🟡 Medium / 🟢 Low), filename and line if known, clear explanation.

## Suggestions
Up to 3 concrete, actionable improvement suggestions tailored to the languages in this PR.

Be concise. If no issues found, say so clearly."""
    return ollama_call(prompt)

def ask_ollama_fix(filepath, file_content, tool_outputs, language):
    """Ask the LLM to return a fully corrected version of a file."""
    issues_text = (
        "\n\n".join(f"### {name}\n{output}" for name, output in tool_outputs)
        or "No specific tool findings — use your best judgement."
    )
    prompt = f"""You are an expert {language} developer. Fix all issues in the file below.

Issues found by static analysis:
{issues_text}

Rules:
- Return ONLY the complete corrected file content, nothing else
- Do not add any explanation, markdown fences, or commentary
- Preserve the original logic and structure exactly
- Only fix real issues: security risks, bugs, lint errors, and style violations
- Do not rewrite or restructure code that has no issues

File: {filepath}
```
{file_content}
```

Return the fixed file content now:"""
    return ollama_call(prompt)

# ── Line count helper ──────────────────────────────────────────────────────────

def count_lines(content):
    return len(content.splitlines())

# ── Auto-fix flow ──────────────────────────────────────────────────────────────

def auto_fix_files(files, groups, all_tool_outputs, pr_head_branch):
    """
    For each changed file that has tool findings:
    1. Check file and line caps before doing anything
    2. Fetch current content from the PR branch
    3. Ask LLM to fix it
    4. Commit fixed version to a new fix branch
    Returns fix branch name, fixed files, cap-skipped files, line-skipped files.
    """
    fix_branch = f"ai-fixes/pr-{PR_NUMBER}"
    head_sha = get_branch_sha(pr_head_branch)
    create_branch(fix_branch, head_sha)

    fixed_files = []
    skipped_cap = []      # files skipped because fix cap was hit
    skipped_long = []     # files skipped because too many lines

    for lang, lang_files in groups.items():
        for f in lang_files:
            filepath = f["filename"]

            if f.get("status") == "removed":
                continue

            # ── Cap 1: max files ───────────────────────────────────────────
            if len(fixed_files) >= MAX_FILES_TO_FIX:
                skipped_cap.append(filepath)
                print(f"  [{len(fixed_files)}/{MAX_FILES_TO_FIX}] File cap reached — skipping {filepath}")
                continue

            # Filter tool outputs to only findings that mention this file
            relevant_tools = [
                (name, output) for name, output in all_tool_outputs
                if filepath in output
            ]
            if not relevant_tools:
                print(f"  No findings for {filepath} — skipping auto-fix.")
                continue

            print(f"  Fetching content of {filepath}...")
            content = get_file_content(filepath, pr_head_branch)
            if content is None:
                print(f"  Could not fetch {filepath} — skipping.")
                continue

            # ── Cap 2: max lines ───────────────────────────────────────────
            line_count = count_lines(content)
            if line_count > MAX_LINES_TO_FIX:
                skipped_long.append((filepath, line_count))
                print(f"  {filepath} has {line_count} lines (limit: {MAX_LINES_TO_FIX}) — skipping.")
                continue

            print(f"  Asking LLM to fix {filepath} ({line_count} lines)...")
            fixed_content = ask_ollama_fix(filepath, content, relevant_tools, lang)

            # Sanity check: don't commit if LLM returned empty or identical content
            if not fixed_content.strip() or fixed_content.strip() == content.strip():
                print(f"  No changes produced for {filepath} — skipping commit.")
                continue

            print(f"  Committing fix for {filepath}...")
            commit_file(
                filepath,
                fixed_content,
                fix_branch,
                f"fix: auto-fix issues in {filepath} (AI review of PR #{PR_NUMBER})"
            )
            fixed_files.append(filepath)

    return fix_branch, fixed_files, skipped_cap, skipped_long

# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Waiting for Ollama to be ready...")
    wait_for_ollama()

    print("Fetching PR info...")
    pr_info = get_pr_info()
    pr_head_branch = pr_info["head"]["ref"]
    pr_base_branch = pr_info["base"]["ref"]
    print(f"  PR branch: {pr_head_branch} → {pr_base_branch}")

    print("Fetching PR files...")
    files = get_pr_files()

    print("Grouping files by language...")
    groups = group_files_by_language(files)
    languages_found = set(groups.keys())
    print(f"  Detected languages: {', '.join(languages_found)}")

    all_tool_outputs = []
    for lang, lang_files in groups.items():
        print(f"\nRunning tools for: {lang} ({len(lang_files)} file(s))")
        results = run_tools_for_language(lang, lang_files)
        all_tool_outputs.extend(results)

    print("\nBuilding diff...")
    diff = get_diff_text(files)

    # ── Step 1: Post review comment ──
    print("\nGenerating review...")
    review = ask_ollama_review(diff, all_tool_outputs, languages_found)
    tool_names = [name for name, _ in all_tool_outputs]
    post_comment(review, languages_found, tool_names)

    # ── Step 2: Auto-fix and open draft PR ──
    if all_tool_outputs:
        print("\nAuto-fixing files with findings...")
        fix_branch, fixed_files, skipped_cap, skipped_long = auto_fix_files(
            files, groups, all_tool_outputs, pr_head_branch
        )

        if fixed_files:
            print(f"\nCreating draft PR from {fix_branch} → {pr_head_branch}...")
            files_list = "\n".join(f"- `{f}`" for f in fixed_files)

            # Build skipped section for draft PR body
            skipped_section = ""
            if skipped_cap:
                skipped_section += (
                    f"\n\n**Skipped (file cap of {MAX_FILES_TO_FIX} reached):**\n"
                    + "\n".join(f"- `{f}`" for f in skipped_cap)
                )
            if skipped_long:
                skipped_section += (
                    f"\n\n**Skipped (over {MAX_LINES_TO_FIX}-line limit):**\n"
                    + "\n".join(f"- `{f}` ({n} lines)" for f, n in skipped_long)
                )

            pr_body = (
                f"## 🤖 AI Auto-fix\n\n"
                f"This draft PR was automatically generated by the AI code reviewer "
                f"in response to PR #{PR_NUMBER}.\n\n"
                f"**Files fixed ({len(fixed_files)}/{MAX_FILES_TO_FIX} cap):**\n{files_list}"
                f"{skipped_section}\n\n"
                f"**Review carefully before merging** — the LLM may have made mistakes. "
                f"Treat this as a starting point, not a final fix."
            )
            draft_url = create_draft_pr(
                head_branch=fix_branch,
                base_branch=pr_head_branch,
                pr_title=f"fix: AI auto-fix suggestions for PR #{PR_NUMBER}",
                pr_body=pr_body
            )

            # Build comment body for original PR
            comment_body = (
                f"## 🔧 Auto-fix PR ready\n\n"
                f"Applied fixes to **{len(fixed_files)} file(s)**. Draft PR for your review:\n\n"
                f"👉 {draft_url}\n\n"
                f"Files fixed: {', '.join(f'`{f}`' for f in fixed_files)}"
            )
            if skipped_cap:
                comment_body += (
                    f"\n\n⚠️ **{len(skipped_cap)} file(s) skipped** — hit the "
                    f"{MAX_FILES_TO_FIX}-file cap: "
                    + ", ".join(f"`{f}`" for f in skipped_cap)
                )
            if skipped_long:
                comment_body += (
                    f"\n\n⚠️ **{len(skipped_long)} file(s) skipped** — over the "
                    f"{MAX_LINES_TO_FIX}-line limit: "
                    + ", ".join(f"`{f}` ({n} lines)" for f, n in skipped_long)
                )
            comment_body += "\n\n_Review and merge only if the fixes look correct._"

            requests.post(
                f"https://api.github.com/repos/{REPO}/issues/{PR_NUMBER}/comments",
                headers=GH_HEADERS,
                json={"body": comment_body},
                timeout=30
            )
            print(f"  Draft PR created: {draft_url}")

        else:
            print("  No files were auto-fixed (no changes produced by LLM).")
    else:
        print("No tool findings — skipping auto-fix step.")

    print("\nDone.")