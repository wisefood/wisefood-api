"""The allergen vocabulary every WiseFood surface has to agree on.

A member's profile ``allergies`` and every ``exclude_allergens`` filter are
matched downstream by RecipeWrangler as ``toLower(al.name) IN $exclude`` (the
graph) and ``terms: {allergens: [...]}`` (the index) — exact comparisons against
the canonical identifiers below. The profile picker used to store ``dairy``,
``nuts`` and ``shellfish``, FoodChat's memory flow writes whatever the member
said, and the console lets a curator type anything; none of those matched, so a
declared allergy excluded nothing and nothing reported it.

This module is the gateway's copy of RecipeWrangler's
``recipe_wrangler.utils.food_ontology`` vocabulary (and of the UI's
``app/utils/allergens.ts``). The three must stay in step: the identifiers are
the EU's fourteen declaration groups (Regulation 1169/2011, Annex II) plus
``wheat``, which RecipeWrangler keeps as a compatibility identifier under the
gluten group.
"""

from __future__ import annotations

import re
from typing import Iterable

#: The fourteen EU declaration groups, in Annex II order.
EU_ALLERGEN_GROUPS: tuple[str, ...] = (
    "gluten",
    "crustacean_shellfish",
    "egg",
    "fish",
    "peanut",
    "soy",
    "milk",
    "tree_nut",
    "celery",
    "mustard",
    "sesame",
    "sulphites",
    "lupin",
    "molluscs",
)

CANONICAL_ALLERGEN_IDS: tuple[str, ...] = EU_ALLERGEN_GROUPS + ("wheat",)

#: Human-readable EU label per identifier, for API descriptions and logs.
EU_ALLERGEN_LABELS: dict[str, str] = {
    "gluten": "Cereals containing gluten",
    "crustacean_shellfish": "Crustaceans",
    "egg": "Eggs",
    "fish": "Fish",
    "peanut": "Peanuts",
    "soy": "Soybeans",
    "milk": "Milk",
    "tree_nut": "Nuts",
    "celery": "Celery",
    "mustard": "Mustard",
    "sesame": "Sesame seeds",
    "sulphites": "Sulphur dioxide and sulphites",
    "lupin": "Lupin",
    "molluscs": "Molluscs",
    "wheat": "Wheat",
}

#: What callers actually send, mapped to the identifier(s) they mean. Keys are
#: compared after ``_allergen_key`` folding (case, hyphens, underscores,
#: surrounding whitespace) and a trailing "s" is tried on a miss, so plurals
#: need no entries of their own. A bare ``shellfish`` means both crustaceans
#: and molluscs: an allergy, so the wider reading is the safe one.
ALLERGEN_ALIASES: dict[str, tuple[str, ...]] = {
    # cereals containing gluten
    "cereals containing gluten": ("gluten",),
    "gluten containing cereals": ("gluten",),
    "wheat gluten": ("gluten",),
    "coeliac": ("gluten",),
    "celiac": ("gluten",),
    "rye": ("gluten",),
    "barley": ("gluten",),
    "oat": ("gluten",),
    "spelt": ("gluten",),
    "kamut": ("gluten",),
    "khorasan": ("gluten",),
    "khorasan wheat": ("gluten",),
    # crustaceans
    "crustacean": ("crustacean_shellfish",),
    "crustacean shellfish": ("crustacean_shellfish",),
    "crab": ("crustacean_shellfish",),
    "prawn": ("crustacean_shellfish",),
    "shrimp": ("crustacean_shellfish",),
    "lobster": ("crustacean_shellfish",),
    "crayfish": ("crustacean_shellfish",),
    "langoustine": ("crustacean_shellfish",),
    "scampi": ("crustacean_shellfish",),
    "shellfish": ("crustacean_shellfish", "molluscs"),
    "seafood": ("fish", "crustacean_shellfish", "molluscs"),
    # eggs
    "hen egg": ("egg",),
    "hens egg": ("egg",),
    "hen's egg": ("egg",),
    # fish
    "finfish": ("fish",),
    # peanuts
    "groundnut": ("peanut",),
    "ground nut": ("peanut",),
    "monkey nut": ("peanut",),
    "arachis": ("peanut",),
    # soybeans
    "soya": ("soy",),
    "soja": ("soy",),
    "soybean": ("soy",),
    "soy bean": ("soy",),
    "soya bean": ("soy",),
    # milk
    "dairy": ("milk",),
    "dairy product": ("milk",),
    "milk product": ("milk",),
    "milk protein": ("milk",),
    "cow milk": ("milk",),
    "cows milk": ("milk",),
    "cow's milk": ("milk",),
    "lactose": ("milk",),
    "casein": ("milk",),
    "whey": ("milk",),
    # nuts
    "nut": ("tree_nut",),
    "tree nut": ("tree_nut",),
    "treenut": ("tree_nut",),
    "almond": ("tree_nut",),
    "hazelnut": ("tree_nut",),
    "walnut": ("tree_nut",),
    "cashew": ("tree_nut",),
    "cashew nut": ("tree_nut",),
    "pecan": ("tree_nut",),
    "pecan nut": ("tree_nut",),
    "brazil nut": ("tree_nut",),
    "pistachio": ("tree_nut",),
    "pistachio nut": ("tree_nut",),
    "macadamia": ("tree_nut",),
    "macadamia nut": ("tree_nut",),
    "queensland nut": ("tree_nut",),
    # celery
    "celeriac": ("celery",),
    "celery root": ("celery",),
    "celery seed": ("celery",),
    # mustard
    "mustard seed": ("mustard",),
    # sesame seeds
    "sesame seed": ("sesame",),
    "sesamum": ("sesame",),
    # sulphur dioxide and sulphites
    "sulphite": ("sulphites",),
    "sulfite": ("sulphites",),
    "sulfites": ("sulphites",),
    "sulphur dioxide": ("sulphites",),
    "sulfur dioxide": ("sulphites",),
    "sulphur dioxide and sulphites": ("sulphites",),
    "sulfur dioxide and sulfites": ("sulphites",),
    "so2": ("sulphites",),
    "metabisulphite": ("sulphites",),
    "metabisulfite": ("sulphites",),
    # lupin
    "lupine": ("lupin",),
    "lupini": ("lupin",),
    "lupin bean": ("lupin",),
    "lupini bean": ("lupin",),
    "lupin flour": ("lupin",),
    # molluscs
    "mollusc": ("molluscs",),
    "mollusk": ("molluscs",),
    "mollusc shellfish": ("molluscs",),
    "mussel": ("molluscs",),
    "oyster": ("molluscs",),
    "clam": ("molluscs",),
    "scallop": ("molluscs",),
    "squid": ("molluscs",),
    "calamari": ("molluscs",),
    "octopus": ("molluscs",),
    "cuttlefish": ("molluscs",),
    "snail": ("molluscs",),
    "escargot": ("molluscs",),
    "cockle": ("molluscs",),
    "whelk": ("molluscs",),
    "abalone": ("molluscs",),
}

_CANONICAL_BY_KEY: dict[str, str] = {
    cid.replace("_", " "): cid for cid in CANONICAL_ALLERGEN_IDS
}


def _allergen_key(value: object) -> str:
    """Fold a caller's spelling to the form the alias table is keyed by."""
    text = str(value or "").casefold().replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", text).strip()


def _lookup(key: str) -> tuple[str, ...] | None:
    if key in _CANONICAL_BY_KEY:
        return (_CANONICAL_BY_KEY[key],)
    if key in ALLERGEN_ALIASES:
        return ALLERGEN_ALIASES[key]
    return None


def canonical_allergen_ids(value: object) -> tuple[str, ...] | None:
    """The canonical identifier(s) one caller-supplied name means.

    ``None`` when the vocabulary does not know the name — "kiwi" is a real
    allergy and must still reach FoodChat's name-level backstop as itself, so
    an unknown name is never dropped and never guessed at.
    """
    key = _allergen_key(value)
    if not key:
        return None
    found = _lookup(key)
    if found is None and key.endswith("s") and len(key) > 3 and not key.endswith("ss"):
        found = _lookup(key[:-1])
    return found


def normalize_allergen_names(values: Iterable[object] | None) -> list[str]:
    """Canonical identifiers for a list of allergen names, in first-seen order.

    Known names and aliases become their identifier(s); unknown names pass
    through lower-cased and whitespace-folded. Empty entries are dropped and
    duplicates (after mapping) collapse, so ``["Dairy", "milk", "eggs"]`` is
    ``["milk", "egg"]``. ``None`` stays ``None`` so a PATCH that did not send
    the field is still "unchanged", not "cleared".
    """
    if values is None:
        return None  # type: ignore[return-value]
    out: list[str] = []
    for value in values:
        ids = canonical_allergen_ids(value)
        if ids is None:
            key = _allergen_key(value)
            ids = (key,) if key else ()
        for cid in ids:
            if cid not in out:
                out.append(cid)
    return out


def is_canonical_allergen(value: object) -> bool:
    return str(value) in CANONICAL_ALLERGEN_IDS


ALLERGEN_FIELD_DESCRIPTION = (
    "Allergen identifiers. The canonical values are the EU declaration groups "
    + ", ".join(f"`{cid}`" for cid in EU_ALLERGEN_GROUPS)
    + " plus `wheat`; common spellings such as 'dairy', 'eggs', 'nuts', "
    "'tree nuts', 'shellfish' or 'sulfites' are folded to them on the way in, "
    "and a name outside the vocabulary is kept as typed (lower-cased)."
)
