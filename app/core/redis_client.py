"""Redis 连接：提醒用默认库，仪表盘缓存用 DB1（可配置）。"""

from __future__ import annotations

from redis.asyncio import Redis

from app.core.config import get_settings

_redis: Redis | None = None
_cache_redis: Redis | None = None


def get_redis() -> Redis:
    """ROI 提醒等通用 Redis（默认 DB0）。"""
    global _redis
    if _redis is None:
        _redis = Redis.from_url(get_settings().REDIS_URL, decode_responses=True)
    return _redis


def get_cache_redis() -> Redis:
    """子账户仪表盘缓存（默认 DB1）。"""
    global _cache_redis
    if _cache_redis is None:
        _cache_redis = Redis.from_url(
            get_settings().REDIS_CACHE_URL, decode_responses=True
        )
    return _cache_redis


async def close_redis() -> None:
    global _redis, _cache_redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
    if _cache_redis is not None:
        await _cache_redis.aclose()
        _cache_redis = None
