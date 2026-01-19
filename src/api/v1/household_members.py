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
from sql import HouseholdMember, HouseholdMemberProfile, Household, AgeGroup, DietaryGroup
from exceptions import NotFoundError, ConflictError
from schemas import HouseholdMemberResponse, HouseholdMemberCreate, HouseholdMemberUpdate
from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY


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
        household_id = query.get("household_id")
        return await self.fetch(household_id=household_id)

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
            # Verify member exists
            result = await db.execute(
                select(HouseholdMember).where(HouseholdMember.id == member_id)
            )
            member = result.scalar_one_or_none()
            if not member:
                raise NotFoundError(f"Household member {member_id} not found")

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
HOUSEHOLD_MEMBER = HouseholdMemberEntity()
