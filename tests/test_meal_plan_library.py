"""Unit tests for the saved meal plan (library) request schemas.

The entity layer needs a live Postgres, so it is exercised separately; these
cover the validation rules that guard the endpoints.
"""
import pytest
from pydantic import ValidationError


def test_create_requires_a_source():
    from schemas import SavedMealPlanCreateRequest

    with pytest.raises(ValidationError):
        SavedMealPlanCreateRequest(name="Pasta week")


def test_create_accepts_a_stored_plan_id():
    from schemas import SavedMealPlanCreateRequest

    req = SavedMealPlanCreateRequest(name="Pasta week", meal_plan_id="plan-1")
    assert req.meal_plan_id == "plan-1"
    assert req.meal_plan is None


def test_name_is_trimmed():
    from schemas import SavedMealPlanCreateRequest

    req = SavedMealPlanCreateRequest(name="  Pasta week  ", meal_plan_id="plan-1")
    assert req.name == "Pasta week"


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_names_are_rejected(blank):
    from schemas import SavedMealPlanCreateRequest, SavedMealPlanUpdateRequest

    with pytest.raises(ValidationError):
        SavedMealPlanCreateRequest(name=blank, meal_plan_id="plan-1")
    with pytest.raises(ValidationError):
        SavedMealPlanUpdateRequest(name=blank)


def test_overlong_names_are_rejected():
    from schemas import SavedMealPlanUpdateRequest

    with pytest.raises(ValidationError):
        SavedMealPlanUpdateRequest(name="x" * 256)


def test_rename_payload_only_carries_a_name():
    """The meals are a snapshot — a rename must not smuggle in new ones."""
    from schemas import SavedMealPlanUpdateRequest

    req = SavedMealPlanUpdateRequest(name="Comfort food")
    assert req.model_dump() == {"name": "Comfort food"}
