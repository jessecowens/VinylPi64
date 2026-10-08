from __future__ import annotations

import re
import time
import unicodedata
from collections.abc import Iterable
from typing import Any

from vinylpi.core.database import get_connection, init_db
from vinylpi.core.title_variants import canonicalize_title

_DISCOGS_ARTIST_SUFFIX = re.compile(r"\s*\(\d+\)\s*$")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_SORTED_ARTICLE = re.compile(
    r"^(?P<artist>.+?)\s*[,;]\s*(?P<article>the|an|a)(?P<suffix>\s*\(\d+\))?\s*$", re.IGNORECASE,
)


def display_artist_name(value: str | None) -> str:
    """Convert legacy Discogs sort names ("Smiths; The") to display names.

    No database migration is needed: older releases and tracks are normalized
    as they are read. Unrelated artist names remain untouched.
    """
    name = (value or "").strip()
    match = _SORTED_ARTICLE.fullmatch(name)
    return f"{match.group('article')} {match.group('artist')}{match.group('suffix') or ''}" if match else name


def normalize_text(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold().replace("&", " and ")
    text = _NON_ALNUM.sub(" ", text)
    return " ".join(text.split())


def normalize_artist(value: str | None) -> str:
    text = _DISCOGS_ARTIST_SUFFIX.sub("", value or "")
    text = display_artist_name(text)
    text = re.sub(r"\s+(feat\.?|ft\.?|featuring)\s+.*$", "", text, flags=re.IGNORECASE)
    return normalize_text(text)


def upsert_release_summary(data: dict[str, Any]) -> None:
    init_db()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO discogs_releases (
                release_id, instance_id, folder_id, title, artist, year, country,
                label, catalog_number, format_text, thumb_url, cover_url,
                date_added, details_loaded, synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(release_id) DO UPDATE SET
                instance_id = excluded.instance_id,
                folder_id = excluded.folder_id,
                title = excluded.title,
                artist = excluded.artist,
                year = excluded.year,
                country = COALESCE(excluded.country, discogs_releases.country),
                label = COALESCE(excluded.label, discogs_releases.label),
                catalog_number = COALESCE(excluded.catalog_number, discogs_releases.catalog_number),
                format_text = excluded.format_text,
                thumb_url = COALESCE(NULLIF(excluded.thumb_url, ''), discogs_releases.thumb_url),
                cover_url = COALESCE(NULLIF(excluded.cover_url, ''), discogs_releases.cover_url),
                date_added = excluded.date_added,
                synced_at = excluded.synced_at
            """,
            (
                int(data["release_id"]),
                data.get("instance_id"),
                data.get("folder_id"),
                data.get("title") or "Unknown release",
                display_artist_name(data.get("artist")) or "Unknown artist",
                data.get("year"),
                data.get("country"),
                data.get("label"),
                data.get("catalog_number"),
                data.get("format_text"),
                data.get("thumb_url"),
                data.get("cover_url"),
                data.get("date_added"),
                int(bool(data.get("details_loaded", False))),
                int(data.get("synced_at") or time.time()),
            ),
        )


def replace_release_details(release: dict[str, Any], tracks: Iterable[dict[str, Any]]) -> None:
    init_db()
    release_id = int(release["release_id"])
    now = int(time.time())
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO discogs_releases (
                release_id, instance_id, folder_id, title, artist, year, country,
                label, catalog_number, format_text, thumb_url, cover_url,
                date_added, details_loaded, synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(release_id) DO UPDATE SET
                instance_id = COALESCE(excluded.instance_id, discogs_releases.instance_id),
                folder_id = COALESCE(excluded.folder_id, discogs_releases.folder_id),
                title = excluded.title,
                artist = excluded.artist,
                year = excluded.year,
                country = excluded.country,
                label = excluded.label,
                catalog_number = excluded.catalog_number,
                format_text = excluded.format_text,
                thumb_url = COALESCE(NULLIF(excluded.thumb_url, ''), discogs_releases.thumb_url),
                cover_url = COALESCE(NULLIF(excluded.cover_url, ''), discogs_releases.cover_url),
                date_added = COALESCE(excluded.date_added, discogs_releases.date_added),
                details_loaded = 1,
                synced_at = excluded.synced_at
            """,
            (
                release_id,
                release.get("instance_id"),
                release.get("folder_id"),
                release.get("title") or "Unknown release",
                display_artist_name(release.get("artist")) or "Unknown artist",
                release.get("year"),
                release.get("country"),
                release.get("label"),
                release.get("catalog_number"),
                release.get("format_text"),
                release.get("thumb_url"),
                release.get("cover_url"),
                release.get("date_added"),
                now,
            ),
        )
        conn.execute("DELETE FROM discogs_tracks WHERE release_id = ?", (release_id,))
        conn.executemany(
            """
            INSERT INTO discogs_tracks (
                release_id, track_index, position, side, title, artist,
                duration_seconds, normalized_title, normalized_artist
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    release_id,
                    int(track["track_index"]),
                    track.get("position"),
                    track.get("side"),
                    track.get("title") or "Unknown track",
                    display_artist_name(track.get("artist") or release.get("artist")) or "Unknown artist",
                    track.get("duration_seconds"),
                    normalize_text(canonicalize_title(track.get("title") or "")),
                    normalize_artist(track.get("artist") or release.get("artist")),
                )
                for track in tracks
            ],
        )


def get_detailed_release_ids() -> set[int]:
    init_db()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT release_id FROM discogs_releases WHERE details_loaded = 1"
        ).fetchall()
    return {int(row["release_id"]) for row in rows}


def delete_releases_not_in(release_ids: set[int]) -> None:
    init_db()
    with get_connection() as conn:
        if not release_ids:
            conn.execute("DELETE FROM discogs_releases")
            return
        placeholders = ",".join("?" for _ in release_ids)
        conn.execute(
            f"DELETE FROM discogs_releases WHERE release_id NOT IN ({placeholders})",
            tuple(sorted(release_ids)),
        )


def set_sync_state(
    *,
    username: str | None = None,
    status: str,
    message: str | None = None,
    error: str | None = None,
    failed_releases: int | None = None,
    completed: bool = False,
) -> None:
    init_db()
    with get_connection() as conn:
        counts = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM discogs_releases) AS releases_count,
                (SELECT COUNT(*) FROM discogs_tracks) AS tracks_count
            """
        ).fetchone()
        conn.execute(
            """
            UPDATE discogs_sync_state
            SET username = COALESCE(?, username),
                status = ?,
                message = ?,
                last_error = ?,
                releases_count = ?,
                tracks_count = ?,
                failed_releases = COALESCE(?, failed_releases),
                last_synced_at = CASE WHEN ? THEN strftime('%s', 'now') ELSE last_synced_at END,
                updated_at = strftime('%s', 'now')
            WHERE id = 1
            """,
            (
                username,
                status,
                message,
                error,
                int(counts["releases_count"] or 0),
                int(counts["tracks_count"] or 0),
                failed_releases,
                int(completed),
            ),
        )


def get_sync_state() -> dict[str, Any]:
    init_db()
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM discogs_sync_state WHERE id = 1").fetchone()
    return {key: row[key] for key in row.keys()} if row else {}


def get_collection_counts() -> dict[str, int]:
    init_db()
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM discogs_releases) AS releases,
                (SELECT COUNT(*) FROM discogs_tracks) AS tracks
            """
        ).fetchone()
    return {"releases": int(row["releases"] or 0), "tracks": int(row["tracks"] or 0)}


def get_collection_folders(username: str = "") -> list[dict[str, Any]]:
    """List locally selectable Discogs folders, including folders with no records.

    Folder 0 is Discogs' virtual "All" folder and folder 1 represents
    uncategorized entries. Older collection rows without an ID are treated as
    uncategorized. Counts reflect locally indexed *unique releases*.
    """
    init_db()
    with get_connection() as conn:
        counts = {
            int(row["folder_id"]): int(row["record_count"])
            for row in conn.execute(
                """SELECT COALESCE(folder_id, 1) AS folder_id, COUNT(*) AS record_count
                   FROM discogs_releases GROUP BY COALESCE(folder_id, 1)"""
            ).fetchall()
        }
        total = sum(counts.values())
        names = {
            int(row["folder_id"]): str(row["name"])
            for row in conn.execute(
                "SELECT folder_id, name FROM discogs_folders WHERE username = ? COLLATE NOCASE",
                (username,),
            ).fetchall()
        } if username else {}

    # Include known empty folders. Omit the virtual All folder from the
    # per-folder list; it is represented by the first entry below.
    folder_ids = (set(counts) | set(names)) - {0}
    folders = [{"id": 0, "name": "All records", "count": total}]
    entries = [
        {"id": folder_id, "name": names.get(folder_id) or (
            "Uncategorized" if folder_id == 1 else f"Folder {folder_id}"
        ), "count": counts.get(folder_id, 0)}
        for folder_id in folder_ids
    ]
    entries.sort(key=lambda item: (item["id"] == 1, item["name"].casefold(), item["id"]))
    return folders + entries


def get_cached_collection_folder_timestamp(username: str) -> int | None:
    """Return the age marker for the last successful folder-name lookup."""
    if not username:
        return None
    init_db()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT MAX(fetched_at) AS fetched_at FROM discogs_folders WHERE username = ? COLLATE NOCASE",
            (username,),
        ).fetchone()
    return int(row["fetched_at"]) if row and row["fetched_at"] is not None else None


def save_collection_folder_names(username: str, folders: list[dict[str, Any]]) -> None:
    """Cache Discogs folder labels without changing the synced release data."""
    if not username or not folders:
        return
    now = int(time.time())
    names = []
    for folder in folders:
        try:
            folder_id = int(folder["id"])
            name = str(folder.get("name") or "").strip()
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if 0 <= folder_id <= 9223372036854775807 and name:
            names.append((folder_id, username, name, now))
    if not names:
        return
    init_db()
    with get_connection() as conn:
        # The database is profile-scoped. Invalidate labels from an old account
        # if this profile was connected to a different Discogs account.
        conn.execute("DELETE FROM discogs_folders")
        conn.executemany(
            "INSERT INTO discogs_folders (folder_id, username, name, fetched_at) VALUES (?, ?, ?, ?)",
            names,
        )


def get_random_release(
    exclude_release_id: int | None = None,
    folder_id: int | None = None,
) -> dict[str, Any] | None:
    """Pick a release from a folder, avoiding an immediate repeat if possible.

    ``None`` or Discogs folder 0 selects all records; a nonzero ID restricts
    both the choice and collection_count to that folder. No network calls.
    """
    init_db()
    folder_id = None if folder_id in (None, 0) else int(folder_id)
    where = "" if folder_id is None else "WHERE COALESCE(folder_id, 1) = ?"
    params = (exclude_release_id,) if folder_id is None else (folder_id, folder_id, exclude_release_id)
    with get_connection() as conn:
        row = conn.execute(
            f"""
            SELECT release_id, title, artist,
                COALESCE(NULLIF(cover_url, ''), thumb_url) AS cover_url,
                (SELECT COUNT(*) FROM discogs_releases {where}) AS collection_count
            FROM discogs_releases
            {where}
            ORDER BY (release_id = ?), RANDOM()
            LIMIT 1
            """,
            params,
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    result["artist"] = display_artist_name(result.get("artist"))
    return result


def _track_row(row: Any) -> dict[str, Any]:
    """Return artist names in display order, including pre-fix cached rows."""
    result = dict(row)
    result["track_artist"] = display_artist_name(result.get("track_artist"))
    result["release_artist"] = display_artist_name(result.get("release_artist"))
    # Old DBs also hold an obsolete normalized_artist ('smiths the'). Derive
    # it from the display name so matching stays correct without a resync.
    result["normalized_artist"] = normalize_artist(
        result.get("track_artist") or result.get("release_artist")
    )
    return result


_TRACK_SELECT = """
    SELECT
        t.release_id, t.track_index, t.position, t.side, t.title AS track_title,
        t.artist AS track_artist, t.duration_seconds, t.normalized_title,
        t.normalized_artist, r.title AS release_title, r.artist AS release_artist,
        r.year, r.country, r.label, r.catalog_number, r.format_text,
        COALESCE(NULLIF(r.cover_url, ''), r.thumb_url) AS cover_url
    FROM discogs_tracks t
    JOIN discogs_releases r ON r.release_id = t.release_id
"""


def find_exact_title_tracks(normalized_title: str, limit: int = 100) -> list[dict[str, Any]]:
    if not normalized_title:
        return []
    init_db()
    with get_connection() as conn:
        rows = conn.execute(
            _TRACK_SELECT + " WHERE t.normalized_title = ? ORDER BY t.release_id, t.track_index LIMIT ?",
            (normalized_title, int(limit)),
        ).fetchall()
    return [_track_row(row) for row in rows]


def find_compact_title_tracks(normalized_title: str, limit: int = 100) -> list[dict[str, Any]]:
    """Find collection tracks while ignoring whitespace in the normalized title.

    Shazam and Discogs occasionally disagree only about word boundaries, e.g.
    ``highschool`` vs. ``high school``.  Normalization deliberately keeps spaces,
    so an exact lookup alone misses those otherwise unambiguous collection hits.
    """
    compact_title = (normalized_title or "").replace(" ", "")
    if not compact_title:
        return []
    init_db()
    with get_connection() as conn:
        rows = conn.execute(
            _TRACK_SELECT
            + " WHERE REPLACE(t.normalized_title, ' ', '') = ? "
              "ORDER BY t.release_id, t.track_index LIMIT ?",
            (compact_title, int(limit)),
        ).fetchall()
    return [_track_row(row) for row in rows]


def get_release_tracks(release_id: int) -> list[dict[str, Any]]:
    init_db()
    with get_connection() as conn:
        rows = conn.execute(
            _TRACK_SELECT + " WHERE t.release_id = ? ORDER BY t.track_index",
            (int(release_id),),
        ).fetchall()
    return [_track_row(row) for row in rows]


def get_release_track(release_id: int, track_index: int) -> dict[str, Any] | None:
    init_db()
    with get_connection() as conn:
        row = conn.execute(
            _TRACK_SELECT + " WHERE t.release_id = ? AND t.track_index = ?",
            (int(release_id), int(track_index)),
        ).fetchone()
    return _track_row(row) if row else None


def get_next_track(release_id: int, track_index: int) -> dict[str, Any] | None:
    init_db()
    with get_connection() as conn:
        row = conn.execute(
            _TRACK_SELECT
            + " WHERE t.release_id = ? AND t.track_index > ? ORDER BY t.track_index LIMIT 1",
            (int(release_id), int(track_index)),
        ).fetchone()
    return _track_row(row) if row else None


def get_track_counts(release_id: int, track_index: int, side: str | None) -> dict[str, int]:
    init_db()
    with get_connection() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM discogs_tracks WHERE release_id = ?",
            (int(release_id),),
        ).fetchone()["n"]
        if side:
            side_rows = conn.execute(
                """
                SELECT track_index FROM discogs_tracks
                WHERE release_id = ? AND side = ? ORDER BY track_index
                """,
                (int(release_id), side),
            ).fetchall()
            indices = [int(row["track_index"]) for row in side_rows]
        else:
            indices = []
    side_number = indices.index(int(track_index)) + 1 if int(track_index) in indices else 0
    return {
        "track_count": int(total or 0),
        "side_track_number": side_number,
        "side_track_count": len(indices),
    }
