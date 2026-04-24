"""
Image upload and retrieval service backed by MinIO.
"""
from __future__ import annotations

from io import BytesIO
from typing import Dict, Tuple
from uuid import uuid4
import logging

from fastapi import UploadFile
from minio.error import S3Error
from PIL import Image, UnidentifiedImageError

from backend.minio import MINIO_CLIENT
from backend.redis import IMAGE_CACHE
from exceptions import DataError, InternalError, NotFoundError
from main import config
from utils import is_valid_uuid


logger = logging.getLogger(__name__)


class ImageStorageService:
    BUCKET_NAME = "images"
    MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024
    ALLOWED_FORMATS = {
        "PNG": "image/png",
        "JPEG": "image/jpeg",
    }

    def _ensure_bucket(self) -> None:
        client = MINIO_CLIENT()
        try:
            if not client.bucket_exists(self.BUCKET_NAME):
                client.make_bucket(self.BUCKET_NAME)
        except S3Error as exc:
            if exc.code in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                return
            logger.error("Failed ensuring image bucket %s: %s", self.BUCKET_NAME, exc)
            raise InternalError(detail="Failed to prepare image storage bucket") from exc

    def _read_upload_bytes(self, upload: UploadFile) -> bytes:
        raw_bytes = upload.file.read()
        if not raw_bytes:
            raise DataError(detail="Uploaded image is empty")
        if len(raw_bytes) > self.MAX_FILE_SIZE_BYTES:
            raise DataError(detail="Image exceeds the 5 MB limit")
        return raw_bytes

    def _optimize_image(self, raw_bytes: bytes) -> Tuple[bytes, str]:
        try:
            image = Image.open(BytesIO(raw_bytes))
            image.load()
        except UnidentifiedImageError as exc:
            raise DataError(
                detail="Unsupported image file. Only PNG and JPEG images are allowed"
            ) from exc
        except OSError as exc:
            raise DataError(detail="Invalid image upload") from exc

        image_format = (image.format or "").upper()
        content_type = self.ALLOWED_FORMATS.get(image_format)
        if content_type is None:
            raise DataError(
                detail="Unsupported image file. Only PNG and JPEG images are allowed"
            )

        save_kwargs = {
            "format": image_format,
        }

        icc_profile = image.info.get("icc_profile")
        if icc_profile is not None:
            save_kwargs["icc_profile"] = icc_profile

        exif = image.info.get("exif")
        if exif is not None:
            save_kwargs["exif"] = exif

        if image_format == "PNG":
            save_kwargs.update(
                optimize=True,
                compress_level=9,
            )
        else:
            if image.mode not in {"RGB", "L", "CMYK", "YCbCr"}:
                image = image.convert("RGB")
            save_kwargs.update(
                optimize=True,
                quality="keep",
                subsampling="keep",
                qtables="keep",
            )

        optimized_buffer = BytesIO()
        try:
            image.save(optimized_buffer, **save_kwargs)
        except OSError as exc:
            raise DataError(detail="Failed to process uploaded image") from exc

        optimized_bytes = optimized_buffer.getvalue()
        if len(optimized_bytes) < len(raw_bytes):
            return optimized_bytes, content_type
        return raw_bytes, content_type

    def upload_image(self, upload: UploadFile) -> Dict[str, object]:
        try:
            raw_bytes = self._read_upload_bytes(upload)
            stored_bytes, content_type = self._optimize_image(raw_bytes)
            image_id = str(uuid4())

            self._ensure_bucket()
            client = MINIO_CLIENT()

            try:
                client.put_object(
                    self.BUCKET_NAME,
                    image_id,
                    data=BytesIO(stored_bytes),
                    length=len(stored_bytes),
                    content_type=content_type,
                )
            except S3Error as exc:
                logger.error("Failed uploading image %s: %s", image_id, exc)
                raise InternalError(detail="Failed to store uploaded image") from exc

            return {
                "image_id": image_id,
                "bucket": self.BUCKET_NAME,
                "content_type": content_type,
                "original_size_bytes": len(raw_bytes),
                "stored_size_bytes": len(stored_bytes),
                "compressed": len(stored_bytes) < len(raw_bytes),
            }
        finally:
            upload.file.close()

    def get_image(self, image_id: str) -> Dict[str, object]:
        if not is_valid_uuid(image_id):
            raise DataError(detail="Invalid image id")

        cache_enabled = config.settings.get("CACHE_ENABLED", False)
        if cache_enabled:
            cached = IMAGE_CACHE.get(image_id)
            if cached is not None:
                data, content_type = cached
                return {
                    "image_id": image_id,
                    "data": data,
                    "content_type": content_type,
                }

        client = MINIO_CLIENT()
        try:
            response = client.get_object(self.BUCKET_NAME, image_id)
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}:
                raise NotFoundError(detail="Image not found") from exc
            logger.error("Failed retrieving image %s: %s", image_id, exc)
            raise InternalError(detail="Failed to retrieve image") from exc

        try:
            body = response.read()
            content_type = response.headers.get("Content-Type", "application/octet-stream")
        finally:
            response.close()
            response.release_conn()

        if cache_enabled:
            max_bytes = config.settings.get("IMAGE_CACHE_MAX_BYTES", 0)
            if max_bytes and len(body) <= max_bytes:
                IMAGE_CACHE.set(image_id, body, content_type)

        return {
            "image_id": image_id,
            "data": body,
            "content_type": content_type,
        }


IMAGE_STORAGE = ImageStorageService()
