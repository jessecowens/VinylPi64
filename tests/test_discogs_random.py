from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vinylpi.core import database
from vinylpi.core.discogs_db import get_random_release, upsert_release_summary
from vinylpi.profiles import (
    get_profile_storage_override,
    reset_profile_storage_override,
    set_profile_storage_override,
)
from vinylpi.web.app import create_app


class RandomRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(database._INITIALIZED_PATHS.clear)
        token = set_profile_storage_override("profile-a")
        self.addCleanup(reset_profile_storage_override, token)
        patches = (
            patch("vinylpi.core.database.get_active_db_path", side_effect=lambda: (
                Path(self.temp.name) / f"{get_profile_storage_override() or 'default'}.db"
            )),
            patch("vinylpi.web.app.initialize_storage"),
            patch("vinylpi.web.app._session_secret", return_value="test-secret"),
            patch("vinylpi.web.app.profile_exists", return_value=True),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        mode_patcher = patch("vinylpi.web.routes.discogs_api.get_mode", return_value="off")
        self.mode = mode_patcher.start()
        self.addCleanup(mode_patcher.stop)
        app = create_app()
        app.config.update(TESTING=True)
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session["vinylpi_profile_id"] = "profile-a"

    def add_release(self, release_id=1, **fields):
        upsert_release_summary({
            "release_id": release_id, "title": f"Album {release_id}",
            "artist": "Artist", "cover_url": "https://i.discogs.com/cover.jpg", **fields,
        })

    def test_empty_collection_returns_none(self):
        self.assertIsNone(get_random_release())

    def test_summary_without_track_details_can_be_suggested(self):
        self.add_release()
        record = get_random_release()
        self.assertEqual(record, {
            "release_id": 1, "title": "Album 1", "artist": "Artist",
            "cover_url": "https://i.discogs.com/cover.jpg", "collection_count": 1,
        })

    def test_reroll_avoids_previous_release_when_alternatives_exist(self):
        self.add_release(1)
        self.add_release(2)
        self.assertEqual(get_random_release(1)["release_id"], 2)
        self.assertEqual(get_random_release(2)["release_id"], 1)
        self.assertEqual(get_random_release(1)["collection_count"], 2)

    def test_reroll_still_returns_single_record(self):
        self.add_release(1)
        self.assertEqual(get_random_release(1)["release_id"], 1)

    def test_excluding_missing_release_does_not_hide_collection(self):
        self.add_release(1)
        self.assertEqual(get_random_release(999)["release_id"], 1)

    def test_cover_falls_back_to_thumbnail(self):
        self.add_release(cover_url="", thumb_url="https://i.discogs.com/thumb.jpg")
        self.assertEqual(get_random_release()["cover_url"], "https://i.discogs.com/thumb.jpg")

    def test_missing_cover_does_not_exclude_record(self):
        self.add_release(cover_url=None)
        self.assertIsNone(get_random_release()["cover_url"])

    @patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected network request"))
    def test_api_reads_local_collection_in_off_and_vinyl_modes(self, outbound_request):
        self.add_release(1)
        self.add_release(2)
        for mode in ("off", "vinyl"):
            with self.subTest(mode=mode):
                self.mode.return_value = mode
                response = self.client.get("/api/discogs/random?exclude_release_id=1")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()["release"]["release_id"], 2)
                self.assertTrue(response.cache_control.no_store)
        outbound_request.assert_not_called()

    def test_api_empty_collection_has_explicit_empty_result(self):
        response = self.client.get("/api/discogs/random")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True, "release": None})

    def test_api_rejects_invalid_exclusion_ids(self):
        for value in ("", "abc", "0", "-1", "1.5", "1 OR 1=1", str(2**63)):
            with self.subTest(value=value):
                response = self.client.get("/api/discogs/random", query_string={"exclude_release_id": value})
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.get_json()["ok"])

    @patch("vinylpi.web.routes.discogs_api.get_random_release")
    def test_api_rejects_spotify_mode_without_selecting_record(self, select):
        self.mode.return_value = "spotify"
        response = self.client.get("/api/discogs/random")
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.get_json()["ok"])
        select.assert_not_called()

    def test_browser_sessions_only_receive_their_own_collection(self):
        self.add_release(1, title="Profile A album")
        token = set_profile_storage_override("profile-b")
        try:
            self.add_release(2, title="Profile B album")
        finally:
            reset_profile_storage_override(token)
        second = self.client.application.test_client()
        with second.session_transaction() as session:
            session["vinylpi_profile_id"] = "profile-b"
        self.assertEqual(self.client.get("/api/discogs/random").get_json()["release"]["title"], "Profile A album")
        self.assertEqual(second.get("/api/discogs/random").get_json()["release"]["title"], "Profile B album")


if __name__ == "__main__":
    unittest.main()
