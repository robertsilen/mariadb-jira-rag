#!/usr/bin/env python3
"""
load_mariadb.py — Load JSON issue files into MariaDB with vector embeddings.

Reads data/issues/MDEV-*.json, extracts fields, embeds via Ollama
(summary + description + comment bodies in the JSON), and inserts/updates into MariaDB.

Usage:
    python load_mariadb.py              Load all JSON files not yet in DB
    python load_mariadb.py --rebuild    Drop tables, recreate schema, reload everything
    python load_mariadb.py --status     Show what's in the database

Prerequisites:
    - MariaDB running (brew services start mariadb)
    - Ollama running with nomic-embed-text pulled (ollama pull nomic-embed-text)
    - pip install mariadb ollama
"""

import json
import sys
import time
import re
import mariadb
import ollama
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────

DATA_DIR = Path("data/issues")

DB_CONFIG = {
    "unix_socket": "/tmp/mysql.sock",
    "user": "robertsilen",
    "password": "",
    "database": "jirarag",
}

EMBED_MODEL = "nomic-embed-text"
VECTOR_DIMS = 768
# After clean_for_embedding; try longest first, then retry shorter on failure
EMBED_CHAR_LIMITS = (8000, 4000, 2000)

# ── Schema ──────────────────────────────────────────────────────────

SCHEMA_SQL = [
    "DROP TABLE IF EXISTS chunks",
    "DROP TABLE IF EXISTS issues",
    """
    CREATE TABLE issues (
        mdev_key     VARCHAR(20) PRIMARY KEY,
        summary      TEXT,
        description  MEDIUMTEXT,
        status       VARCHAR(30),
        resolution   VARCHAR(30),
        issue_type   VARCHAR(50),
        priority     VARCHAR(30),
        component    VARCHAR(100),
        fix_version  VARCHAR(512),
        created_at   DATETIME,
        updated_at   DATETIME
    )
    """,
    f"""
    CREATE TABLE chunks (
        id           INT AUTO_INCREMENT PRIMARY KEY,
        mdev_key     VARCHAR(20) NOT NULL,
        chunk_type   ENUM('issue', 'comment') NOT NULL,
        content      MEDIUMTEXT NOT NULL,
        embedding    VECTOR({VECTOR_DIMS}) NOT NULL,
        VECTOR INDEX (embedding),
        FOREIGN KEY (mdev_key) REFERENCES issues(mdev_key)
    )
    """,
]


# ── Database helpers ────────────────────────────────────────────────

def get_connection():
    return mariadb.connect(**DB_CONFIG)


def create_schema(conn):
    """Drop and recreate tables."""
    cur = conn.cursor()
    labels = (
        "DROP chunks",
        "DROP issues",
        "CREATE issues",
        "CREATE chunks + VECTOR INDEX",
    )
    for i, sql in enumerate(SCHEMA_SQL):
        label = labels[i] if i < len(labels) else f"step {i + 1}"
        print(f"  {label} …", flush=True)
        cur.execute(sql)
    conn.commit()
    print("Schema created.", flush=True)


def ensure_priority_column(conn):
    """Add priority if DB predates this column."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'issues'
          AND COLUMN_NAME = 'priority'
        """
    )
    if cur.fetchone()[0] == 0:
        cur.execute(
            "ALTER TABLE issues ADD COLUMN priority VARCHAR(30) NULL AFTER issue_type"
        )
        conn.commit()


def get_loaded_keys(conn):
    """Get set of MDEV keys already in the issues table."""
    cur = conn.cursor()
    try:
        cur.execute("SELECT mdev_key FROM issues")
        return {row[0] for row in cur.fetchall()}
    except Exception:
        return set()


# ── Text cleaning ───────────────────────────────────────────────────

def clean_for_embedding(text):
    """Remove code blocks, stack traces, and other noise from Jira markup."""
    # Remove {code}...{code} blocks
    text = re.sub(r'\{code[^}]*\}.*?\{code\}', ' ', text, flags=re.DOTALL)
    # Remove {noformat}...{noformat} blocks
    text = re.sub(r'\{noformat\}.*?\{noformat\}', ' ', text, flags=re.DOTALL)
    # Remove {quote}...{quote} blocks
    text = re.sub(r'\{quote\}.*?\{quote\}', ' ', text, flags=re.DOTALL)
    # Collapse stack traces (lines starting with "at " or whitespace+at)
    text = re.sub(r'(\n\s*at\s+\S+)+', '\n[stack trace removed]', text)
    # Remove hex dumps and long hex strings
    text = re.sub(r'[0-9a-fA-F]{20,}', '[hex removed]', text)
    # Collapse repeated whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def build_embed_text(fields):
    """Raw text for embedding: summary, description, then each comment body in order.

    Only comments present in this JSON are included (Jira may paginate `fields.comment`).
    """
    parts: list[str] = []
    s = fields.get("summary") or ""
    if s.strip():
        parts.append(s)
    d = fields.get("description") or ""
    if d.strip():
        parts.append(d)
    comment_block = fields.get("comment") or {}
    for c in comment_block.get("comments") or []:
        body = c.get("body") or ""
        if body.strip():
            parts.append(body)
    return "\n\n".join(parts).strip()


# ── Embedding ───────────────────────────────────────────────────────

def embed_document(text):
    """Clean and embed via Ollama; try EMBED_CHAR_LIMITS in order until one succeeds.

    Returns:
        (embedding_vector, limit_used, did_fallback)
        did_fallback is True if a shorter cut than the first limit succeeded after a failure.
    """
    text = clean_for_embedding(text)
    last_exc: Exception | None = None
    for i, limit in enumerate(EMBED_CHAR_LIMITS):
        snippet = text[:limit]
        try:
            response = ollama.embed(
                model=EMBED_MODEL,
                input=f"search_document: {snippet}",
            )
            return response["embeddings"][0], limit, (i > 0)
        except Exception as e:
            last_exc = e
    assert last_exc is not None
    raise last_exc


# ── JSON parsing ────────────────────────────────────────────────────

def parse_issue(filepath):
    """Parse a MDEV JSON file into a structured dict."""
    data = json.loads(filepath.read_text())
    fields = data.get("fields", {})

    # Extract first component name
    components = fields.get("components", [])
    component = components[0]["name"] if components else None

    # All fix versions (Jira allows multiple, e.g. backports)
    fix_versions = fields.get("fixVersions", [])
    fix_version = (
        ", ".join(v["name"] for v in fix_versions) if fix_versions else None
    )

    # Status and resolution
    status = fields.get("status", {}).get("name")
    resolution = fields.get("resolution")
    if isinstance(resolution, dict):
        resolution = resolution.get("name")

    issuetype = fields.get("issuetype")
    if isinstance(issuetype, dict):
        issue_type = issuetype.get("name")
    else:
        issue_type = None

    prio = fields.get("priority")
    if isinstance(prio, dict):
        priority = prio.get("name")
    else:
        priority = None

    # Parse dates (Jira format: 2024-01-15T10:30:00.000+0000)
    created = fields.get("created", "")[:19].replace("T", " ") or None
    updated = fields.get("updated", "")[:19].replace("T", " ") or None

    summary = fields.get("summary", "") or ""
    description = fields.get("description", "") or ""
    embed_text = build_embed_text(fields)

    return {
        "key": data["key"],
        "summary": summary,
        "description": description,
        "status": status,
        "resolution": resolution,
        "issue_type": issue_type,
        "priority": priority,
        "component": component,
        "fix_version": fix_version,
        "created_at": created,
        "updated_at": updated,
        "embed_text": embed_text,
    }


# ── Loading ─────────────────────────────────────────────────────────

def load_issue(conn, parsed, embedding):
    """Insert or replace a single issue and its embedding into MariaDB."""
    cur = conn.cursor()

    # Upsert issue
    cur.execute(
        """
        REPLACE INTO issues
            (mdev_key, summary, description, status, resolution, issue_type,
             priority, component, fix_version, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            parsed["key"],
            parsed["summary"],
            parsed["description"],
            parsed["status"],
            parsed["resolution"],
            parsed["issue_type"],
            parsed["priority"],
            parsed["component"],
            parsed["fix_version"],
            parsed["created_at"],
            parsed["updated_at"],
        ),
    )

    # Delete old chunks for this issue (in case of re-load)
    cur.execute("DELETE FROM chunks WHERE mdev_key = ?", (parsed["key"],))

    # Insert issue chunk with embedding
    vec_str = json.dumps(embedding)
    cur.execute(
        """
        INSERT INTO chunks (mdev_key, chunk_type, content, embedding)
        VALUES (?, 'issue', ?, VEC_FromText(?))
        """,
        (parsed["key"], parsed["embed_text"], vec_str),
    )

    conn.commit()


def load_all(conn, rebuild=False):
    """Load all JSON files into MariaDB."""
    if not DATA_DIR.exists():
        print(f"No data directory found at {DATA_DIR}/")
        print("Run fetch_jira.py first.")
        return

    json_files = sorted(DATA_DIR.glob("MDEV-*.json"))
    if not json_files:
        print("No MDEV JSON files found.")
        return

    if rebuild:
        print("Rebuilding schema...")
        create_schema(conn)
        skip_keys = set()
    else:
        ensure_priority_column(conn)
        skip_keys = get_loaded_keys(conn)
        if skip_keys:
            print(f"Already in DB: {len(skip_keys)} issues. Loading new ones only.")

    # Filter to only files not yet loaded
    to_load = []
    for f in json_files:
        key = f.stem
        if key not in skip_keys:
            to_load.append(f)

    if not to_load:
        print("Everything is already loaded. Use --rebuild to force reload.")
        return

    print(
        f"Found {len(to_load)} JSON file(s) on disk that are not in the database "
        f"(embedding + inserting where possible)...",
        flush=True,
    )
    print(
        "  (First call to Ollama per issue can take several seconds; Ctrl+C may wait "
        "until the current embed finishes.)",
        flush=True,
    )
    start_time = time.time()
    loaded = 0
    errors = 0
    skipped_short = []
    embed_fallback_count = 0
    embed_fallback_at_4000 = 0
    embed_fallback_at_2000 = 0
    first_embed_announced = False

    for filepath in to_load:
        try:
            parsed = parse_issue(filepath)

            # Skip issues with no meaningful text to embed
            if len(parsed["embed_text"].strip()) < 10:
                skipped_short.append(parsed["key"])
                continue

            if not first_embed_announced:
                print(
                    f"  Embedding via Ollama: {parsed['key']} "
                    f"(Ctrl+C may wait until this returns)…",
                    flush=True,
                )
                first_embed_announced = True
            embedding, limit_used, did_fallback = embed_document(parsed["embed_text"])
            if did_fallback:
                embed_fallback_count += 1
                if limit_used == 4000:
                    embed_fallback_at_4000 += 1
                elif limit_used == 2000:
                    embed_fallback_at_2000 += 1
            load_issue(conn, parsed, embedding)
            loaded += 1

            if loaded % 50 == 0:
                elapsed = time.time() - start_time
                rate = loaded / elapsed
                remaining = (len(to_load) - loaded) / rate if rate > 0 else 0
                print(
                    f"  {loaded}/{len(to_load)} loaded "
                    f"({rate:.1f}/sec, ~{remaining:.0f}s remaining) "
                    f"— latest: {parsed['key']}"
                )

        except Exception as e:
            errors += 1
            print(f"  Error loading {filepath.stem}: {e}")
            if errors > 20:
                print("Too many errors, stopping.")
                break

    elapsed = time.time() - start_time
    rate = loaded / elapsed if elapsed > 0 else 0
    print(f"\nDone. {loaded} issue(s) loaded in {elapsed:.0f}s ({rate:.1f}/sec).")
    if skipped_short:
        print(
            f"  Skipped {len(skipped_short)} issue(s): summary+description under 10 "
            f"characters (no embedding). Keys: {', '.join(skipped_short[:15])}"
            + (" …" if len(skipped_short) > 15 else "")
        )
    if errors:
        print(f"  {errors} error(s) encountered.")
    if embed_fallback_count:
        print(
            f"  Embedding fallback (8000→4000→2000 after failure): "
            f"{embed_fallback_count} issue(s) — "
            f"succeeded at 4000 chars: {embed_fallback_at_4000}, "
            f"at 2000 chars: {embed_fallback_at_2000}.",
            flush=True,
        )


# ── Status ──────────────────────────────────────────────────────────

def show_status(conn):
    """Show what's in the database."""
    cur = conn.cursor()

    try:
        cur.execute("SELECT COUNT(*) FROM issues")
        issue_count = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM chunks")
        chunk_count = cur.fetchone()[0]

        cur.execute("SELECT MIN(mdev_key), MAX(mdev_key) FROM issues")
        min_key, max_key = cur.fetchone()

        cur.execute(
            "SELECT status, COUNT(*) FROM issues GROUP BY status ORDER BY COUNT(*) DESC LIMIT 5"
        )
        statuses = cur.fetchall()

        print(f"Issues in DB:  {issue_count}")
        print(f"Chunks in DB:  {chunk_count}")
        print(f"Key range:     {min_key} to {max_key}")
        print(f"\nTop statuses:")
        for status, count in statuses:
            print(f"  {status:20s} {count}")

    except mariadb.ProgrammingError:
        print("Tables don't exist yet. Run the loader first.")

    # Compare with files on disk
    json_count = len(list(DATA_DIR.glob("MDEV-*.json"))) if DATA_DIR.exists() else 0
    print(f"\nJSON files on disk: {json_count}")
    if json_count > issue_count:
        print(f"  → {json_count - issue_count} files not yet loaded")


# ── Main ────────────────────────────────────────────────────────────

def main():
    rebuild = "--rebuild" in sys.argv
    status = "--status" in sys.argv

    conn = get_connection()

    if status:
        show_status(conn)
    else:
        # Ensure schema exists if not rebuilding
        if not rebuild:
            cur = conn.cursor()
            try:
                cur.execute("SELECT 1 FROM issues LIMIT 1")
            except mariadb.ProgrammingError:
                print("Tables don't exist. Creating schema...")
                create_schema(conn)

        load_all(conn, rebuild=rebuild)

    conn.close()


if __name__ == "__main__":
    main()