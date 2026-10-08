"""Regression coverage for locally filtered random releases and folder labels."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vinylpi.core import database
from vinylpi.core.discogs_db import (
    get_cached_collection_folder_timestamp,
    get_collection_folders,
    get_random_release,
    save_collection_folder_names,
    upsert_release_summary,
)
from vinylpi.integrations.discogs_client import DiscogsClient, DiscogsError
from vinylpi.profiles import (
    get_profile_storage_override,
    reset_profile_storage_override,
    set_profile_storage_override,
)


class DiscogsRandomFolderDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(database._INITIALIZED_PATHS.clear)
        token = set_profile_storage_override("profile-a")
        self.addCleanup(reset_profile_storage_override, token)
        patcher = patch("vinylpi.core.database.get_active_db_path", side_effect=lambda: (
            Path(self.temp.name) / f"{get_profile_storage_override() or 'default'}.db"
        ))
        patcher.start()
        self.addCleanup(patcher.stop)

    def add_release(self, release_id: int, folder_id: int | None) -> None:
        upsert_release_summary({
            "release_id": release_id,
            "folder_id": folder_id,
            "title": f"Album {release_id}",
            "artist": "Horizons; The",
        })

    def test_empty_collection_has_all_option(self):
        self.assertEqual(get_collection_folders("example"), [
            {"id": 0, "name": "All records", "count": 0},
        ])
        self.assertIsNone(get_random_release(folder_id=12))

    def test_folder_counts_and_random_selection_are_local(self):
        self.add_release(1, 12)
        self.add_release(2, 12)
        self.add_release(3, 8)
        self.add_release(4, None)  # legacy entry with no folder ID
        folders = get_collection_folders()
        self.assertEqual({row["id"]: row["count"] for row in folders}, {0: 4, 1: 1, 8: 1, 12: 2})
        self.assertEqual(get_random_release()["collection_count"], 4)
        self.assertEqual(get_random_release(folder_id=0)["collection_count"], 4)
        self.assertEqual(get_random_release(folder_id=12)["collection_count"], 2)
        self.assertEqual(get_random_release(folder_id=8)["release_id"], 3)
        self.assertEqual(get_random_release(folder_id=1)["release_id"], 4)
        self.assertIsNone(get_random_release(folder_id=77))

    def test_reroll_excludes_only_within_the_selected_folder(self):
        self.add_release(1, 12)
        self.add_release(2, 12)
        self.add_release(3, 8)
        for previous, expected in ((1, 2), (2, 1)):
            result = get_random_release(previous, folder_id=12)
            self.assertEqual(result["release_id"], expected)
            self.assertEqual(result["collection_count"], 2)
        # Single-record folder still returns that record after a reroll.
        self.assertEqual(get_random_release(3, folder_id=8)["release_id"], 3)
        self.assertEqual(get_random_release(3)["collection_count"], 3)

    def test_cached_names_include_empty_folders_and_survive_restart(self):
        self.add_release(1, 12)
        save_collection_folder_names("example", [
            {"id": 0, "name": "All"},
            {"id": 1, "name": "Uncategorized"},
            {"id": 12, "name": "Meine Sammlung"},
            {"id": 8, "name": "Alte Sammlung"},
        ])
        self.assertIsNotNone(get_cached_collection_folder_timestamp("example"))
        self.assertEqual(get_cached_collection_folder_timestamp("different"), None)
        database._INITIALIZED_PATHS.clear()
        folders = get_collection_folders("example")
        self.assertEqual([(f["id"], f["name"], f["count"]) for f in folders], [
            (0, "All records", 1),
            (8, "Alte Sammlung", 0),
            (12, "Meine Sammlung", 1),
            (1, "Uncategorized", 0),
        ])

    def test_no_resync_required_for_legacy_database_rows(self):
        self.add_release(10, 12)
        path = database._resolve_db_path()
        with database.get_connection() as conn:
            conn.execute("DROP TABLE discogs_folders")
        database._INITIALIZED_PATHS.clear()
        self.assertEqual(get_collection_folders("example")[1], {
            "id": 12, "name": "Folder 12", "count": 1,
        })
        save_collection_folder_names("example", [{"id": 12, "name": "Meine Sammlung"}])
        self.assertEqual(get_random_release(folder_id=12)["release_id"], 10)
        self.assertTrue(path.exists())

    def test_profile_isolation_for_folder_names_and_random_records(self):
        self.add_release(1, 12)
        save_collection_folder_names("example", [{"id": 12, "name": "Alte Sammlung"}])
        token = set_profile_storage_override("profile-b")
        try:
            self.add_release(2, 12)
            self.assertEqual(get_collection_folders("example")[1]["name"], "Folder 12")
            save_collection_folder_names("example", [{"id": 12, "name": "Meine Sammlung"}])
            self.assertEqual(get_random_release(folder_id=12)["release_id"], 2)
        finally:
            reset_profile_storage_override(token)
        self.assertEqual(get_collection_folders("example")[1]["name"], "Alte Sammlung")
        self.assertEqual(get_random_release(folder_id=12)["release_id"], 1)

    def test_new_account_does_not_inherit_cached_names(self):
        self.add_release(1, 12)
        save_collection_folder_names("old", [{"id": 12, "name": "Old folder"}])
        self.assertEqual(get_collection_folders("new")[1]["name"], "Folder 12")
        save_collection_folder_names("new", [{"id": 12, "name": "New folder"}])
        self.assertIsNone(get_cached_collection_folder_timestamp("old"))
        self.assertEqual(get_collection_folders("new")[1]["name"], "New folder")


class DiscogsFolderClientTests(unittest.TestCase):
    def test_folder_lookup_uses_discogs_endpoint_and_returns_list(self):
        client = DiscogsClient.__new__(DiscogsClient)
        with patch.object(client, "_get", return_value={"folders": [
            {"id": 0, "name": "All"}, {"id": 12, "name": "Meine Sammlung"}, None,
        ]}) as http:
            folders = client.get_collection_folders("my/username")
        self.assertEqual(folders, [{"id": 0, "name": "All"}, {"id": 12, "name": "Meine Sammlung"}])
        http.assert_called_once_with("/users/my%2Fusername/collection/folders")

    def test_malformed_folders_response_raises_error(self):
        client = DiscogsClient.__new__(DiscogsClient)
        with patch.object(client, "_get", return_value={"folders": None}):
            with self.assertRaises(DiscogsError):
                client.get_collection_folders("example")


if __name__ == "__main__":
    unittest.main()
