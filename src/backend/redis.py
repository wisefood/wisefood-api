import redis
from main import config

from exceptions import (
    BadGatewayError,
)
import logging
import json
import time

logger = logging.getLogger(__name__)


class RedisClient:
    _pool = None

    @classmethod
    def _initialize_redis(cls):
        cls._pool = redis.ConnectionPool(
            host=config.settings["REDIS_HOST"],
            port=config.settings["REDIS_PORT"],
            db=2,
            decode_responses=True,
            max_connections=10,
        )
        logging.info("Initialized Redis connection pool")
        return cls._pool

    def set(self, key, value):
        """Set a value in Redis using a connection from the pool."""
        try:
            if self._pool is None:
                self._initialize_redis()
            conn = redis.Redis(connection_pool=self._pool)
            if isinstance(value, dict):
                conn.set(key, json.dumps(value))  # Serialize dict to JSON string
            else:
                conn.set(key, value)
        except Exception as e:
            raise BadGatewayError(e)

    def get(self, key):
        """Get a value from Redis using a connection from the pool."""
        try:
            if self._pool is None:
                self._initialize_redis()
            conn = redis.Redis(connection_pool=self._pool)
            value = conn.get(key)
            try:
                return json.loads(value)  # Attempt to deserialize JSON string
            except (TypeError, json.JSONDecodeError):
                return value  # Return as-is if not JSON
        except Exception as e:
            raise BadGatewayError(e)

    def delete(self, key):
        """Delete a value from Redis using a connection from the pool."""
        try:
            if self._pool is None:
                self._initialize_redis()
            conn = redis.Redis(connection_pool=self._pool)
            conn.delete(key)
        except Exception as e:
            raise BadGatewayError(e)


# Create a singleton instance of RedisClient
REDIS = RedisClient()


class ImageCache:
    """
    LRU-bounded image byte cache backed by Redis on a dedicated DB.

    Keys:
      img:{id}      -> raw image bytes
      img:{id}:ct   -> content-type (utf-8 bytes)
      img:index     -> sorted set of image_ids scored by last-access epoch (LRU)
    """

    _pool = None
    _DATA_PREFIX = b"img:"
    _INDEX_KEY = b"img:index"

    @classmethod
    def _initialize(cls):
        cls._pool = redis.ConnectionPool(
            host=config.settings["REDIS_HOST"],
            port=config.settings["REDIS_PORT"],
            db=config.settings["IMAGE_CACHE_REDIS_DB"],
            decode_responses=False,
            max_connections=10,
        )
        logger.info(
            "Initialized image cache Redis pool (db=%s)",
            config.settings["IMAGE_CACHE_REDIS_DB"],
        )
        return cls._pool

    @classmethod
    def _conn(cls):
        if cls._pool is None:
            cls._initialize()
        return redis.Redis(connection_pool=cls._pool)

    @staticmethod
    def _data_key(image_id: str) -> bytes:
        return f"img:{image_id}".encode("utf-8")

    @staticmethod
    def _ct_key(image_id: str) -> bytes:
        return f"img:{image_id}:ct".encode("utf-8")

    def get(self, image_id: str):
        """Return (bytes, content_type) on hit, or None on miss / error."""
        try:
            conn = self._conn()
            data_key = self._data_key(image_id)
            ct_key = self._ct_key(image_id)
            pipe = conn.pipeline()
            pipe.get(data_key)
            pipe.get(ct_key)
            data, ct = pipe.execute()
            if data is None:
                return None
            conn.zadd(self._INDEX_KEY, {image_id.encode("utf-8"): time.time()})
            content_type = ct.decode("utf-8") if ct else "application/octet-stream"
            return data, content_type
        except Exception as exc:
            logger.warning("Image cache get failed for %s: %s", image_id, exc)
            return None

    def set(self, image_id: str, data: bytes, content_type: str) -> None:
        """Store image bytes. Silently no-ops on error. Enforces LRU bound."""
        try:
            ttl = config.settings["IMAGE_CACHE_TTL_SECONDS"]
            max_items = config.settings["IMAGE_CACHE_MAX_ITEMS"]
            conn = self._conn()

            data_key = self._data_key(image_id)
            ct_key = self._ct_key(image_id)
            member = image_id.encode("utf-8")

            pipe = conn.pipeline()
            pipe.set(data_key, data, ex=ttl)
            pipe.set(ct_key, content_type.encode("utf-8"), ex=ttl)
            pipe.zadd(self._INDEX_KEY, {member: time.time()})
            pipe.execute()

            count = conn.zcard(self._INDEX_KEY)
            if count > max_items:
                to_evict = count - max_items
                victims = conn.zrange(self._INDEX_KEY, 0, to_evict - 1)
                if victims:
                    evict_pipe = conn.pipeline()
                    for v in victims:
                        vid = v.decode("utf-8") if isinstance(v, bytes) else v
                        evict_pipe.delete(self._data_key(vid))
                        evict_pipe.delete(self._ct_key(vid))
                    evict_pipe.zrem(self._INDEX_KEY, *victims)
                    evict_pipe.execute()
        except Exception as exc:
            logger.warning("Image cache set failed for %s: %s", image_id, exc)

    def delete(self, image_id: str) -> None:
        try:
            conn = self._conn()
            pipe = conn.pipeline()
            pipe.delete(self._data_key(image_id))
            pipe.delete(self._ct_key(image_id))
            pipe.zrem(self._INDEX_KEY, image_id.encode("utf-8"))
            pipe.execute()
        except Exception as exc:
            logger.warning("Image cache delete failed for %s: %s", image_id, exc)


IMAGE_CACHE = ImageCache()