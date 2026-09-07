from fastapi import APIRouter, Request, Depends
from routers.generic import render
from auth import auth
from budget import ip_rate_limit
from schemas import LoginSchema, MTMSchema
import kutils
from exceptions import AuthenticationError, AuthorizationError

router = APIRouter(prefix="/api/v1/system", tags=["System Operations"])


@router.get("/ping")
@render()
def ping(request: Request):
    return "pong"


@router.get("/info")
@render()
async def info(request: Request):
    """Public facts about this deployment.

    Unauthenticated on purpose, and that is what makes it the right place for
    the maintenance flag: the browser has to learn the platform is closed
    *before* anyone signs in, or a non-admin sees the login page, signs in,
    and is then turned away — which is worse than being told at the door.
    """
    from analytics import SETTINGS
    from main import config

    values = await SETTINGS.refresh_if_stale()
    return {
        "service": "WiseFood Core API",
        "version": "0.0.1",
        "docs": "/docs",
        "keycloak": config.settings["KEYCLOAK_EXT_URL"],
        "minio": config.settings["MINIO_EXT_URL_CONSOLE"],
        "maintenance": bool(values.get("platform.maintenance_mode", False)),
    }


@router.get("/endpoints")
@render()
def endpoints(request: Request):
    from main import api

    return {
        route.name: {
            "path": route.path,
            "method": (
                list(route.methods)[0]
                if hasattr(route, "methods") and route.methods
                else None
            ),
        }
        for route in api.routes
        if hasattr(route, "name") and hasattr(route, "path")
    }


@router.post("/login")
@render()
def login(request: Request, creds: LoginSchema):
    return kutils.get_token(username=creds.username, password=creds.password)


@router.post("/mtm")
@render()
def login(request: Request, creds: MTMSchema):
    return kutils.get_client_token(
        client_id=creds.client_id, client_secret=creds.client_secret
    )


@router.post(
    "/guest",
    dependencies=[Depends(ip_rate_limit("guest_create", limit=5, window_seconds=3600))],
)
@render()
async def guest_login(request: Request):
    """
    Create an ephemeral guest account and return its token.

    The guest is a real (short-lived) Keycloak user with the 'guest' realm
    role and a pre-provisioned household + member, so the rest of the
    platform treats it like any other isolated user. Guests and all their
    data are deleted automatically after GUEST_TTL_SECONDS.
    """
    import guests

    return await guests.create_guest()


@router.delete(
    "/guest",
    dependencies=[Depends(auth())],
    summary="Erase this guest account and all of its data now",
    description=(
        "Delete the calling guest account immediately: its household and "
        "members, its FoodChat sessions, and the Keycloak user itself. Guests "
        "are already reaped automatically at expiry; this exists so the data "
        "can be erased on demand — at a conference booth, between one attendee "
        "and the next, without waiting for the TTL. Guest accounts only: a "
        "registered user calling this gets 403, because account deletion for "
        "real users is a different, consent-bearing flow."
    ),
)
@render()
async def guest_purge(request: Request):
    """
    Erase the calling guest and everything provisioned for it.

    Same teardown the expiry reaper runs, triggered by the session that owns
    the data. The token is dead afterwards — the client must drop it and start
    a new guest session if it wants to continue.
    """
    import guests

    if not kutils.is_guest(request):
        raise AuthorizationError(
            detail="Only guest accounts can be erased through this endpoint."
        )

    user_id = kutils.current_user(request)["sub"]
    await guests.delete_guest(user_id)
    return {"erased": True}
