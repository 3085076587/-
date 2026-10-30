"""ROI 设置后的倒计时提醒（优先 Redis；连不上时用进程内内存兜底）。"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from redis.exceptions import RedisError

from app.core.redis_client import get_redis

logger = logging.getLogger("uvicorn.error")

KEY_DATA = "ad_helper:roi_remind:data"
KEY_DUE = "ad_helper:roi_remind:due"
KEY_FIRED = "ad_helper:roi_remind:fired"

# Redis 不可用时的进程内兜底（单进程有效）
_memory: dict[str, dict[str, Any]] = {}
_redis_ok: bool | None = None


async def _ping_redis() -> bool:
    global _redis_ok
    try:
        await get_redis().ping()
        _redis_ok = True
        return True
    except Exception as exc:
        if _redis_ok is not False:
            logger.warning("Redis 不可用，ROI 提醒改用内存：%s", exc)
        _redis_ok = False
        return False


def _memory_list(now: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for data in _memory.values():
        row = dict(data)
        fire_at = int(row.get("fire_at") or 0)
        remain = max(0, fire_at - now)
        row["remain_seconds"] = remain
        row["status"] = "fired" if remain <= 0 or row.get("_fired") else "counting"
        if remain <= 0:
            row["_fired"] = True
            _memory[row["id"]]["_fired"] = True
        items.append(row)
    items.sort(key=lambda x: int(x.get("fire_at") or 0))
    return items


async def create_reminder(
    *,
    advertiser_id: int,
    ad_id: int,
    ad_name: str,
    roi2_goal: float,
    remind_after_seconds: int,
) -> dict[str, Any]:
    now = int(time.time())
    fire_at = now + max(1, int(remind_after_seconds))
    reminder_id = uuid.uuid4().hex
    payload = {
        "id": reminder_id,
        "advertiser_id": advertiser_id,
        "ad_id": ad_id,
        "ad_name": ad_name or str(ad_id),
        "roi2_goal": roi2_goal,
        "created_at": now,
        "fire_at": fire_at,
        "remind_after_seconds": remind_after_seconds,
        "storage": "redis",
    }

    if await _ping_redis():
        try:
            r = get_redis()
            await r.hset(KEY_DATA, reminder_id, json.dumps(payload, ensure_ascii=False))
            await r.zadd(KEY_DUE, {reminder_id: fire_at})
            return payload
        except RedisError as exc:
            logger.warning("写入 Redis 提醒失败，改用内存：%s", exc)

    payload["storage"] = "memory"
    _memory[reminder_id] = dict(payload)
    return payload


async def list_active() -> list[dict[str, Any]]:
    now = int(time.time())
    if not await _ping_redis():
        return _memory_list(now)

    try:
        r = get_redis()
        due_ids = await r.zrangebyscore(KEY_DUE, min=0, max=now)
        if due_ids:
            pipe = r.pipeline()
            for rid in due_ids:
                pipe.sadd(KEY_FIRED, rid)
                pipe.zrem(KEY_DUE, rid)
            await pipe.execute()

        raw = await r.hgetall(KEY_DATA)
        fired = set(await r.smembers(KEY_FIRED))
        items: list[dict[str, Any]] = []
        for rid, text in raw.items():
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            fire_at = int(data.get("fire_at") or 0)
            remain = max(0, fire_at - now)
            status = "fired" if rid in fired or remain <= 0 else "counting"
            data["remain_seconds"] = remain
            data["status"] = status
            data["storage"] = "redis"
            items.append(data)
        # 合并内存兜底（例如中途 Redis 挂了又恢复）
        items.extend(_memory_list(now))
        items.sort(key=lambda x: int(x.get("fire_at") or 0))
        return items
    except RedisError as exc:
        logger.warning("读取 Redis 提醒失败：%s", exc)
        return _memory_list(now)


async def pop_due_for_notify() -> list[dict[str, Any]]:
    items = await list_active()
    return [x for x in items if x.get("status") == "fired"]


async def ack_reminder(reminder_id: str) -> bool:
    removed_mem = False
    if reminder_id in _memory:
        _memory.pop(reminder_id, None)
        removed_mem = True

    if not await _ping_redis():
        return removed_mem

    try:
        r = get_redis()
        removed = await r.hdel(KEY_DATA, reminder_id)
        await r.srem(KEY_FIRED, reminder_id)
        await r.zrem(KEY_DUE, reminder_id)
        return bool(removed) or removed_mem
    except RedisError as exc:
        logger.warning("确认 Redis 提醒失败：%s", exc)
        return removed_mem
