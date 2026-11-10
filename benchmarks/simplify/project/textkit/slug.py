"""URL slugs."""

import re

_SEPARATORS = re.compile(r"[^a-z0-9]+")


def slugify(title: str, limit: int = 60) -> str:
    """'Hello, World!' -> 'hello-world', cut at a word boundary within `limit` characters."""
    slug = _SEPARATORS.sub("-", title.lower()).strip("-")
    if len(slug) <= limit:
        return slug
    cut = slug[:limit]
    return cut.rsplit("-", 1)[0] if "-" in cut else cut
