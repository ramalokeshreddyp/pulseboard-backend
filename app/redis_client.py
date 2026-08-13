import uuid
import redis.asyncio as aioredis
import redis as syncredis
from app.config import settings

# Async Redis client for FastAPI
redis_client = aioredis.Redis(
    host=settings.redis_host,
    port=settings.redis_port,
    password=settings.redis_password or None,
    db=settings.redis_db,
    decode_responses=True
)

# Sync Redis client for Worker
sync_redis_client = syncredis.Redis(
    host=settings.redis_host,
    port=settings.redis_port,
    password=settings.redis_password or None,
    db=settings.redis_db,
    decode_responses=True
)

# Lua script to release lock atomically only if the caller owns it
RELEASE_LOCK_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

class AsyncDistributedLock:
    """Async Redis Distributed Lock using SET NX EX and Lua release script."""
    def __init__(self, name: str, expire: int = 10):
        self.client = redis_client
        self.name = f"lock:{name}"
        self.expire = expire
        self.owner_id = str(uuid.uuid4())

    async def acquire(self) -> bool:
        result = await self.client.set(self.name, self.owner_id, nx=True, ex=self.expire)
        return bool(result)

    async def release(self) -> bool:
        result = await self.client.eval(RELEASE_LOCK_LUA, 1, self.name, self.owner_id)
        return bool(result)


class SyncDistributedLock:
    """Sync Redis Distributed Lock for background worker threads."""
    def __init__(self, name: str, expire: int = 10):
        self.client = sync_redis_client
        self.name = f"lock:{name}"
        self.expire = expire
        self.owner_id = str(uuid.uuid4())

    def acquire(self) -> bool:
        result = self.client.set(self.name, self.owner_id, nx=True, ex=self.expire)
        return bool(result)

    def release(self) -> bool:
        result = self.client.eval(RELEASE_LOCK_LUA, 1, self.name, self.owner_id)
        return bool(result)
