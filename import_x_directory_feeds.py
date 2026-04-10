from __future__ import annotations

import datetime as dt
import re
import sqlite3
import urllib.parse
import xml.sax.saxutils as saxutils
from pathlib import Path

from bs4 import BeautifulSoup


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "rss_reader.db"
HTML_PATH = BASE_DIR / "feeds" / "llm_people_directory.html"
LOCAL_RSS_DIR = BASE_DIR / "feeds" / "x_people_rss"
XCANCEL_FALLBACK_HANDLES = {
    "dgros",
    "bl16",
    "sfriar",
    "fidjisimo",
    "TrapitBansal",
    "TimotheeLacroix",
    "patrickvonplaten",
    "mikekrieger",
    "YejinChoi1",
    "samshleifer",
    "lewtun",
}


def slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "item"


def extract_section_titles(soup: BeautifulSoup) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for section in soup.select(".section[id]"):
        heading = section.find("h2")
        if heading:
            mapping[section["id"]] = heading.get_text(" ", strip=True)
    return mapping


def build_local_search_rss(
    *,
    name: str,
    original_url: str,
    org: str,
    focus: str,
    section_id: str,
    section_title: str,
) -> Path:
    LOCAL_RSS_DIR.mkdir(parents=True, exist_ok=True)
    slug = slugify(name)
    target = LOCAL_RSS_DIR / f"{slug}.xml"
    description = f"{name} / {org} / {focus}"
    now_rfc822 = "Sat, 11 Apr 2026 00:00:00 +0800"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>{saxutils.escape(name)} - X 搜索占位 RSS</title>
    <link>{saxutils.escape(original_url)}</link>
    <description>{saxutils.escape(description)}</description>
    <language>zh-cn</language>
    <lastBuildDate>{now_rfc822}</lastBuildDate>
    <item>
      <title>{saxutils.escape(name)} - 待人工确认账号</title>
      <link>{saxutils.escape(original_url)}</link>
      <guid>{saxutils.escape('x-search-' + slug)}</guid>
      <pubDate>{now_rfc822}</pubDate>
      <description><![CDATA[
      这是一条本地生成的占位 RSS。原始链接是 X 用户搜索页，尚未在名单里确认唯一账号。
      <br><br>人物：{saxutils.escape(name)}
      <br>机构 / 背景：{saxutils.escape(org)}
      <br>方向：{saxutils.escape(focus)}
      <br>目录分组：{saxutils.escape(section_title)}
      <br><br>你可以先点开原始搜索页继续人工核对。
      <br><a href="http://127.0.0.1:5050/people-directory#{saxutils.escape(section_id)}">返回人物目录</a>
      ]]></description>
    </item>
  </channel>
</rss>
"""
    target.write_text(xml, encoding="utf-8")
    return target


def main() -> None:
    soup = BeautifulSoup(HTML_PATH.read_text(encoding="utf-8"), "html.parser")
    section_titles = extract_section_titles(soup)
    rows: list[dict[str, str]] = []

    for th in soup.select("table thead tr th:last-child"):
        th.string = "RSS"

    for section in soup.select(".section[id]"):
        section_id = section["id"]
        section_title = section_titles.get(section_id, section_id)
        for tr in section.select("table tbody tr"):
            tds = tr.find_all("td")
            if len(tds) < 4:
                continue
            name = tds[0].get_text(" ", strip=True)
            org = tds[1].get_text(" ", strip=True)
            focus = tds[2].get_text(" ", strip=True)
            link = tds[3].find("a")
            if not link or not link.get("href"):
                continue
            original_url = link["href"].strip()
            original_title = (link.get("title") or "").strip()
            if original_title.startswith("原始链接: "):
                original_url = original_title.replace("原始链接: ", "", 1).strip()

            if original_url.startswith("https://x.com/search?"):
                rss_path = build_local_search_rss(
                    name=name,
                    original_url=original_url,
                    org=org,
                    focus=focus,
                    section_id=section_id,
                    section_title=section_title,
                )
                xml_url = rss_path.resolve().as_uri()
                html_url = original_url
                rss_label = "local-rss"
            else:
                path = urllib.parse.urlparse(original_url).path.strip("/")
                handle = path.split("/")[0]
                if handle in XCANCEL_FALLBACK_HANDLES:
                    xml_url = f"https://xcancel.com/{handle}/rss"
                    rss_label = f"xcancel:{handle}"
                else:
                    xml_url = f"https://nitter.net/{handle}/rss"
                    rss_label = f"nitter:{handle}"
                html_url = original_url

            link["href"] = xml_url
            link.string = rss_label
            link["title"] = f"原始链接: {original_url}"

            tags = ", ".join(
                [
                    "x-person",
                    "ai-people",
                    slugify(section_title),
                    slugify(org)[:40],
                ]
            )
            title = f"X人物 · {name}"
            rows.append(
                {
                    "title": title,
                    "xml_url": xml_url,
                    "html_url": html_url,
                    "tags": tags,
                }
            )

    HTML_PATH.write_text(str(soup), encoding="utf-8")

    conn = sqlite3.connect(DB_PATH)
    now = dt.datetime.utcnow().isoformat()
    inserted = 0
    updated = 0
    for row in rows:
        existing = conn.execute(
            "SELECT id, xml_url FROM feeds WHERE title = ? ORDER BY id",
            (row["title"],),
        ).fetchall()
        if existing:
            keep_id = existing[0][0]
            for dup_id, _ in existing[1:]:
                conn.execute("DELETE FROM entries WHERE feed_id = ?", (dup_id,))
                conn.execute("DELETE FROM feeds WHERE id = ?", (dup_id,))
            conn.execute(
                """
                UPDATE feeds
                SET xml_url = ?, html_url = ?, tags = ?
                WHERE id = ?
                """,
                (row["xml_url"], row["html_url"], row["tags"], keep_id),
            )
            updated += 1
            continue
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO feeds (title, xml_url, html_url, tags, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (row["title"], row["xml_url"], row["html_url"], row["tags"], now),
        )
        if cur.rowcount:
            inserted += 1
        else:
            conn.execute(
                """
                UPDATE feeds
                SET title = ?, html_url = ?, tags = ?
                WHERE xml_url = ?
                """,
                (row["title"], row["html_url"], row["tags"], row["xml_url"]),
            )
            updated += 1
    conn.commit()
    conn.close()

    print(f"processed={len(rows)} inserted={inserted} updated={updated}")


if __name__ == "__main__":
    main()
