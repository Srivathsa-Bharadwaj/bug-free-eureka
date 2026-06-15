import os, json, requests, subprocess, tempfile

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
PR_NUMBER = os.environ["PR_NUMBER"]
REPO = os.environ["REPO"]

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
    return "\n\n---\n\n".join(chunks[:10])  # cap at 10 files

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
        lines = [f"- {i['filename']}:{i['location']['row']} [{i['code']}] {i['message']}" for i in issues[:20]]
        return "Ruff findings:\n" + "\n".join(lines)
    except Exception:
        return result.stdout[:500] or "Ruff: no output."

def ask_gemini(diff, ruff_output):
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-2.0-flash")

    prompt = f"""You are an expert code reviewer. Review the following PR diff and static analysis output.

Focus on:
1. Security issues (hardcoded secrets, injection risks, missing auth)
2. Performance problems (N+1 queries, unnecessary loops, blocking calls)
3. Code quality (unclear naming, missing error handling, no docstrings)

Static analysis output:
{ruff_output}

PR diff:
{diff}

Respond in this format:
## Summary
One paragraph overview of the PR changes.

## Issues found
For each issue: severity (🔴 High / 🟡 Medium / 🟢 Low), file + line if known, and a clear explanation.

## Suggestions
Up to 3 concrete improvement suggestions.

Keep the review concise and actionable. If no issues, say so clearly."""

    response = model.generate_content(prompt)
    return response.text

def post_comment(body):
    url = f"https://api.github.com/repos/{REPO}/issues/{PR_NUMBER}/comments"
    payload = {"body": f"## 🤖 AI Code Review\n\n{body}"}
    resp = requests.post(url, headers=GH_HEADERS, json=payload)
    resp.raise_for_status()
    print(f"Comment posted: {resp.json()['html_url']}")

if __name__ == "__main__":
    print("Fetching PR files...")
    files = get_pr_files()
    diff = get_diff_text(files)

    print("Running ruff...")
    ruff_output = run_ruff(files)

    print("Calling Gemini...")
    review = ask_gemini(diff, ruff_output)

    print("Posting comment...")
    post_comment(review)
    print("Done.")