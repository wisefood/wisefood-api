"""
Pydantic schemas for API request/response validation
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, EmailStr
from enum import Enum


# ---------- Enums ----------

class AgeGroupEnum(str, Enum):
    """Age groups for household members"""
    CHILD = "child"
    TEEN = "teen"
    ADULT = "adult"
    SENIOR = "senior"


class DietaryGroupEnum(str, Enum):
    """Dietary preferences and restrictions"""
    OMNIVORE = "omnivore"
    VEGETARIAN = "vegetarian"
    LACTO_VEGETARIAN = "lacto_vegetarian"
    OVO_VEGETARIAN = "ovo_vegetarian"
    LACTO_OVO_VEGETARIAN = "lacto_ovo_vegetarian"
    PESCATARIAN = "pescatarian"
    VEGAN = "vegan"
    RAW_VEGAN = "raw_vegan"
    PLANT_BASED = "plant_based"
    FLEXITARIAN = "flexitarian"
    HALAL = "halal"
    KOSHER = "kosher"
    JAIN = "jain"
    BUDDHIST_VEGETARIAN = "buddhist_vegetarian"
    GLUTEN_FREE = "gluten_free"
    NUT_FREE = "nut_free"
    PEANUT_FREE = "peanut_free"
    DAIRY_FREE = "dairy_free"
    EGG_FREE = "egg_free"
    SOY_FREE = "soy_free"
    SHELLFISH_FREE = "shellfish_free"
    FISH_FREE = "fish_free"
    SESAME_FREE = "sesame_free"
    LOW_CARB = "low_carb"
    LOW_FAT = "low_fat"
    LOW_SODIUM = "low_sodium"
    SUGAR_FREE = "sugar_free"
    NO_ADDED_SUGAR = "no_added_sugar"
    HIGH_PROTEIN = "high_protein"
    HIGH_FIBER = "high_fiber"
    LOW_CHOLESTEROL = "low_cholesterol"
    LOW_CALORIE = "low_calorie"
    KETO = "keto"
    PALEO = "paleo"
    WHOLE30 = "whole30"
    MEDITERRANEAN = "mediterranean"
    DIABETIC_FRIENDLY = "diabetic_friendly"


# ---------- Household Member Profile Schemas ----------

class HouseholdMemberProfileBase(BaseModel):
    nutritional_preferences: Optional[Dict[str, Any]] = Field(default_factory=dict)
    dietary_groups: Optional[List[DietaryGroupEnum]] = Field(default_factory=list)


class HouseholdMemberProfileCreate(HouseholdMemberProfileBase):
    pass


class HouseholdMemberProfileUpdate(BaseModel):
    nutritional_preferences: Optional[Dict[str, Any]] = None
    dietary_groups: Optional[List[DietaryGroupEnum]] = None


class HouseholdMemberProfileResponse(HouseholdMemberProfileBase):
    id: str
    household_member_id: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ---------- Household Member Schemas ----------

class HouseholdMemberBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    age_group: AgeGroupEnum
    image_url: Optional[str] = None


class HouseholdMemberCreate(HouseholdMemberBase):
    profile: Optional[HouseholdMemberProfileCreate] = None


class HouseholdMemberUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    age_group: Optional[AgeGroupEnum] = None
    image_url: Optional[str] = None


class HouseholdMemberResponse(HouseholdMemberBase):
    id: str
    household_id: str
    joined_at: datetime
    profile: Optional[HouseholdMemberProfileResponse] = None

    class Config:
        from_attributes = True


# ---------- Household Schemas ----------

class HouseholdBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, description="Household name")


class HouseholdCreate(HouseholdBase):
    region: Optional[str] = Field(None, max_length=100)
    metadata: Optional[Dict[str, Any]] = Field(
        default_factory=dict,
        description="Household metadata (preferences, settings, etc.)"
    )
    members: Optional[List[HouseholdMemberCreate]] = Field(
        default_factory=list,
        description="Initial household members to create"
    )


class HouseholdUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    region: Optional[str] = Field(None, max_length=100)
    metadata: Optional[Dict[str, Any]] = None


class HouseholdResponse(HouseholdBase):
    id: str
    owner_id: str
    region: Optional[str] = None
    member_count: int
    created_at: datetime
    updated_at: datetime
    metadata: Dict[str, Any] = Field(default_factory=dict)

    class Config:
        from_attributes = True


class HouseholdDetailResponse(HouseholdResponse):
    members: List[HouseholdMemberResponse] = Field(default_factory=list)


# Update forward references
HouseholdDetailResponse.model_rebuild()
