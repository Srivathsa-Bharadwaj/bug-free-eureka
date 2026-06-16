import os, json, requests, subprocess, time

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


def get_pr_files():
    url = f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}/files"
    resp = requests.get(url, headers=GH_HEADERS)
    resp.raise_for_status()
    return resp.json()


def get_diff_text(files):
    chunks = []
    for f in files:
        if f.get("patch"):
            chunks.append(f"File: {f['filename']}\n{f['patch']}")
    full = "\n\n---\n\n".join(chunks[:5])
    return full[:6000]


def run_ruff(files):
    py_files = [f["filename"] for f in files if f["filename"].endswith(".py")]
    if not py_files:
        return "No Python files to lint."
    result = subprocess.run(
        ["ruff", "check", "--output-format=json"] + py_files,
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
        return "Ruff findings:\n" + "\n".join(lines)
    except Exception:
        return result.stdout[:500] or "Ruff: no output."


def run_bandit(files):
    py_files = [f["filename"] for f in files if f["filename"].endswith(".py")]
    if not py_files:
        return "No Python files for security scan."
    result = subprocess.run(
        ["bandit", "-r", "-f", "json"] + py_files,
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
        return "Bandit security findings:\n" + "\n".join(lines)
    except Exception:
        return result.stdout[:500] or "Bandit: no output."


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


def ask_ollama(diff, ruff_output, bandit_output, radon_output, pytest_output):
    prompt = f"""You are an expert code reviewer. Review the following PR diff and static analysis results.

Focus on:
1. Security issues (hardcoded secrets, injection risks, missing auth checks)
2. Performance problems (N+1 queries, unnecessary loops, blocking I/O)
3. Code quality (unclear naming, missing error handling, missing docstrings)
4. Test coverage gaps (functions changed but not tested)

Static analysis (Ruff):
{ruff_output}

Security scan (Bandit):
{bandit_output}

Complexity analysis (Radon):
{radon_output}

Test status (Pytest):
{pytest_output}

PR diff:
{diff}

Respond using this exact format:

## Summary
One paragraph overview of the changes in this PR.

## Issues found
For each issue use: severity (🔴 High / 🟡 Medium / 🟢 Low), filename and line if known, clear explanation.

## Suggestions
Up to 3 concrete, actionable improvement suggestions.

Be concise. If no issues are found, say so clearly."""

    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.2,
            "num_predict": 1024
        }
    }

    resp = requests.post(OLLAMA_URL, json=payload, timeout=180)
    resp.raise_for_status()
    return resp.json()["response"]


def post_comment(body):
    url = f"https://api.github.com/repos/{REPO}/issues/{PR_NUMBER}/comments"
    payload = {"body": f"## 🤖 AI Code Review\n\n{body}\n\n---\n*Reviewed by qwen2.5-coder running locally on GitHub Actions*"}
    resp = requests.post(url, headers=GH_HEADERS, json=payload)
    resp.raise_for_status()
    print(f"Comment posted: {resp.json()['html_url']}")

def run_radon(files):
    py_files = [f["filename"] for f in files if f["filename"].endswith(".py")]
    if not py_files:
        return "No Python files for complexity analysis."
    result = subprocess.run(
        ["radon", "cc", "--min", "B", "--show-complexity", "-s"] + py_files,
        capture_output=True, text=True
    )
    return result.stdout[:1000] or "Radon: no complex functions found."


def run_pytest():
    result = subprocess.run(
        ["pytest", "--tb=no", "-q", "--co"],  # just collect, don't run (fast)
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return "No tests found or pytest not configured."
    test_count = result.stdout.count("test session starts")
    return f"Pytest: {result.stdout[:500]}"

"""if __name__ == "__main__":
    print("Waiting for Ollama to be ready...")
    wait_for_ollama()

    print("Fetching PR files...")
    files = get_pr_files()

    print("Running Ruff...")
    ruff_output = run_ruff(files)
    print(ruff_output)

    print("Running Bandit...")
    bandit_output = run_bandit(files)
    print(bandit_output)

    print("Building diff...")
    diff = get_diff_text(files)

    radon_output = run_radon(files)

    print("Calling local LLM via Ollama...")
    #review = ask_ollama(diff, ruff_output, bandit_output)
    review = ask_ollama(diff, ruff_output, bandit_output, radon_output)

    print("Posting comment to PR...")
    post_comment(review)
    print("Done.")"""

if __name__ == "__main__":
    print("Waiting for Ollama to be ready...")
    wait_for_ollama()

    print("Fetching PR files...")
    files = get_pr_files()

    print("Running Ruff...")
    ruff_output = run_ruff(files)
    print(ruff_output)

    print("Running Bandit...")
    bandit_output = run_bandit(files)
    print(bandit_output)

    print("Running Radon...")
    radon_output = run_radon(files)
    print(radon_output)

    print("Running Pytest...")
    pytest_output = run_pytest()
    print(pytest_output)

    print("Building diff...")
    diff = get_diff_text(files)

    print("Calling local LLM via Ollama...")
    review = ask_ollama(diff, ruff_output, bandit_output, radon_output, pytest_output)

    print("Posting comment to PR...")
    post_comment(review)
    print("Done.")