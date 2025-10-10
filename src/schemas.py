"""
Pydantic schemas for API request/response validation (no forward refs)
- No `from __future__ import annotations`
- Define classes in dependency order (profile -> member -> household)
- No `.model_rebuild()` calls needed
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, ConfigDict, field_validator


# ------- System Schemas -------
class SearchSchema(BaseModel):
    q: Optional[str] = Field(default=None, description="Search query string")
    limit: int = Field(default=10, ge=1, le=100, description="Maximum number of results to return")
    offset: int = Field(default=0, ge=0, description="Number of results to skip for pagination")
    fl: Optional[List[str]] = Field(default=None, description="List of fields to include in the response")
    fq: Optional[List[str]] = Field(default=None, description="List of filter queries (e.g., 'status:active')")
    sort: Optional[str] = Field(default=None, description="Sort order (e.g., 'created_at desc')")
    fields: Optional[List[str]] = Field(default=None, description="List of fields to aggregate for faceting")

class LoginSchema(BaseModel):
    username: str = Field(..., description="Username or email")
    password: str = Field(..., description="Password")


# ---------- Enums ----------
class AgeGroupEnum(str, Enum):
    """Age groups for household members"""
    child = "child"
    teen = "teen"
    adult = "adult"
    senior = "senior"
    young_adult = "young_adult"
    middle_aged = "middle_aged"
    baby = "baby"


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
    properties: Optional[Dict[str, Any]] = Field(default_factory=dict)


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

    # Pydantic v2 style (equivalent to v1's Config.from_attributes = True)
    model_config = ConfigDict(from_attributes=True)


# ---------- Household Member Schemas ----------
class HouseholdMemberBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    age_group: AgeGroupEnum
    image_url: Optional[str] = None

    @field_validator("age_group", mode="before")
    @classmethod
    def _norm_age_group(cls, v):
        return v.strip().lower()

class HouseholdMemberCreate(HouseholdMemberBase):
    profile: Optional[HouseholdMemberProfileCreate] = None

 
class HouseholdMemberUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    age_group: Optional[AgeGroupEnum] = None
    image_url: Optional[str] = None
    # Allow partial update of the nested profile if your PATCH endpoint accepts it
    profile: Optional[HouseholdMemberProfileUpdate] = None


class HouseholdMemberResponse(HouseholdMemberBase):
    id: str
    household_id: str
    joined_at: datetime
    profile: Optional[HouseholdMemberProfileResponse] = None

    model_config = ConfigDict(from_attributes=True)


# ---------- Household Schemas ----------
class HouseholdBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, description="Household name")


class HouseholdCreate(HouseholdBase):
    region: Optional[str] = Field(None, max_length=100)
    metadata: Optional[Dict[str, Any]] = Field(
        default_factory=dict,
        description="Household metadata (preferences, settings, etc.)",
    )
    members: Optional[List[HouseholdMemberCreate]] = Field(
        default_factory=list,
        description="Initial household members to create",
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

    model_config = ConfigDict(from_attributes=True)


class HouseholdDetailResponse(HouseholdResponse):
    members: List[HouseholdMemberResponse] = Field(default_factory=list)


# ---------- FoodScholar Schemas ----------

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="User message to send to Food Scholar")