"""A session as a story: what somebody did, in order, grouped by what they were doing.

The session page laid every source out as its own panel — conversations, then
questions, then a raw timeline, then searches, errors, model calls, feedback,
vitals — and left the reader to line them up by timestamp. Nobody could say
what a visit had been about without scrolling it twice.

This reads the same sources as one sequence of moments and cuts it into
chapters the way a person would describe the visit: "they made three plans in
FoodChat, went to the recipes and searched for lentils, came back and rated the
plan down". A chapter is a run of moments in one product; a new FoodChat
conversation, or a pause of half an hour, starts another. Errors, frustrated
clicks and anything without a product of its own belong to whichever chapter
they happened in, because that is where they are evidence.

Nothing here reads text a member typed into chat. A question's or a search's
words appear only where the gateway kept them, exactly as on the search list;
a rating carries the reason or comment its author left.

Pure, so the shaping can be tested without a database.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

#: Moments a story keeps before saying how many it left out.
MAX_STORY_MOMENTS = 600

#: A pause this long starts a new chapter even in the same product: it is
#: the convention analytics tools settled on for "came back later".
CHAPTER_IDLE_MINUTES = 30

#: Ratings that mean somebody was unhappy, in every vocabulary in use.
NEGATIVE_RATINGS = frozenset({"down", "not_helpful", "bad", "awful"})

#: Events that are not moments. Their substance arrives from a richer source
#: (the journey's turns and questions, the search table) or they are noise
#: (a row per keystroke of autocomplete).
_SKIPPED_EVENTS = frozenset({
    "chat.turn", "chat.plan_generated", "chat.plan_saved",
    "qa.ask", "qa.answered", "recipe.search", "recipe.autocomplete",
})

#: Props a generic event may carry into the story. Allowlisted rather than
#: passed through: props are free-form, and the story is read by people who
#: should see what was done, not whatever a client chose to attach.
_EVENT_PROPS = (
    "path", "from", "feature", "action", "entry_type", "urn", "recipe_id",
    "position", "source", "mode", "tab", "step", "count", "scope",
)

_PRODUCT_OF_APP = {
    "foodchat": "foodchat",
    "foodscholar": "foodscholar",
    "catalog": "foodscholar",
    "recipewrangler": "recipes",
}

_PRODUCT_OF_PATH = (
    ("/foodchat", "foodchat"),
    ("/foodscholar", "foodscholar"),
    ("/recipe-wrangler", "recipes"),
    ("/recipes", "recipes"),
)

_PRODUCT_OF_TARGET = {
    "chat_message": "foodchat",
    "qa_answer": "foodscholar",
    "article": "foodscholar",
    "guideline": "foodscholar",
    "recipe": "recipes",
}

#: Moments that belong to whatever chapter they happen in and never open one.
_ATTACHED = frozenset({"error", "client_error", "frustration", "event"})


def product_of_path(path: Optional[str]) -> str:
    for prefix, product in _PRODUCT_OF_PATH:
        if path == prefix or (path or "").startswith(prefix + "/"):
            return product
    return "other"


def _product_of_surface(surface: Optional[str]) -> str:
    text = (surface or "").lower()
    if any(word in text for word in ("article", "catalog", "guideline", "textbook", "scholar")):
        return "foodscholar"
    return "recipes"


def _parse(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _negative(value: Any) -> bool:
    return str(value or "").lower() in NEGATIVE_RATINGS


def _rating(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "feedback_id": row.get("id"),
        "value": row.get("rating_value"),
        "negative": _negative(row.get("rating_value")),
        "reason": row.get("reason"),
        "comment": row.get("comment"),
    }


def build_story(
    *,
    journey: Dict[str, Any],
    searches: Iterable[Dict[str, Any]],
    events: Iterable[Dict[str, Any]],
    feedback: Iterable[Dict[str, Any]],
    client_errors: Iterable[Dict[str, Any]] = (),
    frustration: Iterable[Dict[str, Any]] = (),
) -> Dict[str, Any]:
    """The session's moments in order, cut into chapters.

    `journey` is what `_build_journey` made of the same session: FoodChat
    turns already carry their plan and lookups, FoodScholar questions their
    answer and rating. `events` are the product events and failed requests;
    `searches` the person's own searches.
    """
    moments: List[Dict[str, Any]] = []

    def add(kind: str, at: Any, product: Optional[str], **fields: Any) -> None:
        when = _parse(at)
        if when is None:
            return
        moments.append({"kind": kind, "_at": when, "product": product, **fields})

    for conversation in journey.get("foodchat", {}).get("conversations", []):
        number = 0
        for step in conversation.get("steps", []):
            fields = {k: v for k, v in step.items() if k not in ("kind", "occurred_at")}
            if step.get("kind") == "turn":
                number += 1
                fields["number"] = number
            add(
                step.get("kind") or "turn",
                step.get("occurred_at"),
                "foodchat",
                conversation_id=conversation.get("conversation_id"),
                **fields,
            )

    # A question's rating rides on the question; a chat rating cannot, because
    # it names a message and a turn does not know its message's id.
    question_ratings: Dict[str, Dict[str, Any]] = {}
    for row in feedback:
        if row.get("target_type") == "qa_answer" and row.get("target_id"):
            question_ratings[str(row["target_id"])] = row
            continue
        product = _PRODUCT_OF_TARGET.get(row.get("target_type") or "") or _PRODUCT_OF_APP.get(
            row.get("app") or ""
        )
        add(
            "rating",
            row.get("occurred_at"),
            product,
            target_type=row.get("target_type"),
            target_id=row.get("target_id"),
            **_rating(row),
        )

    for question in journey.get("foodscholar", {}).get("questions", []):
        rated = question_ratings.get(str(question.get("qa_request_id") or ""))
        add(
            "question",
            question.get("occurred_at"),
            "foodscholar",
            **{k: v for k, v in question.items() if k not in ("occurred_at", "rating")},
            rating=_rating(rated) if rated else (
                {"value": question.get("rating"), "negative": _negative(question.get("rating")),
                 "feedback_id": None, "reason": None, "comment": None}
                if question.get("rating") else None
            ),
        )

    for row in searches:
        add(
            "search",
            row.get("occurred_at_dt") or row.get("occurred_at"),
            _product_of_surface(row.get("surface")),
            surface=row.get("surface"),
            query=row.get("query"),
            results=row.get("results"),
            zero_result=bool(row.get("zero_result")),
            relaxed=bool(row.get("relaxed")),
        )

    for event in events:
        kind = event.get("event_type") or ""
        props = event.get("props") or {}
        at = event.get("occurred_at")
        if kind == "http.request":
            if (event.get("status") or 0) >= 400:
                add(
                    "error",
                    at,
                    None,
                    status=event.get("status"),
                    route=event.get("route"),
                    method=event.get("method"),
                    duration_ms=event.get("duration_ms"),
                )
            continue
        if kind in _SKIPPED_EVENTS:
            continue
        if kind == "page.view":
            path = props.get("path") or event.get("route")
            add("page", at, product_of_path(path), path=path, previous=props.get("from"))
            continue
        product = _PRODUCT_OF_APP.get(event.get("app") or "")
        kept = {key: props[key] for key in _EVENT_PROPS if props.get(key) is not None}
        named = {
            "recipe.view": "recipe_view",
            "recipe.result_click": "result_opened",
            "recipe.compare": "compare",
            "favorite.add": "favourite",
            "favorite.remove": "unfavourite",
            "qa.citation_opened": "citation",
            "qa.stream_abandoned": "abandoned",
            "catalog.view": "reading",
        }.get(kind)
        if named:
            add(named, at, product or ("foodscholar" if kind.startswith("qa.") else None), **kept)
        else:
            add("event", at, product, event_type=kind, **kept)

    for error in client_errors:
        add(
            "client_error",
            error.get("occurred_at"),
            None,
            name=error.get("name"),
            message=(error.get("message") or "")[:300] or None,
            path=error.get("url_path"),
            handled=error.get("handled"),
        )

    for click in frustration:
        add(
            "frustration",
            click.get("occurred_at"),
            None,
            click_kind=click.get("kind"),
            path=click.get("path"),
            element_key=click.get("element_key"),
            element_label=click.get("element_label"),
            element_role=click.get("element_role"),
            area=click.get("area"),
            clicks=int(click.get("repeats") or 1),
        )

    # Stable within a timestamp: the order sources were read in is the order
    # a turn, its rating and the page that followed actually happened.
    moments.sort(key=lambda m: m["_at"])
    total = len(moments)
    kept = moments[:MAX_STORY_MOMENTS]
    if not kept:
        return {"chapters": [], "moments": 0, "total": 0, "truncated": False}

    start = kept[0]["_at"]
    idle = timedelta(minutes=CHAPTER_IDLE_MINUTES)
    chapters: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    previous_at: Optional[datetime] = None

    for index, moment in enumerate(kept):
        at = moment.pop("_at")
        moment["id"] = index
        moment["occurred_at"] = at.isoformat()
        moment["offset_seconds"] = int((at - start).total_seconds())
        moment["gap_seconds"] = (
            int((at - previous_at).total_seconds()) if previous_at is not None else None
        )
        product = moment.get("product")
        opens = moment["kind"] not in _ATTACHED and product is not None
        conversation = moment.get("conversation_id")
        if (
            current is None
            or (previous_at is not None and at - previous_at > idle)
            or (opens and product != current["product"])
            or (
                conversation
                and current["conversation_id"]
                and conversation != current["conversation_id"]
            )
        ):
            current = _chapter(len(chapters), product if opens else (product or "other"), at, start)
            chapters.append(current)
        if conversation and not current["conversation_id"]:
            current["conversation_id"] = conversation
        current["moments"].append(moment)
        current["ended_at"] = at.isoformat()
        _count(current["counts"], moment)
        previous_at = at

    for chapter in chapters:
        last = _parse(chapter["ended_at"])
        first = _parse(chapter["started_at"])
        chapter["duration_seconds"] = int((last - first).total_seconds()) if last and first else 0

    return {
        "chapters": chapters,
        "moments": len(kept),
        "total": total,
        "truncated": total > len(kept),
    }


def _chapter(index: int, product: str, at: datetime, start: datetime) -> Dict[str, Any]:
    return {
        "id": index,
        "product": product,
        "conversation_id": None,
        "started_at": at.isoformat(),
        "ended_at": at.isoformat(),
        "offset_seconds": int((at - start).total_seconds()),
        "counts": {
            "turns": 0, "failed_turns": 0, "plans": 0, "plans_saved": 0,
            "questions": 0, "unanswered": 0, "searches": 0, "found_nothing": 0,
            "recipes_opened": 0, "rated_up": 0, "rated_down": 0,
            "errors": 0, "frustration": 0, "pages": 0,
        },
        "moments": [],
    }


def _count(counts: Dict[str, int], moment: Dict[str, Any]) -> None:
    kind = moment["kind"]
    if kind == "turn":
        counts["turns"] += 1
        counts["failed_turns"] += 1 if moment.get("outcome") == "error" else 0
        counts["plans"] += 1 if moment.get("plan") else 0
    elif kind == "plan_generated":
        counts["plans"] += 1
    elif kind == "plan_saved":
        counts["plans_saved"] += 1
    elif kind == "question":
        counts["questions"] += 1
        counts["unanswered"] += 0 if moment.get("answered") else 1
        rating = moment.get("rating") or {}
        if rating.get("value"):
            counts["rated_down" if rating.get("negative") else "rated_up"] += 1
    elif kind == "search":
        counts["searches"] += 1
        counts["found_nothing"] += 1 if moment.get("zero_result") else 0
    elif kind in ("recipe_view", "result_opened"):
        counts["recipes_opened"] += 1 if kind == "recipe_view" else 0
    elif kind == "rating" and moment.get("value"):
        # The feedback widget leaves words without a verdict; that is neither.
        counts["rated_down" if moment.get("negative") else "rated_up"] += 1
    elif kind in ("error", "client_error"):
        counts["errors"] += 1
    elif kind == "frustration":
        counts["frustration"] += int(moment.get("clicks") or 1)
    elif kind == "page":
        counts["pages"] += 1
