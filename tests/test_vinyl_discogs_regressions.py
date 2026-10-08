"""Regression tests for album identity, recognition failures, and metadata ownership."""
from __future__ import annotations

from contextlib import ExitStack
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

from vinylpi.core import database
from vinylpi.core.discogs_db import (
    display_artist_name, find_exact_title_tracks, get_next_track,
    get_random_release, get_release_tracks, normalize_artist,
    replace_release_details, upsert_release_summary,
)
from vinylpi.core.discogs_matcher import (
    apply_discogs_match, get_side_flip_prompt, infer_expected_next_track,
)
from vinylpi.core.discogs_service import collection_summary, parse_release_details
from vinylpi.core.loop_logic import (
    handle_song_result, should_ignore_live_album_mismatch, update_album_session_on_switch,
)
from vinylpi.core.loop_state import AlbumState, DiscogsPlaybackState, DisplayState, LoopConfig
from vinylpi.core.models import RecognizedTrack
from vinylpi.core.title_variants import normalize_album_name


class AlbumLockRegressionTests(unittest.TestCase):
    def test_album_identity_ignores_capitalization_and_surrounding_spaces(self):
        self.assertEqual(normalize_album_name(" Echoes At Dawn  "), "echoes at dawn")
        self.assertFalse(should_ignore_live_album_mismatch(
            "Opening Track (Radio Session)", "Echoes at Dawn", "Echoes At Dawn",
        ))
        self.assertFalse(should_ignore_live_album_mismatch(
            "Opening Track (Radio Session)", "  ECHOES AT DAWN ", "Echoes at Dawn",
        ))
        self.assertTrue(should_ignore_live_album_mismatch(
            "Opening Track (Radio Session)", "Some Other Album", "Echoes At Dawn",
        ))
        self.assertFalse(should_ignore_live_album_mismatch(
            "Ordinary Song", "Other Album", "Echoes At Dawn",
        ))

    @patch("vinylpi.core.loop_logic._increment_album_session")
    def test_album_session_and_candidate_are_case_insensitive(self, increment):
        st = AlbumState()
        for title, album in (("One", "Echoes At Dawn"), ("Two", "echoes at dawn")):
            update_album_session_on_switch(
                st=st, album=album, title=title, min_tracks=2, min_consecutive=2,
            )
        self.assertEqual(st.current_album, "Echoes At Dawn")
        self.assertTrue(st.current_album_session_counted)
        increment.assert_called_once_with("Echoes At Dawn")
        update_album_session_on_switch(st=st, album="Another", title="Three", min_tracks=2, min_consecutive=2)
        self.assertEqual(st.candidate_streak, 1)
        update_album_session_on_switch(st=st, album="ANOTHER", title="Three", min_tracks=2, min_consecutive=2)
        self.assertEqual(st.current_album, "ANOTHER")
        self.assertIsNone(st.candidate_album)
        increment.assert_called_once()

    def test_successful_shazam_session_track_never_generates_fallback_or_side_flip(self):
        # main_loop itself is exercised (rather than only its pure helper).
        # Stub Raspberry-Pi-specific optional modules when running on a host.
        originally = set(sys.modules)
        with patch.dict(sys.modules, {"sounddevice": MagicMock(), "shazamio": MagicMock()}):
            from vinylpi.core import runner

        self.addCleanup(lambda: [sys.modules.pop(key, None) for key in (
            "vinylpi.core.runner", "vinylpi.core.audio_capture",
            "vinylpi.core.recognition", "vinylpi.integrations.shazam_client",
        ) if key not in originally])

        display = DisplayState(consecutive_failures=19)
        album = AlbumState(current_album="Echoes At Dawn", current_album_session_counted=True)
        track = RecognizedTrack(
            artist="The Horizons", title="Opening Track (Radio Session)",
            album="Echoes at Dawn", cover_url="https://example.test/art.jpg",
            cover_image=Image.new("RGB", (4, 4), "blue"),
        )
        pickup = MagicMock()
        pickup.config.enabled = False
        audio_sleep = MagicMock()
        audio_sleep.config.enabled = False
        cfg = LoopConfig(delay=0, fallback_allowed_failures=3, auto_sleep=25)
        cfg_payload = {"discogs": {"enabled": False}}

        with ExitStack() as stack:
            def replace(mod, name, **kwargs):
                return stack.enter_context(patch.object(mod, name, **kwargs))

            replace(runner, "initialize_storage")
            replace(runner, "start_display_refresh_watcher")
            replace(runner, "restore_last_vinyl_song")
            replace(runner, "read_config", return_value=cfg_payload)
            replace(runner, "maybe_log_config_reload", return_value=False)
            replace(runner, "LoopConfig").from_config.return_value = cfg
            replace(runner, "PickupUsageConfig").from_config.return_value = None
            replace(runner, "VinylAudioSleepConfig").from_config.return_value = None
            replace(runner, "PickupUsageTracker", return_value=pickup)
            replace(runner, "LowAudioSleepTracker", return_value=audio_sleep)
            replace(runner, "DisplayState", return_value=display)
            replace(runner, "AlbumState", return_value=album)
            replace(runner, "record_sample", return_value=b"audio")
            shazam = replace(runner, "recognize_song", return_value=track)
            replace(runner, "apply_discogs_match", side_effect=lambda t, *_args, **_kwargs: t)
            replace(runner, "discogs_transition_ready", return_value=True)
            replace(runner, "update_song_stats_on_switch", return_value=False)
            replace(runner, "update_album_session_on_switch")
            side_flip = replace(runner, "get_side_flip_prompt")
            no_result = replace(runner, "handle_no_result")
            fallback = replace(runner, "show_fallback_image")
            with patch("vinylpi.core.loop_logic.start_scrolling_display") as pixoo, \
                 patch("vinylpi.core.loop_logic.send_rgb"), \
                 patch("vinylpi.core.loop_logic.write_status") as status:
                replace(runner.time, "sleep", side_effect=[None, None, KeyboardInterrupt])
                with self.assertRaises(KeyboardInterrupt):
                    runner.main_loop()

                self.assertEqual(shazam.call_count, 3)
                self.assertEqual(display.consecutive_failures, 0)
                no_result.assert_not_called()
                side_flip.assert_not_called()
                fallback.assert_not_called()
                pixoo.assert_called_once()
                self.assertEqual(status.call_args.args[0], "The Horizons")

    def test_rejected_actual_live_variant_does_not_trigger_fallback(self):
        originally = set(sys.modules)
        with patch.dict(sys.modules, {"sounddevice": MagicMock(), "shazamio": MagicMock()}):
            from vinylpi.core import runner
        self.addCleanup(lambda: [sys.modules.pop(key, None) for key in (
            "vinylpi.core.runner", "vinylpi.core.audio_capture",
            "vinylpi.core.recognition", "vinylpi.integrations.shazam_client",
        ) if key not in originally])
        display = DisplayState(consecutive_failures=19)
        album = AlbumState(current_album="Echoes At Dawn", current_album_session_counted=True)
        track = RecognizedTrack(
            artist="The Horizons", title="Opening Track (Radio Session)",
            album="Different Live Album", cover_url="https://example.test/art.jpg",
            cover_image=Image.new("RGB", (4, 4), "blue"),
        )
        pickup, standby = MagicMock(), MagicMock()
        pickup.config.enabled = standby.config.enabled = False
        with ExitStack() as stack:
            for name, value in (
                ("initialize_storage", None), ("start_display_refresh_watcher", None),
                ("restore_last_vinyl_song", None),
            ):
                stack.enter_context(patch.object(runner, name))
            for name, value in (
                ("read_config", {"discogs": {"enabled": False}}),
                ("maybe_log_config_reload", False),
                ("PickupUsageTracker", pickup), ("LowAudioSleepTracker", standby),
                ("DisplayState", display), ("AlbumState", album),
                ("record_sample", b"audio"), ("recognize_song", track),
            ):
                stack.enter_context(patch.object(runner, name, return_value=value))
            stack.enter_context(patch.object(runner, "LoopConfig")).from_config.return_value = LoopConfig(delay=0)
            stack.enter_context(patch.object(runner, "PickupUsageConfig")).from_config.return_value = None
            stack.enter_context(patch.object(runner, "VinylAudioSleepConfig")).from_config.return_value = None
            stack.enter_context(patch.object(runner, "apply_discogs_match", side_effect=lambda t, *_args, **_kwargs: t))
            no_result = stack.enter_context(patch.object(runner, "handle_no_result"))
            prompt = stack.enter_context(patch.object(runner, "get_side_flip_prompt"))
            display_song = stack.enter_context(patch.object(runner, "handle_song_result"))
            stack.enter_context(patch.object(runner.time, "sleep", side_effect=[None, KeyboardInterrupt]))
            with self.assertRaises(KeyboardInterrupt):
                runner.main_loop()
            self.assertEqual(display.consecutive_failures, 0)
            no_result.assert_not_called()
            prompt.assert_not_called()
            display_song.assert_not_called()


class DiscogsArtistRegressionTests(unittest.TestCase):
    def test_article_sort_forms_and_matching_names(self):
        for raw, expected in (
            ("Horizons, The", "The Horizons"), ("Horizons; The", "The Horizons"),
            ("Wanderers, The", "The Wanderers"), ("Horizons; The (2)", "The Horizons (2)"),
            ("The Horizons", "The Horizons"),
            ("Ancient Signals", "Ancient Signals"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(display_artist_name(raw), expected)
        self.assertEqual(normalize_artist("Horizons; The"), normalize_artist("The Horizons"))
        self.assertEqual(normalize_artist("Horizons; The (2)"), normalize_artist("The Horizons"))

    def test_sync_prefers_regular_artist_objects_over_sort_name(self):
        summary = collection_summary({
            "id": 10,
            "basic_information": {
                "title": "Echoes At Dawn", "artists_sort": "Horizons, The",
                "artists": [{"name": "The Horizons"}],
            },
        })
        self.assertEqual(summary["artist"], "The Horizons")
        release, tracks = parse_release_details({
            "title": "Echoes At Dawn", "artists_sort": "Horizons; The",
            "artists": [{"name": "The Horizons"}],
            "tracklist": [
                {"title": "Opening Track", "position": "B1", "artists_sort": "Horizons, The"},
                {"title": "Second Track", "position": "B2", "artists_sort": "Wanderers, The", "artists": [{"name": "The Horizons"}]},
            ],
        }, summary)
        self.assertEqual(release["artist"], "The Horizons")
        self.assertEqual([t["artist"] for t in tracks], ["The Horizons", "The Horizons"])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch("vinylpi.core.database.get_active_db_path", return_value=Path(self.temp.name) / "test.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(database._INITIALIZED_PATHS.clear)

    def test_missing_shazam_album_may_use_matched_release_as_fallback(self):
        release = {"release_id": 44, "title": "Evening Echoes", "artist": "The Horizons"}
        upsert_release_summary(release)
        replace_release_details(release, [
            {"track_index": 0, "title": "First Signal", "artist": "Horizons; The", "position": "A1"},
        ])
        track = RecognizedTrack(
            artist="The Horizons", title="First Signal", album=None,
            cover_image=Image.new("RGB", (2, 2)), cover_url="https://example.test/shazam.png",
        )
        matched = apply_discogs_match(track, DiscogsPlaybackState(), {"discogs": {"enabled": True}})
        self.assertEqual(matched.artist, "The Horizons")
        self.assertEqual(matched.title, "First Signal")
        self.assertEqual(matched.album, "Evening Echoes")
        self.assertEqual(matched.discogs_release_id, 44)

    def test_legacy_discogs_sort_names_are_comparison_only(self):
        release = {
            "release_id": 12, "title": "Echoes At Dawn", "artist": "The Horizons",
            "cover_url": "https://example.test/cover.jpg",
        }
        upsert_release_summary(release)
        replace_release_details(release, [
            {"track_index": 0, "title": "Opening Track", "artist": "The Horizons", "position": "B2", "side": "B"},
            {"track_index": 1, "title": "Second Track", "artist": "The Horizons", "position": "B3", "side": "B"},
        ])
        # Simulate collection already synchronized by a previous VinylPi version.
        with database.get_connection() as conn:
            conn.execute("UPDATE discogs_releases SET artist = 'Horizons; The'")
            conn.execute("UPDATE discogs_tracks SET artist = 'Horizons, The', normalized_artist = 'horizons the'")

        random_record = get_random_release()
        self.assertEqual(random_record["artist"], "The Horizons")
        tracks = get_release_tracks(12)
        # Stored Discogs names are retained as-is, including old sort forms.
        # Only the comparison key is normalized for reliable matching.
        self.assertEqual(tracks[0]["track_artist"], "Horizons, The")
        self.assertEqual(tracks[0]["release_artist"], "Horizons; The")
        self.assertEqual(tracks[0]["normalized_artist"], "the horizons")
        self.assertEqual(get_next_track(12, 0)["track_artist"], "Horizons, The")
        self.assertEqual(find_exact_title_tracks("opening track")[0]["track_artist"], "Horizons, The")

        recognized = RecognizedTrack(
            artist="THE Horizons", title="Opening TRACK", album="Echoes at dawn",
            cover_image=Image.new("RGB", (2, 2), "blue"), cover_url="https://example.test/shazam.png",
        )
        matched = apply_discogs_match(
            recognized, DiscogsPlaybackState(),
            {"discogs": {"enabled": True, "prefer_collection": True}},
        )
        self.assertEqual(matched.artist, "THE Horizons")
        self.assertEqual(matched.title, "Opening TRACK")
        self.assertEqual(matched.album, "Echoes at dawn")
        self.assertEqual(matched.discogs_expected_next_artist, "The Horizons")
        self.assertEqual(matched.cover_url, "https://example.test/shazam.png")
        self.assertEqual(matched.discogs_release_id, 12)
        self.assertEqual(matched.discogs_confidence, 1.0)

        # Both Pixoo and dashboard/status must receive the Shazam values,
        # not the Discogs sort form or the canonical statistics title.
        with patch("vinylpi.core.loop_logic.start_scrolling_display") as pixoo, \
             patch("vinylpi.core.loop_logic.write_status") as status, \
             patch("vinylpi.core.loop_logic.send_rgb"):
            handle_song_result(LoopConfig(), DisplayState(), False, matched)
        self.assertEqual(pixoo.call_args.args[1:4], ("THE Horizons", "Opening TRACK", "Echoes at dawn"))
        self.assertEqual(status.call_args.args[0:2], ("THE Horizons", "Opening TRACK"))
        self.assertEqual(status.call_args.kwargs["album"], "Echoes at dawn")
        self.assertEqual(status.call_args.kwargs["discogs_release_id"], 12)

        playback = DiscogsPlaybackState(
            active_release_id=12, current_track_index=0, current_side="B",
            current_started_at=0, current_duration_seconds=60,
        )
        with patch("vinylpi.core.discogs_matcher.load_image", return_value=Image.new("RGB", (2, 2))):
            inferred = infer_expected_next_track(
                playback, {"discogs": {"enabled": True}}, consecutive_failures=2,
            )
        self.assertIsNotNone(inferred)
        self.assertEqual(inferred.artist, "The Horizons")
        self.assertEqual(inferred.discogs_release_id, 12)


if __name__ == "__main__":
    unittest.main()
