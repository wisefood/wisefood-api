"""
Pydantic schemas for API request/response validation (no forward refs)
- No `from __future__ import annotations`
- Define classes in dependency order (profile -> member -> household)
- No `.model_rebuild()` calls needed
"""

from datetime import date as DateType, datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

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


class RecipeRegionEnum(str, Enum):
    """Supported nutrition regions for recipe lookups."""
    US = "US"
    IE = "IE"
    HU = "HU"


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


# ---------- Member Favorite Schemas ----------
class MemberFavoriteResponse(BaseModel):
    recipe_id: str = Field(..., min_length=1, max_length=128, description="Opaque RecipeWrangler recipe id")
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MemberFavoriteDeleteResponse(BaseModel):
    deleted: bool = Field(..., description="Whether a favorite was removed")


# ---------- Member Saved Item (Library) Schemas ----------
SavedItemType = Literal["recipe", "article", "guide", "textbook"]


class MemberSavedItemResponse(BaseModel):
    item_type: SavedItemType
    item_ref: str = Field(
        ...,
        min_length=1,
        max_length=512,
        description="Opaque recipe id (recipe) or urn:<type>:<slug> (literature).",
    )
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MemberSavedItemListResponse(BaseModel):
    member_id: str
    count: int
    saved_items: List[MemberSavedItemResponse] = Field(default_factory=list)


class MemberSavedItemDeleteResponse(BaseModel):
    deleted: bool = Field(..., description="Whether a saved item was removed")


# ---------- Member Adapted Recipe Schemas ----------
class MemberAdaptedRecipeStoreRequest(BaseModel):
    """Body for saving a member's adapted version of a recipe (upsert)."""
    title: Optional[str] = Field(
        default=None, max_length=512,
        description="Display title of the adapted recipe",
    )
    payload: Dict[str, Any] = Field(
        ...,
        description="Adapted recipe content: ingredients with the swap/reduce "
                    "applied, applied-suggestion metadata, simulated nutrition",
    )


class MemberAdaptedRecipeResponse(BaseModel):
    recipe_id: str = Field(..., min_length=1, max_length=128, description="Original opaque RecipeWrangler recipe id")
    title: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MemberAdaptedRecipeDeleteResponse(BaseModel):
    deleted: bool = Field(..., description="Whether an adapted recipe was removed")


# ---------- User Consent Schemas ----------
class UserConsentCreate(BaseModel):
    """
    Body for recording a consent acceptance for the current Keycloak user.

    The user_id is never taken from the body — it always comes from the
    authenticated token's `sub` claim. The recorded consent covers cookies
    and the processing of personal information solely for the provision of
    the service (purpose limitation).
    """

    consent_type: str = Field(
        "service_data_processing",
        min_length=1,
        max_length=64,
        description="Kind of consent being granted",
    )
    version: str = Field(
        ...,
        min_length=1,
        max_length=16,
        description="Version of the consent text the user accepted (e.g. '1.0')",
    )


class UserConsentStatus(BaseModel):
    """
    Latest consent state for a user and consent type.

    granted=False (with null version/granted_at) means the user has never
    accepted this consent type. Consent covers processing solely for the
    provision of the service.
    """

    granted: bool = Field(..., description="Whether the user has recorded this consent")
    consent_type: str = Field(..., description="Kind of consent")
    version: Optional[str] = Field(None, description="Accepted consent text version, if any")
    granted_at: Optional[datetime] = Field(None, description="When consent was last granted, if ever")


class UserConsentRecord(BaseModel):
    """
    A stored consent acceptance (one append-only ledger row). Consent covers
    processing solely for the provision of the service.
    """

    consent_type: str = Field(..., description="Kind of consent")
    version: str = Field(..., description="Accepted consent text version")
    granted_at: datetime = Field(..., description="When the acceptance was recorded")
    ip_address: Optional[str] = Field(None, description="Client IP at acceptance time")

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


class GuidelineExtractionRequest(BaseModel):
    """
    Options for a guideline extraction run.

    ``guide_id`` is what lets an extracted rule carry its population: FoodScholar
    injects the parent guide's title, region, audience and year into every page
    prompt, so a sentence like "Provide portions of red meat twice a week" is
    still attributable once it leaves the page it came from. Without it the run
    falls back to reading the PDF's own opening pages, and without that too the
    rules are context-free.
    """

    guide_id: Optional[str] = Field(
        default=None,
        description=(
            "Guide identifier or URN whose metadata is injected into the "
            "extraction prompts. Strongly recommended."
        ),
    )
    model: Optional[str] = Field(
        default=None, description="Override the extraction model"
    )
    dpi: Optional[int] = Field(
        default=None, ge=72, le=300, description="Page render DPI"
    )
    profile_document: bool = Field(
        default=True,
        description=(
            "Read the guide's opening pages to establish what the document is "
            "when the catalog record does not say."
        ),
    )
    profile_page_count: Optional[int] = Field(
        default=None,
        ge=1,
        le=20,
        description="How many leading pages the document profile pass reads",
    )
    force: bool = Field(
        default=False,
        description=(
            "Re-queue even when a job is already registered for this artifact, "
            "to recover one whose worker died."
        ),
    )


class GuidelineImportRequest(BaseModel):
    guide_id: str = Field(..., min_length=1, description="WiseFood guide identifier")
    dry_run: bool = Field(
        default=True,
        description="Preview the guideline import without creating guide entries",
    )
    dedupe_against_guide: bool = Field(
        default=True,
        description="Skip extracted guidelines that already exist in the guide",
    )
    action_type: str = Field(
        default="encourage",
        min_length=1,
        description=(
            "Fallback action type for rules whose extraction produced no "
            "per-rule hint. Legacy 'encourage' normalizes to 'choose'."
        ),
    )
    existing_scan_limit: Optional[int] = Field(
        default=None,
        ge=1,
        description=(
            "Maximum number of existing guide guidelines to scan for dedupe and "
            "sequence numbering. Omit to scan all of them, which is what "
            "correctness requires — a bounded scan silently misses duplicates."
        ),
    )
    import_facets: bool = Field(
        default=True,
        description=(
            "Carry per-rule facet hints and source references from a v2 "
            "extraction result onto the created guidelines."
        ),
    )


class GuidelineEnrichmentEnqueueRequest(BaseModel):
    """Options for queueing post-extraction facet enrichment."""

    guide_urns: Optional[List[str]] = Field(
        default=None,
        description="Guides to enrich. Omit to enrich every guide with guidelines.",
    )
    force: bool = Field(
        default=False,
        description="Re-enrich guidelines already at the current enrichment version",
    )
    allow_pdf_profile: bool = Field(
        default=True,
        description=(
            "Read a guide's PDF when its catalog metadata does not establish "
            "who its rules are for."
        ),
    )


class GuidelineEnrichmentPreviewRequest(BaseModel):
    """Sample a guide's rules and return proposed facets without writing."""

    guide_urn: str = Field(..., min_length=1, description="Guide to sample")
    limit: int = Field(default=10, ge=1, le=50, description="How many rules to sample")
    allow_pdf_profile: bool = Field(default=True)


class ArticleEnrichmentRequest(BaseModel):
    """Options for queuing selective enrichment of a catalog article."""

    force: bool = Field(
        default=False,
        description="Re-enrich even if the article was already processed",
    )


class ArticleEnrichmentBatchRequest(ArticleEnrichmentRequest):
    """Options for queuing selective enrichment of several catalog articles."""

    urns: List[str] = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Article URNs to enrich",
    )


class EnrichmentSweeperPauseRequest(BaseModel):
    """Pause or resume the FoodScholar catalog enrichment sweeper."""

    paused: bool = Field(
        ..., description="True to pause the sweeper, False to resume it"
    )


class EnrichmentWorkerRestartRequest(BaseModel):
    """Force the FoodScholar enrichment workers back into a running state."""

    sweeper: bool = Field(default=True, description="Restart the catalog sweeper")
    jobs: bool = Field(default=True, description="Restart the on-demand job worker")
    resume: bool = Field(
        default=True,
        description=(
            "Also clear the sweeper pause switch, which has no expiry and "
            "otherwise survives restarts"
        ),
    )


class QAModeEnum(str, Enum):
    simple = "simple"
    advanced = "advanced"


class QARetrieverEnum(str, Enum):
    rag = "rag"
    no_rag = "no_rag"
    linearrag = "linearrag"


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


class QAClarificationResponse(BaseModel):
    question_id: str = Field(..., description="Clarification question identifier")
    selected_values: List[str] = Field(
        default_factory=list,
        description="Selected clarification option values",
    )
    free_text: Optional[str] = Field(
        default=None,
        description="Free-text clarification response",
    )


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
    retriever: QARetrieverEnum = Field(
        default=QARetrieverEnum.rag,
        description="Retriever backend to use: rag, no_rag, or linearrag",
    )
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
    experience_group: Optional[str] = Field(
        default=None, description="Optional user experience group for tracking"
    )
    qa_thread_id: Optional[str] = Field(
        default=None, description="Optional QA thread identifier for follow-up questions"
    )
    clarification_response: Optional[QAClarificationResponse] = Field(
        default=None, description="Optional answer to a requested clarification"
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
    region: Optional[str] = Field(
        default=None,
        min_length=2,
        max_length=2,
        description="Optional ISO-3166-1 alpha-2 region code used during profiling",
    )
    persist_trace: bool = Field(
        default=False,
        description="Whether to persist profiling trace/debug information upstream",
    )
    parse_only: bool = Field(
        default=False,
        description=(
            "When true, return a create-compatible parsed payload with "
            "pre-populated ingredients, instructions, duration, serves, allergens, and tags"
        ),
    )


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
    question: str = Field(default="", description="Natural language recipe question")
    exclude_allergens: List[str] = Field(
        default_factory=list,
        description="Allergen names to exclude (e.g., ['peanut', 'tree_nut'])"
    )
    diet_tags: List[str] = Field(
        default_factory=list,
        description="Member dietary groups (e.g. ['vegan', 'gluten_free']) applied as soft ranking boosts"
    )
    preferred_ingredients: List[str] = Field(
        default_factory=list,
        description="Soft preference boosts from the member profile — reorder results, never filter"
    )
    region: Optional[str] = Field(
        default=None,
        description="Region whose nutri-score the result cards carry: US, IE, HU, or EU"
    )
    dish_types: List[str] = Field(
        default_factory=list,
        description="Course/dish-type filter selected by the caller (e.g. ['main-dish'])",
    )
    sources: List[str] = Field(
        default_factory=list,
        description="Restrict to these recipe collections (e.g. ['myplate'])",
    )
    # Annotation facets. Closed vocabularies owned by RecipeWrangler
    # (`catalog.vocabularies`); this layer forwards them without opinion, so a
    # vocabulary added downstream needs no change here.
    cuisines: List[str] = Field(
        default_factory=list,
        description="Cuisine filter (e.g. ['italian', 'thai'])",
    )
    moods: List[str] = Field(
        default_factory=list,
        description="Eating-occasion filter (e.g. ['comfort', 'quick'])",
    )
    flavor_profiles: List[str] = Field(
        default_factory=list,
        description="Dominant-taste filter (e.g. ['spicy', 'umami'])",
    )
    food_groups: List[str] = Field(
        default_factory=list,
        description="Coarse ingredient-category filter (e.g. ['fish', 'legumes'])",
    )
    require_diet_tags: List[str] = Field(
        default_factory=list,
        description="Diet groups the recipe must carry — a hard filter, unlike "
                    "`diet_tags`, which only boost ranking",
    )
    include_disabled: bool = Field(
        default=False,
        description="When true, disabled (soft-deleted) recipes appear in results — "
                    "console/admin only; requires an admin or expert role",
    )


class RecipeDetailsBatchRequest(BaseModel):
    """Batch recipe-details lookup — resolves ids to slim cards with macros.

    Mirrors RecipeWrangler's own limit of 1-30 ids per call; the UI chunks
    larger sets client-side. This is what makes favourites resolve live rather
    than from a stored snapshot.
    """
    recipe_ids: List[str] = Field(..., min_length=1, max_length=30)
    region: Optional[str] = Field(
        default=None,
        description="Optional nutrition region selector (US, IE, HU, EU).",
    )


class RecipeParamSearchSortEnum(str, Enum):
    random = "random"
    title_asc = "title_asc"
    title_desc = "title_desc"
    time_asc = "time_asc"
    time_desc = "time_desc"


class RecipeParamSearchRequest(BaseModel):
    """Request payload for deterministic parameter-based recipe search endpoint"""
    include_ingredients: List[str] = Field(
        default_factory=list,
        description="Ingredient names that should be present in the recipe",
    )
    exclude_ingredients: List[str] = Field(
        default_factory=list,
        description="Ingredient names that should not be present in the recipe",
    )
    exclude_allergens: List[str] = Field(
        default_factory=list,
        description="Allergen names to exclude",
    )
    diet_tags: List[str] = Field(
        default_factory=list,
        description="Diet tags to enforce (e.g., vegan, gluten_free)",
    )
    sources: List[str] = Field(
        default_factory=list,
        description="Recipe sources to filter by",
    )
    dish_types: List[str] = Field(
        default_factory=list,
        description="Dish types to filter by (e.g., breakfast, dessert)",
    )
    # Annotation facets. Closed vocabularies owned by RecipeWrangler
    # (`catalog.vocabularies`); this layer forwards them without opinion, so a
    # vocabulary added downstream needs no change here.
    cuisines: List[str] = Field(
        default_factory=list,
        description="Cuisine filter (e.g. ['italian', 'thai'])",
    )
    moods: List[str] = Field(
        default_factory=list,
        description="Eating-occasion filter (e.g. ['comfort', 'quick'])",
    )
    flavor_profiles: List[str] = Field(
        default_factory=list,
        description="Dominant-taste filter (e.g. ['spicy', 'umami'])",
    )
    food_groups: List[str] = Field(
        default_factory=list,
        description="Coarse ingredient-category filter (e.g. ['fish', 'legumes'])",
    )
    max_duration_minutes: Optional[int] = Field(
        default=None,
        ge=0,
        description="Maximum recipe duration in minutes",
    )
    limit: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Maximum number of results to return",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="Number of results to skip for pagination",
    )
    sort_by: RecipeParamSearchSortEnum = Field(
        default=RecipeParamSearchSortEnum.title_asc,
        description="Sort order for results",
    )
    include_facets: bool = Field(
        default=False,
        description="Whether to include facet counts in the response",
    )
    include_disabled: bool = Field(
        default=False,
        description=(
            "Console/admin only: include disabled (soft-deleted) recipes in "
            "results. Requires the admin or expert role."
        ),
    )


class CatalogSearchRequest(BaseModel):
    """Request payload for the catalog search contract.

    Field-for-field identical to RecipeWrangler's `/api/v2/recipes/search`,
    which is itself identical to wisefood-data-api's SearchSchema. Kept in
    lockstep deliberately: the point of the contract is that one UI search
    component works against the catalog and the recipe corpus alike, and a
    field renamed or constrained here would break that quietly.

    Unlike `/recipes/search` there is no LLM in the request path — `q` ranks,
    `fq` filters, and a bare noun cannot come back matching the whole corpus.
    """
    q: Optional[str] = Field(
        default=None,
        description="Free-text query. Ranks results; never filters them.",
    )
    fq: List[str] = Field(
        default_factory=list,
        description=(
            "Filter queries in Lucene syntax, ANDed together, e.g. "
            '["cuisines:italian", "food_groups:fruit", "duration:[* TO 30]"]'
        ),
    )
    fl: List[str] = Field(
        default_factory=list,
        description="Fields to return. Empty returns the whole document.",
    )
    sort: List[str] = Field(
        default_factory=list,
        description='Sort clauses, e.g. ["default_nutri_rank:asc", "title.kw:asc"]',
    )
    facets: List[str] = Field(
        default_factory=list,
        description="Keyword fields to facet on. Any mapped keyword field works.",
    )
    facet_limit: int = Field(default=30, ge=1, le=200)
    limit: int = Field(default=20, ge=0, le=200)
    offset: int = Field(default=0, ge=0)
    include_inactive: bool = Field(
        default=False,
        description=(
            "Console/admin only: include withdrawn recipes. Requires the "
            "admin or expert role."
        ),
    )
    highlight: List[str] = Field(
        default_factory=list,
        description="Fields to return highlighted snippets for.",
    )


class RecipeDisableRequest(BaseModel):
    """Payload for disabling (soft-deleting) a single recipe"""
    reason: Optional[str] = Field(default=None, max_length=500)


class RecipeBulkStatusRequest(BaseModel):
    """Payload for bulk disable/enable by explicit recipe IDs"""
    recipe_ids: List[str] = Field(..., min_length=1, max_length=100000)
    reason: Optional[str] = Field(default=None, max_length=500)


class RecipeDisableByQueryRequest(RecipeParamSearchRequest):
    """Bulk disable every recipe matching the given search filters"""
    reason: Optional[str] = Field(default=None, max_length=500)
    allow_unfiltered: bool = Field(
        default=False,
        description="Explicit opt-in for an unconstrained (whole-corpus) disable",
    )


class RecipeStatusResponse(BaseModel):
    """Result of a recipe disable/enable operation"""
    status: str
    requested: int
    updated: int
    recipe_ids: List[str] = Field(default_factory=list)
    es_sync: Dict[str, Dict[str, int]] = Field(default_factory=dict)
    message: str = "Recipe status updated"


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


class RecipeCreateRequest(BaseModel):
    """Request payload for creating a structured recipe."""
    title: str = Field(..., min_length=1, description="Recipe title")
    ingredients: List[str] = Field(
        ...,
        min_length=1,
        description="Raw ingredient strings used to build the recipe",
    )
    instructions: List[str] = Field(
        ...,
        min_length=1,
        description="Ordered preparation instructions",
    )
    duration: int = Field(..., ge=1, description="Recipe duration in minutes")
    serves: int = Field(..., ge=1, description="Number of servings")
    region: str = Field(
        ...,
        min_length=2,
        max_length=2,
        description="ISO-3166-1 alpha-2 region code",
    )
    image_url: Optional[str] = Field(default=None, description="Recipe image URL")
    source_id: Optional[str] = Field(
        default=None,
        min_length=1,
        description="Optional upstream source identifier",
    )
    expert_recipe: bool = Field(
        default=False,
        description="Whether the recipe is marked as expert-curated",
    )
    tags: List[str] = Field(default_factory=list, description="Diet or recipe tags")
    allergens: List[str] = Field(
        default_factory=list,
        description="User-supplied allergens to merge into the recipe graph",
    )
    protein_g: Optional[float] = Field(
        default=None,
        ge=0,
        description="Optional total protein in grams",
    )
    carbohydrate_g: Optional[float] = Field(
        default=None,
        ge=0,
        description="Optional total carbohydrate in grams",
    )
    fat_g: Optional[float] = Field(
        default=None,
        ge=0,
        description="Optional total fat in grams",
    )
    energy_kcal: Optional[float] = Field(
        default=None,
        ge=0,
        description="Optional total energy in kcal",
    )
    sugar_g: Optional[float] = Field(
        default=None,
        ge=0,
        description="Optional total sugar in grams",
    )
    saturated_fat_g: Optional[float] = Field(
        default=None,
        ge=0,
        description="Optional total saturated fat in grams",
    )
    sodium_mg: Optional[float] = Field(
        default=None,
        ge=0,
        description="Optional total sodium in milligrams",
    )
    fibre_g: Optional[float] = Field(
        default=None,
        ge=0,
        description="Optional total fibre in grams",
    )

    @field_validator("region", mode="before")
    @classmethod
    def _normalize_region(cls, value: str) -> str:
        return value.strip().upper()


class RecipeCreateResponse(BaseModel):
    """Response payload for a created recipe."""
    recipe_id: str
    message: str = "Recipe created successfully"


class RecipeUpdateRequest(BaseModel):
    """Request payload for patching mutable recipe fields."""
    instructions: Optional[List[str]] = Field(
        default=None,
        description="Updated ordered preparation instructions",
    )
    image_url: Optional[str] = Field(default=None, description="Updated recipe image URL")
    source_id: Optional[str] = Field(
        default=None,
        min_length=1,
        description="Updated upstream source identifier",
    )
    expert_recipe: Optional[bool] = Field(
        default=None,
        description="Whether the recipe is marked as expert-curated",
    )
    title: Optional[str] = Field(
        default=None,
        min_length=1,
        description="Updated recipe title",
    )
    allergens: Optional[List[str]] = Field(
        default=None,
        description="Updated allergen list",
    )
    tags: Optional[List[str]] = Field(
        default=None,
        description="Updated diet or recipe tags",
    )
    duration: Optional[int] = Field(
        default=None,
        ge=1,
        description="Updated recipe duration in minutes",
    )

    @model_validator(mode="after")
    def _ensure_mutable_field_present(self):
        mutable_fields = (
            self.instructions,
            self.image_url,
            self.source_id,
            self.expert_recipe,
            self.title,
            self.allergens,
            self.tags,
            self.duration,
        )
        if all(value is None for value in mutable_fields):
            raise ValueError("At least one mutable recipe field must be provided")
        return self


class RecipeUpdateResponse(BaseModel):
    """Response payload for a patched recipe."""
    recipe_id: str
    updated_fields: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    allergens: List[str] = Field(default_factory=list)
    message: str = "Recipe updated successfully"


class RecipeSubstituteRequest(BaseModel):
    """Request payload for substituting one ingredient in a recipe."""
    ingredient: str = Field(..., min_length=1, description="Ingredient to substitute")
    region: RecipeRegionEnum = Field(
        default=RecipeRegionEnum.IE,
        description="Nutrition region used to re-profile the modified recipe",
    )


class RecipeSubstituteResponse(BaseModel):
    """Response payload for an ingredient substitution and modified nutrition profile."""
    original_ingredient: str
    substitute: Optional[str] = None
    substitution_source: Optional[str] = None
    candidates: List[str] = Field(default_factory=list)
    modified_recipe_profile: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="allow")


class RecipeAdaptModeEnum(str, Enum):
    """Optimisation targets for recipe adaptation suggestions."""
    nutrition = "nutrition"
    sustainability = "sustainability"
    reduce_quantity = "reduce_quantity"


class RecipeAdaptSuggestionsRequest(BaseModel):
    """Request payload for ranked recipe-adaptation suggestions."""
    region: RecipeRegionEnum = Field(
        default=RecipeRegionEnum.IE,
        description="Nutrition region used to evaluate the recipe",
    )
    mode: RecipeAdaptModeEnum = Field(
        default=RecipeAdaptModeEnum.nutrition,
        description="Optimisation target: improve Nutri-Score, cut CO2e, or reduce quantity",
    )
    max_swaps: int = Field(
        default=1, ge=1, le=3,
        description="Number of top-ranked suggestions to return",
    )
    use_llm: bool = Field(
        default=False,
        description="Run the LLM judge over the deterministic candidate set "
                    "(falls back to the deterministic ranking on any failure)",
    )
    goal_nutrients: List[str] = Field(
        default_factory=list,
        description="Member dietary-goal slugs (e.g. reduce_fat) biasing which "
                    "nutrient the nutrition-mode adaptation targets",
    )


class RecipeAdaptSuggestionsResponse(BaseModel):
    """Ranked adaptation suggestions; recipe-backend payload passes through."""
    recipe_id: str
    region: str
    mode: str
    offending_ingredient: Optional[str] = None
    suggestions: List[Dict[str, Any]] = Field(default_factory=list)

    model_config = ConfigDict(extra="allow")


class RecipeAdaptSwapInput(BaseModel):
    """One specific ingredient swap to simulate."""
    original_ingredient: str = Field(..., min_length=1)
    substitute_ingredient: str = Field(..., min_length=1)
    weight_g: Optional[float] = Field(
        default=None, gt=0,
        description="Override for the substitute weight; defaults to the original's weight",
    )


class RecipeAdaptSimulateRequest(BaseModel):
    """Request payload for simulating a specific ingredient swap."""
    region: RecipeRegionEnum = Field(default=RecipeRegionEnum.IE)
    swap: RecipeAdaptSwapInput


class RecipeAdaptSimulateResponse(BaseModel):
    """Simulated swap outcome; recipe-backend payload passes through."""
    recipe_id: str
    region: str
    original_nutri_score: Optional[str] = None
    simulated_nutri_score: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class ImageUploadResponse(BaseModel):
    """Response payload for a stored image."""
    id: str
    image_id: str
    bucket: str
    content_type: str
    original_size_bytes: int = Field(..., ge=1)
    stored_size_bytes: int = Field(..., ge=1)
    compressed: bool = False
    image_url: Optional[str] = None


# ---------- FoodChat Schemas ----------

class FoodChatCreateSessionRequest(BaseModel):
    """Request payload for creating a FoodChat session."""
    member_id: str = Field(..., description="Household member ID to create session for")
    cooking_for: Optional[List[str]] = Field(
        default=None,
        description="Optional list of household member IDs the session is cooking for",
    )


class FoodChatChatRequest(BaseModel):
    """Request payload for the unified FoodChat conversation endpoint."""
    content: str = Field(..., min_length=1, description="Message content to send")
    member_id: str = Field(
        ...,
        description="Household member ID that owns the FoodChat session",
    )


class FoodChatMemorySuggestion(BaseModel):
    """A memory suggestion surfaced by FoodChat during a chat turn."""
    id: str
    kind: str
    value: str
    statement: str

    model_config = ConfigDict(extra="allow")


class FoodChatMemoryDecisionRequest(BaseModel):
    """Request payload for accepting or declining a memory suggestion."""
    member_id: str = Field(
        ...,
        description="Household member ID that owns the FoodChat session",
    )
    decision: Literal["accept", "decline"] = Field(
        ...,
        description="Whether the member accepts or declines the memory suggestion",
    )
    suggestion: FoodChatMemorySuggestion = Field(
        ...,
        description="The memory suggestion being decided on",
    )


class FoodChatComposePick(BaseModel):
    """One hand-picked recipe on the FoodChat manual-mode canvas."""
    meal_type: Literal["breakfast", "lunch", "dinner"]
    recipe_id: str
    title: Optional[str] = None
    day: Optional[int] = Field(default=None, ge=1, le=7, description="Weekly plans only")


class FoodChatComposeRequest(BaseModel):
    """Request payload for completing a hand-started plan (daily or weekly)."""
    member_id: str = Field(
        ...,
        description="Household member ID that owns the FoodChat session",
    )
    picks: List[FoodChatComposePick] = Field(
        ...,
        description="Hand-picked recipes to pin before FoodChat fills the rest",
    )
    plan_type: Literal["daily", "weekly"] = Field(
        default="daily",
        description="Which plan type to compose",
    )
    message: Optional[str] = Field(
        default=None,
        description="Optional chat text sent alongside the picks",
    )


class FoodChatPlanParametersRequest(BaseModel):
    """Request payload for applying interactive plan-parameter card values."""
    member_id: str = Field(
        ...,
        description="Household member ID that owns the FoodChat session",
    )
    values: Dict[str, Any] = Field(
        ...,
        description="Chosen values keyed by parameter (cooking_time, difficulty, goal)",
    )
    plan_type: Optional[Literal["daily", "weekly"]] = Field(
        default=None,
        description=(
            "The card's own address, echoed back from the card payload, so the "
            "values refine the plan the card was rendered with. Omitted → active canvas."
        ),
    )


class FoodChatUpdateDinersRequest(BaseModel):
    """Request payload for updating the diners of a FoodChat session."""
    member_id: str = Field(
        ...,
        description="Household member ID that owns the FoodChat session",
    )
    cooking_for: List[str] = Field(
        ...,
        description="Household member IDs the session is cooking for",
    )


class FoodChatFeedbackRequest(BaseModel):
    """Request payload for assistant message feedback."""
    member_id: str = Field(
        ...,
        description="Household member ID that owns the FoodChat session",
    )
    rating: str = Field(..., min_length=1, description="Feedback rating")
    comment: Optional[str] = Field(default=None, description="Optional feedback comment")


class FoodChatSessionResponse(BaseModel):
    session_id: str
    member_id: str
    state: str
    message_count: int
    created_at: datetime

    model_config = ConfigDict(extra="allow")


class FoodChatMatchReason(BaseModel):
    """Why a recipe was matched to the household (e.g. preference/constraint hit)."""

    kind: str
    label: str

    model_config = ConfigDict(extra="allow")


class FoodChatMealCourseResponse(BaseModel):
    recipe_id: str
    title: str
    ingredients: str
    directions: str
    nutrition: Optional[Dict[str, Any]] = None
    image_url: Optional[str] = None
    match_reasons: List[FoodChatMatchReason] = Field(default_factory=list)

    model_config = ConfigDict(extra="allow")


class FoodChatMealPlanResponse(BaseModel):
    id: str
    created_at: datetime
    version: int
    parent_id: Optional[str] = None
    breakfast: FoodChatMealCourseResponse
    lunch: FoodChatMealCourseResponse
    dinner: FoodChatMealCourseResponse
    reasoning: str
    llm_score: Optional[int] = None
    llm_reasoning: Optional[str] = None
    fvs_count: Optional[int] = None
    fvs_reasoning: Optional[str] = None
    diversity_llm_score: Optional[int] = None
    diversity_llm_reasoning: Optional[str] = None
    guideline_adherence_score: Optional[int] = None
    guideline_adherence_reasoning: Optional[str] = None
    constraints_applied: List[Dict[str, Any]] = Field(default_factory=list)
    personalization_summary: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(extra="allow")


class FoodChatWeeklyMealPlanEntryResponse(BaseModel):
    day: int
    meal_idx: int
    meal_type: str
    recipe: Dict[str, Any]
    reward: float

    model_config = ConfigDict(extra="allow")


class FoodChatWeeklyMealPlanResponse(BaseModel):
    id: str
    created_at: datetime
    version: int
    parent_id: Optional[str] = None
    entries: List[FoodChatWeeklyMealPlanEntryResponse] = Field(default_factory=list)

    model_config = ConfigDict(extra="allow")


class FoodChatCitation(BaseModel):
    title: str
    source_type: str            # "article" | "guideline"
    url: Optional[str] = None
    label: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class FoodChatAttribution(BaseModel):
    """Provenance of a chat answer delegated to another WiseFood app
    (currently FoodScholar). ``learn_more_url`` is a UI-relative path."""

    source: str                 # "foodscholar"
    confidence: Optional[str] = None
    citations: List[FoodChatCitation] = Field(default_factory=list)
    learn_more_url: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class FoodChatChatTurnResponse(BaseModel):
    role: str
    content: str
    intent: str
    needs_clarification: bool = False
    meal_plan: Optional[FoodChatMealPlanResponse] = None
    weekly_meal_plan: Optional[FoodChatWeeklyMealPlanResponse] = None
    at_message_limit: bool = False
    plan_version: Optional[int] = None
    plan_parent_id: Optional[str] = None
    # Set on nutrition_question turns answered via FoodScholar
    attribution: Optional[FoodChatAttribution] = None
    memory_suggestions: Optional[List[FoodChatMemorySuggestion]] = None
    # Slot-edit proof — {meal_type, day, old{title,kcal}, new{...}, directive, verified}
    changed_slots: Optional[List[Dict[str, Any]]] = None

    model_config = ConfigDict(extra="allow")


class FoodChatConversationPage(BaseModel):
    messages: List[Dict[str, Any]] = Field(default_factory=list)
    has_more: bool
    next_before_id: Optional[int] = None

    model_config = ConfigDict(extra="allow")


class FoodChatFeedbackResponse(BaseModel):
    message_id: int
    rating: str
    comment: Optional[str] = None

    model_config = ConfigDict(extra="allow")


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


class SavedMealPlanCreateRequest(BaseModel):
    """Save a plan to the member's library, either by id or by value."""

    name: str = Field(
        min_length=1,
        max_length=255,
        description="Name the member files this plan under.",
    )
    meal_plan_id: Optional[str] = Field(
        default=None,
        description=(
            "Id of an existing stored meal plan to snapshot. "
            "Omit to save the meals supplied in meal_plan instead."
        ),
    )
    meal_plan: Optional[MealPlanItem] = Field(
        default=None,
        description="Meals to save directly, when there is no stored plan to reference.",
    )

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("name must not be blank")
        return cleaned

    @model_validator(mode="after")
    def _ensure_source(self):
        if self.meal_plan_id is None and self.meal_plan is None:
            raise ValueError("Either meal_plan_id or meal_plan must be provided")
        return self


class SavedMealPlanUpdateRequest(BaseModel):
    """Rename a saved plan. Only the name is mutable; the meals are a snapshot."""

    name: str = Field(min_length=1, max_length=255)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("name must not be blank")
        return cleaned


class SavedMealPlanResponse(BaseModel):
    id: str
    member_id: str
    name: str
    source_meal_plan_id: Optional[str] = None
    source_applied_on: Optional[DateType] = None
    breakfast: Dict[str, Any]
    lunch: Dict[str, Any]
    dinner: Dict[str, Any]
    reasoning: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SavedMealPlanListResponse(BaseModel):
    member_id: str
    count: int
    saved_meal_plans: List[SavedMealPlanResponse] = Field(default_factory=list)


class SavedMealPlanDeleteResponse(BaseModel):
    saved_meal_plan_id: str
    member_id: str
    deleted: bool
