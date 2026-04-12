from __future__ import annotations

import datetime as dt
import email.utils
import html
import concurrent.futures
import sqlite3
import threading
import time
import re
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import feedparser
from bs4 import BeautifulSoup
from flask import Flask, flash, redirect, render_template, request, send_from_directory, url_for


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "rss_reader.db"
DEFAULT_OPML = BASE_DIR / "feeds" / "hn-popular-blogs-2025.opml"

app = Flask(__name__)
app.secret_key = "rss-reader-local-secret"

_scheduler_lock = threading.Lock()
_scheduler_started = False
_refresh_lock = threading.Lock()
_summary_lock = threading.Lock()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.create_function("entry_date", 2, entry_date)
    return conn


def entry_date(published: str | None, created_at: str | None) -> str:
    for value in (published, created_at):
        normalized = parse_date_value(value)
        if normalized:
            return normalized
    return ""


def parse_date_value(value: str | None) -> str:
    if not value:
        return ""
    text = value.strip()
    if not text:
        return ""
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return ""
    return parsed.date().isoformat()


def normalize_date_filter(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    try:
        return dt.date.fromisoformat(value).isoformat()
    except ValueError:
        return ""


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

        CREATE TABLE IF NOT EXISTS daily_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            summary_date TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            html_content TEXT NOT NULL,
            entry_count INTEGER NOT NULL,
            created_at TEXT NOT NULL
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
    conn.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
        ("daily_summary_hour", "0"),
    )
    conn.execute(
        """
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        ("scheduler_refresh_running", "0"),
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
    normalize_cached_entry_links(conn)
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


def set_settings(values: dict[str, str]) -> None:
    conn = get_conn()
    conn.executemany(
        """
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        values.items(),
    )
    conn.commit()
    conn.close()


def local_now() -> dt.datetime:
    return dt.datetime.now().replace(microsecond=0)


def utc_now_iso() -> str:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None, microsecond=0).isoformat()


def parse_local_datetime(value: str) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


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


def canonicalize_entry_link(link: str, feed_row: sqlite3.Row) -> str:
    if not link:
        return link
    parsed = urllib.parse.urlparse(link)
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""
    if host in {"nitter.net", "xcancel.com"}:
        match = re.match(r"^/([^/]+)/status/(\d+)", path)
        if match:
            handle, status_id = match.groups()
            return f"https://x.com/{handle}/status/{status_id}"
    if host == "x.com":
        return link
    title = (feed_row["title"] or "") if feed_row else ""
    if title.startswith("X人物 · "):
        parsed_html = urllib.parse.urlparse(feed_row["html_url"] or "")
        html_host = (parsed_html.netloc or "").lower()
        if html_host == "x.com" and "/status/" not in parsed_html.path:
            return feed_row["html_url"]
    return link


def normalize_cached_entry_links(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        """
        SELECT
            e.id AS entry_pk,
            e.link AS entry_link,
            f.title AS title,
            f.html_url AS html_url
        FROM entries e
        JOIN feeds f ON e.feed_id = f.id
        WHERE e.link LIKE 'http://nitter.net/%/status/%'
           OR e.link LIKE 'https://nitter.net/%/status/%'
           OR e.link LIKE 'http://xcancel.com/%/status/%'
           OR e.link LIKE 'https://xcancel.com/%/status/%'
        """
    ).fetchall()
    updates = []
    for row in rows:
        new_link = canonicalize_entry_link(row["entry_link"], row)
        if new_link != row["entry_link"]:
            updates.append((new_link, row["entry_pk"]))
    if updates:
        conn.executemany("UPDATE entries SET link = ? WHERE id = ?", updates)
    return len(updates)


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
    now = utc_now_iso()
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
    with urllib.request.urlopen(req, timeout=8) as response:
        raw = response.read()
    parsed = feedparser.parse(raw)

    if getattr(parsed, "bozo", 0):
        exc = getattr(parsed, "bozo_exception", None)
        if not getattr(parsed, "entries", None):
            return 0, str(exc) if exc else "Unknown parsing error"

    conn = get_conn()
    added = 0
    now = utc_now_iso()

    for entry in parsed.entries:
        entry_id = (
            entry.get("id")
            or entry.get("guid")
            or entry.get("link")
            or urllib.parse.quote(entry.get("title", "untitled"))
        )
        title = entry.get("title", "Untitled entry")
        link = entry.get("link", feed_row["html_url"] or feed_row["xml_url"])
        link = canonicalize_entry_link(link, feed_row)
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

    def _refresh_one(feed: sqlite3.Row) -> tuple[int, int]:
        try:
            added, error = refresh_feed(feed)
            if error:
                conn = get_conn()
                conn.execute(
                    "UPDATE feeds SET last_error = ?, last_fetched_at = ? WHERE id = ?",
                    (error, utc_now_iso(), feed["id"]),
                )
                conn.commit()
                conn.close()
                return added, 1
            return added, 0
        except Exception as exc:  # noqa: BLE001
            conn = get_conn()
            conn.execute(
                "UPDATE feeds SET last_error = ?, last_fetched_at = ? WHERE id = ?",
                (str(exc), utc_now_iso(), feed["id"]),
            )
            conn.commit()
            conn.close()
            return 0, 1

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(_refresh_one, feed) for feed in feeds]
        for future in concurrent.futures.as_completed(futures):
            added, feed_failed = future.result()
            total_added += added
            failed += feed_failed
    return total_added, failed


def run_refresh_job(trigger: str = "scheduled") -> tuple[int, int, str | None]:
    if not _refresh_lock.acquire(blocking=False):
        return 0, 0, "刷新任务已在运行，已跳过本次触发。"
    started_at = local_now()
    set_settings(
        {
            "scheduler_refresh_running": "1",
            "scheduler_last_refresh_started_at": started_at.isoformat(),
            "scheduler_last_refresh_trigger": trigger,
            "scheduler_last_error": "",
        }
    )
    try:
        added, failed = refresh_all_feeds()
        finished_at = local_now()
        interval = max(int(get_setting("scheduler_interval_minutes", "30")), 1)
        set_settings(
            {
                "scheduler_refresh_running": "0",
                "scheduler_last_refresh_finished_at": finished_at.isoformat(),
                "scheduler_last_refresh_added": str(added),
                "scheduler_last_refresh_failed": str(failed),
                "scheduler_next_refresh_at": (
                    finished_at + dt.timedelta(minutes=interval)
                ).isoformat(),
                "scheduler_last_error": "",
            }
        )
        return added, failed, None
    except Exception as exc:  # noqa: BLE001
        finished_at = local_now()
        set_settings(
            {
                "scheduler_refresh_running": "0",
                "scheduler_last_refresh_finished_at": finished_at.isoformat(),
                "scheduler_last_error": str(exc),
            }
        )
        return 0, 0, str(exc)
    finally:
        _refresh_lock.release()


def should_run_refresh(now: dt.datetime) -> bool:
    if get_setting("scheduler_enabled", "1") != "1":
        return False
    interval = max(int(get_setting("scheduler_interval_minutes", "30")), 1)
    last_finished = parse_local_datetime(
        get_setting("scheduler_last_refresh_finished_at", "")
    )
    if last_finished is None:
        return True
    return now >= last_finished + dt.timedelta(minutes=interval)


def query_entries_for_date(summary_date: str, limit: int = 500) -> list[sqlite3.Row]:
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT e.*, f.title AS feed_title, f.tags AS feed_tags
        FROM entries e
        JOIN feeds f ON e.feed_id = f.id
        WHERE entry_date(e.published, e.created_at) = ?
        ORDER BY COALESCE(NULLIF(e.published, ''), e.created_at) DESC, e.id DESC
        LIMIT ?
        """,
        (summary_date, limit),
    ).fetchall()
    conn.close()
    return rows


EXPERIMENT_HELP_TOPICS = [
    {
        "name": "Agent 记忆与上下文工程",
        "keywords": (
            "agent", "harness", "memory", "context", "retrieval", "langchain",
            "deepagents", "openclaw", "codex", "claude", "compaction",
        ),
        "help": "适合转化为 memory 写入策略、上下文压缩、检索注入时机和 harness 归属的消融实验。",
    },
    {
        "name": "评测与基准验证",
        "keywords": (
            "benchmark", "eval", "ifbench", "terminal bench", "swe-pro", "test",
            "comparison", "5 year old", "score", "fraudulent", "submission",
        ),
        "help": "适合补强回归集、任务成功率、人工复核与基准防作弊检查，避免只看单一榜单分数。",
    },
    {
        "name": "模型与开源工具对照",
        "keywords": (
            "model", "grok", "minimax", "open source", "hugging face", "muse",
            "meta ai", "claude", "openai", "gemini", "m2.7", "spark",
        ),
        "help": "适合做模型替换实验、能力/成本/延迟对照，以及开源模型在具体任务上的迁移验证。",
    },
    {
        "name": "数据与真实场景泛化",
        "keywords": (
            "robot", "robotic", "fsd", "waymo", "tesla", "real-world", "data",
            "unitree", "autonomous", "manipulation", "starlink", "video",
        ),
        "help": "适合提醒实验设计纳入真实扰动、长尾样本、OOD 场景和从演示到稳定上线的差距。",
    },
    {
        "name": "工程化、可靠性与安全",
        "keywords": (
            "security", "ssrf", "stability", "api", "provider", "local", "teams",
            "voice", "tool", "privacy", "startup", "reliability", "docs",
        ),
        "help": "适合改进实验平台可复现性、权限边界、工具调用日志和本地/云端依赖隔离。",
    },
]

EXPERIMENT_HELP_TAG_BOOSTS = (
    "langchain", "openai", "post-training-core", "tools", "engineering",
    "formal-methods", "google-deepmind", "nvidia", "xai", "anthropic",
)


def entry_haystack(entry: sqlite3.Row) -> str:
    return " ".join(
        [
            entry["title"] or "",
            entry["summary"] or "",
            entry["feed_title"] or "",
            entry["feed_tags"] or "",
            entry["author"] or "",
        ]
    ).lower()


def score_experiment_help(entry: sqlite3.Row) -> tuple[int, str, list[str]]:
    haystack = entry_haystack(entry)
    best_score = 0
    best_topic = "其它"
    best_matches: list[str] = []
    for topic in EXPERIMENT_HELP_TOPICS:
        matches = [keyword for keyword in topic["keywords"] if keyword in haystack]
        if not matches:
            continue
        score = len(matches) * 2
        if any(tag in haystack for tag in EXPERIMENT_HELP_TAG_BOOSTS):
            score += 2
        if "rt by" not in (entry["title"] or "").lower():
            score += 1
        if score > best_score:
            best_score = score
            best_topic = topic["name"]
            best_matches = matches
    return best_score, best_topic, best_matches


def summarize_entries_for_experiment_help(entries: list[sqlite3.Row]) -> tuple[list[dict[str, object]], int]:
    buckets: dict[str, list[dict[str, object]]] = {
        topic["name"]: [] for topic in EXPERIMENT_HELP_TOPICS
    }
    for entry in entries:
        score, topic_name, matches = score_experiment_help(entry)
        if score <= 0 or topic_name not in buckets:
            continue
        buckets[topic_name].append({"entry": entry, "score": score, "matches": matches})

    topics: list[dict[str, object]] = []
    for topic in EXPERIMENT_HELP_TOPICS:
        rows = sorted(
            buckets[topic["name"]],
            key=lambda item: (int(item["score"]), item["entry"]["published"] or ""),
            reverse=True,
        )
        if rows:
            topics.append(
                {
                    "name": topic["name"],
                    "help": topic["help"],
                    "entries": rows[:5],
                    "count": len(rows),
                }
            )
    selected_count = sum(topic["count"] for topic in topics)
    return topics, selected_count


def build_daily_summary_html(summary_date: str, entries: list[sqlite3.Row]) -> str:
    topics, selected_count = summarize_entries_for_experiment_help(entries)
    title = f"{summary_date} 实验效果提升日报"
    if not entries:
        return f"""
        <article class="daily-article">
          <div class="daily-kicker">每日 00:00 自动生成 · 面向算法工程师</div>
          <h2>{title}</h2>
          <p>前一天没有筛选到带发布时间的资讯条目。</p>
        </article>
        """

    top_sources: dict[str, int] = {}
    for entry in entries:
        top_sources[entry["feed_title"]] = top_sources.get(entry["feed_title"], 0) + 1
    source_text = "、".join(
        f"{html.escape(name, quote=False)} {count} 篇"
        for name, count in sorted(top_sources.items(), key=lambda item: item[1], reverse=True)[:5]
    )
    source_meta = f'<p class="daily-meta">主要来源：{source_text}</p>' if source_text else ""

    topic_cards = []
    for topic in topics:
        items = []
        for item in topic["entries"]:
            entry = item["entry"]
            summary = entry["summary"] or ""
            if len(summary) > 140:
                summary = summary[:140] + "..."
            safe_link = html.escape(entry["link"] or "", quote=True)
            safe_title = html.escape(entry["title"] or "Untitled", quote=False)
            safe_feed_title = html.escape(entry["feed_title"] or "", quote=False)
            safe_summary = html.escape(summary, quote=False)
            safe_matches = html.escape("、".join(item["matches"][:5]), quote=False)
            items.append(
                f"""
                <li>
                  <a href="{safe_link}" target="_blank" rel="noreferrer">{safe_title}</a>
                  <span>来自 {safe_feed_title} · 命中：{safe_matches}</span>
                  {f"<p>{safe_summary}</p>" if summary else ""}
                </li>
                """
            )
        topic_cards.append(
            f"""
            <section class="daily-topic">
              <h3>{topic['name']} <small>{topic['count']} 篇</small></h3>
              <p>{html.escape(topic['help'], quote=False)}</p>
              <ul>{''.join(items)}</ul>
            </section>
            """
        )

    if not topic_cards:
        topic_cards.append(
            """
            <section class="daily-topic">
              <h3>没有高相关实验线索</h3>
              <p>昨日资讯里没有明显指向实验设计、模型对照、评测或工程可靠性的内容；建议只做浏览，不投入实验排期。</p>
            </section>
            """
        )

    experiment_actions = """
      <section class="daily-topic">
        <h3>今日可执行实验清单</h3>
        <ul>
          <li>把 agent memory 拆成写入策略、检索策略、压缩策略三组消融，分别记录成功率、token 成本和人工判分。</li>
          <li>为关键任务建立最小回归集：固定输入、固定工具权限、固定评分脚本，避免只凭单次 demo 判断效果。</li>
          <li>做模型/ harness 解耦实验：同一 harness 下替换模型，同一模型下替换 memory 与 retrieval，定位收益来源。</li>
          <li>记录完整实验日志：prompt、模型版本、memory 命中、retrieval 结果、tool call、输出、评分和失败原因。</li>
          <li>对真实场景样本单独建桶：长尾、噪声、权限异常、超长上下文和工具失败都要进入验证集。</li>
        </ul>
      </section>
      <section class="daily-topic">
        <h3>不建议优先投入实验排期</h3>
        <p>航天、政治争议、品牌宣传和纯观点转发如果没有可复现实验变量，今天只作为背景信息，不应占用算法实验资源。</p>
      </section>
    """

    return f"""
    <article class="daily-article">
      <div class="daily-kicker">每日 00:00 自动生成 · 面向算法工程师</div>
      <h2>{title}</h2>
      <p class="daily-lead">昨日共抓取到 <strong>{len(entries)}</strong> 篇资讯，其中 <strong>{selected_count}</strong> 篇被判定为可能帮助算法工程师提高实验效果。筛选标准是：是否能转化为模型对照、评测改进、上下文/记忆消融、真实场景泛化或工程可靠性动作。</p>
      {source_meta}
      {experiment_actions}
      {''.join(topic_cards)}
    </article>
    """

def generate_daily_summary(summary_date: dt.date) -> sqlite3.Row:
    summary_date_text = summary_date.isoformat()
    if not _summary_lock.acquire(blocking=False):
        raise RuntimeError("日报生成任务已在运行。")
    try:
        entries = query_entries_for_date(summary_date_text, limit=800)
        title = f"{summary_date_text} 实验效果提升日报"
        html_content = build_daily_summary_html(summary_date_text, entries)
        created_at = local_now().isoformat()
        conn = get_conn()
        conn.execute(
            """
            INSERT INTO daily_summaries
            (summary_date, title, html_content, entry_count, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(summary_date) DO UPDATE SET
                title = excluded.title,
                html_content = excluded.html_content,
                entry_count = excluded.entry_count,
                created_at = excluded.created_at
            """,
            (summary_date_text, title, html_content, len(entries), created_at),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM daily_summaries WHERE summary_date = ?",
            (summary_date_text,),
        ).fetchone()
        conn.close()
        set_settings(
            {
                "daily_summary_last_date": summary_date_text,
                "daily_summary_last_created_at": created_at,
                "daily_summary_last_error": "",
            }
        )
        return row
    except Exception as exc:  # noqa: BLE001
        set_setting("daily_summary_last_error", str(exc))
        raise
    finally:
        _summary_lock.release()


def should_generate_daily_summary(now: dt.datetime) -> dt.date | None:
    summary_hour = max(min(int(get_setting("daily_summary_hour", "0")), 23), 0)
    if now.hour < summary_hour:
        return None
    target_date = now.date() - dt.timedelta(days=1)
    if get_setting("daily_summary_last_date", "") == target_date.isoformat():
        return None
    return target_date


def get_latest_daily_summary() -> sqlite3.Row | None:
    conn = get_conn()
    row = conn.execute(
        """
        SELECT *
        FROM daily_summaries
        ORDER BY summary_date DESC
        LIMIT 1
        """
    ).fetchone()
    conn.close()
    return row


def scheduler_loop() -> None:
    while True:
        now = local_now()
        try:
            if should_run_refresh(now):
                run_refresh_job(trigger="scheduled")
            summary_date = should_generate_daily_summary(local_now())
            if summary_date:
                generate_daily_summary(summary_date)
        except Exception as exc:  # noqa: BLE001
            set_setting("scheduler_last_error", str(exc))
        time.sleep(30)


def run_startup_jobs() -> None:
    if should_run_refresh(local_now()):
        threading.Thread(
            target=run_refresh_job,
            kwargs={"trigger": "startup"},
            daemon=True,
        ).start()
    summary_date = should_generate_daily_summary(local_now())
    if summary_date:
        try:
            generate_daily_summary(summary_date)
        except Exception:
            pass


def scheduler_bootstrap() -> None:
    run_startup_jobs()
    scheduler_loop()


def ensure_scheduler_status() -> None:
    interval = max(int(get_setting("scheduler_interval_minutes", "30")), 1)
    last_finished = parse_local_datetime(
        get_setting("scheduler_last_refresh_finished_at", "")
    )
    if not get_setting("scheduler_next_refresh_at", "") and last_finished:
        set_setting(
            "scheduler_next_refresh_at",
            (last_finished + dt.timedelta(minutes=interval)).isoformat(),
        )


def force_daily_summary_for_yesterday() -> dt.date:
    target_date = local_now().date() - dt.timedelta(days=1)
    generate_daily_summary(target_date)
    return target_date


def get_scheduler_status() -> dict[str, str]:
    ensure_scheduler_status()
    return {
        "refresh_running": get_setting("scheduler_refresh_running", "0"),
        "last_refresh_started_at": get_setting("scheduler_last_refresh_started_at", ""),
        "last_refresh_finished_at": get_setting("scheduler_last_refresh_finished_at", ""),
        "last_refresh_added": get_setting("scheduler_last_refresh_added", "0"),
        "last_refresh_failed": get_setting("scheduler_last_refresh_failed", "0"),
        "next_refresh_at": get_setting("scheduler_next_refresh_at", ""),
        "last_error": get_setting("scheduler_last_error", ""),
        "daily_summary_hour": get_setting("daily_summary_hour", "0"),
        "daily_summary_last_date": get_setting("daily_summary_last_date", ""),
        "daily_summary_last_created_at": get_setting("daily_summary_last_created_at", ""),
        "daily_summary_last_error": get_setting("daily_summary_last_error", ""),
    }


def start_scheduler() -> None:
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        thread = threading.Thread(target=scheduler_bootstrap, daemon=True)
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
    start_date: str = "",
    end_date: str = "",
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
    if start_date:
        sql += " AND entry_date(e.published, e.created_at) >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND entry_date(e.published, e.created_at) <= ?"
        params.append(end_date)
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
    start_date = normalize_date_filter(request.args.get("start_date", ""))
    end_date = normalize_date_filter(request.args.get("end_date", ""))

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
        entries=query_entries(
            q=q,
            tag=tag,
            favorites_only=favorites_only,
            start_date=start_date,
            end_date=end_date,
        ),
        stats=get_stats(),
        all_tags=list_all_tags(),
        current_q=q,
        current_tag=tag,
        favorites_only=favorites_only,
        current_start_date=start_date,
        current_end_date=end_date,
        latest_summary=get_latest_daily_summary(),
        scheduler_status=get_scheduler_status(),
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
        current_start_date="",
        current_end_date="",
        latest_summary=get_latest_daily_summary(),
        scheduler_status=get_scheduler_status(),
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


@app.route("/people-directory")
def people_directory():
    return send_from_directory(BASE_DIR / "feeds", "llm_people_directory.html")


@app.post("/refresh")
def refresh():
    added, failed, error = run_refresh_job(trigger="manual")
    if error:
        flash(f"刷新未完成：{error}", "error")
        return redirect(request.referrer or url_for("index"))
    flash(f"刷新完成：新增 {added} 篇，失败 {failed} 个源。", "success")
    return redirect(request.referrer or url_for("index"))


@app.post("/summaries/generate-yesterday")
def generate_yesterday_summary_route():
    try:
        summary_date = force_daily_summary_for_yesterday()
    except Exception as exc:  # noqa: BLE001
        flash(f"日报生成失败：{exc}", "error")
        return redirect(request.referrer or url_for("index"))
    flash(f"{summary_date.isoformat()} 日报已生成。", "success")
    return redirect(url_for("index"))


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
    summary_hour = request.form.get("daily_summary_hour", "0").strip()
    try:
        interval_value = max(int(interval), 1)
    except ValueError:
        flash("刷新间隔必须是正整数分钟。", "error")
        return redirect(url_for("index"))
    try:
        summary_hour_value = max(min(int(summary_hour), 23), 0)
    except ValueError:
        flash("日报生成小时必须是 0 到 23 的整数。", "error")
        return redirect(url_for("index"))

    last_finished = parse_local_datetime(
        get_setting("scheduler_last_refresh_finished_at", "")
    )
    values = {
        "scheduler_enabled": enabled,
        "scheduler_interval_minutes": str(interval_value),
        "daily_summary_hour": str(summary_hour_value),
    }
    if last_finished:
        values["scheduler_next_refresh_at"] = (
            last_finished + dt.timedelta(minutes=interval_value)
        ).isoformat()
    set_settings(values)
    flash("后台自动刷新设置已保存。", "success")
    return redirect(url_for("index"))


if __name__ == "__main__":
    init_db()
    ensure_seed_data()
    start_scheduler()
    app.run(host="127.0.0.1", port=5050, debug=False)
