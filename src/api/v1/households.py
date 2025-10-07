"""
Household Entity implementation with full CRUD operations
"""
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from uuid import uuid4

from entity import Entity
from sql import Household, HouseholdMember, HouseholdMemberProfile, AgeGroup, DietaryGroup
from exceptions import NotFoundError, ConflictError
from schemas import HouseholdResponse, HouseholdCreate, HouseholdUpdate
from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY


class HouseholdEntity(Entity):
    """
    Household entity for managing household resources via the Entity API pattern.

    Includes CRUD operations for:
    - Households
    - Household Members
    - Household Member Profiles
    """

    def __init__(self):
        # Import schemas locally to avoid circular dependency

        super().__init__(
            name="household",
            collection_name="households",
            orm_class=Household,
            dump_schema=HouseholdResponse,
            creation_schema=HouseholdCreate,
            update_schema=HouseholdUpdate,
        )

    # ========== Household Operations ==========

    async def fetch(
        self,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        owner_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Fetch a list of households.

        :param limit: Maximum number of households to return
        :param offset: Number of households to skip
        :param owner_id: Filter by owner ID
        :return: List of household dictionaries
        """
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            query = select(Household)
            if owner_id:
                query = query.where(Household.owner_id == owner_id)
            query = query.order_by(Household.created_at.desc())

            result = await db.execute(query)
            households = list(result.scalars().all())

            # Apply pagination
            if offset:
                households = households[offset:]
            if limit:
                households = households[:limit]

            return [h.to_dict() for h in households]

    async def list(
        self,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        owner_id: Optional[str] = None,
    ) -> List[str]:
        """
        List household IDs.

        :param limit: Maximum number of IDs to return
        :param offset: Number of IDs to skip
        :param owner_id: Filter by owner ID
        :return: List of household IDs
        """
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            query = select(Household)
            if owner_id:
                query = query.where(Household.owner_id == owner_id)
            query = query.order_by(Household.created_at.desc())

            result = await db.execute(query)
            households = list(result.scalars().all())

            # Apply pagination
            if offset:
                households = households[offset:]
            if limit:
                households = households[:limit]

            return [h.id for h in households]

    async def get(self, entity_id: str) -> Dict[str, Any]:
        """
        Get a household by ID.

        :param entity_id: The household ID
        :return: Household dictionary
        """
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            result = await db.execute(
                select(Household).where(Household.id == entity_id)
            )
            household = result.scalar_one_or_none()

            if not household:
                raise NotFoundError(f"Household {entity_id} not found")

            return household.to_dict(include_members=True)

    async def get_by_owner(self, owner_id: str) -> Optional[Dict[str, Any]]:
        """
        Get the household owned by a specific user.

        :param owner_id: The owner's user ID
        :return: Household dictionary or None
        """
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            result = await db.execute(
                select(Household).where(Household.owner_id == owner_id)
            )
            household = result.scalar_one_or_none()

            if household:
                return household.to_dict(include_members=True)
            return None

    async def create(
        self,
        spec: Dict[str, Any],
        creator: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Create a new household.

        :param spec: Household creation data
        :param creator: Creator user dict (from token payload)
        :return: Created household dictionary
        """
        # Get owner ID from creator
        owner_id = creator.get("sub")

        # Check if owner already has a household
        existing = await self.get_by_owner(owner_id)
        if existing:
            raise ConflictError(f"User {owner_id} already owns a household")

        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            # Create household
            household_id = str(uuid4())
            household = Household(
                id=household_id,
                name=spec["name"],
                region=spec.get("region"),
                owner_id=owner_id,
                metadata_=spec.get("metadata", {}),
            )
            db.add(household)
            await db.flush()

            # Create initial members if provided
            members_data = spec.get("members", [])
            for member_data in members_data:
                # Note: This uses internal method that works within the same session
                await self._add_member_in_session(db, household_id, member_data)

            await db.commit()
            return household.to_dict(include_members=True)

    async def patch(
        self,
        entity_id: str,
        spec: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Update a household.

        :param entity_id: The household ID
        :param spec: Update data
        :return: Updated household dictionary
        """
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            result = await db.execute(
                select(Household).where(Household.id == entity_id)
            )
            household = result.scalar_one_or_none()

            if not household:
                raise NotFoundError(f"Household {entity_id} not found")

            if "name" in spec and spec["name"] is not None:
                household.name = spec["name"]
            if "region" in spec:
                household.region = spec["region"]
            if "metadata" in spec and spec["metadata"] is not None:
                household.metadata_ = spec["metadata"]

            household.updated_at = datetime.now(timezone.utc)
            await db.flush()
            await db.commit()

            return household.to_dict(include_members=True)

    async def delete(
        self,
        entity_id: str,
    ) -> bool:
        """
        Delete a household.

        :param entity_id: The household ID
        :return: True if deleted
        """
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            result = await db.execute(
                delete(Household).where(Household.id == entity_id)
            )
            await db.commit()
            return result.rowcount > 0

    async def search(
        self,
        query: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """
        Search for households.

        :param query: Search query
        :return: List of matching households
        """
        owner_id = query.get("owner_id")
        return await self.fetch(owner_id=owner_id)

    # ========== Household Member Operations ==========

    async def _add_member_in_session(
        self,
        db: AsyncSession,
        household_id: str,
        member_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Internal method to add a member within an existing session.

        :param db: Database session
        :param household_id: The household ID
        :param member_data: Member creation data
        :return: Created member dictionary
        """
        # Create member
        member_id = str(uuid4())
        age_group_value = member_data.get("age_group")
        if isinstance(age_group_value, str):
            age_group = AgeGroup(age_group_value)
        else:
            age_group = age_group_value

        member = HouseholdMember(
            id=member_id,
            name=member_data["name"],
            image_url=member_data.get("image_url"),
            age_group=age_group,
            household_id=household_id,
        )
        db.add(member)
        await db.flush()

        # Create profile if provided
        profile_data = member_data.get("profile")
        if profile_data:
            await self._create_member_profile_in_session(db, member_id, profile_data)

        await db.flush()
        return member.to_dict(include_profile=True)

    async def add_member(
        self,
        household_id: str,
        member_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Add a member to a household.

        :param household_id: The household ID
        :param member_data: Member creation data
        :return: Created member dictionary
        """
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            # Verify household exists
            result = await db.execute(
                select(Household).where(Household.id == household_id)
            )
            household = result.scalar_one_or_none()
            if not household:
                raise NotFoundError(f"Household {household_id} not found")

            member_dict = await self._add_member_in_session(db, household_id, member_data)
            await db.commit()
            return member_dict

    async def get_member(
        self,
        member_id: str,
    ) -> Dict[str, Any]:
        """
        Get a household member by ID.

        :param member_id: The member ID
        :return: Member dictionary
        """
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            result = await db.execute(
                select(HouseholdMember).where(HouseholdMember.id == member_id)
            )
            member = result.scalar_one_or_none()

            if not member:
                raise NotFoundError(f"Member {member_id} not found")

            return member.to_dict(include_profile=True)

    async def list_members(
        self,
        household_id: str,
    ) -> List[Dict[str, Any]]:
        """
        List all members of a household.

        :param household_id: The household ID
        :return: List of member dictionaries
        """
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            result = await db.execute(
                select(HouseholdMember).where(HouseholdMember.household_id == household_id)
            )
            members = list(result.scalars().all())
            return [m.to_dict(include_profile=True) for m in members]

    async def update_member(
        self,
        member_id: str,
        update_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Update a household member.

        :param member_id: The member ID
        :param update_data: Update data
        :return: Updated member dictionary
        """
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            result = await db.execute(
                select(HouseholdMember).where(HouseholdMember.id == member_id)
            )
            member = result.scalar_one_or_none()

            if not member:
                raise NotFoundError(f"Member {member_id} not found")

            if "name" in update_data and update_data["name"] is not None:
                member.name = update_data["name"]
            if "age_group" in update_data and update_data["age_group"] is not None:
                age_group_value = update_data["age_group"]
                if isinstance(age_group_value, str):
                    member.age_group = AgeGroup(age_group_value)
                else:
                    member.age_group = age_group_value
            if "image_url" in update_data:
                member.image_url = update_data["image_url"]

            await db.flush()
            await db.commit()
            return member.to_dict(include_profile=True)

    async def remove_member(
        self,
        member_id: str,
    ) -> bool:
        """
        Remove a member from a household.

        :param member_id: The member ID
        :return: True if deleted
        """
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            result = await db.execute(
                delete(HouseholdMember).where(HouseholdMember.id == member_id)
            )
            await db.commit()
            return result.rowcount > 0

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
            profile_dict = await self._create_member_profile_in_session(db, member_id, profile_data)
            await db.commit()
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
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            result = await db.execute(
                select(HouseholdMemberProfile).where(
                    HouseholdMemberProfile.household_member_id == member_id
                )
            )
            profile = result.scalar_one_or_none()

            if profile:
                return profile.to_dict()
            return None

    async def update_member_profile(
        self,
        member_id: str,
        profile_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Update a member's profile (creates if doesn't exist).

        :param member_id: The member ID
        :param profile_data: Profile update data
        :return: Updated profile dictionary
        """
        return await self.create_member_profile(member_id, profile_data)

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
            return result.rowcount > 0


# Singleton instance
HOUSEHOLD = HouseholdEntity()
