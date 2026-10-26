"""Allergen names are folded to the shared vocabulary at the gateway.

RecipeWrangler compares a member's allergies and a search's exclusions exactly
against its graph (``milk``, ``tree_nut``, ``crustacean_shellfish`` ...). The
profile picker used to store ``dairy``, ``nuts`` and ``shellfish``, and
FoodChat's memory flow writes whatever the member said, so a declared allergy
excluded nothing. Every writer passes through these schemas, which is why the
folding lives here rather than in each of them.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from allergens import (  # noqa: E402
    ALLERGEN_ALIASES,
    CANONICAL_ALLERGEN_IDS,
    EU_ALLERGEN_GROUPS,
    EU_ALLERGEN_LABELS,
    canonical_allergen_ids,
    normalize_allergen_names,
)
import schemas  # noqa: E402


class TestTheVocabularyIsTheEUList:
    def test_fourteen_groups_in_annex_order(self):
        assert EU_ALLERGEN_GROUPS == (
            "gluten", "crustacean_shellfish", "egg", "fish", "peanut", "soy",
            "milk", "tree_nut", "celery", "mustard", "sesame", "sulphites",
            "lupin", "molluscs",
        )
        assert CANONICAL_ALLERGEN_IDS == EU_ALLERGEN_GROUPS + ("wheat",)

    def test_every_id_has_a_label_and_every_alias_points_at_an_id(self):
        assert set(EU_ALLERGEN_LABELS) == set(CANONICAL_ALLERGEN_IDS)
        for alias, targets in ALLERGEN_ALIASES.items():
            assert set(targets) <= set(CANONICAL_ALLERGEN_IDS), alias


class TestNormalization:
    @pytest.mark.parametrize("sent, expected", [
        ("milk", ("milk",)),
        ("Dairy", ("milk",)),
        ("lactose", ("milk",)),
        ("eggs", ("egg",)),
        ("nuts", ("tree_nut",)),
        ("Tree Nuts", ("tree_nut",)),
        ("peanuts", ("peanut",)),
        ("shellfish", ("crustacean_shellfish", "molluscs")),
        ("Crustacean-Shellfish", ("crustacean_shellfish",)),
        ("sulfites", ("sulphites",)),
        ("soya", ("soy",)),
        ("rye", ("gluten",)),
        ("wheat", ("wheat",)),
    ])
    def test_the_spellings_the_old_picker_stored(self, sent, expected):
        assert canonical_allergen_ids(sent) == expected

    def test_an_unknown_allergy_is_kept_not_dropped(self):
        assert canonical_allergen_ids("kiwi") is None
        assert normalize_allergen_names(["Kiwi"]) == ["kiwi"]

    def test_dedupes_after_mapping_and_keeps_order(self):
        assert normalize_allergen_names(["Dairy", "milk", "", None, "eggs"]) == ["milk", "egg"]

    def test_none_stays_none(self):
        """A PATCH that did not send the field means "unchanged", not "cleared"."""
        assert normalize_allergen_names(None) is None


class TestTheSchemasFoldOnTheWayIn:
    def test_profile_create(self):
        profile = schemas.HouseholdMemberProfileCreate(allergies=["Dairy", "nuts", "shellfish"])
        assert profile.allergies == ["milk", "tree_nut", "crustacean_shellfish", "molluscs"]

    def test_profile_patch_keeps_an_unsent_field_unset(self):
        patch = schemas.HouseholdMemberProfileUpdate(nutritional_preferences={})
        assert patch.allergies is None
        assert "allergies" not in patch.model_dump(exclude_unset=True)

    def test_profile_patch_folds_what_foodchat_remembers(self):
        """The memory flow writes the member's own words ("tree nuts")."""
        patch = schemas.HouseholdMemberProfileUpdate(allergies=["tree nuts", "peanuts"])
        assert patch.model_dump(exclude_unset=True)["allergies"] == ["tree_nut", "peanut"]

    def test_search_and_param_search(self):
        assert schemas.RecipeSearchRequest(exclude_allergens=["eggs"]).exclude_allergens == ["egg"]
        assert schemas.RecipeParamSearchRequest(exclude_allergens=["Sulfites"]).exclude_allergens == ["sulphites"]

    def test_recipe_create_and_update(self):
        create = schemas.RecipeCreateRequest(
            title="t", ingredients=["1 cup flour"], instructions=["mix"], duration=10,
            serves=2, region="IE", allergens=["dairy"],
        )
        assert create.allergens == ["milk"]
        assert schemas.RecipeUpdateRequest(allergens=["Tree Nuts"]).allergens == ["tree_nut"]
        assert schemas.RecipeUpdateRequest(title="x").allergens is None

    def test_the_contract_names_the_vocabulary(self):
        for model, field in [
            (schemas.HouseholdMemberProfileCreate, "allergies"),
            (schemas.HouseholdMemberProfileUpdate, "allergies"),
            (schemas.RecipeSearchRequest, "exclude_allergens"),
            (schemas.RecipeParamSearchRequest, "exclude_allergens"),
            (schemas.RecipeCreateRequest, "allergens"),
            (schemas.RecipeUpdateRequest, "allergens"),
        ]:
            description = model.model_fields[field].description or ""
            assert "`tree_nut`" in description and "`sulphites`" in description, (model, field)
