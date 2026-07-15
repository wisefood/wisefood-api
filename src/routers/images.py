"""
Image upload and retrieval endpoints.
"""

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile

from api.v1.images import IMAGE_STORAGE
from auth import auth
from routers.generic import render
from schemas import ImageUploadResponse


router = APIRouter(prefix="/api/v1/images", tags=["Image Storage Operations"])


@router.post(
    "",
    dependencies=[Depends(auth("admin,expert"))],
    summary="Upload an image",
    description=(
        "Uploads a PNG or JPEG image up to 5 MB into the images MinIO bucket. "
        "The bucket is created automatically if it does not already exist."
    ),
)
@render()
def upload_image(
    request: Request,
    file: UploadFile = File(..., description="PNG or JPEG image file up to 5 MB"),
):
    stored = IMAGE_STORAGE.upload_image(file)
    stored["id"] = stored["image_id"]
    stored["image_url"] = str(
        request.url_for("get_image", image_id=stored["image_id"])
    )
    return ImageUploadResponse(**stored)


# NOTE: declared before /{image_id} so "preview" is not captured as an id.
@router.get(
    "/preview",
    name="get_image_preview",
    summary="Downscaled preview of an external recipe image",
    description=(
        "Fetches an external recipe image once, downscales it to the requested "
        "width (WebP), and serves it from the server-side image cache thereafter. "
        "Used by search-result cards; falls back client-side to the original URL "
        "when the preview cannot be produced."
    ),
)
@render()
def get_image_preview(
    request: Request,
    src: str = Query(..., min_length=8, max_length=2048, description="Absolute http(s) image URL"),
    w: int = Query(480, ge=64, le=1024, description="Target width in px (snapped to 160/320/480/640)"),
):
    stored = IMAGE_STORAGE.get_preview(src, w)
    return Response(
        content=stored["data"],
        media_type=stored["content_type"],
        headers={
            "Cache-Control": "public, max-age=604800",
            "Content-Length": str(len(stored["data"])),
        },
    )


@router.get(
    "/{image_id}",
    name="get_image",
    summary="Get an uploaded image",
    description="Returns a previously uploaded image by UUID.",
)
@render()
def get_image(request: Request, image_id: str):
    stored = IMAGE_STORAGE.get_image(image_id)
    return Response(
        content=stored["data"],
        media_type=stored["content_type"],
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "Content-Length": str(len(stored["data"])),
        },
    )
