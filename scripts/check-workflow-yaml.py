#!/usr/bin/env python3
"""Reject duplicate keys in .github/workflows/*.yml before GitHub does.

On 2026-10-03 a PR added SUPABASE_URL and SUPABASE_SERVICE_KEY to the Bing
report's `env:` block, eleven lines above the pair that was already there. The
whole workflow went offline: GitHub refuses to parse a mapping with a repeated
key, so every run died before creating a single job and every dispatch returned
422 Unprocessable Entity. The cron-job.org dispatcher emailed a failure; the
GitHub-side runs showed up with the file path as their name and no jobs at all,
which looks like nothing much until you read the dispatch error.

Nothing caught it. The file is valid YAML by the spec's own rules -- a repeated
key is "unspecified behaviour", and PyYAML's safe_load silently keeps the last
value -- so parsing it locally reports success. `yamllint` would have caught it,
and so would an actual run, but the first run was the scheduled Sunday send.

Deliberately stdlib only, matching check-no-repo-state.py, so the lint job
installs nothing. That rules out PyYAML's loader hook, so this walks the file
by indentation instead:

  - a key is `  foo:` at some indent inside some parent block
  - two keys collide only at the SAME indent under the SAME parent
  - block scalars (`run: |`, `description: >`) hold arbitrary text, including
    lines that look exactly like keys, and are skipped wholesale
  - list items reset the namespace, since `- name:` starts a new mapping

It does not validate anything else about the workflow. GitHub's own parser is
the authority on the rest; this covers the one failure mode that is invisible
locally and takes a workflow completely offline.

Usage:
  python3 scripts/check-workflow-yaml.py            # repo's workflows
  python3 scripts/check-workflow-yaml.py a.yml b.yml
"""

import glob
import re
import sys

KEY = re.compile(r'^(\s*)(-\s+)?([A-Za-z_][\w.\-]*)\s*:(\s|$)')
BLOCK_SCALAR = re.compile(r':\s*[|>][-+]?\d*\s*(#.*)?$')


def duplicate_keys(path):
    """Return [(key, first_line, repeat_line)] for keys defined twice."""
    with open(path, encoding="utf-8") as fh:
        lines = fh.readlines()

    seen = {}        # indent -> {key: line number}
    findings = []
    skip_deeper_than = None

    for n, raw in enumerate(lines, 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue

        indent = len(raw) - len(raw.lstrip())

        # Inside a block scalar: everything more indented than its key is text.
        if skip_deeper_than is not None:
            if indent > skip_deeper_than:
                continue
            skip_deeper_than = None

        m = KEY.match(raw)
        if not m:
            continue
        lead, dash, key = m.group(1), m.group(2), m.group(3)

        # `- name: x` puts the key one level deeper than the dash itself, and
        # starts a fresh mapping, so anything recorded at that depth is stale.
        if dash:
            indent = len(lead) + len(dash)
            for d in [d for d in seen if d >= indent]:
                del seen[d]

        # Leaving a block ends the scope of every key inside it.
        for d in [d for d in seen if d > indent]:
            del seen[d]

        bucket = seen.setdefault(indent, {})
        if key in bucket:
            findings.append((key, bucket[key], n))
        else:
            bucket[key] = n

        if BLOCK_SCALAR.search(raw):
            skip_deeper_than = indent

    return findings


def main():
    paths = sys.argv[1:] or sorted(glob.glob(".github/workflows/*.yml"))
    if not paths:
        print("No workflow files found.")
        return 1

    failed = 0
    for path in paths:
        for key, first, repeat in duplicate_keys(path):
            print(f"{path}:{repeat}: duplicate key '{key}' "
                  f"(already defined on line {first})")
            failed = 1

    if failed:
        print("\nGitHub rejects duplicate keys outright: the workflow will not "
              "run and dispatches return 422. Remove the repeated key.")
    else:
        print(f"OK: no duplicate keys in {len(paths)} workflow file(s).")
    return failed


if __name__ == "__main__":
    sys.exit(main())
