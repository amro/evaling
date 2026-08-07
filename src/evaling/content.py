"""Resolution of binary content references (images, documents, audio).

Media files are identified by extension, validated against the declaring part
type, and hashed by content so cache keys and artifact storage can address them
by what they contain rather than where they live.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from evaling.errors import ContentError

MediaKind = Literal["image", "file", "audio", "video"]

MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}

ALLOWED_MEDIA_TYPES: dict[MediaKind, frozenset[str]] = {
    "image": frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"}),
    "file": frozenset({"application/pdf"}),
    "audio": frozenset(
        {"audio/mpeg", "audio/wav", "audio/ogg", "audio/flac", "audio/mp4", "audio/aac"}
    ),
    "video": frozenset({"video/mp4", "video/quicktime", "video/webm"}),
}


@dataclass(frozen=True)
class MediaRef:
    """A resolved, content-hashed reference to a binary input."""

    kind: MediaKind
    path: Path  # absolute
    media_type: str
    sha256: str

    def read_bytes(self) -> bytes:
        return self.path.read_bytes()


def resolve_media(
    kind: MediaKind,
    path_str: str,
    base_dir: Path,
    where: str = "",
    *,
    may_reach_outside: bool = True,
) -> MediaRef:
    """Resolve a media path against base_dir, validate its type, and hash it.

    ``may_reach_outside=False`` when the path came from case data rather than
    from the config: this file is read, hashed, sent to a model API, and
    archived with the run, so a dataset must not be able to choose it. Case
    attachments (``files.<name>``) are contained when the case loads and arrive
    here already absolute; a path templated straight out of a case *variable*
    is not contained anywhere else.
    """
    prefix = f"{where}: " if where else ""
    path = Path(path_str)
    if not path.is_absolute():
        path = base_dir / path
    path = path.resolve()

    if not may_reach_outside:
        root = base_dir.resolve()
        if path != root and root not in path.parents:
            raise ContentError(
                f"{prefix}{kind} path {path_str!r} comes from case data and resolves "
                f"outside {root} — it would be read, sent to a model API, and archived "
                "with the run. Attach it with `files` on the case, which is checked "
                "when the case loads."
            )

    if not path.is_file():
        raise ContentError(f"{prefix}{kind} file not found: {path_str} (resolved to {path})")

    media_type = MEDIA_TYPES.get(path.suffix.lower())
    if media_type is None:
        allowed = ALLOWED_MEDIA_TYPES[kind]
        supported = ", ".join(sorted(ext for ext, mt in MEDIA_TYPES.items() if mt in allowed))
        raise ContentError(
            f"{prefix}unsupported {kind} extension {path.suffix!r} for {path_str} "
            f"(supported: {supported})"
        )
    if media_type not in ALLOWED_MEDIA_TYPES[kind]:
        raise ContentError(
            f"{prefix}{path_str} is {media_type}, which is not valid for a {kind!r} content part"
        )

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return MediaRef(kind=kind, path=path, media_type=media_type, sha256=digest)
