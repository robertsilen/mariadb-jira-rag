# MariaDB Jira RAG

In a locally run web UI, do semantic, vector search over Jira over 36 000 MDEV issues issues fetched from [jira.mariadb.org](https://jira.mariadb.org). 

**What happens**
1. Python script fetches all MDEV issues from [jira.mariadb.org](https://jira.mariadb.org) and stores them locally as indivudal JSON files. 
2. Pyhton script loads issues into a **MariaDB** database as vector and metadata. Summary+description+comments (up to 8000 chars) are vectorized to embeddings locally with **Ollama** (`nomic-embed-text`). 36 000 issues take about half a minute with MacBook M4 processor.
3. In a Web UI (StreamLit) user can search with a word or phrase for semanticaly similar issues. The vector search can also be combined SQL options (issue type, components, etc). Search results can be downloaded as JSON file with prompt, so it can be uploaded to AI chat till claude.ai for dialog and anlysis. Alternatively the chat feature could be implemented in the actual Streamlit web UI. 

---

## Screenshot of Web UI with search results.

![MariaDB Jira RAG — Streamlit search UI](screenshot.png)

---

## How to run on MacBook

```bash
# If you use [Homebrew](https://brew.sh), install MariaDB and Ollama (skip any you already have):

brew install mariadb ollama

# Start MariaDB and create the database (adjust user/password to match your setup):

brew services start mariadb
mariadb -u root -e "CREATE DATABASE IF NOT EXISTS jirarag;"

# Pull the Ollama embedding model and install Python dependencies:

ollama pull nomic-embed-text
pip install streamlit mariadb ollama requests
```

Then run these three steps (for more parameters se below)

```bash
# 1. **Fetch Jira issues**

python fetch_jira.py newest 40000

# 2. **Load into MariaDB**

python load_mariadb.py

# 3. **Run RAG-webUI, usually at http://localhost:8501**

streamlit run search_app.py
```

---

## Fetching Jira JSON

Writes one file per issue under `data/issues/`. Uses `data/fetch_state.json` to remember fetch history.

| Command | Description |
|--------|-------------|
| `python fetch_jira.py newest [N]` | Fetch the **N** most recently updated issues (default **100**). Good for first import or catching up on recent changes. |
| `python fetch_jira.py backfill [N]` | Fetch **N** older issues starting from below the oldest key already on disk (default **500**). Repeat to grow history. |
| `python fetch_jira.py status` | Show counts, key range, gaps, and last fetch metadata. |

Examples:

```bash
python fetch_jira.py newest 1000    # initial grab of latest 1000
python fetch_jira.py backfill 2000  # add 2000 older issues
python fetch_jira.py newest 200     # periodic sync of recent updates
python fetch_jira.py status
```

`newest` uses JQL ordered by **`updated DESC`**, so it refreshes the most recently touched tickets first (good for picking up new API fields after you change `FIELDS`).

---

## Loading MariaDB

### What gets embedded (for the vector)

The model sees **one string per issue**, built in this order from the JSON on disk:

1. **Summary** (title)  
2. **Description**  
3. **Comment bodies** — each `fields.comment.comments[].body`, in order (only comments **included in that file**; Jira may paginate)

Then **`clean_for_embedding()`** in `load_mariadb.py` normalizes that combined text:

- Removes Jira **`{code}…{code}`**, **`{noformat}…{noformat}`**, and **`{quote}…{quote}`** blocks (so large code / log dumps are dropped for embedding)  
- Collapses Java-style **stack trace** lines  
- Replaces long **hex** runs with a placeholder  
- Collapses **whitespace** to single spaces  

The embedding call uses **the first 8000 characters** of that **cleaned** string (so anything after 8k is not in the vector). If Ollama fails on that length, the loader retries with **4000**, then **2000** characters; the run summary says how many issues needed a shorter fallback.

The **`chunks.content`** column still stores the **raw** combined text (before clean/truncate) for reference; only the **vector** is derived from cleaned + truncated text.

Otherwise: the loader reads `data/issues/MDEV-*.json`, embeds as above, and upserts rows into `jirarag`.

| Command | Description |
|--------|-------------|
| `python load_mariadb.py` | Load any JSON files **not yet** present in the database (skips existing keys). Creates tables if missing. |
| `python load_mariadb.py --rebuild` | **Drop** `issues` / `chunks`, recreate schema, and **reload all** JSON files from disk. |
| `python load_mariadb.py --status` | Print issue/chunk counts, key range, top statuses, and how many JSON files are still unloaded. |

Examples:

```bash
python load_mariadb.py --status
python load_mariadb.py
python load_mariadb.py --rebuild
```

**Comments in Jira JSON** may be paginated (`fields.comment.total` vs length of `comments`); only what the API returned in that file is included in the combined text above.

---

## Run the Web UI

From the project directory:

```bash
streamlit run search_app.py
```

Open the URL Streamlit prints (usually `http://localhost:8501`). Enter a query, set filters, click **Search**. After results, use **Download search results with prompt for RAG in e.g. Claude Chat** to save JSON (includes `task_for_claude`, `user_query`, `filters_at_export`, and `search_hits` with full per-issue JSON from `data/issues/`). Run Streamlit from the repo root so those paths resolve.

---

## Requirements

- **Python 3** with the packages above.
- **MariaDB** with vector support compatible with this schema (see `load_mariadb.py` for `VECTOR` columns and indexes).
- **Ollama** with the **`nomic-embed-text`** model for both loading and search (same model must be used end-to-end).
