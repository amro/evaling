"""AAC audio support (raw ADTS; .m4a already covered AAC in an MP4 container)."""

import pytest

from evaling.content import ALLOWED_MEDIA_TYPES, MEDIA_TYPES, resolve_media
from evaling.errors import ContentError


class TestAac:
    def test_extension_is_mapped(self):
        assert MEDIA_TYPES[".aac"] == "audio/aac"

    def test_allowed_as_audio(self):
        assert "audio/aac" in ALLOWED_MEDIA_TYPES["audio"]

    def test_resolves_as_audio(self, tmp_path):
        path = tmp_path / "clip.aac"
        path.write_bytes(b"\xff\xf1fake adts frame")
        ref = resolve_media("audio", "clip.aac", tmp_path, "message 1")
        assert ref.media_type == "audio/aac"
        assert ref.sha256

    def test_rejected_where_audio_is_not_expected(self, tmp_path):
        (tmp_path / "clip.aac").write_bytes(b"\xff\xf1")
        with pytest.raises(ContentError):
            resolve_media("image", "clip.aac", tmp_path, "message 1")
