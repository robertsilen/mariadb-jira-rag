#!/usr/bin/env python3
"""
search_app.py — Streamlit search UI for MariaDB Jira RAG.

Usage:
    streamlit run search_app.py

Prerequisites:
    - MariaDB running with loaded data (python load_mariadb.py)
    - Ollama running with nomic-embed-text
    - pip install streamlit mariadb ollama
"""

import json
import mariadb
import ollama
import re
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

from load_mariadb import ensure_priority_column

# ── Config ──────────────────────────────────────────────────────────

DB_CONFIG = {
    "unix_socket": "/tmp/mysql.sock",
    "user": "robertsilen",
    "password": "",
    "database": "jirarag",
}

EMBED_MODEL = "nomic-embed-text"
JIRA_BASE = "https://jira.mariadb.org/browse"
DATA_DIR = Path("data/issues")

TASK_FOR_CLAUDE = """This file was exported from a local MariaDB Jira RAG app (semantic search over MDEV issues). It contains the user's search text and the full Jira-style JSON for each issue returned as a search hit. Comment lists in each issue may be paginated (only the first page is in the file).

Your job:
1. Read the user's input and restate what they are trying to find or fix.
2. Compare that intent to each exported issue (summary, description, comments, metadata).
3. Say whether any issue looks like an exact or near-duplicate match, versus only loosely related.
4. For similar but not identical issues, explain what is the same (symptoms, component, root cause hints) and what differs.
5. If the user were about to file a new Jira issue, recommend which existing MDEV(s) to read or link first, and whether a new ticket is still justified.
6. Be explicit when the export is incomplete (e.g. limited number of hits or truncated comments in the source JSON)."""

# ── Helpers ─────────────────────────────────────────────────────────

@st.cache_resource
def get_connection():
    conn = mariadb.connect(**DB_CONFIG)
    ensure_priority_column(conn)
    return conn


def embed_query(text):
    """Embed a user query for search."""
    response = ollama.embed(
        model=EMBED_MODEL,
        input=f"search_query: {text}",
    )
    return response["embeddings"][0]


def _sql_for_display(sql: str, params: list) -> str:
    """Interleave SQL and params for UI; first param is embedding JSON (omitted)."""
    parts = sql.split("?")
    chunks: list[str] = []
    for i, part in enumerate(parts):
        chunks.append(part)
        if i < len(params):
            val = params[i]
            if i == 0:
                chunks.append("/* embedding vector omitted */")
            elif isinstance(val, (int, float)) and not isinstance(val, bool):
                chunks.append(str(val))
            else:
                chunks.append("'" + str(val).replace("'", "''") + "'")
    return "".join(chunks).strip()


def _sql_expander(display_sql: str) -> None:
    with st.expander("SQL query", expanded=False):
        st.code(display_sql, language="sql")


def search(
    query_text,
    status_filter="All",
    resolution_filter="All",
    component_filter="All",
    issue_type_filter="All",
    priority_filter="All",
    limit=10,
):
    """Embed query and run hybrid vector search against MariaDB."""
    embedding = embed_query(query_text)
    vec_str = json.dumps(embedding)

    conn = get_connection()
    cur = conn.cursor()

    conditions = ["c.chunk_type = 'issue'"]
    params: list = [vec_str]

    if status_filter != "All":
        conditions.append("i.status = ?")
        params.append(status_filter)
    if resolution_filter != "All":
        if resolution_filter == "(none)":
            conditions.append("i.resolution IS NULL")
        else:
            conditions.append("i.resolution = ?")
            params.append(resolution_filter)
    if component_filter != "All":
        if component_filter == "(none)":
            conditions.append("i.component IS NULL")
        else:
            conditions.append("i.component = ?")
            params.append(component_filter)
    if issue_type_filter != "All":
        if issue_type_filter == "(none)":
            conditions.append("i.issue_type IS NULL")
        else:
            conditions.append("i.issue_type = ?")
            params.append(issue_type_filter)
    if priority_filter != "All":
        if priority_filter == "(none)":
            conditions.append("i.priority IS NULL")
        else:
            conditions.append("i.priority = ?")
            params.append(priority_filter)

    where_sql = " AND ".join(conditions)
    sql = f"""
            SELECT i.mdev_key, i.summary, i.status, i.resolution,
                   i.issue_type, i.priority, i.component, i.fix_version,
                   VEC_DISTANCE(c.embedding, VEC_FromText(?)) AS distance
            FROM chunks c
            JOIN issues i ON c.mdev_key = i.mdev_key
            WHERE {where_sql}
            ORDER BY distance ASC
            LIMIT ?
            """
    params.append(limit)
    display_sql = _sql_for_display(sql, params)
    cur.execute(sql, params)

    results = cur.fetchall()
    return results, display_sql


def _distinct_options(column: str) -> list[str]:
    """Distinct values for a nullable column; '(none)' if NULLs exist."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        f"SELECT DISTINCT {column} FROM issues ORDER BY {column} IS NULL, {column}"
    )
    raw = [row[0] for row in cur.fetchall()]
    opts: list[str] = ["All"]
    if any(v is None for v in raw):
        opts.append("(none)")
    opts.extend(v for v in raw if v is not None)
    return opts


def get_status_options():
    """Get distinct status values from the database."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT status FROM issues ORDER BY status")
    statuses = [row[0] for row in cur.fetchall()]
    return ["All"] + statuses


def get_resolution_options():
    return _distinct_options("resolution")


def get_component_options():
    return _distinct_options("component")


def get_issue_type_options():
    return _distinct_options("issue_type")


def get_priority_options():
    return _distinct_options("priority")


def get_issue_count():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM issues")
    return cur.fetchone()[0]


def build_rag_export_payload(meta: dict) -> dict:
    """Assemble JSON-serializable dict for Claude / external RAG chat."""
    search_hits: list[dict] = []
    for h in meta["hits"]:
        key = h["mdev_key"]
        path = DATA_DIR / f"{key}.json"
        entry: dict = {
            "mdev_key": key,
            "vector_distance": h["vector_distance"],
        }
        if path.is_file():
            try:
                entry["issue_json"] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                entry["issue_json"] = None
                entry["error"] = f"could not read JSON: {e}"
        else:
            entry["issue_json"] = None
            entry["error"] = "file not found under data/issues/"
        search_hits.append(entry)

    return {
        "task_for_claude": TASK_FOR_CLAUDE,
        "disclaimer": "Jira content is user-exported from a private/local index; verify on jira.mariadb.org before acting.",
        "user_query": meta["query"],
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "filters_at_export": meta.get("filters"),
        "search_hits": search_hits,
    }


# ── UI ──────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="MariaDB Jira RAG",
    page_icon="🔍",
    layout="wide",
)

st.markdown(
    """
    <style>
    .stApp .block-container {
        padding-top: 3.25rem;
        padding-left: 1.25rem;
        padding-right: 1.25rem;
        padding-bottom: 1rem;
        max-width: 100%;
    }
    .stApp .block-container h1 {
        margin-top: 0;
        padding-top: 0;
        margin-bottom: 0.15rem;
        font-size: 1.55rem;
        line-height: 1.2;
    }
    [data-testid="stCaptionContainer"] {
        margin-bottom: 0.35rem !important;
    }
    [data-testid="stCaptionContainer"] p {
        font-size: 0.82rem !important;
        line-height: 1.35 !important;
        margin: 0 !important;
    }
    /* Tighter markdown (results line, etc.) */
    .main .block-container .stMarkdown p {
        margin-top: 0.1rem !important;
        margin-bottom: 0.35rem !important;
        font-size: 0.92rem !important;
    }
    .stTextArea label,
    .stSelectbox label,
    .stSlider label {
        font-size: 0.78rem !important;
        min-height: auto !important;
    }
    .stTextArea textarea {
        padding-top: 0.4rem !important;
        padding-bottom: 0.4rem !important;
        font-size: 0.9rem !important;
    }
    div[data-testid="column"] {
        padding-left: 0.35rem !important;
        padding-right: 0.35rem !important;
    }
    .stButton > button {
        padding-top: 0.35rem !important;
        padding-bottom: 0.35rem !important;
        margin-top: 0.15rem !important;
    }
    [data-testid="stDataFrame"] {
        font-size: 0.85rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("🔍 MariaDB Jira RAG")
st.caption(
    f"Semantic search over {get_issue_count()} MDEV issues · "
    f"Powered by MariaDB VECTOR + Ollama"
)

query = st.text_area(
    "Describe what you're looking for",
    placeholder="e.g. I want better error messages when I mistype a column name in GROUP BY",
    height=58,
)

f_status, f_resolution, f_type, f_priority, f_component, f_limit, f_search = st.columns(
    [1, 1, 1, 1, 1, 1.15, 0.95]
)
with f_status:
    status_filter = st.selectbox("Status", get_status_options())
with f_resolution:
    resolution_filter = st.selectbox("Resolution", get_resolution_options())
with f_type:
    issue_type_filter = st.selectbox("Type", get_issue_type_options())
with f_priority:
    priority_filter = st.selectbox("Priority", get_priority_options())
with f_component:
    component_filter = st.selectbox("Component", get_component_options())
with f_limit:
    limit = st.slider("Results", min_value=5, max_value=25, value=10)
with f_search:
    st.markdown(
        '<div style="height:1.35rem" aria-hidden="true"></div>',
        unsafe_allow_html=True,
    )
    search_clicked = st.button("Search", type="primary", use_container_width=True)

# Results
if search_clicked and query.strip():
    with st.spinner("Embedding query and searching..."):
        results, display_sql = search(
            query,
            status_filter,
            resolution_filter,
            component_filter,
            issue_type_filter,
            priority_filter,
            limit,
        )

    if not results:
        _sql_expander(display_sql)
        st.warning(
            "No results found. Try broadening your search or relaxing the filters."
        )
    else:
        st.markdown(f"**{len(results)} results** — closest matches first (lowest distance)")

        import pandas as pd

        rows = []
        for (
            mdev_key,
            summary,
            status,
            resolution,
            issue_type,
            priority,
            component,
            fix_version,
            distance,
        ) in results:
            url = f"{JIRA_BASE}/{mdev_key}"
            rows.append({
                "MDEV": url,
                "Summary": summary,
                "Status": status or "",
                "Resolution": resolution or "",
                "Type": issue_type or "",
                "Priority": priority or "",
                "Component": component or "",
                "Fix Versions": fix_version or "",
                "Distance": round(distance, 4),
            })

        df = pd.DataFrame(rows)

        _sql_expander(display_sql)

        st.dataframe(
            df,
            column_config={
                "MDEV": st.column_config.LinkColumn(
                    "MDEV",
                    display_text=r".*/browse/(MDEV-\d+)",
                ),
                "Distance": st.column_config.NumberColumn(format="%.4f"),
            },
            hide_index=True,
            use_container_width=True,
        )

        st.session_state["rag_export_meta"] = {
            "query": query.strip(),
            "hits": [
                {"mdev_key": row[0], "vector_distance": float(row[8])}
                for row in results
            ],
            "filters": {
                "status": status_filter,
                "resolution": resolution_filter,
                "issue_type": issue_type_filter,
                "priority": priority_filter,
                "component": component_filter,
                "result_limit": limit,
            },
        }

        export_payload = build_rag_export_payload(st.session_state["rag_export_meta"])
        export_bytes = json.dumps(
            export_payload, indent=2, ensure_ascii=False
        ).encode("utf-8")
        fname = (
            f"jira_rag_export_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
        )
        st.download_button(
            label="Download search results with prompt for RAG in e.g. Claude Chat",
            data=export_bytes,
            file_name=fname,
            mime="application/json",
            help="JSON with task_for_claude, your query, filters, and full issue JSON per hit.",
            use_container_width=False,
        )

elif search_clicked:
    st.warning("Enter a search query.")
