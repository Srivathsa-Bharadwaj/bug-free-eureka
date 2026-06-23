import os, json, requests, subprocess, time
from collections import defaultdict

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
PR_NUMBER = os.environ["PR_NUMBER"]
REPO = os.environ["REPO"]

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "qwen2.5-coder:1.5b"

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

def get_pr_files():
    url = f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}/files"
    resp = requests.get(url, headers=GH_HEADERS)
    resp.raise_for_status()
    return resp.json()

def get_diff_text(files):
    chunks = []
    for f in files:
        if f.get("patch"):
            lang = detect_language(f["filename"])
            chunks.append(f"File: {f['filename']} (language: {lang})\n{f['patch']}")
    full = "\n\n---\n\n".join(chunks[:8])
    return full[:8000]

# ── Static analysis tools ──────────────────────────────────────────────────────

def tool_exists(name):
    result = subprocess.run(["which", name], capture_output=True)
    return result.returncode == 0

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
    if not results:
        return "JSON validation: all files valid."
    return "JSON validation errors:\n" + "\n".join(results)

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
                issues.append(f"- {file_result['filepath']}:{v['line_no']} [{v['code']}] {v['description']}")
        if not issues:
            return "sqlfluff: no SQL issues found."
        return "sqlfluff (SQL lint):\n" + "\n".join(issues[:15])
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

    if lang == "python":
        for name, fn in [("Ruff", run_ruff), ("Bandit", run_bandit), ("Radon", run_radon)]:
            print(f"  Running {name}...")
            out = fn(files)
            if out:
                results.append((name, out))
        print("  Running Pytest...")
        out = run_pytest()
        if out:
            results.append(("Pytest", out))

    elif lang in ("javascript", "typescript"):
        print("  Running ESLint...")
        out = run_eslint(files)
        if out:
            results.append(("ESLint", out))

    elif lang == "shell":
        print("  Running ShellCheck...")
        out = run_shellcheck(files)
        if out:
            results.append(("ShellCheck", out))

    elif lang == "yaml":
        print("  Running yamllint...")
        out = run_yamllint(files)
        if out:
            results.append(("yamllint", out))

    elif lang == "json":
        print("  Running JSON validation...")
        out = run_json_check(files)
        if out:
            results.append(("JSON check", out))

    elif lang == "docker":
        print("  Running Hadolint...")
        out = run_hadolint(files)
        if out:
            results.append(("Hadolint", out))

    elif lang == "sql":
        print("  Running sqlfluff...")
        out = run_sqlfluff(files)
        if out:
            results.append(("sqlfluff", out))

    elif lang == "go":
        print("  Running go vet...")
        out = run_go_vet(files)
        if out:
            results.append(("go vet", out))

    else:
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

def ask_ollama(diff, tool_outputs, languages_found):
    tools_section = ""
    if tool_outputs:
        tools_section = "\n\n".join(
            f"### {name}\n{output}" for name, output in tool_outputs
        )
    else:
        tools_section = "No static analysis tools ran for these file types."

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

    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": 1024}
    }

    resp = requests.post(OLLAMA_URL, json=payload, timeout=180)
    resp.raise_for_status()
    return resp.json()["response"]

# ── GitHub comment ─────────────────────────────────────────────────────────────

def post_comment(body, languages_found, tool_names):
    lang_badges = " ".join(f"`{l}`" for l in sorted(languages_found))
    tools_used = " ".join(f"`{t}`" for t in tool_names) or "LLM only"
    header = f"## 🤖 AI Code Review\n\n**Languages detected:** {lang_badges}  \n**Tools run:** {tools_used}\n\n---\n\n"
    url = f"https://api.github.com/repos/{REPO}/issues/{PR_NUMBER}/comments"
    payload = {"body": header + body}
    resp = requests.post(url, headers=GH_HEADERS, json=payload)
    resp.raise_for_status()
    print(f"Comment posted: {resp.json()['html_url']}")

# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Waiting for Ollama to be ready...")
    wait_for_ollama()

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

    print("Calling local LLM via Ollama...")
    review = ask_ollama(diff, all_tool_outputs, languages_found)

    tool_names = [name for name, _ in all_tool_outputs]
    print("Posting comment to PR...")
    post_comment(review, languages_found, tool_names)
    print("Done.")