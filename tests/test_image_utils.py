from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image
import requests

from vinylpi.core.image_utils import load_image
from vinylpi.integrations.discogs_client import USER_AGENT


class LoadImageTests(unittest.TestCase):
    @patch("vinylpi.core.image_utils.requests.get")
    def test_remote_artwork_identifies_app_and_decodes_rgb(self, get):
        data = BytesIO()
        Image.new("RGBA", (2, 3), (12, 34, 56, 255)).save(data, format="PNG")
        get.return_value = Mock(content=data.getvalue())

        for url in (
            "https://i.discogs.com/signed/path.jpeg",
            "https://example.com/shazam.png",
            "http://example.com/cover.png",
        ):
            with self.subTest(url=url):
                get.reset_mock()
                image = load_image(url)
                get.assert_called_once_with(
                    url, headers={"User-Agent": USER_AGENT}, timeout=15
                )
                get.return_value.raise_for_status.assert_called_once_with()
                self.assertEqual(image.mode, "RGB")
                self.assertEqual(image.size, (2, 3))
                self.assertEqual(image.getpixel((0, 0)), (12, 34, 56))

    @patch("vinylpi.core.image_utils.Image.open")
    @patch("vinylpi.core.image_utils.requests.get")
    def test_http_403_propagates_before_image_decoding(self, get, open_image):
        error = requests.HTTPError("403 Forbidden")
        get.return_value.raise_for_status.side_effect = error
        with self.assertRaises(requests.HTTPError) as caught:
            load_image("https://i.discogs.com/signed/path.jpeg")
        self.assertIs(caught.exception, error)
        get.assert_called_once()
        open_image.assert_not_called()

    @patch("vinylpi.core.image_utils.requests.get")
    def test_timeout_propagates(self, get):
        get.side_effect = requests.Timeout("timed out")
        with self.assertRaises(requests.Timeout):
            load_image("https://i.discogs.com/signed/path.jpeg")
        get.assert_called_once()

    @patch("vinylpi.core.image_utils.requests.get")
    def test_local_file_does_not_make_http_request(self, get):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cover.png"
            Image.new("L", (2, 3), 42).save(path)
            image = load_image(str(path))
        get.assert_not_called()
        self.assertEqual(image.mode, "RGB")
        self.assertEqual(image.getpixel((0, 0)), (42, 42, 42))

    @patch("vinylpi.core.image_utils.requests.get")
    def test_empty_path_is_rejected_without_http(self, get):
        with self.assertRaises(ValueError):
            load_image("")
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
