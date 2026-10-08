from __future__ import annotations

import time

from flask import Blueprint, jsonify, request

from vinylpi.config.runtime import read_config, write_config
from vinylpi.core.discogs_db import (
    get_cached_collection_folder_timestamp,
    get_collection_folders,
    get_random_release,
    get_release_tracks,
    get_sync_state,
    save_collection_folder_names,
)
from vinylpi.core.discogs_service import (
    DISCOGS_TOKEN_ENV,
    SYNC_MANAGER,
    get_discogs_token,
)
from vinylpi.integrations.discogs_client import DiscogsClient, DiscogsError
from vinylpi.web.services.source import get_mode

# Folder labels rarely change. Store them in the active profile's SQLite DB,
# including across restarts; a stale/offline lookup never blocks random picks.
_FOLDER_LABEL_TTL_SECONDS = 24 * 60 * 60


discogs_bp = Blueprint("discogs_api", __name__)


def _missing_token_response():
    return jsonify({
        "ok": False,
        "error": (
            f"Set {DISCOGS_TOKEN_ENV}=... in vinylpi.env and restart VinylPi first."
        ),
    }), 400


@discogs_bp.get("/api/discogs/status")
def api_discogs_status():
    return jsonify({"ok": True, **SYNC_MANAGER.status()})


@discogs_bp.get("/api/discogs/folders")
def api_discogs_folders():
    if get_mode() == "spotify":
        return jsonify({"ok": False, "error": "Switch to Vinyl or Off to choose a record."}), 409

    cfg = read_config()
    # The stored collection's account takes precedence over possibly changed
    # preferences, so labels cannot silently refer to a different collection.
    username = str(get_sync_state().get("username") or (cfg.get("discogs") or {}).get("username") or "").strip()
    token = get_discogs_token(cfg)
    cached_at = get_cached_collection_folder_timestamp(username)
    if token and username and (cached_at is None or time.time() - cached_at > _FOLDER_LABEL_TTL_SECONDS):
        try:
            folders = DiscogsClient(token, timeout=6.0).get_collection_folders(username)
            save_collection_folder_names(username, folders)
        except (DiscogsError, OSError, ValueError) as exc:
            # Local IDs and counts still work when Discogs is unreachable.
            print(f"Discogs folder labels unavailable, using local folder IDs: {exc}")

    response = jsonify({"ok": True, "folders": get_collection_folders(username)})
    response.cache_control.no_store = True
    return response


@discogs_bp.get("/api/discogs/random")
def api_discogs_random():
    if get_mode() == "spotify":
        return jsonify({"ok": False, "error": "Switch to Vinyl or Off to choose a record."}), 409

    previous = request.args.get("exclude_release_id")
    try:
        previous_id = int(previous) if previous is not None else None
        if previous_id is not None and not 1 <= previous_id <= 9223372036854775807:
            raise ValueError
    except ValueError:
        return jsonify({"ok": False, "error": "Invalid release ID."}), 400

    selected_folder = request.args.get("folder_id")
    try:
        folder_id = int(selected_folder) if selected_folder is not None else None
        if folder_id is not None and not 0 <= folder_id <= 9223372036854775807:
            raise ValueError
    except ValueError:
        return jsonify({"ok": False, "error": "Invalid folder ID."}), 400

    # Selection is entirely local and uses the browser's current profile.
    response = jsonify({"ok": True, "release": get_random_release(previous_id, folder_id=folder_id)})
    response.cache_control.no_store = True
    return response


@discogs_bp.get("/api/discogs/releases/<int:release_id>/tracklist")
def api_discogs_release_tracklist(release_id: int):
    tracks = get_release_tracks(release_id)
    if not tracks:
        return jsonify({"ok": False, "error": "release_not_found"}), 404

    first = tracks[0]
    payload_tracks = [
        {
            "track_index": int(track.get("track_index") or 0),
            "position": track.get("position") or "",
            "side": track.get("side") or "",
            "title": track.get("track_title") or "",
            "artist": track.get("track_artist") or track.get("release_artist") or "",
            "duration_seconds": track.get("duration_seconds"),
        }
        for track in tracks
    ]

    return jsonify(
        {
            "ok": True,
            "release_id": int(release_id),
            "title": first.get("release_title") or "",
            "artist": first.get("release_artist") or "",
            "track_count": len(payload_tracks),
            "tracks": payload_tracks,
        }
    )


@discogs_bp.post("/api/discogs/connect")
def api_discogs_connect():
    token = get_discogs_token()
    if not token:
        return _missing_token_response()

    try:
        identity = DiscogsClient(token).identity()
    except DiscogsError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    username = str(identity.get("username") or "").strip()
    if not username:
        return jsonify({"ok": False, "error": "Discogs did not return an account name."}), 502

    write_config(
        {
            "discogs": {
                "username": username,
                "enabled": True,
            }
        }
    )
    return jsonify({"ok": True, "username": username})


@discogs_bp.post("/api/discogs/sync")
def api_discogs_sync():
    cfg = read_config()
    if not get_discogs_token(cfg):
        return _missing_token_response()
    if not bool((cfg.get("discogs") or {}).get("enabled", False)):
        write_config({"discogs": {"enabled": True}})

    started = SYNC_MANAGER.start()
    if not started:
        return jsonify({"ok": True, "started": False, "message": "A Discogs sync is already running."})
    return jsonify({"ok": True, "started": True}), 202
