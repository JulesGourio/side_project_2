"""Fetch the comments of the Bitbucket Cloud pull request of the current branch.

Usage (PowerShell, from the repository root): python runbooks/scripts/bitbucket_pr/fetch_pr_comments.py
Workspace and repo come from the `origin` remote, the PR from the current branch
(else the latest open PR); username and token are asked once per run, never stored.
Only comments not seen by a previous run are written (<out>/seen.json); --all ignores it.
Inline comments get the line they point to in the current checkout (PR_COMMIT..HEAD diff).
"""
import argparse
import getpass
import json
import logging
import os
import re
import subprocess
from pathlib import Path

import requests

PR_COMMIT = "14296499a19baf710dc6c10d74bdd71204176b2d"
OUT = "runbooks/pr_review/comments"
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
    ap.add_argument("--pr", type=int)
    ap.add_argument("--pr-commit", default=PR_COMMIT)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    git = lambda *a: subprocess.run(["git", *a], capture_output=True, text=True, check=True).stdout.strip()
    remote = re.search(r"bitbucket\.org[:/]([^/]+)/([^/]+?)(?:\.git)?$", git("remote", "get-url", "origin"))
    ws, repo = remote[1], remote[2]
    auth = (os.environ.get("BB_USER") or input("Bitbucket user: "),
            os.environ.get("BB_TOKEN") or getpass.getpass("Bitbucket app password/token: "))
    prs = f"https://api.bitbucket.org/2.0/repositories/{ws}/{repo}/pullrequests"
    pr = args.pr
    if not pr:
        branch = git("branch", "--show-current")
        found = list(get_all(f'{prs}?state=OPEN&q=source.branch.name="{branch}"', auth)) or list(get_all(prs + "?state=OPEN", auth))
        pr = found[0]["id"]
        logger.info("PR %s (%s)", pr, found[0]["title"])
    args.workspace, args.repo, args.pr = ws, repo, pr
    base = API.format(ws=ws, repo=repo, pr=pr)
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
