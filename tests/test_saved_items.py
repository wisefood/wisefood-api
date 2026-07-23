"""Unit tests for the member saved-item (library) schemas and type guard."""
import pytest
from pydantic import ValidationError


def test_allowed_types_cover_recipe_and_literature():
    # The response literal is the public contract for what may be saved; the
    # entity's ALLOWED_TYPES guard must stay in step with it.
    from typing import get_args
    from schemas import SavedItemType

    allowed = set(get_args(SavedItemType))
    assert "recipe" in allowed
    assert {"article", "guide", "textbook"} <= allowed


def test_saved_item_response_accepts_urn():
    from schemas import MemberSavedItemResponse
    from datetime import datetime, timezone

    resp = MemberSavedItemResponse(
        item_type="article",
        item_ref="urn:article:mediterranean-diet",
        created_at=datetime.now(timezone.utc),
    )
    assert resp.item_type == "article"
    assert resp.item_ref.startswith("urn:article:")


def test_saved_item_response_rejects_unknown_type():
    from schemas import MemberSavedItemResponse
    from datetime import datetime, timezone

    with pytest.raises(ValidationError):
        MemberSavedItemResponse(
            item_type="podcast",
            item_ref="x",
            created_at=datetime.now(timezone.utc),
        )


def test_saved_item_response_rejects_blank_ref():
    from schemas import MemberSavedItemResponse
    from datetime import datetime, timezone

    with pytest.raises(ValidationError):
        MemberSavedItemResponse(
            item_type="recipe", item_ref="", created_at=datetime.now(timezone.utc)
        )


def test_favorite_response_still_recipe_shaped():
    """The legacy contract FoodChat/RecipeWrangler read must not have changed."""
    from schemas import MemberFavoriteResponse
    from datetime import datetime, timezone

    resp = MemberFavoriteResponse(
        recipe_id="rw-123", created_at=datetime.now(timezone.utc)
    )
    assert resp.recipe_id == "rw-123"
    assert not hasattr(resp, "item_type")
