"""Fetch the comments of a Bitbucket Cloud pull request into a JSON + Markdown file.

Usage (PowerShell, from the repository root):
    $env:BB_USER  = "<bitbucket username or email>"
    $env:BB_TOKEN = "<app password / API token with Pull requests: Read>"
    python runbooks/scripts/bitbucket_pr/fetch_pr_comments.py `
        --workspace <workspace> --repo <repo_slug> --pr <id> `
        --pr-commit <hash the PR was cut from> --out runbooks/pr_review/comments

Only comments not seen by a previous run are written (state in <out>/seen.json);
pass --all to ignore it. Inline comments get the line they point to in the
current checkout (--pr-commit .. HEAD diff), since the PR is older than HEAD.
"""
import argparse
import json
import logging
import os
import re
import subprocess
from pathlib import Path

import requests

logger = logging.getLogger("fetch_pr_comments")
API = "https://api.bitbucket.org/2.0/repositories/{ws}/{repo}/pullrequests/{pr}"
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def get_all(url, auth):
    while url:
        resp = requests.get(url, auth=auth, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        yield from data.get("values", [])
        url = data.get("next")


def hunks(pr_commit, path):
    out = subprocess.run(
        ["git", "diff", "-U0", pr_commit, "HEAD", "--", path],
        capture_output=True, text=True, check=False,
    ).stdout
    return [
        (int(m[1]), int(m[2] or 1), int(m[3]), int(m[4] or 1))
        for m in map(HUNK.match, out.splitlines()) if m
    ]


def current_line(line, hs):
    """Line `line` of the old file -> (line in HEAD, status)."""
    shift = 0
    for old_start, old_len, new_start, new_len in hs:
        if line < old_start + (old_len if old_len else 1) and line >= old_start and old_len:
            return None, "line changed or removed since the PR commit"
        if old_start + old_len <= line:
            shift += new_len - old_len
        elif old_len == 0 and old_start < line:
            shift += new_len
    return line + shift, "moved" if shift else "unchanged"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", required=True, type=int)
    ap.add_argument("--pr-commit", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    auth = (os.environ["BB_USER"], os.environ["BB_TOKEN"])
    base = API.format(ws=args.workspace, repo=args.repo, pr=args.pr)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    seen_file = out / "seen.json"
    seen = set() if args.all or not seen_file.exists() else set(json.loads(seen_file.read_text()))

    comments = [c for c in get_all(base + "/comments?pagelen=100", auth) if not c.get("deleted")]
    new = [c for c in comments if c["id"] not in seen]
    cache = {}
    rows = []
    for c in new:
        inline = c.get("inline") or {}
        path = inline.get("path")
        old = inline.get("to") or inline.get("from")
        row = {
            "id": c["id"],
            "parent": (c.get("parent") or {}).get("id"),
            "author": c["user"]["display_name"],
            "created": c["created_on"],
            "file": path,
            "line_in_pr": old,
            "content": c["content"]["raw"],
            "link": c["links"]["html"]["href"],
        }
        if path and old:
            hs = cache.setdefault(path, hunks(args.pr_commit, path))
            row["line_now"], row["line_status"] = current_line(old, hs)
        rows.append(row)

    stamp = max((r["created"] for r in rows), default="none").replace(":", "-")
    (out / f"comments_{stamp}.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    md = [f"# PR {args.pr} comments (PR commit {args.pr_commit})", ""]
    for r in rows:
        where = f"`{r['file']}` L{r['line_in_pr']} (now L{r.get('line_now')}, {r.get('line_status')})" if r["file"] else "general"
        reply = f" - reply to #{r['parent']}" if r["parent"] else ""
        md += [f"## #{r['id']} {where}{reply}", f"*{r['author']}, {r['created']}* - {r['link']}", "", r["content"], ""]
    (out / f"comments_{stamp}.md").write_text("\n".join(md), encoding="utf-8")
    seen_file.write_text(json.dumps(sorted(seen | {c["id"] for c in comments})))
    logger.info("%d new comment(s) of %d, written to %s", len(rows), len(comments), out)


if __name__ == "__main__":
    main()
