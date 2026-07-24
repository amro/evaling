import hashlib

import pytest

from evaling.content import MediaRef, resolve_media
from evaling.errors import ContentError


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "dog.png"
    path.write_bytes(b"\x89PNG fake image bytes")
    return path


def test_resolves_relative_path_and_hashes_content(image, tmp_path):
    ref = resolve_media("image", "dog.png", tmp_path)
    assert ref.path == image.resolve()
    assert ref.media_type == "image/png"
    assert ref.sha256 == hashlib.sha256(b"\x89PNG fake image bytes").hexdigest()
    assert ref.read_bytes() == b"\x89PNG fake image bytes"


def test_absolute_path_ignores_base_dir(image, tmp_path):
    ref = resolve_media("image", str(image), tmp_path / "elsewhere")
    assert ref.path == image.resolve()


def test_same_content_same_hash_different_paths(tmp_path):
    (tmp_path / "a.png").write_bytes(b"same")
    (tmp_path / "b.png").write_bytes(b"same")
    ref_a = resolve_media("image", "a.png", tmp_path)
    ref_b = resolve_media("image", "b.png", tmp_path)
    assert ref_a.sha256 == ref_b.sha256


def test_missing_file_raises_content_error(tmp_path):
    with pytest.raises(ContentError, match="image file not found: ghost.png"):
        resolve_media("image", "ghost.png", tmp_path)


def test_unknown_extension_lists_supported(tmp_path):
    (tmp_path / "pic.tiff").write_bytes(b"x")
    with pytest.raises(ContentError, match=r"unsupported image extension '\.tiff'.*\.png"):
        resolve_media("image", "pic.tiff", tmp_path)


def test_kind_mismatch_rejected(tmp_path):
    (tmp_path / "doc.pdf").write_bytes(b"%PDF")
    with pytest.raises(ContentError, match="application/pdf.*not valid for a 'image'"):
        resolve_media("image", "doc.pdf", tmp_path)


def test_pdf_valid_for_file_part(tmp_path):
    (tmp_path / "doc.pdf").write_bytes(b"%PDF")
    assert resolve_media("file", "doc.pdf", tmp_path).media_type == "application/pdf"


@pytest.mark.parametrize(
    "name,media_type",
    [
        ("a.mp3", "audio/mpeg"),
        ("a.wav", "audio/wav"),
        ("a.flac", "audio/flac"),
        ("a.m4a", "audio/mp4"),
    ],
)
def test_audio_types(tmp_path, name, media_type):
    (tmp_path / name).write_bytes(b"x")
    assert resolve_media("audio", name, tmp_path).media_type == media_type


@pytest.mark.parametrize(
    "name,media_type",
    [
        ("a.mp4", "video/mp4"),
        ("a.mov", "video/quicktime"),
        ("a.webm", "video/webm"),
    ],
)
def test_video_types(tmp_path, name, media_type):
    (tmp_path / name).write_bytes(b"x")
    assert resolve_media("video", name, tmp_path).media_type == media_type


def test_video_not_valid_for_audio_part(tmp_path):
    (tmp_path / "clip.mp4").write_bytes(b"x")
    with pytest.raises(ContentError, match="video/mp4.*not valid for a 'audio'"):
        resolve_media("audio", "clip.mp4", tmp_path)


def test_uppercase_extension_recognized(tmp_path):
    (tmp_path / "DOG.JPG").write_bytes(b"x")
    assert resolve_media("image", "DOG.JPG", tmp_path).media_type == "image/jpeg"


def test_where_prefix_in_errors(tmp_path):
    with pytest.raises(ContentError, match=r"^message 1 \(user\): image file not found"):
        resolve_media("image", "nope.png", tmp_path, where="message 1 (user)")


def test_media_ref_is_frozen(tmp_path):
    (tmp_path / "a.png").write_bytes(b"x")
    ref = resolve_media("image", "a.png", tmp_path)
    with pytest.raises(AttributeError):
        ref.sha256 = "tampered"
    assert isinstance(ref, MediaRef)
