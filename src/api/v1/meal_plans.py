"""
Meal plan entity implementation for household members.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from backend.postgres import POSTGRES_ASYNC_SESSION_FACTORY
from exceptions import ConflictError, NotFoundError
from sql import HouseholdMember, MealPlan, MealPlanMember


class MealPlanEntity:
    async def _get_members_in_same_household(
        self,
        member_ids: List[str],
    ) -> List[HouseholdMember]:
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            result = await db.execute(
                select(HouseholdMember).where(HouseholdMember.id.in_(member_ids))
            )
            members = list(result.scalars().all())

            if len(members) != len(set(member_ids)):
                found_ids = {m.id for m in members}
                missing = sorted(set(member_ids) - found_ids)
                raise NotFoundError(
                    detail=f"Members not found: {', '.join(missing)}"
                )

            household_ids = {m.household_id for m in members}
            if len(household_ids) != 1:
                raise ConflictError(
                    detail="All assigned members must belong to the same household"
                )

            return members

    async def store_for_members(
        self,
        *,
        requested_member_id: str,
        target_date: date,
        meal_plan_spec: Dict[str, Any],
        additional_member_ids: List[str],
    ) -> Dict[str, Any]:
        target_member_ids = sorted(set([requested_member_id, *additional_member_ids]))
        members = await self._get_members_in_same_household(target_member_ids)
        household_id = members[0].household_id

        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            existing_result = await db.execute(
                select(MealPlanMember.member_id)
                .join(MealPlan, MealPlan.id == MealPlanMember.meal_plan_id)
                .where(
                    MealPlan.household_id == household_id,
                    MealPlan.applied_on == target_date,
                    MealPlanMember.member_id.in_(target_member_ids),
                )
            )
            conflicting_member_ids = sorted(set(existing_result.scalars().all()))
            if conflicting_member_ids:
                raise ConflictError(
                    detail=(
                        "Meal plan already exists for date "
                        f"{target_date.isoformat()} and members: {', '.join(conflicting_member_ids)}"
                    )
                )

            source_created_at = meal_plan_spec.get("created_at")
            if isinstance(source_created_at, str):
                source_created_at = source_created_at.replace("Z", "+00:00")
                source_created_at = datetime.fromisoformat(source_created_at)

            plan = MealPlan(
                id=str(uuid4()),
                household_id=household_id,
                applied_on=target_date,
                source_meal_plan_id=meal_plan_spec.get("id"),
                source_created_at=source_created_at,
                breakfast=meal_plan_spec["breakfast"],
                lunch=meal_plan_spec["lunch"],
                dinner=meal_plan_spec["dinner"],
                reasoning=meal_plan_spec.get("reasoning"),
            )
            db.add(plan)
            await db.flush()

            for member_id in target_member_ids:
                db.add(
                    MealPlanMember(
                        id=str(uuid4()),
                        meal_plan_id=plan.id,
                        member_id=member_id,
                    )
                )

            await db.commit()

            result = await db.execute(
                select(MealPlan)
                .options(selectinload(MealPlan.assignments))
                .where(MealPlan.id == plan.id)
            )
            created_plan = result.scalar_one()
            return created_plan.to_dict(
                include_member_ids=True,
                current_member_id=requested_member_id,
            )

    async def get_for_member_and_date(
        self,
        *,
        member_id: str,
        target_date: date,
    ) -> Dict[str, Any]:
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            result = await db.execute(
                select(MealPlan)
                .join(MealPlanMember, MealPlanMember.meal_plan_id == MealPlan.id)
                .options(selectinload(MealPlan.assignments))
                .where(
                    MealPlanMember.member_id == member_id,
                    MealPlan.applied_on == target_date,
                )
                .order_by(MealPlan.created_at.desc())
            )
            plan = result.scalars().first()
            if not plan:
                raise NotFoundError(
                    detail=(
                        f"No meal plan found for member {member_id} "
                        f"on date {target_date.isoformat()}"
                    )
                )
            return plan.to_dict(
                include_member_ids=True,
                current_member_id=member_id,
            )

    async def revoke(
        self,
        *,
        member_id: str,
        meal_plan_id: str,
        revoke_for_all_members: bool = False,
    ) -> Dict[str, Any]:
        async with POSTGRES_ASYNC_SESSION_FACTORY()() as db:
            member_result = await db.execute(
                select(HouseholdMember).where(HouseholdMember.id == member_id)
            )
            member = member_result.scalar_one_or_none()
            if not member:
                raise NotFoundError(detail=f"Household member {member_id} not found")

            plan_result = await db.execute(
                select(MealPlan)
                .options(selectinload(MealPlan.assignments))
                .where(MealPlan.id == meal_plan_id)
            )
            meal_plan = plan_result.scalar_one_or_none()
            if not meal_plan:
                raise NotFoundError(detail=f"Meal plan {meal_plan_id} not found")

            if meal_plan.household_id != member.household_id:
                raise NotFoundError(
                    detail=f"Meal plan {meal_plan_id} not found for member {member_id}"
                )

            assigned_member_ids = {assignment.member_id for assignment in meal_plan.assignments}
            if member_id not in assigned_member_ids:
                raise NotFoundError(
                    detail=f"Meal plan {meal_plan_id} not assigned to member {member_id}"
                )

            meal_plan_deleted = False
            if revoke_for_all_members:
                await db.execute(delete(MealPlan).where(MealPlan.id == meal_plan_id))
                meal_plan_deleted = True
            else:
                await db.execute(
                    delete(MealPlanMember).where(
                        MealPlanMember.meal_plan_id == meal_plan_id,
                        MealPlanMember.member_id == member_id,
                    )
                )
                await db.flush()

                remaining_result = await db.execute(
                    select(MealPlanMember.id).where(
                        MealPlanMember.meal_plan_id == meal_plan_id
                    )
                )
                remaining_assignment = remaining_result.scalars().first()
                if not remaining_assignment:
                    await db.execute(delete(MealPlan).where(MealPlan.id == meal_plan_id))
                    meal_plan_deleted = True

            await db.commit()
            return {
                "meal_plan_id": meal_plan_id,
                "revoked_for_member_id": member_id,
                "revoked_for_all_members": revoke_for_all_members,
                "meal_plan_deleted": meal_plan_deleted,
            }


MEAL_PLAN = MealPlanEntity()
