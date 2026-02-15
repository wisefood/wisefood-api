"""
Pydantic schemas for API request/response validation (no forward refs)
- No `from __future__ import annotations`
- Define classes in dependency order (profile -> member -> household)
- No `.model_rebuild()` calls needed
"""

from datetime import date as DateType, datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator


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

class MTMSchema(BaseModel):
    client_id: str = Field(..., description="Client ID")
    client_secret: str = Field(..., description="Client Secret")

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
    allergies: Optional[List[str]] = Field(default_factory=list)
    properties: Optional[Dict[str, Any]] = Field(default_factory=dict)


class HouseholdMemberProfileCreate(HouseholdMemberProfileBase):
    pass


class HouseholdMemberProfileUpdate(BaseModel):
    nutritional_preferences: Optional[Dict[str, Any]] = None
    dietary_groups: Optional[List[DietaryGroupEnum]] = None
    allergies: Optional[List[str]] = None
    properties: Optional[Dict[str, Any]] = None


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


class HouseholdMemberCreateWithHousehold(HouseholdMemberBase):
    """Schema for creating a member with explicit household_id"""
    household_id: str = Field(..., description="The household this member belongs to")
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


class SummarizeRequest(BaseModel):
    query: str = Field(..., min_length=1, description="Search query string")
    results: List[Dict[str, Any]] = Field(..., description="List of search result items")
    user_id: Optional[str] = Field(None, description="User ID for context")
    language: Optional[str] = Field("en", description="Language code for the summary")
    expertise_level: Optional[str] = Field("general", description="Expertise level of the user")

class ArticleInput(BaseModel):
    """Input model for article enrichment."""

    urn: str = Field(description="Article URN (unique identifier)")
    title: str = Field(description="Article title")
    abstract: str = Field(description="Article abstract text")
    authors: Optional[str] = Field(
        default=None, description="Comma-separated list of authors"
    )

class QAModeEnum(str, Enum):
    simple = "simple"
    advanced = "advanced"


class QAExpertiseLevelEnum(str, Enum):
    beginner = "beginner"
    intermediate = "intermediate"
    expert = "expert"


class QAPreferredAnswerEnum(str, Enum):
    a = "a"
    b = "b"
    neither = "neither"
    both = "both"


class QAHelpfulnessEnum(str, Enum):
    helpful = "helpful"
    not_helpful = "not_helpful"


class QATargetAnswerEnum(str, Enum):
    primary = "primary"
    secondary = "secondary"
    overall = "overall"


class Reference(BaseModel):
    source_type: str = Field(..., description="Type of source")
    description: str = Field(..., description="Brief source description")


class RetrievedArticle(BaseModel):
    urn: str = Field(..., description="Article URN")
    title: str = Field(..., description="Article title")
    authors: Optional[List[str]] = Field(default=None, description="Article authors")
    venue: Optional[str] = Field(default=None, description="Publication venue")
    publication_year: Optional[str] = Field(default=None, description="Publication year")
    category: Optional[str] = Field(default=None, description="Article category")
    tags: Optional[List[str]] = Field(default=None, description="Article tags")
    similarity_score: float = Field(..., description="Cosine similarity score (0-1)")


class QAAnswer(BaseModel):
    """
    Flexible answer schema. Upstream may include additional fields depending on mode.
    """

    model_config = ConfigDict(extra="allow")

    answer: Optional[str] = Field(default=None, description="Generated answer text")
    references: Optional[List[Reference]] = Field(
        default=None, description="References used in the answer"
    )


class DualAnswerFeedback(BaseModel):
    request_id: str = Field(..., description="Unique request identifier for tracking")
    answer_a_label: str = Field(..., description="Label describing approach A")
    answer_b_label: str = Field(..., description="Label describing approach B")


class QARequest(BaseModel):
    question: str = Field(
        ..., min_length=3, max_length=1000, description="Food science question"
    )
    mode: QAModeEnum = Field(
        default=QAModeEnum.simple,
        description="simple = default pipeline, advanced = custom model/RAG settings",
    )
    model: Optional[str] = Field(default=None, description="Model (advanced mode only)")
    rag_enabled: bool = Field(
        default=True, description="Enable retrieval in advanced mode"
    )
    top_k: int = Field(default=5, ge=1, le=20, description="Retrieved article count")
    expertise_level: QAExpertiseLevelEnum = Field(
        default=QAExpertiseLevelEnum.intermediate,
        description="Answer complexity level",
    )
    language: str = Field(default="en", description="ISO 639-1 language code")
    user_id: Optional[str] = Field(
        default=None, description="Set by API for tracking"
    )
    member_id: Optional[str] = Field(
        default=None, description="Optional member identifier for tracking"
    )


class QAFeedbackRequest(BaseModel):
    request_id: str = Field(..., description="QA request identifier")
    preferred_answer: Optional[QAPreferredAnswerEnum] = Field(
        default=None,
        description=(
            "Dual-answer preference (A/B feedback only). Use when both primary "
            "and secondary answers are shown."
        ),
    )
    helpfulness: Optional[QAHelpfulnessEnum] = Field(
        default=None,
        description=(
            "General helpfulness feedback. Use for single-answer or overall "
            "quality feedback."
        ),
    )
    target_answer: QATargetAnswerEnum = Field(
        default=QATargetAnswerEnum.overall,
        description="Which answer the feedback targets.",
    )
    reason: Optional[str] = Field(
        default=None, max_length=500, description="Optional reason for feedback"
    )

    @model_validator(mode="after")
    def _validate_feedback_shape(self):
        if self.preferred_answer is None and self.helpfulness is None:
            raise ValueError(
                "At least one of 'preferred_answer' or 'helpfulness' must be provided."
            )
        return self


class QAFeedbackResponse(BaseModel):
    request_id: str = Field(..., description="Request identifier")
    status: str = Field(..., description="Feedback status")
    message: str = Field(..., description="Confirmation message")


class QAResponse(BaseModel):
    question: str = Field(..., description="Original question")
    mode: QAModeEnum = Field(..., description="Mode used")
    primary_answer: QAAnswer = Field(..., description="Primary answer")
    secondary_answer: Optional[QAAnswer] = Field(
        default=None, description="Secondary answer for A/B comparison"
    )
    dual_answer_feedback: Optional[DualAnswerFeedback] = Field(
        default=None, description="Feedback metadata for dual-answer mode"
    )
    retrieved_articles: List[RetrievedArticle] = Field(
        default_factory=list, description="Articles retrieved by semantic search"
    )
    follow_up_suggestions: Optional[List[str]] = Field(
        default=None, description="Suggested follow-up questions"
    )
    generated_at: str = Field(..., description="ISO response generation timestamp")
    cache_hit: bool = Field(default=False, description="Whether result came from cache")
    request_id: str = Field(..., description="Unique request identifier")


# ---------- RecipeWrangler Schemas ----------

class IngredientProfile(BaseModel):
    """Ingredient nutritional and sustainability profile"""
    name: Optional[str] = None
    measurement: Optional[str] = None
    weight_g: float = 0
    source: Optional[str] = None
    matched_nutritional_ingredient: Optional[str] = None
    protein_per_100g: Optional[float] = None
    carbs_per_100g: Optional[float] = None
    fat_per_100g: Optional[float] = None
    protein_g: Optional[float] = None
    carbs_g: Optional[float] = None
    fat_g: Optional[float] = None
    distance: Optional[float] = None
    sustainability_ingredient: Optional[str] = None
    matched_sustainability_ingredient: Optional[str] = None
    sustainability_weight_g: Optional[float] = None
    cf_val: Optional[float] = Field(None, description="Carbon footprint value")
    sustainability_distance: Optional[float] = None
    contribution: Optional[float] = None


class RecipeProfileRequest(BaseModel):
    """Request payload for recipe profiling endpoint"""
    raw_recipe: str = Field(..., min_length=1, description="Unstructured recipe text to analyze")


class RecipeProfileResponse(BaseModel):
    """Response payload from recipe profiling endpoint"""
    raw_recipe: Optional[str] = None
    title: Optional[str] = None
    ingredient_names: List[str] = Field(default_factory=list)
    measurements: List[str] = Field(default_factory=list)
    weights: Optional[Any] = None
    ingredients: List[IngredientProfile] = Field(default_factory=list)
    debug: bool = False
    directions: List[str] = Field(default_factory=list)
    total_time: Optional[float] = None
    tags: List[str] = Field(default_factory=list)
    allergens: List[str] = Field(default_factory=list)
    sustainability_per_kg: Optional[float] = None
    total_protein_g: Optional[float] = None
    total_fat_g: Optional[float] = None
    total_carbohydrate_g: Optional[float] = None
    total_energy_kcal: Optional[float] = None
    profiling_totals: Dict[str, float] = Field(default_factory=dict)
    full_profile: Dict[str, Any] = Field(default_factory=dict)
    serves: Optional[float] = None
    serving_size_g: Optional[float] = None
    min_similarity: Optional[float] = None
    similar_recipes: List[Dict[str, Any]] = Field(default_factory=list)
    agent_decision: Optional[str] = None
    query: Optional[str] = None
    cypher: Optional[str] = None
    tag_list: List[str] = Field(default_factory=list)
    message: str = "Success"

    model_config = ConfigDict(extra='allow')


class RecipeSearchRequest(BaseModel):
    """Request payload for recipe search endpoint"""
    question: str = Field(..., min_length=1, description="Natural language recipe question")
    exclude_allergens: List[str] = Field(
        default_factory=list,
        description="Allergen names to exclude (e.g., ['peanut', 'tree_nut'])"
    )


class RecipeDetailResponse(BaseModel):
    """Detailed recipe representation fetched from Neo4j"""
    recipe_id: str
    title: str
    image_url: Optional[str] = None
    ingredients: List[Dict[str, Any]]
    instructions: List[str]
    duration: Optional[float] = None
    serves: Optional[float] = None
    total_kcal_per_serving: Optional[float] = None
    total_protein_g_per_serving: Optional[float] = None
    total_carbs_g_per_serving: Optional[float] = None
    total_fat_g_per_serving: Optional[float] = None
    total_fiber_g_per_serving: Optional[float] = None
    total_sugar_g_per_serving: Optional[float] = None
    total_sodium_mg_per_serving: Optional[float] = None
    total_cholesterol_mg_per_serving: Optional[float] = None
    nutri_score: Optional[float] = None


# ---------- FoodChat Schemas ----------

class FoodChatCreateSessionRequest(BaseModel):
    """Request payload for creating a FoodChat session."""
    member_id: str = Field(..., description="Household member ID to create session for")


class FoodChatMessageRequest(BaseModel):
    """Request payload for sending a message in a FoodChat session."""
    content: str = Field(..., min_length=1, description="Message content to send")


# ---------- Meal Plan Storage Schemas ----------

class MealPlanMeal(BaseModel):
    recipe_id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    ingredients: str = Field(..., min_length=1)
    directions: str = Field(..., min_length=1)

    model_config = ConfigDict(extra="allow")


class MealPlanItem(BaseModel):
    """
    Meal plan object as returned by FoodChat result entries.
    """

    id: Optional[str] = Field(default=None, description="Source meal plan id from upstream app")
    created_at: Optional[datetime] = Field(default=None, description="Source creation time from upstream app")
    breakfast: MealPlanMeal
    lunch: MealPlanMeal
    dinner: MealPlanMeal
    reasoning: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class FoodChatMealPlanEnvelope(BaseModel):
    help: Optional[str] = None
    success: bool = True
    result: List[MealPlanItem] = Field(default_factory=list)

    model_config = ConfigDict(extra="allow")


class MealPlanStoreRequest(BaseModel):
    date: Optional[DateType] = Field(
        default=None,
        description="Date the meal plan applies to. Defaults to current date if omitted.",
    )
    applies_to_member_ids: List[str] = Field(
        default_factory=list,
        description="Additional member ids in the same household that share this plan.",
    )
    meal_plan: Optional[MealPlanItem] = Field(
        default=None,
        description="Direct meal plan item payload.",
    )
    foodchat_response: Optional[FoodChatMealPlanEnvelope] = Field(
        default=None,
        description="Optional raw FoodChat response envelope (help/success/result).",
    )

    @model_validator(mode="after")
    def _ensure_meal_plan_source(self):
        if self.meal_plan is not None:
            return self
        if self.foodchat_response is None:
            raise ValueError("Either meal_plan or foodchat_response must be provided")
        if not self.foodchat_response.result:
            raise ValueError("foodchat_response.result must contain at least one meal plan")
        self.meal_plan = self.foodchat_response.result[0]
        return self


class MealPlanResponse(BaseModel):
    id: str
    household_id: str
    date: DateType
    source_meal_plan_id: Optional[str] = None
    source_created_at: Optional[datetime] = None
    breakfast: Dict[str, Any]
    lunch: Dict[str, Any]
    dinner: Dict[str, Any]
    reasoning: Optional[str] = None
    applies_to_member_ids: List[str] = Field(default_factory=list)
    other_member_ids: List[str] = Field(default_factory=list)
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MealPlanRevokeResponse(BaseModel):
    meal_plan_id: str
    revoked_for_member_id: str
    revoked_for_all_members: bool
    meal_plan_deleted: bool
