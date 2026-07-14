"""
review_agent.py — Multi-agent AI code reviewer
Runs on GitHub Actions, uses a local Ollama LLM, posts review comments and
opens a draft auto-fix PR. Zero API cost, zero rate limits.
"""

import os
import re
import sys
import json
import time
import base64
import logging
import subprocess
import requests
from collections import defaultdict

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Environment / config ───────────────────────────────────────────────────────

def _require_env(name: str) -> str:
    """Read a required environment variable; exit with a clear message if missing."""
    val = os.environ.get(name, "").strip()
    if not val:
        log.error("Required environment variable '%s' is not set. Exiting.", name)
        sys.exit(1)
    return val

def _optional_env(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default

GITHUB_TOKEN = _require_env("GITHUB_TOKEN")
PR_NUMBER    = _require_env("PR_NUMBER")
REPO         = _require_env("REPO")

# OLLAMA_URL and MODEL can be overridden via env vars or will fall back to defaults.
# Set OLLAMA_HOST in your workflow env to point at a remote Ollama instance.
OLLAMA_HOST  = _optional_env("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_URL   = f"{OLLAMA_HOST.rstrip('/')}/api/generate"
MODEL        = _optional_env("OLLAMA_MODEL", "qwen2.5-coder:1.5b")

log.info("Ollama endpoint : %s", OLLAMA_URL)
log.info("Model           : %s", MODEL)

# ── Auto-fix safety caps ───────────────────────────────────────────────────────
MAX_FILES_TO_FIX = int(_optional_env("MAX_FILES_TO_FIX", "10"))
MAX_LINES_TO_FIX = int(_optional_env("MAX_LINES_TO_FIX", "500"))
MAX_FINDINGS_PER_REVIEW = int(_optional_env("MAX_FINDINGS_PER_REVIEW", "5"))  # cap LLM findings per PR

# ── Paths the auto-fixer must never touch ─────────────────────────────────────
EXCLUDED_PATH_PREFIXES = (
    ".github/",   # workflow and script files — avoids self-modification
    ".git/",      # git internals
)
EXCLUDED_FILENAMES = (
    "review_agent.py",  # this script itself
)

GH_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# ── Input validation ───────────────────────────────────────────────────────────

# Only allow safe filesystem path characters — no shell metacharacters
_SAFE_PATH_RE = re.compile(r'^[a-zA-Z0-9_\-\.\/@ ]+$')

def validate_file_paths(files: list) -> list:
    """
    Return only paths that:
    - exist on disk
    - contain only safe characters (no shell metacharacters)
    - are not absolute paths trying to escape the workspace
    Logs a warning and drops any path that fails validation.
    """
    safe = []
    for path in files:
        if not isinstance(path, str):
            log.warning("Skipping non-string path: %r", path)
            continue
        if os.path.isabs(path):
            log.warning("Skipping absolute path (potential path traversal): %s", path)
            continue
        if ".." in path.split(os.sep):
            log.warning("Skipping path with directory traversal: %s", path)
            continue
        if not _SAFE_PATH_RE.match(path):
            log.warning("Skipping path with unsafe characters: %s", path)
            continue
        if not os.path.exists(path):
            log.debug("Skipping non-existent path: %s", path)
            continue
        safe.append(path)
    return safe

def safe_subprocess(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    """
    Run a subprocess with a validated command list.
    - Enforces shell=False (always — never builds a shell string)
    - Validates that the executable name contains only word characters
    - Sets a default timeout of 60 s if not provided
    Raises ValueError for obviously malformed commands.
    """
    if not cmd or not isinstance(cmd, list):
        raise ValueError(f"Command must be a non-empty list, got: {cmd!r}")
    executable = os.path.basename(cmd[0])
    if not re.match(r'^[\w\-\.]+$', executable):
        raise ValueError(f"Executable name contains unsafe characters: {executable!r}")
    kwargs.setdefault("timeout", 60)
    return subprocess.run(cmd, shell=False, **kwargs)  # noqa: S603

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

def is_excluded_from_review(filepath: str) -> bool:
    """Return True for files that should never be shown to the LLM reviewer."""
    basename = os.path.basename(filepath)
    return (
        any(filepath.startswith(p) for p in EXCLUDED_PATH_PREFIXES)
        or basename in EXCLUDED_FILENAMES
    )

def detect_language(filename: str) -> str:
    base = os.path.basename(filename).lower()
    if base in SPECIAL_FILENAMES:
        return SPECIAL_FILENAMES[base]
    ext = os.path.splitext(filename)[1].lower()
    return EXTENSION_MAP.get(ext, "unknown")

def group_files_by_language(files: list) -> dict:
    groups: dict = defaultdict(list)
    for f in files:
        # skip infra/self-referential files from tool runs AND review diff
        if is_excluded_from_review(f["filename"]):
            log.info("Skipping excluded file from all analysis: %s", f["filename"])
            continue
        lang = detect_language(f["filename"])
        groups[lang].append(f)
    return dict(groups)

# ── GitHub helpers ─────────────────────────────────────────────────────────────

def _gh_get(url: str) -> dict:
    try:
        resp = requests.get(url, headers=GH_HEADERS, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.Timeout:
        log.error("GitHub GET timed out: %s", url)
        raise
    except requests.exceptions.HTTPError as exc:
        log.error("GitHub GET failed (%s): %s", exc.response.status_code, url)
        raise

def _gh_post(url: str, payload: dict) -> dict:
    try:
        resp = requests.post(url, headers=GH_HEADERS, json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.Timeout:
        log.error("GitHub POST timed out: %s", url)
        raise
    except requests.exceptions.HTTPError as exc:
        log.error("GitHub POST failed (%s): %s", exc.response.status_code, url)
        raise

def _gh_put(url: str, payload: dict) -> dict:
    try:
        resp = requests.put(url, headers=GH_HEADERS, json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.Timeout:
        log.error("GitHub PUT timed out: %s", url)
        raise
    except requests.exceptions.HTTPError as exc:
        log.error("GitHub PUT failed (%s): %s", exc.response.status_code, url)
        raise

def get_pr_info() -> dict:
    return _gh_get(f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}")

def get_pr_files() -> list:
    return _gh_get(f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}/files")

def get_file_content(filepath: str, ref: str):
    url = f"https://api.github.com/repos/{REPO}/contents/{filepath}?ref={ref}"
    try:
        resp = requests.get(url, headers=GH_HEADERS, timeout=30)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return base64.b64decode(resp.json()["content"]).decode("utf-8", errors="replace")
    except requests.exceptions.RequestException as exc:
        log.warning("Could not fetch %s @ %s: %s", filepath, ref, exc)
        return None

def get_diff_text(files: list) -> str:
    chunks = []
    for f in files:
        if f.get("patch") and not is_excluded_from_review(f["filename"]):
            lang = detect_language(f["filename"])
            chunks.append(f"File: {f['filename']} (language: {lang})\n{f['patch']}")
    if not chunks:
        return "(no reviewable files in this PR)"
    return "\n\n---\n\n".join(chunks[:5])[:5000]  # tighter cap — keeps prompt within context window

def get_branch_sha(branch: str) -> str:
    data = _gh_get(f"https://api.github.com/repos/{REPO}/git/ref/heads/{branch}")
    return data["object"]["sha"]

def create_branch(branch_name: str, sha: str) -> None:
    url = f"https://api.github.com/repos/{REPO}/git/refs"
    try:
        _gh_post(url, {"ref": f"refs/heads/{branch_name}", "sha": sha})
    except requests.exceptions.HTTPError as exc:
        if exc.response.status_code == 422:
            log.info("Branch '%s' already exists — reusing.", branch_name)
        else:
            raise

def get_file_sha(filepath: str, branch: str):
    url = f"https://api.github.com/repos/{REPO}/contents/{filepath}?ref={branch}"
    try:
        resp = requests.get(url, headers=GH_HEADERS, timeout=30)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()["sha"]
    except requests.exceptions.RequestException:
        return None

def commit_file(filepath: str, content: str, branch: str, message: str) -> None:
    url = f"https://api.github.com/repos/{REPO}/contents/{filepath}"
    file_sha = get_file_sha(filepath, branch)
    payload: dict = {
        "message": message,
        "content": base64.b64encode(content.encode()).decode(),
        "branch": branch,
    }
    if file_sha:
        payload["sha"] = file_sha
    _gh_put(url, payload)

def create_draft_pr(head_branch: str, base_branch: str, pr_title: str, pr_body: str) -> str:
    data = _gh_post(
        f"https://api.github.com/repos/{REPO}/pulls",
        {"title": pr_title, "body": pr_body,
         "head": head_branch, "base": base_branch, "draft": True},
    )
    return data["html_url"]

def post_comment(body: str, languages_found: set, tool_names: list) -> None:
    lang_badges = " ".join(f"`{l}`" for l in sorted(languages_found))
    tools_used  = " ".join(f"`{t}`" for t in tool_names) or "LLM only"
    header = (
        f"## 🤖 AI Code Review\n\n"
        f"**Languages detected:** {lang_badges}  \n"
        f"**Tools run:** {tools_used}\n\n---\n\n"
    )
    try:
        data = _gh_post(
            f"https://api.github.com/repos/{REPO}/issues/{PR_NUMBER}/comments",
            {"body": header + body},
        )
        log.info("Review comment posted: %s", data["html_url"])
    except requests.exceptions.RequestException as exc:
        log.error("Failed to post review comment: %s", exc)
        raise

# ── Static analysis tools ──────────────────────────────────────────────────────

def tool_exists(name: str) -> bool:
    try:
        return safe_subprocess(["which", name], capture_output=True).returncode == 0
    except (ValueError, OSError):
        return False

def _safe_targets(files: list) -> list:
    """Extract and validate filenames from the PR files list."""
    return validate_file_paths([f["filename"] for f in files])

def run_ruff(files: list):
    targets = _safe_targets(files)
    if not targets:
        return None
    try:
        result = safe_subprocess(
            ["ruff", "check", "--output-format=json"] + targets,
            capture_output=True, text=True,
        )
        issues = json.loads(result.stdout)
        if not issues:
            return "Ruff: no issues found."
        lines = [
            f"- {i['filename']}:{i['location']['row']} [{i['code']}] {i['message']}"
            for i in issues[:20]
        ]
        return "Ruff (Python lint):\n" + "\n".join(lines)
    except json.JSONDecodeError:
        return result.stdout[:500] or None
    except Exception as exc:
        log.warning("Ruff failed: %s", exc)
        return None

def run_bandit(files: list):
    targets = _safe_targets(files)
    if not targets:
        return None
    try:
        result = safe_subprocess(
            ["bandit", "-r", "-f", "json"] + targets,
            capture_output=True, text=True,
        )
        data   = json.loads(result.stdout)
        issues = data.get("results", [])
        if not issues:
            return "Bandit: no security issues found."
        lines = [
            f"- {i['filename']}:{i['line_number']} [{i['issue_severity']}] {i['issue_text']}"
            for i in issues[:15]
        ]
        return "Bandit (Python security):\n" + "\n".join(lines)
    except json.JSONDecodeError:
        return result.stdout[:500] or None
    except Exception as exc:
        log.warning("Bandit failed: %s", exc)
        return None

def run_radon(files: list):
    targets = _safe_targets(files)
    if not targets:
        return None
    try:
        result = safe_subprocess(
            ["radon", "cc", "--min", "B", "--show-complexity", "-s"] + targets,
            capture_output=True, text=True,
        )
        out = result.stdout.strip()
        return f"Radon (Python complexity):\n{out[:800]}" if out else "Radon: no complex functions found."
    except Exception as exc:
        log.warning("Radon failed: %s", exc)
        return None

def run_pytest():
    if not tool_exists("pytest"):
        return None
    try:
        result = safe_subprocess(
            ["pytest", "--tb=short", "-q"],
            capture_output=True, text=True,
        )
        output = (result.stdout + result.stderr).strip()
        return f"Pytest:\n{output[:800]}" if output else None
    except Exception as exc:
        log.warning("Pytest failed: %s", exc)
        return None

def run_eslint(files: list):
    if not tool_exists("eslint"):
        return "ESLint not installed — skipped. Add `npm install -g eslint` to workflow to enable."
    targets = _safe_targets(files)
    if not targets:
        return None
    try:
        result = safe_subprocess(
            ["eslint", "--format=compact"] + targets,
            capture_output=True, text=True,
        )
        out = (result.stdout + result.stderr).strip()
        return f"ESLint (JS/TS lint):\n{out[:800]}" if out else "ESLint: no issues found."
    except Exception as exc:
        log.warning("ESLint failed: %s", exc)
        return None

def run_shellcheck(files: list):
    if not tool_exists("shellcheck"):
        return "ShellCheck not installed — skipped."
    targets = _safe_targets(files)
    if not targets:
        return None
    try:
        result = safe_subprocess(
            ["shellcheck", "--format=gcc"] + targets,
            capture_output=True, text=True,
        )
        out = (result.stdout + result.stderr).strip()
        return f"ShellCheck (shell lint):\n{out[:800]}" if out else "ShellCheck: no issues found."
    except Exception as exc:
        log.warning("ShellCheck failed: %s", exc)
        return None

def run_yamllint(files: list):
    if not tool_exists("yamllint"):
        return "yamllint not installed — skipped."
    targets = _safe_targets(files)
    if not targets:
        return None
    try:
        result = safe_subprocess(
            ["yamllint", "-f", "parsable"] + targets,
            capture_output=True, text=True,
        )
        out = (result.stdout + result.stderr).strip()
        return f"yamllint (YAML lint):\n{out[:600]}" if out else "yamllint: no issues found."
    except Exception as exc:
        log.warning("yamllint failed: %s", exc)
        return None

def run_json_check(files: list):
    results = []
    for path in _safe_targets(files):
        try:
            result = safe_subprocess(
                ["python3", "-m", "json.tool", path],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                results.append(f"- {path}: {result.stderr.strip()}")
        except Exception as exc:
            log.warning("JSON check failed for %s: %s", path, exc)
    return (
        "JSON validation errors:\n" + "\n".join(results)
        if results else "JSON validation: all files valid."
    )

def run_hadolint(files: list):
    if not tool_exists("hadolint"):
        return "Hadolint not installed — skipped."
    targets = _safe_targets(files)
    if not targets:
        return None
    try:
        result = safe_subprocess(
            ["hadolint"] + targets,
            capture_output=True, text=True,
        )
        out = (result.stdout + result.stderr).strip()
        return f"Hadolint (Dockerfile lint):\n{out[:600]}" if out else "Hadolint: no issues found."
    except Exception as exc:
        log.warning("Hadolint failed: %s", exc)
        return None

def run_sqlfluff(files: list):
    if not tool_exists("sqlfluff"):
        return "sqlfluff not installed — skipped."
    targets = _safe_targets(files)
    if not targets:
        return None
    try:
        result = safe_subprocess(
            ["sqlfluff", "lint", "--format", "json"] + targets,
            capture_output=True, text=True,
        )
        data   = json.loads(result.stdout)
        issues = [
            f"- {fr['filepath']}:{v['line_no']} [{v['code']}] {v['description']}"
            for fr in data
            for v in fr.get("violations", [])
        ]
        return "sqlfluff (SQL lint):\n" + "\n".join(issues[:15]) if issues else "sqlfluff: no issues found."
    except json.JSONDecodeError:
        return result.stdout[:500] or None
    except Exception as exc:
        log.warning("sqlfluff failed: %s", exc)
        return None

def run_go_vet(files: list):
    if not tool_exists("go"):
        return "Go not installed — skipped."
    dirs = set(os.path.dirname(f["filename"]) or "." for f in files)
    results = []
    for d in dirs:
        # validate directory path before passing to subprocess
        if not _SAFE_PATH_RE.match(d):
            log.warning("Skipping unsafe Go directory: %s", d)
            continue
        try:
            result = safe_subprocess(
                ["go", "vet", f"./{d}/..."],
                capture_output=True, text=True,
            )
            out = (result.stdout + result.stderr).strip()
            if out:
                results.append(out)
        except Exception as exc:
            log.warning("go vet failed for %s: %s", d, exc)
    return "go vet:\n" + "\n".join(results)[:600] if results else "go vet: no issues found."

# ── Dynamic tool runner ────────────────────────────────────────────────────────

def run_tools_for_language(lang: str, files: list) -> list:
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
        log.info("  Running %s...", name)
        try:
            out = fn(files)
            if out:
                results.append((name, out))
        except Exception as exc:
            log.warning("  %s raised an unexpected error: %s", name, exc)

    if lang == "python":
        log.info("  Running Pytest...")
        try:
            out = run_pytest()
            if out:
                results.append(("Pytest", out))
        except Exception as exc:
            log.warning("  Pytest raised an unexpected error: %s", exc)

    if lang not in dispatch:
        log.info("  No static analysis tool available for '%s' — LLM will review diff only.", lang)

    return results

# ── Ollama ─────────────────────────────────────────────────────────────────────

def wait_for_ollama(retries: int = 20, delay: int = 5) -> None:
    """
    Poll until Ollama's health endpoint responds, then do a warm-up call
    to ensure the model is loaded into memory before the real review call.
    A 500 on the first real call usually means the model isn't loaded yet.
    """
    health_url = f"{OLLAMA_HOST.rstrip('/')}/api/tags"
    for i in range(retries):
        try:
            resp = requests.get(health_url, timeout=5)
            if resp.status_code == 200:
                log.info("Ollama server is up — warming up model %s...", MODEL)
                _warm_up_model()
                return
        except requests.exceptions.RequestException:
            pass
        log.info("Waiting for Ollama... (%d/%d)", i + 1, retries)
        time.sleep(delay)
    raise RuntimeError(
        f"Ollama did not become ready after {retries * delay}s. "
        f"Check that Ollama is running at {OLLAMA_HOST}."
    )

def _warm_up_model() -> None:
    """
    Send a minimal prompt to force the model to load into memory.
    Retries up to 3 times on 500 errors (model still loading).
    """
    payload = {
        "model": MODEL,
        "prompt": "Say OK.",
        "stream": False,
        "options": {"num_predict": 5, "temperature": 0.0},
    }
    for attempt in range(3):
        try:
            resp = requests.post(OLLAMA_URL, json=payload, timeout=120)
            if resp.status_code == 200:
                log.info("Model warm-up complete.")
                return
            log.warning(
                "Warm-up attempt %d/%d got status %d — retrying in 10s...",
                attempt + 1, 3, resp.status_code,
            )
        except requests.exceptions.RequestException as exc:
            log.warning("Warm-up attempt %d/%d failed: %s — retrying in 10s...", attempt + 1, 3, exc)
        time.sleep(10)
    log.warning("Model warm-up did not succeed — proceeding anyway.")

# Context window budget — qwen2.5-coder:1.5b supports ~4096 tokens.
# At ~4 chars/token, 10000 chars is a safe prompt ceiling.
MAX_PROMPT_CHARS = int(_optional_env("MAX_PROMPT_CHARS", "10000"))

def _truncate_prompt(prompt: str) -> str:
    """Hard-truncate prompt to stay within the model context window."""
    if len(prompt) <= MAX_PROMPT_CHARS:
        return prompt
    log.warning(
        "Prompt is %d chars — truncating to %d to avoid context overflow.",
        len(prompt), MAX_PROMPT_CHARS,
    )
    # Keep the instructions at the top and truncate the diff/file content in the middle
    half = MAX_PROMPT_CHARS // 2
    return prompt[:half] + "\n\n[... truncated to fit context window ...]\n\n" + prompt[-half:]

def ollama_call(prompt: str) -> str:
    """
    Call Ollama with automatic retry on 500 (model still loading / OOM transient).
    Truncates prompt if it exceeds the safe context window budget.
    """
    prompt = _truncate_prompt(prompt)
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.1,
            "num_predict": 1024,    # reduced from 2048 — enough for review output,
                                    # lower memory pressure on the 7GB Actions runner
            "num_ctx": 4096,        # explicit context window — prevents 500 on overflow
        },
    }
    last_exc = None
    for attempt in range(3):
        try:
            resp = requests.post(OLLAMA_URL, json=payload, timeout=300)
            if resp.status_code == 500:
                # Transient 500 — model may still be loading or hit a memory spike
                log.warning(
                    "Ollama returned 500 on attempt %d/3 — retrying in 15s...", attempt + 1
                )
                time.sleep(15)
                continue
            resp.raise_for_status()
            return resp.json()["response"]
        except requests.exceptions.Timeout:
            log.error("Ollama call timed out after 300s on attempt %d/3.", attempt + 1)
            last_exc = RuntimeError("Ollama timeout")
            time.sleep(10)
        except requests.exceptions.HTTPError as exc:
            log.error("Ollama call failed (%s) on attempt %d/3.", exc.response.status_code, attempt + 1)
            last_exc = exc
            break  # non-500 HTTP errors are not retryable
        except (KeyError, ValueError) as exc:
            log.error("Unexpected Ollama response format: %s", exc)
            raise
    raise last_exc or RuntimeError("Ollama call failed after 3 attempts.")

def deduplicate_findings(review_text: str) -> str:
    """
    Post-process the LLM review output to remove duplicate findings.
    Detects repeated (filename, issue_type) pairs and keeps only the first occurrence,
    replacing the rest with a summary line.
    Also hard-caps total findings at MAX_FINDINGS_PER_REVIEW.
    """
    lines = review_text.split("\n")
    seen_signatures: set = set()
    output_lines = []
    in_issues_section = False
    finding_count = 0
    current_finding_lines: list = []
    current_sig: str = ""

    def flush_finding():
        nonlocal finding_count
        if not current_finding_lines:
            return
        if current_sig in seen_signatures:
            return  # drop duplicate silently
        if finding_count >= MAX_FINDINGS_PER_REVIEW:
            return  # drop once cap is hit
        seen_signatures.add(current_sig)
        output_lines.extend(current_finding_lines)
        finding_count += 1

    for line in lines:
        if line.strip().startswith("## Issues found"):
            in_issues_section = True
            output_lines.append(line)
            continue
        if line.strip().startswith("## ") and in_issues_section:
            flush_finding()
            current_finding_lines = []
            current_sig = ""
            in_issues_section = False
            output_lines.append(line)
            continue

        if in_issues_section:
            # Detect start of a new finding by severity emoji
            if any(line.strip().startswith(s) for s in ("🔴", "🟡", "🟢", "Severity")):
                flush_finding()
                current_finding_lines = [line]
                # Build a signature from filename + first 6 words of the line
                words = line.split()
                current_sig = " ".join(words[:6]).lower()
            elif current_finding_lines:
                current_finding_lines.append(line)
                # Refine signature once we see the Filename line
                if "filename" in line.lower() or "app.py" in line.lower():
                    fname = line.strip().split(":")[-1].strip()
                    current_sig = fname + "|" + current_sig
                # Refine further once we see the Explanation line
                if "explanation" in line.lower():
                    words = line.lower().split()[:8]
                    current_sig += "|" + " ".join(words)
            else:
                output_lines.append(line)
        else:
            output_lines.append(line)

    flush_finding()  # flush last finding

    if finding_count >= MAX_FINDINGS_PER_REVIEW:
        output_lines.append(
            f"\n> ⚠️ Output capped at {MAX_FINDINGS_PER_REVIEW} findings. "
            f"Run static analysis tools directly for the full list."
        )

    return "\n".join(output_lines)


def ask_ollama_review(diff: str, tool_outputs: list, languages_found: set) -> str:
    tools_section = (
        "\n\n".join(f"### {name}\n{output}" for name, output in tool_outputs)
        or "No static analysis tools ran for these file types."
    )
    lang_list = ", ".join(sorted(languages_found)) or "unknown"
    prompt = f"""You are an expert code reviewer. This PR contains changes in: {lang_list}.

Review ONLY the application code in the diff below. Do NOT review CI scripts, workflow files,
or any file under .github/. Those are infrastructure files maintained separately.

STRICT RULES — you must follow all of these:
- Report a MAXIMUM of 5 issues total. Choose only the most important ones.
- If the same issue type appears in multiple functions, report it ONCE with a note that
  it affects multiple locations. Do NOT repeat the same finding for every line.
- Only report issues clearly visible in the diff. Do NOT invent issues.
- Do NOT report issues already handled in the code (shell=False present, try/except
  present, timeout= set, etc.).
- Do NOT flag single-letter loop variables (i, f, l, k, v) — standard Python idioms.
- Do NOT flag hardcoded limits or constants as security issues — they are configuration.
- Every finding MUST include a direct quote of the problematic line from the diff.
  If you cannot quote the exact line, do not report the issue.
- If static analysis tools report no issues, write "No issues found." and stop.

Focus only on:
1. Actual hardcoded secrets or real injection risks
2. Provably inefficient code visible in the diff
3. Real bugs — logic errors, unhandled edge cases
4. Missing functionality — functions that do nothing yet

## Static Analysis Results (ground truth — trust these over your own analysis)
{tools_section}

## PR Diff (application code only — max 5 issues, no duplicates)
{diff}

Respond using this exact format:

## Summary
One paragraph overview of what this PR changes.

## Issues found
Maximum 5 issues. If the same problem appears in multiple places, report it ONCE.
For each issue: severity (🔴 High / 🟡 Medium / 🟢 Low), filename:line, quoted problematic
line, clear explanation.
If no real issues: write "No issues found."

## Suggestions
Up to 3 suggestions — only if genuinely useful and not already in the code."""
    raw = ollama_call(prompt)
    return deduplicate_findings(raw)

def ask_ollama_fix(filepath: str, file_content: str, tool_outputs: list, language: str) -> str:
    issues_text = (
        "\n\n".join(f"### {name}\n{output}" for name, output in tool_outputs)
        or "No specific tool findings — use your best judgement."
    )
    prompt = f"""You are an expert {language} developer. Your job is to fix real code defects.

Issues found in this file:
{issues_text}

STRICT RULES:
- Return ONLY the complete corrected file content. Nothing else.
- Do NOT change comments, docstrings, or variable names unless they are the bug.
- Do NOT change ❌ or ✅ markers in comments — leave all comments exactly as-is.
- Fix ONLY the actual executable code that has a defect. Examples of real fixes:
    * Add try/except around network calls that have no error handling
    * Add timeout= to requests.request() calls that are missing it
    * Add isinstance() validation before using a parameter as a specific type
    * Hash passwords before including them in a request payload
    * Add pagination parameters to API calls that fetch all records at once
- Do NOT add markdown fences, explanations, or any text before or after the code.
- Do NOT rewrite functions that have no defect.
- If a function already has try/except, do not add another one.

File to fix: {filepath}
{file_content}

Return the complete fixed file now, starting from the first line of the file:"""
    return ollama_call(prompt)

# ── Helpers ────────────────────────────────────────────────────────────────────

def count_lines(content: str) -> int:
    return len(content.splitlines())

# ── Fix output validation ──────────────────────────────────────────────────────

# Patterns that indicate the LLM returned garbage / placeholder text instead of
# real code — object notation, template variables, meta-commentary, etc.
SUSPICIOUS_OUTPUT_PATTERNS = (
    r"^\s*obj\[",                    # obj['fixed_file_content'] style hallucination
    r"^\s*\{\{.*\}\}\s*$",             # {{ template }} placeholders
    r"^\s*<.*>\s*$",                  # <placeholder> tags
    r"^\s*\.\.\.\s*$",                # bare ellipsis as the entire file
    r"^(here'?s|sure,?|certainly|i've fixed)",  # conversational preamble instead of code
    r"^```",                          # markdown fence leaked into output
)
_SUSPICIOUS_RE = re.compile("|".join(SUSPICIOUS_OUTPUT_PATTERNS), re.IGNORECASE | re.MULTILINE)

def is_valid_fix(fixed_content: str, original_content: str, language: str, filepath: str) -> bool:
    """
    Reject LLM fix output that is empty, identical, implausibly short, contains
    hallucinated placeholder text, or fails to parse as valid syntax (Python only).
    Returns True only if the fix looks like genuine, usable code.
    """
    stripped = fixed_content.strip()

    if not stripped:
        log.warning("  Rejected fix for %s: empty output.", filepath)
        return False

    if stripped == original_content.strip():
        log.info("  No changes produced for %s.", filepath)
        return False

    # Reject if drastically shorter than the original — a real fix adds or
    # adjusts code, it rarely shrinks a file to a fraction of its size
    if len(stripped) < len(original_content.strip()) * 0.5:
        log.warning(
            "  Rejected fix for %s: output is %d chars vs original %d chars (too short).",
            filepath, len(stripped), len(original_content.strip()),
        )
        return False

    # Reject known hallucination / placeholder patterns
    match = _SUSPICIOUS_RE.search(stripped)
    if match:
        log.warning(
            "  Rejected fix for %s: output matches suspicious pattern %r near: %s",
            filepath, match.group(0), stripped[:80],
        )
        return False

    # For Python files, the output must be syntactically valid — this is the
    # strongest guard available and catches almost all hallucinated garbage
    if language == "python":
        try:
            import ast as _ast
            _ast.parse(stripped)
        except SyntaxError as exc:
            log.warning(
                "  Rejected fix for %s: output is not valid Python (%s).",
                filepath, exc,
            )
            return False

    return True

# ── Auto-fix flow ──────────────────────────────────────────────────────────────

def auto_fix_files(files: list, groups: dict, all_tool_outputs: list, pr_head_branch: str, llm_review: str = ""):
    """
    For each changed file that has tool findings:
    1. Skip excluded / removed / oversized files
    2. Ask LLM to produce a fixed version
    3. Commit fixes DIRECTLY to the PR branch (no separate branch, no draft PR needed)
       — this avoids the 403 that occurs when GITHUB_TOKEN tries to open PRs.
    Returns: (fixed_files, skipped_cap, skipped_long)
    """
    fixed_files  = []
    skipped_cap  = []
    skipped_long = []

    for lang, lang_files in groups.items():
        for f in lang_files:
            filepath = f["filename"]

            if f.get("status") == "removed":
                continue

            # ── Exclusion: never touch infra / self-referential files ──────
            basename = os.path.basename(filepath)
            if (
                any(filepath.startswith(p) for p in EXCLUDED_PATH_PREFIXES)
                or basename in EXCLUDED_FILENAMES
            ):
                log.info("  Excluded path — skipping auto-fix for %s", filepath)
                continue

            # ── Cap 1: max files ───────────────────────────────────────────
            if len(fixed_files) >= MAX_FILES_TO_FIX:
                skipped_cap.append(filepath)
                log.info(
                    "  [%d/%d] File cap reached — skipping %s",
                    len(fixed_files), MAX_FILES_TO_FIX, filepath,
                )
                continue

            # Check static tool findings for this file
            relevant_tools = [
                (name, output) for name, output in all_tool_outputs
                if filepath in output
            ]

            # Fallback: use LLM review text if static tools had nothing for this file
            basename_only = os.path.basename(filepath)
            llm_mentions_file = filepath in llm_review or basename_only in llm_review

            if not relevant_tools and not llm_mentions_file:
                log.info("  No findings for %s in tools or LLM review — skipping.", filepath)
                continue

            if not relevant_tools and llm_mentions_file:
                log.info("  No static tool findings for %s — using LLM review findings.", filepath)
                relevant_tools = [("LLM Review", llm_review)]

            log.info("  Fetching content of %s...", filepath)
            file_content = get_file_content(filepath, pr_head_branch)
            if file_content is None:
                log.warning("  Could not fetch %s — skipping.", filepath)
                continue

            # ── Cap 2: max lines ───────────────────────────────────────────
            line_count = count_lines(file_content)
            if line_count > MAX_LINES_TO_FIX:
                skipped_long.append((filepath, line_count))
                log.info(
                    "  %s has %d lines (limit: %d) — skipping.",
                    filepath, line_count, MAX_LINES_TO_FIX,
                )
                continue

            log.info("  Asking LLM to fix %s (%d lines)...", filepath, line_count)
            try:
                fixed_content = ask_ollama_fix(filepath, file_content, relevant_tools, lang)
            except Exception as exc:
                log.warning("  LLM fix call failed for %s: %s — skipping.", filepath, exc)
                continue

            # Sanity check: reject empty, identical, or implausible output
            if not is_valid_fix(fixed_content, file_content, lang, filepath):
                log.info("  Fix output failed validation for %s — skipping commit.", filepath)
                continue

            # Commit directly to the PR branch — no separate branch needed,
            # no draft PR needed, no extra permissions needed.
            log.info("  Committing fix directly to PR branch for %s...", filepath)
            try:
                commit_file(
                    filepath,
                    fixed_content,
                    pr_head_branch,
                    f"fix(ai-review): auto-fix issues in {filepath} [PR #{PR_NUMBER}]",
                )
                fixed_files.append(filepath)
            except Exception as exc:
                log.warning("  Could not commit fix for %s: %s — skipping.", filepath, exc)

    return fixed_files, skipped_cap, skipped_long

# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        log.info("Waiting for Ollama to be ready...")
        wait_for_ollama()

        log.info("Fetching PR info...")
        pr_info = get_pr_info()
        pr_head_branch = pr_info["head"]["ref"]
        pr_base_branch = pr_info["base"]["ref"]
        log.info("PR branch: %s → %s", pr_head_branch, pr_base_branch)

        log.info("Fetching PR files...")
        files = get_pr_files()
        if not files:
            log.info("No files changed in this PR. Exiting.")
            sys.exit(0)

        log.info("Grouping files by language...")
        groups = group_files_by_language(files)
        languages_found = set(groups.keys())
        log.info("Detected languages: %s", ", ".join(languages_found))

        all_tool_outputs: list = []
        for lang, lang_files in groups.items():
            log.info("Running tools for: %s (%d file(s))", lang, len(lang_files))
            results = run_tools_for_language(lang, lang_files)
            all_tool_outputs.extend(results)

        log.info("Building diff...")
        diff = get_diff_text(files)

        # ── Step 1: Post review comment ────────────────────────────────────
        log.info("Generating review...")
        review = ask_ollama_review(diff, all_tool_outputs, languages_found)
        tool_names = [name for name, _ in all_tool_outputs]
        post_comment(review, languages_found, tool_names)

        # ── Step 2: Auto-fix — commits fixes directly to the PR branch ──────
        if all_tool_outputs:
            log.info("Auto-fixing files with findings...")
            fixed_files, skipped_cap, skipped_long = auto_fix_files(
                files, groups, all_tool_outputs, pr_head_branch, llm_review=review
            )

            if fixed_files:
                files_list = ", ".join(f"`{f}`" for f in fixed_files)

                comment_body = (
                    f"## 🔧 Auto-fix applied\n\n"
                    f"Fixed **{len(fixed_files)} file(s)** and committed directly "
                    f"to this PR branch for your review:\n\n"
                    f"{chr(10).join(f'- `{f}`' for f in fixed_files)}\n\n"
                    f"Check the latest commits on this PR — the AI fixes are there. "
                    f"Review each change carefully before merging."
                )
                if skipped_cap:
                    comment_body += (
                        f"\n\n⚠️ **{len(skipped_cap)} file(s) skipped** — hit the "
                        f"{MAX_FILES_TO_FIX}-file cap: "
                        + ", ".join(f"`{f}`" for f in skipped_cap)
                    )
                if skipped_long:
                    comment_body += (
                        f"\n\n⚠️ **{len(skipped_long)} file(s) skipped** — over "
                        f"the {MAX_LINES_TO_FIX}-line limit: "
                        + ", ".join(f"`{f}` ({n} lines)" for f, n in skipped_long)
                    )
                comment_body += "\n\n_Treat AI fixes as a starting point — verify before merging._"

                try:
                    _gh_post(
                        f"https://api.github.com/repos/{REPO}/issues/{PR_NUMBER}/comments",
                        {"body": comment_body},
                    )
                    log.info("Auto-fix comment posted. Files fixed: %s", ", ".join(fixed_files))
                except Exception as exc:
                    log.error("Failed to post auto-fix comment: %s", exc)
            else:
                log.info("No files were auto-fixed (no changes produced by LLM).")
        else:
            log.info("No tool findings — skipping auto-fix step.")

        log.info("Done.")

    except KeyboardInterrupt:
        log.info("Interrupted by user.")
        sys.exit(1)
    except Exception as exc:
        log.exception("Unhandled exception — review agent failed: %s", exc)
        sys.exit(1)