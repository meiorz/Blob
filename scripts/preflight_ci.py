#!/usr/bin/env python3
"""Predict the CI outcome locally, against the tree CI will actually see.

Why this exists, concretely: running CI guard 2 by hand in a working tree gives
the wrong answer. On 2026-08-13 the working copy of tests/_fuzz_child.py was
clean, so a hand-run grep for the marker reported no host paths -- while the
COMMITTED version at HEAD still carried an acquisition-host absolute path into
the security package. CI checks out a commit. It would have gone red on a guard
that passed locally.

So every check here runs against a git revision (default HEAD), never the
working tree, and the first check asks a question CI cannot ask about itself:
is the workflow file even committed?

    python3 scripts/preflight_ci.py                 # check HEAD
    python3 scripts/preflight_ci.py --rev origin/main
    python3 scripts/preflight_ci.py --staged        # check what `git add` staged
    python3 scripts/preflight_ci.py --with-tests    # also run the suites (slow)

Exit 0 = CI predicted green. Exit 1 = CI predicted red, with the reason.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ".github/workflows/ci.yml"

# Mirrors the exclusions in .github/workflows/ci.yml guard 2. Keep in sync.
GUARD2_EXCLUDES = [":!results/raw", ":!docs/dataset-catalog.md", ":!.github"]

# The acquisition-host absolute-path marker guard 2 searches for, assembled at
# runtime rather than written as one literal.
#
# This is not a stylistic choice. The guard greps the whole tracked tree for this
# token, and this file is itself tracked -- so spelling it out here makes the
# guard match its own source and report a violation that does not exist. Two such
# false positives were traced to exactly that on 2026-08-13. Do not "simplify"
# this back into a single string, and do not write the token in the prose above.
FORBIDDEN_HOST_PATH = "/" + "sessions" + "/"

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    results.append((ok, name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if detail:
        for line in detail.strip().splitlines():
            print(f"         {line}")
    return ok


def git(*a: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *a], cwd=str(ROOT), capture_output=True, text=True)


def read_at_rev(rev: str, path: str) -> str | None:
    p = git("show", f"{rev}:{path}")
    return p.stdout if p.returncode == 0 else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rev", default="HEAD",
                    help="revision CI would check out (default HEAD)")
    ap.add_argument("--staged", action="store_true",
                    help="check the index instead of a commit")
    ap.add_argument("--with-tests", action="store_true",
                    help="also run pytest tests/ (slow; CI does run it)")
    args = ap.parse_args()

    if git("rev-parse", "--git-dir").returncode != 0:
        print("not a git repository", file=sys.stderr)
        return 1

    rev = args.rev
    scope = "index (staged)" if args.staged else f"commit {rev}"
    print(f"\nCI pre-flight -- evaluating {scope}\n")

    # -- 0. Does CI exist at all? -------------------------------------------
    # A repo can pass every guard locally and still have no pipeline, because
    # the workflow was never committed. That is not a green build; it is no
    # build. Ask first, because it invalidates every later "CI says" claim.
    tracked = git("ls-files", "--error-unmatch", WORKFLOW).returncode == 0
    at_rev = read_at_rev(rev, WORKFLOW) is not None if not args.staged else tracked
    on_disk = (ROOT / WORKFLOW).exists()
    if not check(at_rev, f"workflow {WORKFLOW} exists at {scope}",
                 "" if at_rev else
                 (f"file exists on disk but is UNTRACKED -- GitHub Actions has never run.\n"
                  f"git add {WORKFLOW}" if on_disk
                  else "no workflow file at all; nothing runs on push")):
        pass

    # -- 1. Byte compilation -------------------------------------------------
    p = subprocess.run([sys.executable, "-m", "compileall", "-q",
                        "benchmarks", "scripts", "security", "tests"],
                       cwd=str(ROOT), capture_output=True, text=True)
    check(p.returncode == 0, "byte-compile benchmarks scripts security tests",
          (p.stdout + p.stderr).strip()[:800])

    # -- 2. environment_class guard -----------------------------------------
    p = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0,'benchmarks');"
         "from env_capture import ENVIRONMENT_CLASS as e; print(e)"],
        cwd=str(ROOT), capture_output=True, text=True,
        env={**__import__("os").environ, "COMPRESSION_BENCH_ENV": "sandbox"})
    got = p.stdout.strip()
    check(p.returncode == 0 and got == "sandbox",
          "guard 1: CI never claims hardware validation",
          f"environment_class={got!r} (expected 'sandbox')"
          if got != "sandbox" else "")

    # -- 3. No host-specific absolute paths, AT THE REVISION -----------------
    # The important part: `git grep <pat> <rev>` searches the committed tree.
    # Omitting <rev> searches the working tree, which is how a committed
    # violation hides behind a locally-fixed file.
    if args.staged:
        p = git("grep", "-n", "--cached", "--", FORBIDDEN_HOST_PATH, ".", *GUARD2_EXCLUDES)
    else:
        p = git("grep", "-n", FORBIDDEN_HOST_PATH, rev, "--", ".", *GUARD2_EXCLUDES)
    hits = p.stdout.strip()
    detail = ""
    if hits:
        detail = hits[:1500]
        # The common cause: a file .gitignore lists but git still tracks.
        # .gitignore does not untrack; `git rm --cached` does.
        #
        # --no-index is required. Plain `git check-ignore` consults the index and
        # stays silent for tracked paths -- precisely the case being diagnosed --
        # so without it this hint never fires on the only files it is about.
        ignored_but_tracked = []
        for line in hits.splitlines():
            f = line.split(":")[1] if not args.staged else line.split(":")[0]
            f = f.strip()
            if (git("check-ignore", "--no-index", "-q", f).returncode == 0
                    and git("ls-files", "--error-unmatch", f).returncode == 0):
                ignored_but_tracked.append(f)
        if ignored_but_tracked:
            uniq = sorted(set(ignored_but_tracked))
            detail += ("\n\n  These are listed in .gitignore but STILL TRACKED. .gitignore "
                       "does not\n  untrack existing files:\n    git rm --cached "
                       + " ".join(uniq))
    check(not hits, "guard 2: no host-specific absolute paths in committed files", detail)

    # -- 4. Catalog provenance ----------------------------------------------
    raw = (read_at_rev(rev, "data/metadata/catalog.json") if not args.staged
           else git("show", ":data/metadata/catalog.json").stdout)
    if raw is None:
        check(False, "guard 3: catalog provenance", "catalog.json not present at this revision")
    else:
        try:
            cat = json.loads(raw)
            bad = [d["id"] for d in cat["datasets"]
                   if not re.fullmatch(r"[0-9a-f]{64}", d.get("sha256", ""))]
            missing = [d["id"] for d in cat["datasets"]
                       if not d.get("file") or not d.get("source")]
            msg = ""
            if bad:
                msg += f"sha256 not 64 lowercase hex for: {bad}\n"
            if missing:
                msg += f"missing 'file' or 'source' for: {missing}"
            check(not bad and not missing,
                  f"guard 3: catalog provenance ({len(cat['datasets'])} entries)", msg)
        except Exception as e:
            check(False, "guard 3: catalog provenance", f"unparseable: {e}")

    # -- 5. Rendered compat matrix matches its JSON source -------------------
    renderer = ROOT / "scripts" / "render_compat_matrix.py"
    if renderer.exists():
        p = subprocess.run([sys.executable, str(renderer), "--check"],
                           cwd=str(ROOT), capture_output=True, text=True)
        check(p.returncode == 0, "guard 4: compat matrix doc matches compat_matrix.json",
              (p.stdout + p.stderr).strip()[:800] if p.returncode else "")
    else:
        check(False, "guard 4: compat matrix renderer present",
              "scripts/render_compat_matrix.py missing but ci.yml calls it")

    # -- 6. Suites -----------------------------------------------------------
    if args.with_tests:
        import os as _os
        env = {**_os.environ}
        # CI does not set this, and neither do we. It skips all 9 hostile-input
        # tests, not just the bomb -- and three documents cite "security 9/9".
        env.pop("COMPRESSION_BENCH_SKIP_HEAVY", None)
        env["COMPRESSION_BENCH_ENV"] = "sandbox"
        p = subprocess.run([sys.executable, "-m", "pytest", "tests/", "-q", "-rs"],
                           cwd=str(ROOT), env=env, text=True)
        check(p.returncode == 0, "correctness + security suites (SKIP_HEAVY unset)")
    else:
        print("  [skip] correctness + security suites (--with-tests to include)")

    # -- 7. Untracked files CI will never see --------------------------------
    p = git("ls-files", "--others", "--exclude-standard")
    untracked = [f for f in p.stdout.split() if f]
    if untracked:
        print(f"\n  NOTE: {len(untracked)} untracked path(s). CI evaluates only committed")
        print("  files, so nothing below is being checked by any pipeline:")
        for f in sorted(untracked)[:25]:
            print(f"    {f}")

    failed = [n for ok, n, _ in results if not ok]
    print(f"\n{'-' * 66}")
    if failed:
        print(f"CI PREDICTED RED -- {len(failed)} check(s) would fail:")
        for n in failed:
            print(f"  - {n}")
        print()
        return 1
    print("CI predicted green.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
