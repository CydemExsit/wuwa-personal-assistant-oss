"""Conservative name normalization shared by OCR and local character lookups."""

from __future__ import annotations

import unicodedata

_NAME_DOTS = str.maketrans({character: "·" for character in ".・･•‧∙⋅"})


def canonical_character_name(name: str) -> str:
    """Normalize spacing and separator glyphs while preserving every name character."""

    return "".join(unicodedata.normalize("NFKC", name).split()).translate(_NAME_DOTS)


def character_name_key(name: str) -> str:
    """Match separator variants without merging a base name with a named variant."""

    return canonical_character_name(name).replace("·", "").casefold()
