"""Render prompt messages for a case: template the text, resolve the media."""

from dataclasses import dataclass
from pathlib import Path

from evaling.config.schema import (
    AudioPart,
    Case,
    FilePart,
    ImagePart,
    Message,
    TextPart,
)
from evaling.content import MediaKind, MediaRef, resolve_media
from evaling.templating import build_context, render_text


@dataclass(frozen=True)
class RenderedText:
    text: str


RenderedPart = RenderedText | MediaRef


@dataclass(frozen=True)
class RenderedMessage:
    role: str
    parts: tuple[RenderedPart, ...]

    @property
    def text(self) -> str:
        """All text parts joined — convenient for text-only providers."""
        return "".join(part.text for part in self.parts if isinstance(part, RenderedText))


def render_messages(messages: list[Message], case: Case, base_dir: Path) -> list[RenderedMessage]:
    """Render a prompt's messages for one case.

    Text parts go through Jinja2 with the case's context; media parts have
    their path expressions templated, then are resolved against base_dir and
    content-hashed. Case attachment paths (``files.<name>``) are expected to be
    absolute already (case loading resolves them).
    """
    context = build_context(case)
    rendered: list[RenderedMessage] = []
    for index, message in enumerate(messages, start=1):
        where = f"message {index} ({message.role})"
        parts: list[RenderedPart] = []
        content = message.content
        if isinstance(content, str):
            parts.append(RenderedText(render_text(content, context, where)))
        else:
            for part in content:
                parts.append(_render_part(part, context, base_dir, where))
        rendered.append(RenderedMessage(role=message.role, parts=tuple(parts)))
    return rendered


def _render_part(part, context, base_dir: Path, where: str) -> RenderedPart:
    if isinstance(part, TextPart):
        return RenderedText(render_text(part.text, context, where))
    kind: MediaKind
    if isinstance(part, ImagePart):
        kind, expr = "image", part.image
    elif isinstance(part, FilePart):
        kind, expr = "file", part.file
    elif isinstance(part, AudioPart):
        kind, expr = "audio", part.audio
    else:  # pragma: no cover - schema guarantees exhaustiveness
        raise TypeError(f"unknown content part: {part!r}")
    path_str = render_text(expr, context, where)
    return resolve_media(kind, path_str, base_dir, where)
