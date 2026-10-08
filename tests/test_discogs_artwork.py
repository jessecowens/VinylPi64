from contextlib import ExitStack
from io import BytesIO
import unittest
from unittest.mock import Mock, patch

from PIL import Image
import requests

from vinylpi.core.discogs_matcher import _load_discogs_cover, apply_discogs_match
from vinylpi.core.loop_state import DiscogsPlaybackState
from vinylpi.core.models import RecognizedTrack
from vinylpi.integrations.discogs_client import USER_AGENT


class DiscogsArtworkTests(unittest.TestCase):
    def setUp(self):
        _load_discogs_cover.cache_clear()
        self.addCleanup(_load_discogs_cover.cache_clear)
        self.url = "https://i.discogs.com/signed/path.jpeg"
        self.original = Image.new("RGB", (2, 2), "red")
        self.track = RecognizedTrack(
            artist="Artist", title="Song", album="Album",
            cover_image=self.original, cover_url="https://example.com/shazam.png",
        )

    def apply_match(self, cover_source=None):
        candidate = {
            "release_id": 123, "track_index": 0,
            "normalized_title": "song", "normalized_artist": "artist",
            "track_title": "Song", "track_artist": "Artist",
            "release_title": "Album", "cover_url": self.url, "position": "A1",
        }
        with ExitStack() as stack:
            for name, result in (
                ("find_exact_title_tracks", [candidate]),
                ("find_compact_title_tracks", []),
                ("get_track_counts", {
                    "track_count": 1, "side_track_number": 1, "side_track_count": 1,
                }),
                ("get_next_track", None),
            ):
                stack.enter_context(patch(
                    f"vinylpi.core.discogs_matcher.{name}", return_value=result
                ))
            config = {"enabled": True}
            if cover_source is not None:
                config["cover_source"] = cover_source
            return apply_discogs_match(
                self.track, DiscogsPlaybackState(), {"discogs": config}
            )

    @patch("vinylpi.core.image_utils.requests.get")
    def test_default_shazam_keeps_clean_artwork_but_discogs_metadata(self, get):
        result = self.apply_match()
        self.assertIs(result.cover_image, self.original)
        self.assertEqual(result.cover_url, "https://example.com/shazam.png")
        self.assertEqual(result.discogs_cover_url, self.url)
        self.assertEqual(result.discogs_release_id, 123)
        self.assertEqual(result.discogs_position, "A1")
        get.assert_not_called()

    @patch("vinylpi.core.image_utils.requests.get")
    def test_success_replaces_shazam_artwork_and_caches_download(self, get):
        data = BytesIO()
        Image.new("RGB", (2, 2), "blue").save(data, format="PNG")
        get.return_value = Mock(content=data.getvalue())
        result = self.apply_match(cover_source="discogs")
        self.assertEqual(result.cover_url, self.url)
        self.assertEqual(result.cover_image.getpixel((0, 0)), (0, 0, 255))
        self.assertIsNot(result.cover_image, _load_discogs_cover(self.url))
        get.assert_called_once_with(
            self.url, headers={"User-Agent": USER_AGENT}, timeout=15
        )

    @patch("vinylpi.core.image_utils.requests.get")
    def test_discogs_artwork_option_does_not_override_shazam_text(self, get):
        data = BytesIO()
        Image.new("RGB", (2, 2), "blue").save(data, format="PNG")
        get.return_value = Mock(content=data.getvalue())
        self.track.artist = "ARTIST"
        self.track.title = "SoNg"
        self.track.album = "aLbUm"

        result = self.apply_match(cover_source="discogs")
        self.assertEqual((result.artist, result.title, result.album), ("ARTIST", "SoNg", "aLbUm"))
        self.assertEqual(result.discogs_release_id, 123)
        self.assertEqual(result.cover_url, self.url)

    @patch("vinylpi.core.image_utils.requests.get")
    def test_403_keeps_shazam_artwork_and_does_not_cache_failure(self, get):
        get.return_value.raise_for_status.side_effect = requests.HTTPError("403 Forbidden")
        for _ in range(2):
            result = self.apply_match(cover_source="discogs")
            self.assertIs(result.cover_image, self.original)
            self.assertEqual(result.cover_url, "https://example.com/shazam.png")
            self.assertEqual(result.discogs_release_id, 123)
            self.assertEqual(result.discogs_cover_url, self.url)
        self.assertEqual(get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
