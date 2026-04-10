from __future__ import annotations

import datetime as dt
import sqlite3
import threading
import time
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import feedparser
from bs4 import BeautifulSoup
from flask import Flask, flash, redirect, render_template, request, url_for


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "rss_reader.db"
DEFAULT_OPML = BASE_DIR / "feeds" / "hn-popular-blogs-2025.opml"

app = Flask(__name__)
app.secret_key = "rss-reader-local-secret"

_scheduler_lock = threading.Lock()
_scheduler_started = False


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS feeds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            xml_url TEXT NOT NULL UNIQUE,
            html_url TEXT,
            tags TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            last_fetched_at TEXT,
            last_error TEXT
        );

        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feed_id INTEGER NOT NULL,
            entry_id TEXT NOT NULL,
            title TEXT NOT NULL,
            link TEXT NOT NULL,
            author TEXT,
            published TEXT,
            summary TEXT,
            is_favorite INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            UNIQUE(feed_id, entry_id),
            FOREIGN KEY(feed_id) REFERENCES feeds(id)
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
        ("scheduler_enabled", "1"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
        ("scheduler_interval_minutes", "30"),
    )
    feed_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(feeds)").fetchall()
    }
    if "tags" not in feed_columns:
        conn.execute("ALTER TABLE feeds ADD COLUMN tags TEXT NOT NULL DEFAULT ''")

    entry_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(entries)").fetchall()
    }
    if "is_favorite" not in entry_columns:
        conn.execute(
            "ALTER TABLE entries ADD COLUMN is_favorite INTEGER NOT NULL DEFAULT 0"
        )
    conn.commit()
    conn.close()


def get_setting(key: str, default: str) -> str:
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )
    conn.commit()
    conn.close()


def extract_text(html: str, limit: int = 280) -> str:
    text = BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)
    text = " ".join(text.split())
    return text[:limit] + ("..." if len(text) > limit else "")


def normalize_tags(tags_text: str) -> str:
    tags = []
    seen = set()
    for raw in tags_text.replace("，", ",").split(","):
        tag = raw.strip().lower()
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return ", ".join(tags)


def list_all_tags() -> list[str]:
    conn = get_conn()
    rows = conn.execute("SELECT tags FROM feeds WHERE tags != ''").fetchall()
    conn.close()
    tags = set()
    for row in rows:
        for tag in [t.strip() for t in row["tags"].split(",")]:
            if tag:
                tags.add(tag)
    return sorted(tags)


def import_opml(opml_path: Path) -> int:
    tree = ET.parse(opml_path)
    root = tree.getroot()
    imported = 0
    conn = get_conn()
    outlines = root.findall(".//outline[@xmlUrl]")
    now = dt.datetime.utcnow().isoformat()
    for outline in outlines:
        title = outline.attrib.get("title") or outline.attrib.get("text") or "Untitled Feed"
        xml_url = outline.attrib["xmlUrl"].strip()
        html_url = outline.attrib.get("htmlUrl", "").strip()
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO feeds (title, xml_url, html_url, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (title, xml_url, html_url, now),
        )
        if cur.rowcount:
            imported += 1
    conn.commit()
    conn.close()
    return imported


def ensure_seed_data() -> None:
    conn = get_conn()
    count = conn.execute("SELECT COUNT(*) FROM feeds").fetchone()[0]
    conn.close()
    if count == 0 and DEFAULT_OPML.exists():
        import_opml(DEFAULT_OPML)


def refresh_feed(feed_row: sqlite3.Row) -> tuple[int, str | None]:
    req = urllib.request.Request(
        feed_row["xml_url"],
        headers={"User-Agent": "LocalRSSReader/1.0 (+https://localhost)"},
    )
    with urllib.request.urlopen(req, timeout=12) as response:
        raw = response.read()
    parsed = feedparser.parse(raw)

    if getattr(parsed, "bozo", 0):
        exc = getattr(parsed, "bozo_exception", None)
        if not getattr(parsed, "entries", None):
            return 0, str(exc) if exc else "Unknown parsing error"

    conn = get_conn()
    added = 0
    now = dt.datetime.utcnow().isoformat()

    for entry in parsed.entries:
        entry_id = (
            entry.get("id")
            or entry.get("guid")
            or entry.get("link")
            or urllib.parse.quote(entry.get("title", "untitled"))
        )
        title = entry.get("title", "Untitled entry")
        link = entry.get("link", feed_row["html_url"] or feed_row["xml_url"])
        author = entry.get("author", "")
        published = entry.get("published", "") or entry.get("updated", "")
        summary = entry.get("summary", "") or entry.get("description", "")
        summary = extract_text(summary)

        cur = conn.execute(
            """
            INSERT OR IGNORE INTO entries
            (feed_id, entry_id, title, link, author, published, summary, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (feed_row["id"], entry_id, title, link, author, published, summary, now),
        )
        if cur.rowcount:
            added += 1

    conn.execute(
        "UPDATE feeds SET last_fetched_at = ?, last_error = NULL WHERE id = ?",
        (now, feed_row["id"]),
    )
    conn.commit()
    conn.close()
    return added, None


def refresh_all_feeds() -> tuple[int, int]:
    conn = get_conn()
    feeds = conn.execute("SELECT * FROM feeds ORDER BY title").fetchall()
    conn.close()
    total_added = 0
    failed = 0
    for feed in feeds:
        try:
            added, error = refresh_feed(feed)
            total_added += added
            if error:
                failed += 1
                conn = get_conn()
                conn.execute(
                    "UPDATE feeds SET last_error = ?, last_fetched_at = ? WHERE id = ?",
                    (error, dt.datetime.utcnow().isoformat(), feed["id"]),
                )
                conn.commit()
                conn.close()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            conn = get_conn()
            conn.execute(
                "UPDATE feeds SET last_error = ?, last_fetched_at = ? WHERE id = ?",
                (str(exc), dt.datetime.utcnow().isoformat(), feed["id"]),
            )
            conn.commit()
            conn.close()
    return total_added, failed


def scheduler_loop() -> None:
    while True:
        enabled = get_setting("scheduler_enabled", "1") == "1"
        interval = max(int(get_setting("scheduler_interval_minutes", "30")), 1)
        if enabled:
            try:
                refresh_all_feeds()
            except Exception:
                pass
        time.sleep(interval * 60)


def start_scheduler() -> None:
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        thread = threading.Thread(target=scheduler_loop, daemon=True)
        thread.start()
        _scheduler_started = True


def get_stats() -> dict[str, int | str]:
    conn = get_conn()
    feed_count = conn.execute("SELECT COUNT(*) FROM feeds").fetchone()[0]
    entry_count = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    favorite_count = conn.execute(
        "SELECT COUNT(*) FROM entries WHERE is_favorite = 1"
    ).fetchone()[0]
    conn.close()
    return {
        "feed_count": feed_count,
        "entry_count": entry_count,
        "favorite_count": favorite_count,
        "scheduler_enabled": get_setting("scheduler_enabled", "1"),
        "scheduler_interval_minutes": get_setting("scheduler_interval_minutes", "30"),
    }


def query_entries(
    q: str = "",
    tag: str = "",
    favorites_only: bool = False,
    limit: int = 120,
) -> list[sqlite3.Row]:
    conn = get_conn()
    sql = """
        SELECT e.*, f.title AS feed_title, f.tags AS feed_tags
        FROM entries e
        JOIN feeds f ON e.feed_id = f.id
        WHERE 1=1
    """
    params: list[object] = []
    if q:
        sql += " AND (e.title LIKE ? OR e.summary LIKE ? OR e.author LIKE ? OR f.title LIKE ?)"
        like = f"%{q}%"
        params.extend([like, like, like, like])
    if tag:
        sql += " AND LOWER(f.tags) LIKE ?"
        params.append(f"%{tag.lower()}%")
    if favorites_only:
        sql += " AND e.is_favorite = 1"
    sql += """
        ORDER BY COALESCE(NULLIF(e.published, ''), e.created_at) DESC, e.id DESC
        LIMIT ?
    """
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    tag = request.args.get("tag", "").strip().lower()
    favorites_only = request.args.get("favorites") == "1"

    conn = get_conn()
    feeds = conn.execute(
        """
        SELECT f.*, COUNT(e.id) AS entry_count
        FROM feeds f
        LEFT JOIN entries e ON e.feed_id = f.id
        GROUP BY f.id
        ORDER BY f.title COLLATE NOCASE
        """
    ).fetchall()
    conn.close()

    return render_template(
        "index.html",
        feeds=feeds,
        entries=query_entries(q=q, tag=tag, favorites_only=favorites_only),
        stats=get_stats(),
        all_tags=list_all_tags(),
        current_q=q,
        current_tag=tag,
        favorites_only=favorites_only,
    )


@app.route("/favorites")
def favorites():
    conn = get_conn()
    feeds = conn.execute(
        """
        SELECT f.*, COUNT(e.id) AS entry_count
        FROM feeds f
        LEFT JOIN entries e ON e.feed_id = f.id
        GROUP BY f.id
        ORDER BY f.title COLLATE NOCASE
        """
    ).fetchall()
    conn.close()

    return render_template(
        "index.html",
        feeds=feeds,
        entries=query_entries(favorites_only=True, limit=300),
        stats=get_stats(),
        all_tags=list_all_tags(),
        current_q="",
        current_tag="",
        favorites_only=True,
    )


@app.route("/feeds/<int:feed_id>")
def feed_detail(feed_id: int):
    conn = get_conn()
    feed = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
    entries = conn.execute(
        """
        SELECT *
        FROM entries
        WHERE feed_id = ?
        ORDER BY COALESCE(NULLIF(published, ''), created_at) DESC, id DESC
        LIMIT 200
        """,
        (feed_id,),
    ).fetchall()
    conn.close()
    if not feed:
        flash("未找到对应订阅源。", "error")
        return redirect(url_for("index"))
    return render_template("feed_detail.html", feed=feed, entries=entries, stats=get_stats())


@app.post("/refresh")
def refresh():
    added, failed = refresh_all_feeds()
    flash(f"刷新完成：新增 {added} 篇，失败 {failed} 个源。", "success")
    return redirect(request.referrer or url_for("index"))


@app.post("/import-opml")
def import_opml_route():
    content = request.form.get("opml_content", "").strip()
    if not content:
        flash("请粘贴 OPML 内容。", "error")
        return redirect(url_for("index"))

    temp_path = BASE_DIR / "feeds" / "imported.opml"
    temp_path.write_text(content, encoding="utf-8")
    try:
        imported = import_opml(temp_path)
    except Exception as exc:  # noqa: BLE001
        flash(f"导入失败：{exc}", "error")
        return redirect(url_for("index"))

    flash(f"OPML 导入完成：新增 {imported} 个订阅源。", "success")
    return redirect(url_for("index"))


@app.post("/feeds/<int:feed_id>/tags")
def update_feed_tags(feed_id: int):
    tags = normalize_tags(request.form.get("tags", ""))
    conn = get_conn()
    conn.execute("UPDATE feeds SET tags = ? WHERE id = ?", (tags, feed_id))
    conn.commit()
    conn.close()
    flash("标签已更新。", "success")
    return redirect(request.referrer or url_for("feed_detail", feed_id=feed_id))


@app.post("/entries/<int:entry_id>/favorite")
def toggle_favorite(entry_id: int):
    conn = get_conn()
    row = conn.execute(
        "SELECT is_favorite FROM entries WHERE id = ?",
        (entry_id,),
    ).fetchone()
    if row:
        new_value = 0 if row["is_favorite"] else 1
        conn.execute(
            "UPDATE entries SET is_favorite = ? WHERE id = ?",
            (new_value, entry_id),
        )
        conn.commit()
    conn.close()
    flash("收藏状态已更新。", "success")
    return redirect(request.referrer or url_for("index"))


@app.post("/settings/scheduler")
def update_scheduler_settings():
    enabled = "1" if request.form.get("scheduler_enabled") == "on" else "0"
    interval = request.form.get("scheduler_interval_minutes", "30").strip()
    try:
        interval_value = max(int(interval), 1)
    except ValueError:
        flash("刷新间隔必须是正整数分钟。", "error")
        return redirect(url_for("index"))

    set_setting("scheduler_enabled", enabled)
    set_setting("scheduler_interval_minutes", str(interval_value))
    flash("后台自动刷新设置已保存。", "success")
    return redirect(url_for("index"))


if __name__ == "__main__":
    init_db()
    ensure_seed_data()
    start_scheduler()
    app.run(host="127.0.0.1", port=5050, debug=False)
