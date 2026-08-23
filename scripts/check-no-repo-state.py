#!/usr/bin/env python3
"""Fail the build if a script writes automation state back into the repo.

WHY THIS EXISTS

Phase 0 moved every script's state into Supabase, which removed ~78 bot
commits a day from main and fixed a race that was double-emailing customers.
But nothing *enforced* it. All 16 scripts were wired by hand, and a new script
that wrote a JSON file into data/ would work perfectly, commit its state on
every run, and go unnoticed for months -- exactly how the repo got that way the
first time.

Supabase was a convention. This makes it a rule.

WHAT COUNTS AS A VIOLATION

Writing to a path under data/ or .claude/states/. Those are the two places
state used to live.

Reports are NOT state and are not flagged: .claude/reports/ holds output meant
to be committed and read by humans, which is a different thing from a cursor a
script needs on its next run. Nor is anything under /tmp.

USAGE
    python3 scripts/check-no-repo-state.py          # exits 1 on a violation
    python3 scripts/check-no-repo-state.py --list   # show what it inspected
"""

import ast
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)

# Directories that used to hold state and must stay free of it.
BANNED_FRAGMENTS = ("data/", "data\\", ".claude/states", ".claude\\states")

# Bare path SEGMENTS, matched exactly. os.path.join(REPO_ROOT, "data", "x.json")
# contains no slash anywhere, and that is precisely how all five migrated
# scripts built their paths -- so fragment matching alone misses the real
# pattern entirely. Found by testing the checker against a deliberate
# violation rather than trusting it because it printed "clean".
BANNED_SEGMENTS = ("data", "states")

# Write-ish modes. 'r' and 'rb' are fine; anything that can create or truncate
# is not.
WRITE_MODES = ("w", "a", "x", "+")

# Scripts allowed to write into those directories, with the reason. Keep this
# short and justified -- an allowlist that grows without argument is the same
# as no rule at all.
ALLOWLIST = {
    # db_migrate_state.py reads the old files during a migration; it does not
    # write them. Listed only so a future edit that adds a write is a conscious
    # decision rather than an accident.
}


def _strings_in(node):
    """Every string constant reachable inside an expression node."""
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            out.append(sub.value)
    return out


def _is_write_mode(node):
    """True if this open() call looks like it can create or truncate."""
    # open(path) with no mode defaults to 'r' -- not a write.
    mode = None
    if len(node.args) >= 2:
        mode = node.args[1]
    for kw in node.keywords:
        if kw.arg == "mode":
            mode = kw.value
    if mode is None:
        return False
    for s in _strings_in(mode):
        if any(c in s for c in WRITE_MODES):
            return True
    return False


def check_file(path):
    """Return a list of (lineno, description) violations."""
    rel = os.path.relpath(path, REPO_ROOT)
    if rel in ALLOWLIST:
        return []
    try:
        tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
    except SyntaxError as e:
        return [(getattr(e, "lineno", 0), f"could not parse: {e.msg}")]

    # Module-level constants, so `open(STATE_FILE, "w")` is resolvable. Without
    # this the checker only sees a bare Name and the path is invisible to it.
    consts = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, (ast.Call, ast.Constant, ast.BinOp, ast.JoinedStr)):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    consts[tgt.id] = node.value

    def offending(expr, _depth=0):
        """The first banned string in this path expression, or None."""
        if _depth > 3:
            return None
        # A bare name: follow it to its module-level definition.
        if isinstance(expr, ast.Name) and expr.id in consts:
            return offending(consts[expr.id], _depth + 1)
        for s in _strings_in(expr):
            if any(frag in s for frag in BANNED_FRAGMENTS):
                return s
            if s.strip("/\\") in BANNED_SEGMENTS:
                return s
        return None

    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "id", None) or getattr(fn, "attr", None)
        if name != "open" or not _is_write_mode(node) or not node.args:
            continue
        hit = offending(node.args[0])
        if hit:
            bad.append((node.lineno, f'writes to a repo state path: "{hit}"'))
    return bad


def main():
    listing = "--list" in sys.argv
    files = sorted(
        os.path.join(SCRIPT_DIR, f)
        for f in os.listdir(SCRIPT_DIR)
        if f.endswith(".py")
    )

    violations = []
    for path in files:
        found = check_file(path)
        rel = os.path.relpath(path, REPO_ROOT)
        if listing:
            print(f"  {'FAIL' if found else 'ok  '}  {rel}")
        for lineno, why in found:
            violations.append((rel, lineno, why))

    print(f"\nChecked {len(files)} scripts for state written into the repo.")
    if not violations:
        print("Clean: state lives in Supabase.")
        return 0

    print(f"\n{len(violations)} violation(s):\n")
    for rel, lineno, why in violations:
        print(f"  {rel}:{lineno}  {why}")
    print(
        "\nState belongs in Supabase, not the repo. Use scripts/db.py:\n"
        "    import db\n"
        "    state = db.load_state(NAME, default={...})\n"
        "    db.save_state(NAME, state)\n"
        "\nWriting state here means committing it on every run, which is what\n"
        "put ~78 bot commits a day on main and caused the read-modify-write\n"
        "race that double-emailed customers. If this write is genuinely not\n"
        "state -- a report, an export -- put it under .claude/reports/ instead,\n"
        "or add it to ALLOWLIST in this script with a reason."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
