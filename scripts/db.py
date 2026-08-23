"""Supabase (Postgres) access for the Black Hill automations.

Dual-credential like every other script in this repo: environment variables
when running in GitHub Actions, ~/.config/supabase/config.json when running
locally.

Talks to Supabase over its REST interface (PostgREST) using urllib, so nothing
new lands in requirements.txt and CI installs stay fast. 23 scripts here
already use urllib; this matches them.

FAILS CLOSED. If the database is unreachable these functions raise, the
workflow fails, and notify-failure.yml sends an alert. Do not add a fallback
to the old JSON files. A skipped 5-minute cycle is harmless because the next
run picks the work up, but reading stale state would re-send lead
notifications to Evelin and Denisse.

Phase 0 of docs/architecture/data-platform-plan.md.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

CONFIG_PATH = os.path.expanduser("~/.config/supabase/config.json")
TIMEOUT = 30

# Writes go in a request body, so they can batch generously.
WRITE_BATCH = 200

# Reads filter via ?key=in.(...) in the URL, and long URLs get rejected around
# 8 KB. Outlook message ids are ~152 chars and expand to ~210 once the base64
# '=' and '+' are percent-encoded, so a fixed count is the wrong knob: 200 of
# them would build a 40 KB URL. Budget by encoded length instead, which also
# batches short keys (bid-monitor uses 'city:uuid') far more efficiently.
READ_URL_BUDGET = 6000


class DatabaseError(RuntimeError):
    """Raised when the database cannot be reached or returns an error."""


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def _creds():
    """Return (base_url, service_key).

    Cloud mode is detected the same way the other scripts detect it: presence
    of the secret in the environment.
    """
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()

    # .strip() is deliberate. A trailing newline in a GitHub secret silently
    # broke the Aspire and WhatConverts Mailchimp syncs once already.
    if url and key:
        return url.rstrip("/"), key

    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        url = str(cfg.get("url", "")).strip().rstrip("/")
        key = str(cfg.get("service_key", "")).strip()
        if url and key:
            return url, key

    raise DatabaseError(
        "No Supabase credentials. Set SUPABASE_URL and SUPABASE_SERVICE_KEY, "
        f"or create {CONFIG_PATH} with {{\"url\": ..., \"service_key\": ...}}"
    )


def is_configured():
    """True if credentials are available, without raising. For soft checks."""
    try:
        _creds()
        return True
    except DatabaseError:
        return False


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _request(method, path, body=None, prefer=None, retries=3):
    """Issue a PostgREST request and return the decoded JSON body (or None)."""
    base, key = _creds()
    url = f"{base}/rest/v1/{path}"

    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer

    payload = json.dumps(body).encode() if body is not None else None

    last_err = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=payload, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode().strip()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            # 4xx is our bug (bad schema, bad filter). Retrying won't help.
            if 400 <= e.code < 500 and e.code not in (408, 429):
                raise DatabaseError(f"{method} {path} -> HTTP {e.code}: {detail}") from e
            last_err = DatabaseError(f"{method} {path} -> HTTP {e.code}: {detail}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = DatabaseError(f"{method} {path} -> {type(e).__name__}: {e}")

        if attempt < retries - 1:
            time.sleep(2 ** attempt)  # 1s, 2s

    raise last_err


def _q(value):
    """URL-encode a value for a PostgREST filter."""
    return urllib.parse.quote(str(value), safe="")


# ---------------------------------------------------------------------------
# State documents  (drop-in replacement for data/*.json)
# ---------------------------------------------------------------------------

def load_state(name, default=None):
    """Return the stored state dict for `name`, or `default` if absent."""
    rows = _request("GET", f"automation_state?name=eq.{_q(name)}&select=state")
    if rows:
        return rows[0].get("state") or (default if default is not None else {})
    return default if default is not None else {}


def save_state(name, state):
    """Upsert the state document for `name`."""
    _request(
        "POST",
        "automation_state",
        body={
            "name": name,
            "state": state,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        prefer="resolution=merge-duplicates,return=minimal",
    )


# ---------------------------------------------------------------------------
# Idempotency ledger
# ---------------------------------------------------------------------------

def filter_unprocessed(script, keys):
    """Return the subset of `keys` this script has not handled yet.

    Order is preserved so callers can keep processing oldest-first.
    """
    keys = [str(k) for k in keys]
    if not keys:
        return []

    seen = set()
    for batch in _read_batches(keys):
        # PostgREST in.() needs each value double-quoted, with any embedded
        # quote doubled; ids routinely contain , = / and +
        joined = ",".join('"' + k.replace('"', '""') + '"' for k in batch)
        # Keep the quoting out of the f-string: CI runs Python 3.11, where a
        # backslash inside an f-string expression is a SyntaxError (allowed
        # from 3.12 / PEP 701, which is why this compiled fine locally).
        encoded = urllib.parse.quote(joined, safe='(),"')
        rows = _request(
            "GET",
            f"processed_keys?script=eq.{_q(script)}&key=in.({encoded})&select=key",
        ) or []
        seen.update(r["key"] for r in rows)

    return [k for k in keys if k not in seen]


def _read_batches(keys):
    """Split keys into batches whose encoded filter stays under the URL budget.

    Always yields at least one key per batch, so a single pathologically long
    key still gets attempted rather than silently dropped.
    """
    batch, size = [], 0
    for k in keys:
        encoded = len(urllib.parse.quote(k, safe="")) + 3  # quotes + comma
        if batch and size + encoded > READ_URL_BUDGET:
            yield batch
            batch, size = [], 0
        batch.append(k)
        size += encoded
    if batch:
        yield batch


def mark_processed(script, keys, note=None, kind=None):
    """Record `keys` as handled by `script`. Re-marking is a no-op.

    `kind` is 'lead', 'spam', 'call' or None, and is what makes the counters
    in stats() derivable instead of stored.
    """
    if isinstance(keys, str):
        keys = [keys]
    keys = [str(k) for k in keys]
    if not keys:
        return

    for i in range(0, len(keys), WRITE_BATCH):
        rows = [
            {"script": script, "key": k, "note": note, "kind": kind}
            for k in keys[i:i + WRITE_BATCH]
        ]
        _request(
            "POST",
            "processed_keys",
            body=rows,
            prefer="resolution=ignore-duplicates,return=minimal",
        )


def latest_key(script):
    """Most recently processed key for `script`, or None. Used by health checks."""
    rows = _request(
        "GET",
        f"processed_keys?script=eq.{_q(script)}&select=key"
        "&order=processed_at.desc&limit=1",
    )
    return rows[0]["key"] if rows else None


# ---------------------------------------------------------------------------
# Shared lead index
#
# Its own table rather than a field inside a state document, so concurrent
# writers touch separate rows instead of overwriting each other's work.
# ---------------------------------------------------------------------------

_MAPPING_COLUMNS = {
    "aspire_contact_id", "opp_number", "lead_type",
    "service", "traffic_source", "lead_date",
}


def load_lead_mappings():
    """Return {wc_lead_id: {...}}, matching the old state["lead_mappings"] shape."""
    rows = _request("GET", "lead_mappings?select=*") or []
    out = {}
    for r in rows:
        lead_id = r.pop("wc_lead_id")
        extra = r.pop("extra", None) or {}
        r.pop("updated_at", None)
        # 'date' was the old field name; keep callers working unchanged
        if r.get("lead_date") is not None:
            r["date"] = r.pop("lead_date")
        else:
            r.pop("lead_date", None)
        out[lead_id] = {**{k: v for k, v in r.items() if v is not None}, **extra}
    return out


def save_lead_mappings(mappings):
    """Upsert every mapping. Rows absent from `mappings` are left alone.

    Deliberately does not delete missing rows: whatconverts-roi-sync.py used to
    write the whole collection at once, which is exactly how concurrent updates
    got erased.
    """
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for lead_id, m in (mappings or {}).items():
        if not isinstance(m, dict):
            continue
        # PostgREST rejects a bulk insert whose objects have differing key sets
        # ("All object keys must match"), and these mappings are ragged: some
        # carry match_score/match_method, some have no service. Start from every
        # column so each row has an identical shape.
        row = {c: None for c in _MAPPING_COLUMNS}
        row["wc_lead_id"] = str(lead_id)
        row["extra"] = {}
        row["updated_at"] = now  # merge-duplicates won't fire the column default
        for k, v in m.items():
            key = "lead_date" if k == "date" else k
            if key in _MAPPING_COLUMNS:
                row[key] = v
            else:
                row["extra"][k] = v
        rows.append(row)

    for i in range(0, len(rows), WRITE_BATCH):
        _request(
            "POST",
            "lead_mappings",
            body=rows[i:i + WRITE_BATCH],
            prefer="resolution=merge-duplicates,return=minimal",
        )


def is_processed(script, key):
    """True if this script already handled `key`."""
    return not filter_unprocessed(script, [key])


# ---------------------------------------------------------------------------
# Run history
# ---------------------------------------------------------------------------

def run_start(script):
    """Open a run row. Returns its id, or None if the insert fails.

    Never raises: run tracking is observability, and losing it must not take
    down the automation it is observing.
    """
    try:
        rows = _request(
            "POST",
            "automation_runs",
            body={
                "script": script,
                "run_env": "cloud" if os.environ.get("GITHUB_ACTIONS") else "local",
            },
            prefer="return=representation",
        )
        return rows[0]["id"] if rows else None
    except DatabaseError:
        return None


def run_finish(run_id, status="ok", records=None, error=None):
    """Close a run row. Never raises, for the same reason as run_start()."""
    if run_id is None:
        return
    try:
        _request(
            "PATCH",
            f"automation_runs?id=eq.{_q(run_id)}",
            body={
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "status": status,
                "records_processed": records,
                "error_text": (str(error)[:2000] if error else None),
            },
            prefer="return=minimal",
        )
    except DatabaseError:
        pass


def latest_run_times(scripts):
    """{script: started_at ISO string} for the most recent run of each.

    One small query per script rather than one big one. Fetching N recent rows
    and reducing in Python looks cheaper but silently breaks as volume grows:
    whatconverts-lead-monitor alone writes ~290 rows a day, so a daily script
    would fall off the end of any fixed limit and read as "never ran" -- a
    false alarm that arrives precisely when the table is healthiest.

    A script with no rows at all is omitted from the result rather than given
    a null, so callers must decide explicitly what "never seen" means.
    """
    out = {}
    for name in scripts:
        rows = _request(
            "GET",
            f"automation_runs?script=eq.{_q(name)}"
            "&select=started_at&order=started_at.desc&limit=1",
        )
        if rows:
            out[name] = rows[0]["started_at"]
    return out


def succeeded_since(script, since_iso):
    """True if `script` has a run with status 'ok' at or after since_iso.

    Written for backstop schedules: a fallback trigger that fires whether or
    not the primary worked would just send a duplicate every day, which costs
    more trust than the outage it guards against. Checks status='ok'
    specifically -- the caller's own in-flight run is status='running', so it
    cannot see itself and skip.
    """
    rows = _request(
        "GET",
        f"automation_runs?script=eq.{_q(script)}&status=eq.ok"
        f"&started_at=gte.{_q(since_iso)}&select=id&limit=1",
    )
    return bool(rows)


def table_started_at():
    """Timestamp of the oldest row in automation_runs, or None if empty.

    Lets a caller tell "this script has never run" apart from "tracking has
    not been switched on long enough to have seen it run yet". Those look
    identical in the data and mean opposite things.

    Returns the instant rather than an age so the caller can measure it in
    whatever units its own threshold uses. Returning wall hours here caused a
    real false alarm: a weekday-only job's allowance is in business hours, and
    comparing 27 wall hours against a 26-business-hour threshold un-suppressed
    a script that had not had a single working day to run in.
    """
    rows = _request(
        "GET", "automation_runs?select=started_at&order=started_at.asc&limit=1")
    return datetime.fromisoformat(rows[0]["started_at"]) if rows else None


def unfinished_runs(older_than_minutes=90):
    """Runs that opened a row and never closed it.

    A crash usually fails the workflow too, so notify-failure.yml already
    covers most of these. The gap this catches is the process that dies
    without the workflow registering failure: runner eviction, cancellation,
    OOM. The age floor exists so runs still legitimately in flight are not
    reported -- the longest job here is bid-monitor at a 20-minute timeout.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)
    return _request(
        "GET",
        "automation_runs?finished_at=is.null"
        f"&started_at=lt.{_q(cutoff.isoformat())}"
        "&select=id,script,started_at,run_env&order=started_at.desc&limit=50",
    )


def run_counts_since(since_iso):
    """[{script, n}] run totals since a timestamp. Used by the heartbeat."""
    rows = _request(
        "GET",
        f"automation_runs?started_at=gte.{_q(since_iso)}"
        "&select=script,status&limit=10000",
    )
    tally = {}
    for r in rows:
        d = tally.setdefault(r["script"], {"ok": 0, "error": 0, "running": 0})
        d[r["status"]] = d.get(r["status"], 0) + 1
    return tally


class _Run:
    """Handle yielded by track(). Set .records to record a count."""
    __slots__ = ("id", "records")

    def __init__(self, run_id):
        self.id = run_id
        self.records = None


@contextmanager
def track(script):
    """Record one execution in automation_runs. Wrap a script's entry point:

        if __name__ == "__main__":
            with db.track("lead-monitor"):
                main()

    Why this exists: notify-failure.yml cannot see a startup_failure, so a run
    that never starts produces no alert at all. That is how the 2026-07-25
    GitHub outage passed unnoticed. A row here with started_at and no
    finished_at is a crash; no row at all for a scheduled script is an outage.
    Neither is visible from workflow status alone.

    SystemExit is handled separately and deliberately. Several scripts here end
    in sys.exit(main()), and SystemExit inherits from BaseException, not
    Exception. Catching only Exception would let a sys.exit(1) unwind straight
    past this and close the run as 'ok' -- a failure recorded as a success,
    which is worse than no tracking at all.

    Never raises on its own account. If Supabase is unreachable, run_start and
    run_finish swallow it and the wrapped code runs untouched: observability
    must not take down the thing it observes.
    """
    run = _Run(run_start(script))
    try:
        yield run
    except SystemExit as exc:
        code = exc.code
        # sys.exit() and sys.exit(None) both mean success; a string means
        # failure with a message, which argparse and friends do use.
        if code is None or code == 0:
            run_finish(run.id, "ok", records=run.records)
        else:
            run_finish(run.id, "error", records=run.records,
                       error=f"exited with {code!r}")
        raise
    except BaseException as exc:
        run_finish(run.id, "error", records=run.records, error=exc)
        raise
    else:
        run_finish(run.id, "ok", records=run.records)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _selftest():
    """Prove the connection, schema, and every operation. Cleans up after."""
    marker = f"_selftest_{int(time.time())}"
    print(f"Supabase self-test ({marker})")

    base, _ = _creds()
    print(f"  credentials  OK  ({base})")

    save_state(marker, {"hello": "world", "n": 1})
    got = load_state(marker)
    assert got == {"hello": "world", "n": 1}, f"state roundtrip failed: {got}"
    save_state(marker, {"hello": "world", "n": 2})
    assert load_state(marker)["n"] == 2, "state update failed"
    print("  automation_state  OK  (write, read, update)")

    keys = [f"{marker}-a", f"{marker}-b"]
    assert filter_unprocessed(marker, keys) == keys, "expected both keys unseen"
    mark_processed(marker, keys[:1], note="selftest")
    remaining = filter_unprocessed(marker, keys)
    assert remaining == keys[1:], f"expected one key left, got {remaining}"
    mark_processed(marker, keys[:1])  # duplicate must not error
    print("  processed_keys  OK  (filter, mark, duplicate-safe)")

    rid = run_start(marker)
    assert rid is not None, "run_start returned no id"
    run_finish(rid, status="ok", records=2)
    print(f"  automation_runs  OK  (id {rid})")

    _request("DELETE", f"automation_state?name=eq.{_q(marker)}", prefer="return=minimal")
    _request("DELETE", f"processed_keys?script=eq.{_q(marker)}", prefer="return=minimal")
    _request("DELETE", f"automation_runs?script=eq.{_q(marker)}", prefer="return=minimal")
    print("  cleanup  OK")
    print("\nAll checks passed. Safe to migrate scripts.")


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        try:
            _selftest()
        except AssertionError as e:
            print(f"\nFAILED: {e}")
            sys.exit(1)
        except DatabaseError as e:
            print(f"\nFAILED: {e}")
            sys.exit(1)
    else:
        print(__doc__)
        print(f"Configured: {is_configured()}")
        print("Run with --selftest to verify the connection.")
