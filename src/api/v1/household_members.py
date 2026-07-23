"""
Household Member Entity implementation with full CRUD operations
"""
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from uuid import uuid4

from entity import Entity
from sql import HouseholdMember, HouseholdMemberProfile, Household, MemberAdaptedRecipe, MemberFavorite, MemberSavedItem, AgeGroup, DietaryGroup
from exceptions import NotFoundError, ConflictError, DataError
from schemas import HouseholdMemberResponse, HouseholdMemberCreate, HouseholdMemberUpdate
from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
from backend.redis import REDIS

# Member profiles are read on nearly every personalized request (search
# personalization, FoodScholar context, FoodChat plans) but change rarely —
# cache them briefly and invalidate on every write. Cache failures must never
# break profile reads, hence the blanket try/excepts.
_PROFILE_CACHE_TTL_SECONDS = 300


def _profile_cache_key(member_id: str) -> str:
    return f"member_profile:{member_id}"


def _profile_cache_get(member_id: str):
    try:
        value = REDIS.get(_profile_cache_key(member_id))
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def _profile_cache_put(member_id: str, profile: Dict[str, Any]) -> None:
    try:
        REDIS.set(_profile_cache_key(member_id), profile, ttl_seconds=_PROFILE_CACHE_TTL_SECONDS)
    except Exception:
        pass


def _profile_cache_invalidate(member_id: str) -> None:
    try:
        REDIS.delete(_profile_cache_key(member_id))
    except Exception:
        pass


class HouseholdMemberEntity(Entity):
    """
    Household Member entity for managing member resources via the Entity API pattern.

    Members are dependent on households - they cannot exist without a parent household.

    Includes CRUD operations for:
    - Household Members
    - Household Member Profiles
    """

    def __init__(self):
        super().__init__(
            name="household_member",
            collection_name="household_members",
            orm_class=HouseholdMember,
            dump_schema=HouseholdMemberResponse,
            creation_schema=HouseholdMemberCreate,
            update_schema=HouseholdMemberUpdate,
        )

    # ========== Household Member Operations ==========

    async def fetch(
        self,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        household_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Fetch a list of household members.

        :param limit: Maximum number of members to return
        :param offset: Number of members to skip
        :param household_id: Filter by household ID
        :return: List of member dictionaries
        """
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            query = select(HouseholdMember).options(
            selectinload(HouseholdMember.profile)
            )

            if household_id:
                query = query.where(HouseholdMember.household_id == household_id)

            query = query.order_by(HouseholdMember.joined_at.desc())

            if offset:
                query = query.offset(offset)
            if limit:
                query = query.limit(limit)

            result = await db.execute(query)
            members = list(result.scalars().all())

            return [m.to_dict(include_profile=True) for m in members]

    async def list(
        self,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        household_id: Optional[str] = None,
    ) -> List[str]:
        """
        List household member IDs.

        :param limit: Maximum number of IDs to return
        :param offset: Number of IDs to skip
        :param household_id: Filter by household ID
        :return: List of member IDs
        """
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            query = select(HouseholdMember.id)

            if household_id:
                query = query.where(HouseholdMember.household_id == household_id)

            query = query.order_by(HouseholdMember.joined_at.desc())

            if offset:
                query = query.offset(offset)
            if limit:
                query = query.limit(limit)

            result = await db.execute(query)
            return result.scalars().all()

    async def get(self, entity_id: str) -> Dict[str, Any]:
        """
        Get a household member by ID.

        :param entity_id: The member ID
        :return: Member dictionary
        """
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            result = await db.execute(
                select(HouseholdMember)
                .options(selectinload(HouseholdMember.profile))
                .where(HouseholdMember.id == entity_id)
            )
            member = result.scalar_one_or_none()

            if not member:
                raise NotFoundError(f"Household member {entity_id} not found")

            return member.to_dict(include_profile=True)

    async def create(
        self,
        spec: Dict[str, Any],
        creator: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Create a new household member.

        Members are dependent on households - household_id is required.

        :param spec: Member creation data (must include household_id)
        :param creator: Creator user dict (from token payload)
        :return: Created member dictionary
        """
        household_id = spec.get("household_id")
        if not household_id:
            raise ConflictError("household_id is required to create a member")

        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            # Verify household exists
            result = await db.execute(
                select(Household).where(Household.id == household_id)
            )
            household = result.scalar_one_or_none()
            if not household:
                raise NotFoundError(f"Household {household_id} not found")

            # Create member
            member_id = str(uuid4())
            age_group_value = spec.get("age_group")
            if isinstance(age_group_value, str):
                age_group = AgeGroup(age_group_value)
            else:
                age_group = age_group_value

            member = HouseholdMember(
                id=member_id,
                name=spec["name"],
                image_url=spec.get("image_url"),
                age_group=age_group,
                household_id=household_id,
            )
            db.add(member)
            await db.flush()

            # Create profile if provided
            profile_data = spec.get("profile")
            if profile_data:
                await self._create_member_profile_in_session(db, member_id, profile_data)

            await db.commit()

            result = await db.execute(
                select(HouseholdMember)
                .options(selectinload(HouseholdMember.profile))
                .where(HouseholdMember.id == member_id)
            )
            member = result.scalar_one()
            return member.to_dict(include_profile=True)

    async def patch(
        self,
        entity_id: str,
        spec: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Update a household member.

        :param entity_id: The member ID
        :param spec: Update data
        :return: Updated member dictionary
        """
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            result = await db.execute(
                select(HouseholdMember)
                .options(selectinload(HouseholdMember.profile))
                .where(HouseholdMember.id == entity_id)
            )
            member = result.scalar_one_or_none()

            if not member:
                raise NotFoundError(f"Household member {entity_id} not found")

            if "name" in spec and spec["name"] is not None:
                member.name = spec["name"]
            if "age_group" in spec and spec["age_group"] is not None:
                age_group_value = spec["age_group"]
                if isinstance(age_group_value, str):
                    member.age_group = AgeGroup(age_group_value)
                else:
                    member.age_group = age_group_value
            if "image_url" in spec:
                member.image_url = spec["image_url"]

            # Update profile if provided
            if "profile" in spec and spec["profile"] is not None:
                await self._create_member_profile_in_session(
                    db, entity_id, spec["profile"]
                )

            await db.flush()
            await db.commit()
            return member.to_dict(include_profile=True)

    async def delete(
        self,
        entity_id: str,
    ) -> bool:
        """
        Delete a household member.

        :param entity_id: The member ID
        :return: True if deleted
        """
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            result = await db.execute(
                delete(HouseholdMember).where(HouseholdMember.id == entity_id)
            )
            await db.commit()
            # The member's profile is removed with them (cascade).
            _profile_cache_invalidate(entity_id)
            return result.rowcount > 0

    async def search(
        self,
        query: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """
        Search for household members.

        :param query: Search query (supports household_id filter)
        :return: List of matching members
        """
        raise NotImplementedError("Search not implemented for HouseholdMemberEntity")

    # ========== Household Member Profile Operations ==========

    async def _create_member_profile_in_session(
        self,
        db: AsyncSession,
        member_id: str,
        profile_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Internal method to create or update a member's profile within an existing session.

        :param db: Database session
        :param member_id: The member ID
        :param profile_data: Profile data
        :return: Created/updated profile dictionary
        """
        # Check if profile already exists
        result = await db.execute(
            select(HouseholdMemberProfile).where(
                HouseholdMemberProfile.household_member_id == member_id
            )
        )
        existing_profile = result.scalar_one_or_none()

        if existing_profile:
            # Update existing profile
            if "nutritional_preferences" in profile_data:
                existing_profile.nutritional_preferences = profile_data["nutritional_preferences"]
            if "dietary_groups" in profile_data:
                dietary_groups = profile_data["dietary_groups"]
                if dietary_groups:
                    existing_profile.dietary_groups = [
                        DietaryGroup(dg) if isinstance(dg, str) else dg
                        for dg in dietary_groups
                    ]
                else:
                    existing_profile.dietary_groups = []
            if "allergies" in profile_data:
                existing_profile.allergies = profile_data.get("allergies") or []
            existing_profile.updated_at = datetime.now(timezone.utc)
            await db.flush()
            return existing_profile.to_dict()

        # Create new profile
        profile_id = str(uuid4())
        dietary_groups = profile_data.get("dietary_groups", [])
        if dietary_groups:
            dietary_groups = [
                DietaryGroup(dg) if isinstance(dg, str) else dg
                for dg in dietary_groups
            ]

        profile = HouseholdMemberProfile(
            id=profile_id,
            household_member_id=member_id,
            nutritional_preferences=profile_data.get("nutritional_preferences", {}),
            dietary_groups=dietary_groups,
            allergies=profile_data.get("allergies", []),
        )
        db.add(profile)
        await db.flush()
        return profile.to_dict()

    async def create_member_profile(
        self,
        member_id: str,
        profile_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Create or update a member's profile.

        :param member_id: The member ID
        :param profile_data: Profile data
        :return: Created/updated profile dictionary
        """
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            # Verify member exists
            result = await db.execute(
                select(HouseholdMember).where(HouseholdMember.id == member_id)
            )
            member = result.scalar_one_or_none()
            if not member:
                raise NotFoundError(f"Household member {member_id} not found")

            profile_dict = await self._create_member_profile_in_session(db, member_id, profile_data)
            await db.commit()
            _profile_cache_invalidate(member_id)
            return profile_dict

    async def get_member_profile(
        self,
        member_id: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Get a member's profile.

        :param member_id: The member ID
        :return: Profile dictionary or None
        """
        cached = _profile_cache_get(member_id)
        if cached is not None:
            return cached

        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            result = await db.execute(
                select(HouseholdMemberProfile).where(
                    HouseholdMemberProfile.household_member_id == member_id
                )
            )
            profile = result.scalar_one_or_none()

            if profile:
                profile_dict = profile.to_dict()
                _profile_cache_put(member_id, profile_dict)
                return profile_dict
            return None

    async def update_member_profile(
        self,
        member_id: str,
        profile_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Update a member's profile.

        :param member_id: The member ID
        :param profile_data: Profile update data
        :return: Updated profile dictionary
        """
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            result = await db.execute(
                select(HouseholdMemberProfile).where(
                    HouseholdMemberProfile.household_member_id == member_id
                )
            )
            profile = result.scalar_one_or_none()

            if not profile:
                raise NotFoundError(f"Profile for household member {member_id} not found")

            if "nutritional_preferences" in profile_data:
                profile.nutritional_preferences = profile_data["nutritional_preferences"]
            if "dietary_groups" in profile_data:
                dietary_groups = profile_data["dietary_groups"]
                if dietary_groups:
                    profile.dietary_groups = [
                        DietaryGroup(dg) if isinstance(dg, str) else dg
                        for dg in dietary_groups
                    ]
                else:
                    profile.dietary_groups = []
            if "allergies" in profile_data:
                profile.allergies = profile_data.get("allergies") or []
            if "properties" in profile_data:
                profile.properties = profile_data["properties"]

            profile.updated_at = datetime.now(timezone.utc)
            await db.flush()
            await db.commit()
            profile_dict = profile.to_dict()
            _profile_cache_invalidate(member_id)
            return profile_dict

    async def delete_member_profile(
        self,
        member_id: str,
    ) -> bool:
        """
        Delete a member's profile.

        :param member_id: The member ID
        :return: True if deleted
        """
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            result = await db.execute(
                delete(HouseholdMemberProfile).where(
                    HouseholdMemberProfile.household_member_id == member_id
                )
            )
            await db.commit()
            _profile_cache_invalidate(member_id)
            return result.rowcount > 0

    # ========== Member Saved Item (Library) Operations ==========
    #
    # The library is one typed table (MemberSavedItem). Recipe favourites are
    # just item_type='recipe' rows in it, so the /favorites methods below are a
    # recipe-only view that keeps the old contract for FoodChat / RecipeWrangler
    # while the generic saved-item methods serve every type.

    @staticmethod
    def _validate_item_type(item_type: str) -> str:
        if item_type not in MemberSavedItem.ALLOWED_TYPES:
            raise DataError(
                detail=(
                    f"Unsupported saved item type '{item_type}'. "
                    f"Allowed: {', '.join(sorted(MemberSavedItem.ALLOWED_TYPES))}."
                )
            )
        return item_type

    async def list_saved_items(
        self,
        member_id: str,
        item_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        List a member's saved items, newest first, optionally filtered by type.
        """
        if item_type is not None:
            self._validate_item_type(item_type)

        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            query = select(MemberSavedItem).where(MemberSavedItem.member_id == member_id)
            if item_type is not None:
                query = query.where(MemberSavedItem.item_type == item_type)
            result = await db.execute(query.order_by(MemberSavedItem.created_at.desc()))
            return [i.to_dict() for i in result.scalars().all()]

    async def add_saved_item(
        self,
        member_id: str,
        item_type: str,
        item_ref: str,
    ) -> Dict[str, Any]:
        """
        Add an item to a member's library (idempotent).

        Re-adding an existing item returns the existing row unchanged.
        """
        self._validate_item_type(item_type)

        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            result = await db.execute(
                select(MemberSavedItem).where(
                    MemberSavedItem.member_id == member_id,
                    MemberSavedItem.item_type == item_type,
                    MemberSavedItem.item_ref == item_ref,
                )
            )
            existing = result.scalar_one_or_none()
            if existing:
                return existing.to_dict()

            saved = MemberSavedItem(
                member_id=member_id, item_type=item_type, item_ref=item_ref
            )
            db.add(saved)
            await db.flush()
            saved_dict = saved.to_dict()
            await db.commit()
            return saved_dict

    async def remove_saved_item(
        self,
        member_id: str,
        item_type: str,
        item_ref: str,
    ) -> bool:
        """
        Remove an item from a member's library (idempotent).

        :return: True if a row was deleted, False if it did not exist.
        """
        self._validate_item_type(item_type)

        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            result = await db.execute(
                delete(MemberSavedItem).where(
                    MemberSavedItem.member_id == member_id,
                    MemberSavedItem.item_type == item_type,
                    MemberSavedItem.item_ref == item_ref,
                )
            )
            await db.commit()
            return result.rowcount > 0

    # ---------- Recipe-only favourites view (legacy contract) ----------

    async def list_favorites(
        self,
        member_id: str,
    ) -> List[Dict[str, Any]]:
        """
        List a member's favorite recipes, newest first.

        Recipe rows of the library, in the legacy {recipe_id, created_at} shape
        FoodChat and RecipeWrangler consume.
        """
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            result = await db.execute(
                select(MemberSavedItem)
                .where(
                    MemberSavedItem.member_id == member_id,
                    MemberSavedItem.item_type == MemberSavedItem.RECIPE,
                )
                .order_by(MemberSavedItem.created_at.desc())
            )
            return [f.to_favorite_dict() for f in result.scalars().all()]

    async def add_favorite(
        self,
        member_id: str,
        recipe_id: str,
    ) -> Dict[str, Any]:
        """
        Add a recipe to a member's favorites (idempotent).

        :param member_id: The member ID
        :param recipe_id: Opaque RecipeWrangler recipe ID
        :return: Favorite dictionary in the legacy shape
        """
        await self.add_saved_item(
            member_id=member_id,
            item_type=MemberSavedItem.RECIPE,
            item_ref=recipe_id,
        )
        # Re-read so the returned created_at reflects the stored row on both the
        # insert and the idempotent-hit path.
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            result = await db.execute(
                select(MemberSavedItem).where(
                    MemberSavedItem.member_id == member_id,
                    MemberSavedItem.item_type == MemberSavedItem.RECIPE,
                    MemberSavedItem.item_ref == recipe_id,
                )
            )
            saved = result.scalar_one()
            return saved.to_favorite_dict()

    async def remove_favorite(
        self,
        member_id: str,
        recipe_id: str,
    ) -> bool:
        """
        Remove a recipe from a member's favorites (idempotent).

        :return: True if a favorite was deleted, False if it did not exist
        """
        return await self.remove_saved_item(
            member_id=member_id,
            item_type=MemberSavedItem.RECIPE,
            item_ref=recipe_id,
        )

    # ========== Member Adapted Recipe Operations ==========

    async def list_adapted_recipes(
        self,
        member_id: str,
    ) -> List[Dict[str, Any]]:
        """
        List a member's adapted recipes, most recently updated first.

        :param member_id: The member ID
        :return: List of adapted-recipe dictionaries
        """
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            result = await db.execute(
                select(MemberAdaptedRecipe)
                .where(MemberAdaptedRecipe.member_id == member_id)
                .order_by(MemberAdaptedRecipe.updated_at.desc())
            )
            return [r.to_dict() for r in result.scalars().all()]

    async def get_adapted_recipe(
        self,
        member_id: str,
        recipe_id: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Get a member's adaptation of one recipe, or None if not saved.

        :param member_id: The member ID
        :param recipe_id: Original opaque RecipeWrangler recipe ID
        :return: Adapted-recipe dictionary or None
        """
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            result = await db.execute(
                select(MemberAdaptedRecipe).where(
                    MemberAdaptedRecipe.member_id == member_id,
                    MemberAdaptedRecipe.recipe_id == recipe_id,
                )
            )
            row = result.scalar_one_or_none()
            return row.to_dict() if row else None

    async def upsert_adapted_recipe(
        self,
        member_id: str,
        recipe_id: str,
        title: Optional[str],
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Save (or replace) a member's adaptation of a recipe.

        One adaptation per (member, recipe): saving again overwrites the
        previous title/payload.

        :param member_id: The member ID
        :param recipe_id: Original opaque RecipeWrangler recipe ID
        :param title: Display title of the adapted recipe
        :param payload: Adapted recipe content (ingredients, swap, nutrition)
        :return: Adapted-recipe dictionary
        """
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            result = await db.execute(
                select(MemberAdaptedRecipe).where(
                    MemberAdaptedRecipe.member_id == member_id,
                    MemberAdaptedRecipe.recipe_id == recipe_id,
                )
            )
            existing = result.scalar_one_or_none()
            if existing:
                existing.title = title
                existing.payload = payload or {}
                existing.updated_at = datetime.now(timezone.utc)
                await db.flush()
                adapted_dict = existing.to_dict()
            else:
                adapted = MemberAdaptedRecipe(
                    member_id=member_id,
                    recipe_id=recipe_id,
                    title=title,
                    payload=payload or {},
                )
                db.add(adapted)
                await db.flush()
                adapted_dict = adapted.to_dict()
            await db.commit()
            return adapted_dict

    async def remove_adapted_recipe(
        self,
        member_id: str,
        recipe_id: str,
    ) -> bool:
        """
        Remove a member's adaptation of a recipe (idempotent).

        :param member_id: The member ID
        :param recipe_id: Original opaque RecipeWrangler recipe ID
        :return: True if an adaptation was deleted, False if it did not exist
        """
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            result = await db.execute(
                delete(MemberAdaptedRecipe).where(
                    MemberAdaptedRecipe.member_id == member_id,
                    MemberAdaptedRecipe.recipe_id == recipe_id,
                )
            )
            await db.commit()
            return result.rowcount > 0


# Singleton instance
HOUSEHOLD_MEMBER = HouseholdMemberEntity()

# The entity's type guard (MemberSavedItem.ALLOWED_TYPES) and the API contract
# (schemas.SavedItemType) list the same types in two files that cannot import
# each other. Fail loudly at import time if they drift apart.
from typing import get_args as _get_args  # noqa: E402
from schemas import SavedItemType as _SavedItemType  # noqa: E402

assert set(_get_args(_SavedItemType)) == set(MemberSavedItem.ALLOWED_TYPES), (
    "SavedItemType (schemas) and MemberSavedItem.ALLOWED_TYPES (sql) disagree"
)
