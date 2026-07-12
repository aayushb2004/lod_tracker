import csv
import io
import os
import time
from datetime import date

import psycopg2
import psycopg2.extras
from flask import Flask, Response, g, jsonify, redirect, render_template, request, url_for
from markupsafe import Markup, escape

DATABASE_URL = os.environ.get("DATABASE_URL")
DEFAULT_TAGS = [
    "Aware of Meesho Mall",
    "Recall of Branded Offer page",
    "Exploratory user",
    "High intent user",
]

app = Flask(__name__)


# Idle connections kept alive between requests. A fresh TLS handshake to Neon
# costs over a second, so reuse dominates page latency. Entries are
# (connection, last_used_ts); connections idle less than _FRESH_SECS skip the
# liveness probe, saving a round trip per request.
_conn_pool = []
_POOL_MAX = 5
_FRESH_SECS = 60


def _new_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


class _DBProxy:
    """Stand-in for the request's connection that always resolves to the live
    one, so a mid-request reconnect isn't defeated by stale local references."""

    def cursor(self, *args, **kwargs):
        return g._db_conn.cursor(*args, **kwargs)

    def commit(self):
        return g._db_conn.commit()

    def rollback(self):
        return g._db_conn.rollback()


_db_proxy = _DBProxy()


def get_db():
    if "_db_conn" not in g:
        conn = None
        while _conn_pool:
            candidate, last_used = _conn_pool.pop()
            if time.monotonic() - last_used < _FRESH_SECS:
                conn = candidate
                break
            try:
                cur = candidate.cursor()
                cur.execute("SELECT 1")
                cur.fetchone()
                candidate.rollback()
                conn = candidate
                break
            except Exception:
                try:
                    candidate.close()
                except Exception:
                    pass
        if conn is None:
            conn = _new_conn()
        g._db_conn = conn
        g._db_dirty = False
    return _db_proxy


def db_execute(db, sql, params=()):
    get_db()
    try:
        cur = g._db_conn.cursor()
        cur.execute(sql, params)
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        # Server dropped the pooled connection. Safe to retry only if this
        # request hasn't executed anything yet (nothing to lose mid-transaction).
        if g.get("_db_dirty"):
            raise
        try:
            g._db_conn.close()
        except Exception:
            pass
        g._db_conn = _new_conn()
        cur = g._db_conn.cursor()
        cur.execute(sql, params)
    g._db_dirty = True
    return cur


@app.teardown_appcontext
def close_db(exception=None):
    conn = g.pop("_db_conn", None)
    if conn is not None:
        try:
            conn.rollback()
            if len(_conn_pool) < _POOL_MAX:
                _conn_pool.append((conn, time.monotonic()))
            else:
                conn.close()
        except Exception:
            try:
                conn.close()
            except Exception:
                pass


def init_db():
    db = psycopg2.connect(DATABASE_URL)
    cur = db.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS entries (
            id SERIAL PRIMARY KEY,
            date TEXT NOT NULL,
            title TEXT NOT NULL,
            user_id TEXT NOT NULL,
            summary TEXT NOT NULL,
            poc TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP NOT NULL DEFAULT now()
        );

        CREATE TABLE IF NOT EXISTS tags (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS entry_tags (
            entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
            tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            PRIMARY KEY (entry_id, tag_id)
        );

        ALTER TABLE entries ADD COLUMN IF NOT EXISTS poc TEXT NOT NULL DEFAULT '';
        """
    )
    for t in DEFAULT_TAGS:
        cur.execute("INSERT INTO tags (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (t,))
    db.commit()
    db.close()


def get_all_tags(db):
    return [r["name"] for r in db_execute(db, "SELECT name FROM tags ORDER BY name").fetchall()]


def get_or_create_tag(db, name):
    name = name.strip()
    if not name:
        return None
    row = db_execute(db, "SELECT id FROM tags WHERE name = %s", (name,)).fetchone()
    if row:
        return row["id"]
    cur = db_execute(db, "INSERT INTO tags (name) VALUES (%s) RETURNING id", (name,))
    return cur.fetchone()["id"]


def get_distinct_titles(db):
    return [r["title"] for r in db_execute(db, "SELECT DISTINCT title FROM entries ORDER BY title").fetchall()]


def get_distinct_pocs(db):
    return [
        r["poc"]
        for r in db_execute(
            db, "SELECT DISTINCT poc FROM entries WHERE poc != '' ORDER BY poc"
        ).fetchall()
    ]


# Dropdown lists change only on writes; caching them briefly saves a DB round
# trip (~0.5s to Neon) on almost every page load.
_meta_cache = {"data": None, "ts": 0.0}
_META_TTL_SECS = 30


def invalidate_meta():
    _meta_cache["data"] = None


def get_meta(db):
    """Tags, titles, and POCs for dropdowns in one round trip, briefly cached."""
    if _meta_cache["data"] is not None and time.monotonic() - _meta_cache["ts"] < _META_TTL_SECS:
        return _meta_cache["data"]
    row = db_execute(
        db,
        """
        SELECT
            (SELECT COALESCE(array_agg(name ORDER BY name), '{}') FROM tags) AS tags,
            (SELECT COALESCE(array_agg(DISTINCT title ORDER BY title), '{}') FROM entries) AS titles,
            (SELECT COALESCE(array_agg(DISTINCT poc ORDER BY poc), '{}')
             FROM entries WHERE poc != '') AS pocs
        """,
    ).fetchone()
    data = (row["tags"], row["titles"], row["pocs"])
    _meta_cache["data"] = data
    _meta_cache["ts"] = time.monotonic()
    return data


def dup_key(user_id, summary):
    """Whitespace- and case-insensitive key so trivial formatting differences
    (CRLF vs LF newlines, double spaces, casing) still count as duplicates."""
    return (
        " ".join(user_id.split()).lower(),
        " ".join(summary.split()).lower(),
    )


def format_summary(text):
    """Lines starting with '- ' become bullet points; blank lines separate paragraphs."""
    if not text:
        return Markup("")

    html_parts = []
    bullet_buffer = []
    para_buffer = []

    def flush_bullets():
        if bullet_buffer:
            items = "".join(f"<li>{escape(b)}</li>" for b in bullet_buffer)
            html_parts.append(f"<ul>{items}</ul>")
            bullet_buffer.clear()

    def flush_para():
        if para_buffer:
            html_parts.append(f"<p>{'<br>'.join(escape(p) for p in para_buffer)}</p>")
            para_buffer.clear()

    for line in text.replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if stripped.startswith("- "):
            flush_para()
            bullet_buffer.append(stripped[2:].strip())
        elif stripped == "":
            flush_bullets()
            flush_para()
        else:
            flush_bullets()
            para_buffer.append(stripped)
    flush_bullets()
    flush_para()
    return Markup("".join(html_parts))


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/")
def index():
    return redirect(url_for("dashboard"))


@app.route("/add", methods=["GET", "POST"])
def add():
    db = get_db()
    if request.method == "POST":
        entry_date = request.form.get("date") or date.today().isoformat()
        title = request.form.get("title", "").strip()
        batch_poc = request.form.get("poc", "").strip()
        if not title:
            tags, titles, pocs = get_meta(db)
            return render_template(
                "add.html",
                tags=tags,
                titles=titles,
                pocs=pocs,
                today=date.today().isoformat(),
                error="Title is required.",
            )

        indices = set()
        for key in request.form:
            if key.startswith("user_id_"):
                indices.add(key.split("_", 2)[-1])

        rows = []
        for idx in sorted(indices):
            user_id = request.form.get(f"user_id_{idx}", "").strip()
            summary = request.form.get(f"summary_{idx}", "").strip().replace("\r\n", "\n")
            if not user_id and not summary:
                continue

            row_poc = request.form.get(f"poc_{idx}")
            poc = row_poc.strip() if row_poc is not None else batch_poc

            checked_tags = request.form.getlist(f"tags_{idx}")
            new_tags_raw = request.form.get(f"newtags_{idx}", "")
            tag_names = {t.strip() for t in checked_tags if t.strip()}
            tag_names |= {t.strip() for t in new_tags_raw.split(",") if t.strip()}
            rows.append({"user_id": user_id, "summary": summary, "poc": poc, "tags": tag_names})

        # Batched inserts: constant number of round trips to the DB no matter
        # how many rows are saved (matters a lot on a remote Postgres).
        saved = len(rows)
        if rows:
            # Heals a server-dropped pooled connection before the batch writes,
            # which bypass db_execute's reconnect logic.
            db_execute(db, "SELECT 1")
            all_tags = set().union(*(r["tags"] for r in rows))
            tag_ids = {}
            if all_tags:
                psycopg2.extras.execute_values(
                    db.cursor(),
                    "INSERT INTO tags (name) VALUES %s ON CONFLICT (name) DO NOTHING",
                    [(t,) for t in all_tags],
                )
                tag_ids = {
                    r["name"]: r["id"]
                    for r in db_execute(
                        db, "SELECT id, name FROM tags WHERE name = ANY(%s)", (list(all_tags),)
                    ).fetchall()
                }

            inserted = psycopg2.extras.execute_values(
                db.cursor(),
                "INSERT INTO entries (date, title, user_id, summary, poc) VALUES %s RETURNING id",
                [(entry_date, title, r["user_id"], r["summary"], r["poc"]) for r in rows],
                fetch=True,
            )
            links = [
                (rec["id"], tag_ids[t])
                for row, rec in zip(rows, inserted)
                for t in row["tags"]
                if t in tag_ids
            ]
            if links:
                psycopg2.extras.execute_values(
                    db.cursor(),
                    "INSERT INTO entry_tags (entry_id, tag_id) VALUES %s ON CONFLICT DO NOTHING",
                    links,
                )

        db.commit()
        invalidate_meta()
        return redirect(url_for("dashboard", flash=f"Saved {saved} entr{'y' if saved == 1 else 'ies'}"))

    tags, titles, pocs = get_meta(db)
    return render_template(
        "add.html",
        tags=tags,
        titles=titles,
        pocs=pocs,
        today=date.today().isoformat(),
        error=None,
    )


def parse_tag_groups(args):
    """Each tags_g<N> checkbox group is OR'd internally; groups are AND'd together."""
    groups = {}
    for key in args:
        if key.startswith("tags_g"):
            suffix = key[len("tags_g"):]
            if not suffix.isdigit():
                continue
            values = args.getlist(key)
            if values:
                groups[int(suffix)] = values
    return [groups[k] for k in sorted(groups.keys())]


def build_filtered_query(args, select_cols="e.*", include_tags=True, order=True):
    where = []
    params = []

    date_from = args.get("date_from")
    date_to = args.get("date_to")
    title = args.get("title")
    poc = args.get("poc")
    search = args.get("search", "").strip()

    if date_from:
        where.append("e.date >= %s")
        params.append(date_from)
    if date_to:
        where.append("e.date <= %s")
        params.append(date_to)
    if title:
        where.append("e.title = %s")
        params.append(title)
    if poc:
        where.append("e.poc = %s")
        params.append(poc)
    if search:
        where.append("(e.summary ILIKE %s OR e.user_id ILIKE %s)")
        params.append(f"%{search}%")
        params.append(f"%{search}%")

    if include_tags:
        for group_tags in parse_tag_groups(args):
            placeholders = ",".join("%s" for _ in group_tags)
            where.append(
                f"""e.id IN (
                    SELECT et.entry_id FROM entry_tags et
                    JOIN tags t ON t.id = et.tag_id
                    WHERE t.name IN ({placeholders})
                )"""
            )
            params.extend(group_tags)

    query = f"SELECT {select_cols} FROM entries e"
    if where:
        query += " WHERE " + " AND ".join(where)
    if order:
        query += " ORDER BY e.date DESC, e.id DESC"
    return query, params


def fetch_entries_with_tags(db, args):
    select_cols = """e.*,
        COALESCE(
            (SELECT array_agg(t.name ORDER BY t.name)
             FROM entry_tags et JOIN tags t ON t.id = et.tag_id
             WHERE et.entry_id = e.id),
            '{}'
        ) AS tag_names"""
    query, params = build_filtered_query(args, select_cols=select_cols)
    rows = db_execute(db, query, params).fetchall()
    entries = []
    for r in rows:
        row = dict(r)
        tags = row.pop("tag_names") or []
        entries.append(
            {
                **row,
                "tags": tags,
                "summary_html": format_summary(row["summary"]),
            }
        )
    return entries


@app.route("/dashboard")
def dashboard():
    db = get_db()
    entries = fetch_entries_with_tags(db, request.args)

    tag_counts = {}
    for e in entries:
        for t in e["tags"]:
            tag_counts[t] = tag_counts.get(t, 0) + 1
    tag_counts = dict(sorted(tag_counts.items(), key=lambda x: -x[1]))

    parsed_groups = parse_tag_groups(request.args)
    has_tag_filter = len(parsed_groups) > 0
    tag_groups = [
        {"index": i, "selected": set(g)} for i, g in enumerate(parsed_groups or [[]])
    ]

    total_without_tags = len(entries)
    if has_tag_filter:
        count_query, count_params = build_filtered_query(
            request.args, select_cols="COUNT(*) AS c", include_tags=False, order=False
        )
        total_without_tags = db_execute(db, count_query, count_params).fetchone()["c"]

    all_tags, titles, pocs = get_meta(db)
    return render_template(
        "dashboard.html",
        entries=entries,
        tag_counts=tag_counts,
        all_tags=all_tags,
        titles=titles,
        pocs=pocs,
        filters=request.args,
        tag_groups=tag_groups,
        has_tag_filter=has_tag_filter,
        total=len(entries),
        total_without_tags=total_without_tags,
        flash=request.args.get("flash"),
    )


@app.route("/export")
def export():
    db = get_db()
    entries = fetch_entries_with_tags(db, request.args)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["date", "title", "poc", "user_id", "summary", "tags"])
    for e in entries:
        writer.writerow(
            [e["date"], e["title"], e["poc"], e["user_id"], e["summary"], "; ".join(e["tags"])]
        )
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=lod_export.csv"},
    )


@app.route("/edit/<int:entry_id>", methods=["GET", "POST"])
def edit_entry(entry_id):
    db = get_db()
    entry = db_execute(db, "SELECT * FROM entries WHERE id = %s", (entry_id,)).fetchone()
    if entry is None:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        entry_date = request.form.get("date") or entry["date"]
        title = request.form.get("title", "").strip() or entry["title"]
        poc = request.form.get("poc", "").strip()
        user_id = request.form.get("user_id", "").strip()
        summary = request.form.get("summary", "").strip().replace("\r\n", "\n")
        checked_tags = request.form.getlist("tags")
        new_tags_raw = request.form.get("newtags", "")
        new_tags = [t.strip() for t in new_tags_raw.split(",") if t.strip()]

        db_execute(
            db,
            "UPDATE entries SET date = %s, title = %s, poc = %s, user_id = %s, summary = %s WHERE id = %s",
            (entry_date, title, poc, user_id, summary, entry_id),
        )
        db_execute(db, "DELETE FROM entry_tags WHERE entry_id = %s", (entry_id,))
        for tname in set(checked_tags) | set(new_tags):
            tag_id = get_or_create_tag(db, tname)
            if tag_id:
                db_execute(
                    db,
                    "INSERT INTO entry_tags (entry_id, tag_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (entry_id, tag_id),
                )
        db.commit()
        invalidate_meta()
        return redirect(url_for("dashboard", flash="Entry updated"))

    current_tags = [
        r["name"]
        for r in db_execute(
            db,
            """SELECT t.name FROM tags t
               JOIN entry_tags et ON et.tag_id = t.id
               WHERE et.entry_id = %s""",
            (entry_id,),
        ).fetchall()
    ]

    tags, titles, pocs = get_meta(db)
    return render_template(
        "edit.html",
        entry=entry,
        tags=tags,
        titles=titles,
        pocs=pocs,
        current_tags=current_tags,
    )


def resolve_column(selected, fieldnames):
    if selected in fieldnames:
        return selected
    for fn in fieldnames:
        if fn.strip() == selected.strip():
            return fn
    return selected


@app.route("/import", methods=["GET", "POST"])
def import_csv():
    db = get_db()
    if request.method == "POST":
        file = request.files.get("csv_file")
        entry_date = request.form.get("date") or date.today().isoformat()
        title = request.form.get("title", "").strip()
        user_id_col = request.form.get("user_id_col", "")
        remark_col = request.form.get("remark_col", "")
        poc_col = request.form.get("poc_col", "")

        if not file or file.filename == "" or not title or not user_id_col or not remark_col:
            _tags, titles, _pocs = get_meta(db)
            return render_template(
                "import.html",
                titles=titles,
                today=date.today().isoformat(),
                error="Please provide a title, choose a file, and select both columns.",
            )

        text = file.stream.read().decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        fieldnames = reader.fieldnames or []
        user_id_col = resolve_column(user_id_col, fieldnames)
        remark_col = resolve_column(remark_col, fieldnames)
        poc_col = resolve_column(poc_col, fieldnames) if poc_col else None

        existing = set()
        for r in db_execute(
            db, "SELECT user_id, summary FROM entries WHERE title = %s", (title,)
        ).fetchall():
            existing.add(dup_key(r["user_id"], r["summary"]))

        total_rows = 0
        skipped_empty = 0
        skipped_duplicate = 0
        candidates = []
        for row in reader:
            total_rows += 1
            user_id = (row.get(user_id_col) or "").strip()
            summary = (row.get(remark_col) or "").strip()
            if not summary:
                skipped_empty += 1
                continue
            key = dup_key(user_id, summary)
            if key in existing:
                skipped_duplicate += 1
                continue
            existing.add(key)  # also dedupe repeated rows within this file
            poc = (row.get(poc_col) or "").strip() if poc_col else ""
            candidates.append({"user_id": user_id, "summary": summary, "poc": poc})

        tags, _titles, pocs = get_meta(db)
        return render_template(
            "import_review.html",
            candidates=candidates,
            date=entry_date,
            title=title,
            tags=tags,
            pocs=pocs,
            total_rows=total_rows,
            skipped_empty=skipped_empty,
            skipped_duplicate=skipped_duplicate,
        )

    _tags, titles, _pocs = get_meta(db)
    return render_template(
        "import.html",
        titles=titles,
        today=date.today().isoformat(),
        error=None,
    )


@app.route("/delete/<int:entry_id>", methods=["POST"])
def delete_entry(entry_id):
    db = get_db()
    db_execute(db, "DELETE FROM entries WHERE id = %s", (entry_id,))
    db.commit()
    invalidate_meta()
    return redirect(url_for("dashboard"))


init_db()

if __name__ == "__main__":
    app.run(debug=True, port=5050)
