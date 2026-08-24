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
    BigInteger,
    Column,
    String,
    Date,
    DateTime,
    ForeignKey,
    Table,
    Text,
    Index,
    UniqueConstraint,
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
    """Age groups for household members (single source of truth)"""
    baby = "baby"
    child = "child"
    teen = "teen"
    young_adult = "young_adult"
    adult = "adult"
    middle_aged = "middle_aged"
    senior = "senior"


class DietaryGroup(str, enum.Enum):
    """Dietary preferences and restrictions"""
    omnivore = "omnivore"
    vegetarian = "vegetarian"
    lacto_vegetarian = "lacto_vegetarian"
    ovo_vegetarian = "ovo_vegetarian"
    lacto_ovo_vegetarian = "lacto_ovo_vegetarian"
    pescatarian = "pescatarian"
    vegan = "vegan"
    raw_vegan = "raw_vegan"
    plant_based = "plant_based"
    flexitarian = "flexitarian"
    halal = "halal"
    kosher = "kosher"
    jain = "jain"
    buddhist_vegetarian = "buddhist_vegetarian"
    gluten_free = "gluten_free"
    nut_free = "nut_free"
    peanut_free = "peanut_free"
    dairy_free = "dairy_free"
    egg_free = "egg_free"
    soy_free = "soy_free"
    shellfish_free = "shellfish_free"
    fish_free = "fish_free"
    sesame_free = "sesame_free"
    low_carb = "low_carb"
    low_fat = "low_fat"
    low_sodium = "low_sodium"
    sugar_free = "sugar_free"
    no_added_sugar = "no_added_sugar"
    high_protein = "high_protein"
    high_fiber = "high_fiber"
    low_cholesterol = "low_cholesterol"
    low_calorie = "low_calorie"
    keto = "keto"
    paleo = "paleo"
    whole30 = "whole30"
    mediterranean = "mediterranean"
    # Present in the SQL enum and in schemas.DietaryGroupEnum but missing here,
    # so a PATCH carrying it passed Pydantic and then raised ValueError inside
    # DietaryGroup(dg) — a 500 on a value the schema advertises as valid.
    diabetic_friendly = "diabetic_friendly"


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
    meal_plans: Mapped[List["MealPlan"]] = relationship(
        "MealPlan", back_populates="household", cascade="all, delete-orphan"
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
    meal_plan_assignments: Mapped[List["MealPlanMember"]] = relationship(
        "MealPlanMember", back_populates="member", cascade="all, delete-orphan"
    )
    favorites: Mapped[List["MemberFavorite"]] = relationship(
        "MemberFavorite", back_populates="member", cascade="all, delete-orphan"
    )
    adapted_recipes: Mapped[List["MemberAdaptedRecipe"]] = relationship(
        "MemberAdaptedRecipe", back_populates="member", cascade="all, delete-orphan"
    )
    saved_meal_plans: Mapped[List["SavedMealPlan"]] = relationship(
        "SavedMealPlan", back_populates="member", cascade="all, delete-orphan"
    )
    saved_items: Mapped[List["MemberSavedItem"]] = relationship(
        "MemberSavedItem", back_populates="member", cascade="all, delete-orphan"
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
        if include_profile:
            insp = sa_inspect(self)
            if "profile" not in insp.unloaded and self.profile:
                result["profile"] = self.profile.to_dict()
            # else: do not touch relationship; avoid triggering IO
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
    allergies = mapped_column(ARRAY(String), nullable=True, default=list)
    properties = mapped_column(JSONB, nullable=True, default=dict)
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
            "allergies": self.allergies or [],
            "properties": self.properties or {},
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class MemberFavorite(Base):
    """
    Recipe favorited by a household member.

    recipe_id is an opaque RecipeWrangler identifier; favorites are scoped
    per member and removed with the member via ON DELETE CASCADE.
    """

    __tablename__ = "member_favorite"
    __table_args__ = {"schema": "wisefood"}

    member_id = mapped_column(
        String(100),
        ForeignKey("wisefood.household_member.id", ondelete="CASCADE", onupdate="CASCADE"),
        primary_key=True,
    )
    recipe_id = mapped_column(String(128), primary_key=True)
    created_at = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    member: Mapped["HouseholdMember"] = relationship("HouseholdMember", back_populates="favorites")

    def to_dict(self) -> dict:
        return {
            "recipe_id": self.recipe_id,
            "created_at": self.created_at.isoformat(),
        }


class MemberSavedItem(Base):
    """
    A typed entry in a member's library.

    Generalises MemberFavorite: item_type says what kind of asset item_ref
    points at ('recipe' -> opaque RecipeWrangler id; 'article'/'guide'/
    'textbook' -> a urn:<type>:<slug> catalog handle). The gateway treats
    item_ref as opaque. Recipe rows are the same data the /favorites endpoints
    serve, so those keep working as a recipe-only view over this table.
    """

    # Types accepted today. Kept deliberately small; adding one is a code change
    # here plus (for literature) a UI affordance, but needs no migration.
    RECIPE = "recipe"
    ALLOWED_TYPES = frozenset({"recipe", "article", "guide", "textbook"})

    __tablename__ = "member_saved_item"
    __table_args__ = (
        Index("ix_member_saved_item_member_created", "member_id", "created_at"),
        Index("ix_member_saved_item_member_type", "member_id", "item_type"),
        {"schema": "wisefood"},
    )

    member_id = mapped_column(
        String(100),
        ForeignKey("wisefood.household_member.id", ondelete="CASCADE", onupdate="CASCADE"),
        primary_key=True,
    )
    item_type = mapped_column(String(32), primary_key=True)
    item_ref = mapped_column(String(512), primary_key=True)
    created_at = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    member: Mapped["HouseholdMember"] = relationship(
        "HouseholdMember", back_populates="saved_items"
    )

    def to_dict(self) -> dict:
        return {
            "item_type": self.item_type,
            "item_ref": self.item_ref,
            "created_at": self.created_at.isoformat(),
        }

    def to_favorite_dict(self) -> dict:
        """Legacy shape for the recipe-only /favorites endpoints."""
        return {
            "recipe_id": self.item_ref,
            "created_at": self.created_at.isoformat(),
        }


class MemberAdaptedRecipe(Base):
    """
    A member's personal adapted version of a RecipeWrangler recipe.

    recipe_id is the ORIGINAL opaque RecipeWrangler identifier; one adaptation
    per (member, recipe) — saving again replaces the previous adaptation.
    payload holds the adapted recipe (title, ingredients, applied swap/reduce,
    simulated nutrition) and is only ever served back to the owning member's
    household owner (or admin/agent service callers). Removed with the member
    via ON DELETE CASCADE.
    """

    __tablename__ = "member_adapted_recipe"
    __table_args__ = {"schema": "wisefood"}

    member_id = mapped_column(
        String(100),
        ForeignKey("wisefood.household_member.id", ondelete="CASCADE", onupdate="CASCADE"),
        primary_key=True,
    )
    recipe_id = mapped_column(String(128), primary_key=True)
    title = mapped_column(String(512), nullable=True)
    payload = mapped_column(JSONB, nullable=False, default=dict)
    created_at = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    member: Mapped["HouseholdMember"] = relationship("HouseholdMember", back_populates="adapted_recipes")

    def to_dict(self) -> dict:
        return {
            "recipe_id": self.recipe_id,
            "title": self.title,
            "payload": self.payload or {},
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class UserConsent(Base):
    """
    GDPR-style consent record for a Keycloak USER (user_id is the token
    ``sub`` claim), NOT a household member.

    Append-only ledger: every acceptance inserts a new row and rows are never
    updated or deleted, so the trail stays auditable. The latest row per
    (user_id, consent_type) is the currently effective consent.

    Legal scope: the recorded consent covers cookies and the processing of
    personal information solely for the provision of the service
    (purpose limitation).
    """

    __tablename__ = "user_consent"
    __table_args__ = {"schema": "wisefood"}

    id = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id = mapped_column(String(100), nullable=False, index=True)
    consent_type = mapped_column(
        String(64), nullable=False, default="service_data_processing"
    )
    version = mapped_column(String(16), nullable=False)
    granted_at = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    ip_address = mapped_column(String(64), nullable=True)

    def to_dict(self) -> dict:
        return {
            "consent_type": self.consent_type,
            "version": self.version,
            "granted_at": self.granted_at.isoformat(),
            "ip_address": self.ip_address,
        }


class MealPlan(Base):
    """
    Household meal plan for a specific date.

    One meal plan can be assigned to one or more household members.
    """

    __tablename__ = "meal_plan"
    __table_args__ = (
        Index("ix_meal_plan_household_id", "household_id"),
        Index("ix_meal_plan_applied_on", "applied_on"),
        {"schema": "wisefood"},
    )

    id = mapped_column(String(64), primary_key=True, default=lambda: str(uuid4()))
    household_id = mapped_column(
        String(100),
        ForeignKey("wisefood.household.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
    )
    applied_on = mapped_column(Date, nullable=False)
    source_meal_plan_id = mapped_column(String(100), nullable=True)
    source_created_at = mapped_column(DateTime(timezone=True), nullable=True)
    breakfast = mapped_column(JSONB, nullable=False, default=dict)
    lunch = mapped_column(JSONB, nullable=False, default=dict)
    dinner = mapped_column(JSONB, nullable=False, default=dict)
    reasoning = mapped_column(Text, nullable=True)
    created_at = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    household: Mapped["Household"] = relationship("Household", back_populates="meal_plans")
    assignments: Mapped[List["MealPlanMember"]] = relationship(
        "MealPlanMember", back_populates="meal_plan", cascade="all, delete-orphan"
    )

    def to_dict(
        self,
        include_member_ids: bool = False,
        current_member_id: Optional[str] = None,
    ) -> dict:
        result = {
            "id": self.id,
            "household_id": self.household_id,
            "date": self.applied_on.isoformat(),
            "source_meal_plan_id": self.source_meal_plan_id,
            "source_created_at": self.source_created_at.isoformat() if self.source_created_at else None,
            "breakfast": self.breakfast or {},
            "lunch": self.lunch or {},
            "dinner": self.dinner or {},
            "reasoning": self.reasoning,
            "created_at": self.created_at.isoformat(),
        }

        member_ids: List[str] = []
        if include_member_ids:
            insp = sa_inspect(self)
            if "assignments" not in insp.unloaded:
                member_ids = sorted([a.member_id for a in self.assignments])
            result["applies_to_member_ids"] = member_ids
            if current_member_id:
                result["other_member_ids"] = [mid for mid in member_ids if mid != current_member_id]
            else:
                result["other_member_ids"] = member_ids

        return result


class SavedMealPlan(Base):
    """
    A meal plan a member saved to their library under a name.

    A snapshot, not a reference: the meals are copied in, so revoking the
    scheduled plan it came from does not empty the saved copy. Kept separate
    from MealPlan because a scheduled plan is pinned to a date and a saved one
    deliberately is not — see schemas/30_meal_plan_library.sql.
    """

    __tablename__ = "saved_meal_plan"
    __table_args__ = (
        Index("ix_saved_meal_plan_member_id", "member_id"),
        Index("ix_saved_meal_plan_member_created", "member_id", "created_at"),
        {"schema": "wisefood"},
    )

    id = mapped_column(String(64), primary_key=True, default=lambda: str(uuid4()))
    member_id = mapped_column(
        String(100),
        ForeignKey("wisefood.household_member.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
    )
    name = mapped_column(String(255), nullable=False)
    # Provenance only — intentionally not a FK, since the plan this was saved
    # from may be revoked while the saved copy remains.
    source_meal_plan_id = mapped_column(String(64), nullable=True)
    source_applied_on = mapped_column(Date, nullable=True)
    breakfast = mapped_column(JSONB, nullable=False, default=dict)
    lunch = mapped_column(JSONB, nullable=False, default=dict)
    dinner = mapped_column(JSONB, nullable=False, default=dict)
    reasoning = mapped_column(Text, nullable=True)
    created_at = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    member: Mapped["HouseholdMember"] = relationship(
        "HouseholdMember", back_populates="saved_meal_plans"
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "member_id": self.member_id,
            "name": self.name,
            "source_meal_plan_id": self.source_meal_plan_id,
            "source_applied_on": (
                self.source_applied_on.isoformat() if self.source_applied_on else None
            ),
            "breakfast": self.breakfast or {},
            "lunch": self.lunch or {},
            "dinner": self.dinner or {},
            "reasoning": self.reasoning,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class MealPlanMember(Base):
    """
    Join table assigning a meal plan to household members.
    """

    __tablename__ = "meal_plan_member"
    __table_args__ = (
        UniqueConstraint("meal_plan_id", "member_id", name="uq_meal_plan_member_plan_member"),
        Index("ix_meal_plan_member_member_id", "member_id"),
        {"schema": "wisefood"},
    )

    id = mapped_column(String(64), primary_key=True, default=lambda: str(uuid4()))
    meal_plan_id = mapped_column(
        String(64),
        ForeignKey("wisefood.meal_plan.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    member_id = mapped_column(
        String(100),
        ForeignKey("wisefood.household_member.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
    )
    created_at = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    meal_plan: Mapped["MealPlan"] = relationship("MealPlan", back_populates="assignments")
    member: Mapped["HouseholdMember"] = relationship("HouseholdMember", back_populates="meal_plan_assignments")
