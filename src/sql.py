"""
SQLAlchemy models and database access methods for WiseFood API
"""
from __future__ import annotations
from sqlalchemy import inspect as sa_inspect
from datetime import datetime, timezone
from typing import List, Optional
from uuid import uuid4
import enum

from sqlalchemy import (
    Column,
    String,
    DateTime,
    ForeignKey,
    Table,
    Text,
    Index,
    select,
    delete,
    Enum,
    ARRAY,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship, Mapped, mapped_column
from sqlalchemy.ext.asyncio import AsyncSession

from backend.postgres import Base


# ---------- Enums ----------

class AgeGroup(str, enum.Enum):
    """Age groups for household members"""
    CHILD = "child"
    TEEN = "teen"
    ADULT = "adult"
    SENIOR = "senior"


class DietaryGroup(str, enum.Enum):
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


# ---------- SQLAlchemy Models ----------

class Household(Base):
    """
    Household that groups multiple members together.

    Only the owner is a Keycloak user; household members are profiles.
    A household shares:
    - Meal plans
    - Grocery lists
    - Recipes
    - Dietary preferences
    """
    __tablename__ = "household"
    __table_args__ = {"schema": "wisefood"}

    id = mapped_column(String(64), primary_key=True, default=lambda: str(uuid4()))
    name = mapped_column(String(255), nullable=False)
    region = mapped_column(String(100), nullable=True)
    owner_id = mapped_column(String(100), nullable=False, index=True)
    created_at = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)
    metadata_ = mapped_column("metadata", JSONB, nullable=True, default=dict)

    # Relationships
    members: Mapped[List["HouseholdMember"]] = relationship(
        "HouseholdMember", back_populates="household", cascade="all, delete-orphan"
    )

    def to_dict(self, include_members: bool = False) -> dict:
        result = {
            "id": self.id,
            "name": self.name,
            "region": self.region,
            "owner_id": self.owner_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "metadata": self.metadata_ or {},
        }

        insp = sa_inspect(self)

        # Only include members if requested AND already loaded
        if include_members:
            if "members" not in insp.unloaded:
                members = [m.to_dict(include_profile=True) for m in self.members]
                result["members"] = members
                result["member_count"] = len(members)
            else:
                result["members"] = []
                result["member_count"] = 0
        else:
            # Don't touch relationship at all
            result["member_count"] = 0

        return result


class HouseholdMember(Base):
    """
    Household member profile (not a user account).

    Members are people in the household with names, age groups, and dietary preferences.
    Only the household owner has a Keycloak user account.
    """
    __tablename__ = "household_member"
    __table_args__ = {"schema": "wisefood"}

    id = mapped_column(String(64), primary_key=True, default=lambda: str(uuid4()))
    name = mapped_column(String(255), nullable=False)
    image_url = mapped_column(Text, nullable=True)
    age_group = mapped_column(Enum(AgeGroup, name="age_groups", create_type=False), nullable=False)
    household_id = mapped_column(String(100), ForeignKey("wisefood.household.id", ondelete="CASCADE", onupdate="CASCADE"), nullable=False, index=True)
    joined_at = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    # Relationships
    household: Mapped["Household"] = relationship("Household", back_populates="members")
    profile: Mapped[Optional["HouseholdMemberProfile"]] = relationship(
        "HouseholdMemberProfile", back_populates="member", cascade="all, delete-orphan", uselist=False
    )

    def to_dict(self, include_profile: bool = False) -> dict:
        result = {
            "id": self.id,
            "name": self.name,
            "image_url": self.image_url,
            "age_group": self.age_group.value if self.age_group else None,
            "household_id": self.household_id,
            "joined_at": self.joined_at.isoformat(),
        }
        if include_profile and self.profile:
            result["profile"] = self.profile.to_dict()
        return result


class HouseholdMemberProfile(Base):
    """
    Dietary preferences and nutritional profile for a household member.
    """
    __tablename__ = "household_member_profile"
    __table_args__ = {"schema": "wisefood"}

    id = mapped_column(String(64), primary_key=True, default=lambda: str(uuid4()))
    household_member_id = mapped_column(
        String(100),
        ForeignKey("wisefood.household_member.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        unique=True,
        index=True
    )
    nutritional_preferences = mapped_column(JSONB, nullable=True, default=dict)
    dietary_groups = mapped_column(ARRAY(Enum(DietaryGroup, name="dietary_groups", create_type=False)), nullable=True, default=list)
    created_at = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    # Relationships
    member: Mapped["HouseholdMember"] = relationship("HouseholdMember", back_populates="profile")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "household_member_id": self.household_member_id,
            "nutritional_preferences": self.nutritional_preferences or {},
            "dietary_groups": [dg.value for dg in (self.dietary_groups or [])],
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
