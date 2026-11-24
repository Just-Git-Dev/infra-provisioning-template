#!/usr/bin/env python3
# VENDORED from claude-config skills/public-name-guard @ 4cd9e42b038e — edit canonical, not this copy
"""Refuse names that must never appear in a PUBLIC repo, without ever printing them.

Canonical copy: claude-config `skills/public-name-guard/scripts/`. Repos carry a VENDORED copy
(see vendor.sh): edit the canonical one, never a copy.

The list of forbidden patterns never lives in the repo (the list would itself be a mention).
Source, first one found wins:
  1. $NAME_DENYLIST       the list's TEXT (CI: a repo secret; an empty value counts as unset)
  2. $NAME_DENYLIST_FILE  a path to the list
  3. ~/.config/jgd/public-denylist.txt
No list, or a list with zero patterns, FAILS: a guard that skips when unconfigured is no guard.

List format: one case-insensitive Python regex per line; blank lines and `#` lines ignored.
Make patterns word-bounded (`\\bname\\b`): a bare substring catches "fatally" for "tally".

Scans:
  - every tracked file (`git ls-files`): its path and its text lines. Binary files and any file
    named CODEOWNERS are skipped (GitHub needs real handles there).
  - commit messages in --range A..B (or $NAMES_RANGE); an empty range scans none. Older history
    is never scanned: it may hold names by the owner's no-rewrite ruling.
  - $PR_TITLE and $PR_BODY when non-empty (CI, pull_request events).

Output never contains the matched text: only `path:line: pattern #k`, `commit <sha>: pattern #k`,
`PR title: pattern #k`, `PR body:<line>: pattern #k`. A summary line always states how much
was examined. Exit 0 clean, 1 hits / no list / empty list, 2 bad regex or bad range.
"""
import argparse
import os
import re
import subprocess
import sys

DEFAULT_LIST = "~/.config/jgd/public-denylist.txt"
NOT_FOUND = f"name list not found: create {DEFAULT_LIST} or set NAME_DENYLIST"


class Usage(Exception):
    pass


def load_list():
    text = os.environ.get("NAME_DENYLIST", "")
    if text.strip():
        return text, "NAME_DENYLIST"
    path = os.environ.get("NAME_DENYLIST_FILE") or os.path.expanduser(DEFAULT_LIST)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read(), "file"
    except FileNotFoundError:
        return None, None


def compile_patterns(text):
    pats = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            pats.append(re.compile(line, re.IGNORECASE))
        except re.error:
            # Never echo the pattern or the regex error (it quotes the pattern).
            raise Usage(f"names: name list line {lineno}: invalid regex")
    return pats


def first_hit(pats, text):
    for k, p in enumerate(pats, 1):
        if p.search(text):
            return k
    return None


def git(root, *args):
    r = subprocess.run(["git", "-C", root, *args], capture_output=True)
    if r.returncode != 0:
        raise Usage(f"names: git {args[0]} failed (exit {r.returncode})")
    return r.stdout


def scan_files(root, pats, hits):
    n = 0
    for i, rel in enumerate(git(root, "ls-files", "-z").decode("utf-8", "replace").split("\0"), 1):
        if not rel or os.path.basename(rel) == "CODEOWNERS":
            continue
        path = os.path.join(root, rel)
        if not os.path.isfile(path):   # deleted in the tree, or a submodule
            continue
        n += 1
        k = first_hit(pats, rel)
        if k:
            # The path itself holds the name, so printing it would leak it: name the file by its
            # position instead (`git ls-files | sed -n '<i>p'` shows it locally).
            hits.append(f"git ls-files entry #{i}: (file path) pattern #{k}")
            rel = f"git ls-files entry #{i}"
        with open(path, "rb") as f:
            data = f.read()
        if b"\0" in data[:8192]:
            continue
        for lineno, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            k = first_hit(pats, line)
            if k:
                hits.append(f"{rel}:{lineno}: pattern #{k}")
    return n


def scan_commits(root, rng, pats, hits):
    if not rng:
        return 0
    out = git(root, "log", "--format=%H%x00%B%x1e", rng, "--").decode("utf-8", "replace")
    n = 0
    for rec in out.split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        sha, _, msg = rec.partition("\0")
        n += 1
        k = first_hit(pats, msg)
        if k:
            hits.append(f"commit {sha[:12]}: pattern #{k}")
    return n


def scan_pr(pats, hits):
    n = 0
    title = os.environ.get("PR_TITLE", "")
    if title:
        n += 1
        k = first_hit(pats, title)
        if k:
            hits.append(f"PR title: pattern #{k}")
    body = os.environ.get("PR_BODY", "")
    if body:
        n += 1
        for lineno, line in enumerate(body.splitlines(), 1):
            k = first_hit(pats, line)
            if k:
                hits.append(f"PR body:{lineno}: pattern #{k}")
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=".", help="repo to scan (default: cwd)")
    ap.add_argument("--range", dest="rng", default=None,
                    help="commit range A..B whose messages to scan (default: $NAMES_RANGE)")
    a = ap.parse_args(argv)
    rng = a.rng if a.rng is not None else os.environ.get("NAMES_RANGE", "")
    try:
        text, _src = load_list()
        if text is None:
            print(f"names: {NOT_FOUND}", file=sys.stderr)
            return 1
        pats = compile_patterns(text)
        if not pats:
            print("names: the name list has 0 patterns; refusing to pass on an empty list", file=sys.stderr)
            return 1
        root = git(a.root, "rev-parse", "--show-toplevel").decode().strip()
        hits = []
        nf = scan_files(root, pats, hits)
        nc = scan_commits(root, rng, pats, hits)
        np_ = scan_pr(pats, hits)
    except Usage as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"names: examined {nf} files, {nc} commit messages, {np_} PR fields against {len(pats)} patterns", flush=True)
    for h in hits:
        print(h)
    if hits:
        print(f"names: {len(hits)} hit(s). Remove the listed name (or reword); the list is in "
              f"{DEFAULT_LIST} / secret NAME_DENYLIST, pattern #k = k-th pattern line.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
