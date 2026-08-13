import json
import time
import uuid
import logging
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, Depends, HTTPException, status, Header, Query, Request, Path
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app.config import settings
from app.redis_client import redis_client, AsyncDistributedLock

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("pulseboard-api")

# Lifespan connection management
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup connection check
    try:
        await redis_client.ping()
        logger.info("Connected to Redis server successfully.")
        # Auto-seed mock data on startup
        from app.mock_data import seed_data
        seed_data()
        logger.info("Auto-seeded initial mock data.")
    except Exception as e:
        logger.error(f"Failed to connect to Redis on startup: {e}")
    yield
    # Shutdown close connection
    await redis_client.close()
    logger.info("Redis connection closed.")

app = FastAPI(
    title="PulseBoard API",
    description="Real-Time Collaborative Operations Platform Backend with Redis",
    version="1.0.0",
    lifespan=lifespan
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------------------------------
# Pydantic Schemas
# ----------------------------------------------------
class LoginRequest(BaseModel):
    email: str = Field(..., example="engineer@pulseboard.io")
    name: str = Field(..., example="Devin Ross")
    role: str = Field("Engineer", example="Lead SRE")

class ProfileUpdateRequest(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None

class WorkspaceCreateRequest(BaseModel):
    id: str = Field(..., example="ws_infra")
    name: str = Field(..., example="Infrastructure Operations")

class WorkspaceMemberRequest(BaseModel):
    user_id: str = Field(..., example="usr_abc123")

class MessagePublishRequest(BaseModel):
    sender_id: str = Field(..., example="usr_abc123")
    content: str = Field(..., example="Database connection latency spiking.")

class TypingRequest(BaseModel):
    user_id: str = Field(..., example="usr_abc123")

class StreamEventRequest(BaseModel):
    event_type: str = Field(..., example="deployment_started")
    data: Dict[str, Any] = Field(..., example={"service": "auth-api", "version": "v2.1.0"})

class JobQueueRequest(BaseModel):
    job_type: str = Field(..., example="send_welcome_email")
    payload: Dict[str, Any] = Field(..., example={"email": "engineer@pulseboard.io", "subject": "Welcome to PulseBoard!"})

class UserLocationRequest(BaseModel):
    user_id: str = Field(..., example="usr_abc123")
    longitude: float = Field(..., example=-122.4194)
    latitude: float = Field(..., example=37.7749)

class ReputationIncrementRequest(BaseModel):
    increment: int = Field(1, example=5)

# ----------------------------------------------------
# Dependencies
# ----------------------------------------------------
async def get_current_user(authorization: str = Header(None)) -> str:
    """Authenticates user token against Redis sessions."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header. Expected Bearer <token>"
        )
    token = authorization.split(" ")[1]
    user_id = await redis_client.get(f"session:{token}")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session has expired or is invalid."
        )
    # Refresh session TTL (sliding window session expiration)
    await redis_client.expire(f"session:{token}", 3600)
    return user_id

async def rate_limiter(request: Request, user_id: str = Depends(get_current_user)):
    """API Rate Limiter for authenticated requests using atomic Redis counters."""
    current_minute = int(time.time() // 60)
    key = f"rate_limit:{user_id}:{current_minute}"
    
    count = await redis_client.incr(key)
    if count == 1:
        await redis_client.expire(key, 60)
        
    if count > settings.rate_limit_per_minute:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded. Maximum is {settings.rate_limit_per_minute} requests per minute."
        )

async def anonymous_rate_limiter(request: Request):
    """API Rate Limiter for anonymous endpoints using client IP address."""
    client_ip = request.client.host if request.client else "unknown_ip"
    current_minute = int(time.time() // 60)
    key = f"rate_limit:ip:{client_ip}:{current_minute}"
    
    count = await redis_client.incr(key)
    if count == 1:
        await redis_client.expire(key, 60)
        
    if count > settings.rate_limit_per_minute:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded for IP. Maximum is {settings.rate_limit_per_minute} requests per minute."
        )

# Helper to log daily active user metrics and bitmap attendance
async def track_user_activity(user_id: str):
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    month_str = now.strftime("%Y-%m")
    day_of_month = now.day

    # 1. HyperLogLog for DAU
    await redis_client.pfadd(f"analytics:dau:{date_str}", user_id)
    # 2. Bitmap for Monthly Attendance
    await redis_client.setbit(f"attendance:{user_id}:{month_str}", day_of_month, 1)


# ----------------------------------------------------
# 1. Sessions & Authentication
# ----------------------------------------------------
@app.post("/auth/login", tags=["Authentication"], dependencies=[Depends(anonymous_rate_limiter)])
async def login(payload: LoginRequest):
    # Retrieve or generate unique user_id derived from email
    email = payload.email.strip().lower()
    user_id = f"usr_{uuid.uuid5(uuid.NAMESPACE_DNS, email).hex[:8]}"
    
    # Store user profile fields in Hash
    await redis_client.hset(
        f"user:{user_id}",
        mapping={
            "id": user_id,
            "email": email,
            "name": payload.name,
            "role": payload.role,
            "created_at": datetime.now().isoformat()
        }
    )
    
    # Create temporary session in Redis with 1 hour TTL
    session_token = str(uuid.uuid4())
    await redis_client.setex(f"session:{session_token}", 3600, user_id)
    
    # Set presence to online automatically on login
    await redis_client.sadd("online_users", user_id)
    
    # Track DAU & Attendance
    await track_user_activity(user_id)
    
    return {
        "status": "success",
        "user_id": user_id,
        "session_token": session_token,
        "expires_in": 3600
    }

@app.post("/auth/logout", tags=["Authentication"])
async def logout(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=400, detail="Missing authorization header")
    token = authorization.split(" ")[1]
    
    user_id = await redis_client.get(f"session:{token}")
    if user_id:
        # Transaction to clean up session, presence, and logs
        pipe = redis_client.pipeline()
        pipe.delete(f"session:{token}")
        pipe.srem("online_users", user_id)
        await pipe.execute()
        
    return {"status": "success", "message": "Successfully logged out."}


# ----------------------------------------------------
# 2. User Profiles
# ----------------------------------------------------
@app.get("/users/{user_id}/profile", tags=["User Profiles"], dependencies=[Depends(rate_limiter)])
async def get_profile(user_id: str):
    profile = await redis_client.hgetall(f"user:{user_id}")
    if not profile:
        raise HTTPException(status_code=404, detail="User profile not found")
    return profile

@app.put("/users/{user_id}/profile", tags=["User Profiles"], dependencies=[Depends(rate_limiter)])
async def update_profile(user_id: str, payload: ProfileUpdateRequest):
    # Check if profile exists
    exists = await redis_client.exists(f"user:{user_id}")
    if not exists:
        raise HTTPException(status_code=404, detail="User profile not found")
        
    updates = {}
    if payload.name is not None:
        updates["name"] = payload.name
    if payload.role is not None:
        updates["role"] = payload.role
        
    if updates:
        await redis_client.hset(f"user:{user_id}", mapping=updates)
        
    return {"status": "success", "updated_fields": list(updates.keys())}

@app.get("/users/{user_id}/profile/fields", tags=["User Profiles"], dependencies=[Depends(rate_limiter)])
async def get_profile_fields(user_id: str, fields: List[str] = Query(..., description="Fields to retrieve")):
    # Check for existence of profile
    exists = await redis_client.exists(f"user:{user_id}")
    if not exists:
        raise HTTPException(status_code=404, detail="User profile not found")
    
    values = await redis_client.hmget(f"user:{user_id}", fields)
    return dict(zip(fields, values))


# ----------------------------------------------------
# 3. Presence Tracking
# ----------------------------------------------------
@app.post("/presence/online", tags=["Presence Tracking"], dependencies=[Depends(rate_limiter)])
async def go_online(user_id: str = Depends(get_current_user)):
    await redis_client.sadd("online_users", user_id)
    await track_user_activity(user_id)
    return {"status": "success", "user_id": user_id, "presence": "online"}

@app.post("/presence/offline", tags=["Presence Tracking"], dependencies=[Depends(rate_limiter)])
async def go_offline(user_id: str = Depends(get_current_user)):
    await redis_client.srem("online_users", user_id)
    return {"status": "success", "user_id": user_id, "presence": "offline"}

@app.get("/presence/online", tags=["Presence Tracking"], dependencies=[Depends(rate_limiter)])
async def get_online_users():
    users = await redis_client.smembers("online_users")
    return {"online_users": list(users), "count": len(users)}

@app.get("/presence/{user_id}/status", tags=["Presence Tracking"], dependencies=[Depends(rate_limiter)])
async def check_user_online(user_id: str):
    is_online = await redis_client.sismember("online_users", user_id)
    return {"user_id": user_id, "online": bool(is_online)}


# ----------------------------------------------------
# 4. Workspaces & Membership (Transactions / SINTER)
# ----------------------------------------------------
@app.post("/workspaces", tags=["Workspaces"], dependencies=[Depends(rate_limiter)])
async def create_workspace(payload: WorkspaceCreateRequest):
    await redis_client.hset(
        f"workspace:{payload.id}:meta",
        mapping={"id": payload.id, "name": payload.name, "created_at": datetime.now().isoformat()}
    )
    return {"status": "success", "workspace_id": payload.id}

@app.post("/workspaces/{workspace_id}/members", tags=["Workspaces"], dependencies=[Depends(rate_limiter)])
async def add_workspace_member(workspace_id: str, payload: WorkspaceMemberRequest):
    member_id = payload.user_id
    
    # Check if workspace exists
    ws_exists = await redis_client.exists(f"workspace:{workspace_id}:meta")
    if not ws_exists:
        raise HTTPException(status_code=404, detail="Workspace not found")
        
    # Check if member profile exists
    user_exists = await redis_client.exists(f"user:{member_id}")
    if not user_exists:
        raise HTTPException(status_code=404, detail="User profile not found")

    # Transaction: MULTI/EXEC to atomically add to workspace members, user workspaces, and feed
    pipe = redis_client.pipeline()
    pipe.sadd(f"workspace:{workspace_id}:members", member_id)
    pipe.sadd(f"user:{member_id}:workspaces", workspace_id)
    
    # Activity feed event
    feed_event = {
        "event_id": str(uuid.uuid4()),
        "event_type": "workspace_membership",
        "description": f"Added to workspace {workspace_id}",
        "timestamp": datetime.now().isoformat()
    }
    pipe.lpush(f"feed:{member_id}", json.dumps(feed_event))
    pipe.ltrim(f"feed:{member_id}", 0, 99) # Cap feed size at 100 items
    
    # Execute pipeline
    await pipe.execute()
    
    return {"status": "success", "message": f"User {member_id} added to workspace {workspace_id}"}

@app.get("/workspaces/{workspace_id}/members", tags=["Workspaces"], dependencies=[Depends(rate_limiter)])
async def list_workspace_members(workspace_id: str):
    # Check if workspace exists
    ws_exists = await redis_client.exists(f"workspace:{workspace_id}:meta")
    if not ws_exists:
        raise HTTPException(status_code=404, detail="Workspace not found")
        
    members = await redis_client.smembers(f"workspace:{workspace_id}:members")
    
    # Enrich member details
    enriched_members = []
    for member_id in members:
        profile = await redis_client.hgetall(f"user:{member_id}")
        if profile:
            enriched_members.append(profile)
        else:
            enriched_members.append({"id": member_id, "error": "Profile not initialized"})
            
    return enriched_members

@app.get("/workspaces/common", tags=["Workspaces"], dependencies=[Depends(rate_limiter)])
async def common_workspaces(user1: str = Query(..., description="First User ID"), user2: str = Query(..., description="Second User ID")):
    # Run SINTER to find overlapping workspaces
    shared_workspaces = await redis_client.sinter(f"user:{user1}:workspaces", f"user:{user2}:workspaces")
    return {"user1": user1, "user2": user2, "common_workspaces": list(shared_workspaces)}


# ----------------------------------------------------
# 5. Activity Feed
# ----------------------------------------------------
@app.get("/users/{user_id}/feed", tags=["Activity Feed"], dependencies=[Depends(rate_limiter)])
async def get_activity_feed(user_id: str, limit: int = Query(20, ge=1, le=100)):
    # Retrieve from List (most recent events are at index 0 because of LPUSH)
    feed_items = await redis_client.lrange(f"feed:{user_id}", 0, limit - 1)
    
    parsed_feed = []
    for item in feed_items:
        try:
            parsed_feed.append(json.loads(item))
        except json.JSONDecodeError:
            parsed_feed.append({"raw": item})
            
    return parsed_feed


# ----------------------------------------------------
# 6. Real-Time Messaging (Pub/Sub)
# ----------------------------------------------------
@app.post("/channels/{channel_id}/messages", tags=["Real-Time Messaging"], dependencies=[Depends(rate_limiter)])
async def publish_message(channel_id: str, payload: MessagePublishRequest):
    message_payload = {
        "message_id": str(uuid.uuid4()),
        "channel_id": channel_id,
        "sender_id": payload.sender_id,
        "content": payload.content,
        "timestamp": datetime.now().isoformat()
    }
    
    # 1. Publish to channels Pub/Sub topic
    await redis_client.publish(f"channel:{channel_id}:messages", json.dumps(message_payload))
    
    # 2. Increment score for trending channels in Sorted Set
    await redis_client.zincrby("trending:channels", 1, channel_id)
    
    return {"status": "success", "message_id": message_payload["message_id"]}

@app.post("/channels/{channel_id}/typing", tags=["Real-Time Messaging"], dependencies=[Depends(rate_limiter)])
async def publish_typing_indicator(channel_id: str, payload: TypingRequest):
    typing_payload = {
        "channel_id": channel_id,
        "user_id": payload.user_id,
        "timestamp": datetime.now().isoformat()
    }
    # Publish to typing Pub/Sub topic
    await redis_client.publish(f"channel:{channel_id}:typing", json.dumps(typing_payload))
    return {"status": "success"}


# ----------------------------------------------------
# 7. Event Streaming
# ----------------------------------------------------
@app.post("/events", tags=["Event Streaming"], dependencies=[Depends(rate_limiter)])
async def publish_stream_event(payload: StreamEventRequest):
    event_payload = {
        "event_id": str(uuid.uuid4()),
        "event_type": payload.event_type,
        "data": json.dumps(payload.data),
        "timestamp": datetime.now().isoformat()
    }
    
    # XADD adds events to the stream
    stream_id = await redis_client.xadd("stream:events", event_payload, id="*")
    return {"status": "success", "stream_id": stream_id}


# ----------------------------------------------------
# 8. Trending Channels & Reputation (Sorted Sets)
# ----------------------------------------------------
@app.get("/analytics/trending", tags=["Analytics & Reputation"], dependencies=[Depends(rate_limiter)])
async def get_trending_channels(limit: int = Query(5, ge=1, le=50)):
    # ZREVRANGE to get top N members by score in descending order
    trending = await redis_client.zrevrange("trending:channels", 0, limit - 1, withscores=True)
    
    # Format list
    formatted_trending = []
    for rank, (channel_id, score) in enumerate(trending, start=1):
        formatted_trending.append({
            "rank": rank,
            "channel_id": channel_id,
            "activity_score": int(score)
        })
    return formatted_trending

@app.post("/users/{user_id}/reputation", tags=["Analytics & Reputation"], dependencies=[Depends(rate_limiter)])
async def update_user_reputation(user_id: str, payload: ReputationIncrementRequest):
    # Check user existence
    user_exists = await redis_client.exists(f"user:{user_id}")
    if not user_exists:
        raise HTTPException(status_code=404, detail="User profile not found")
        
    new_score = await redis_client.zincrby("reputation:users", payload.increment, user_id)
    return {"user_id": user_id, "reputation_score": int(new_score)}

@app.get("/users/{user_id}/reputation", tags=["Analytics & Reputation"], dependencies=[Depends(rate_limiter)])
async def get_user_reputation(user_id: str):
    score = await redis_client.zscore("reputation:users", user_id)
    if score is None:
        return {"user_id": user_id, "reputation_score": 0}
    return {"user_id": user_id, "reputation_score": int(score)}


# ----------------------------------------------------
# 9. Distributed Locking
# ----------------------------------------------------
@app.post("/locks/trigger-daily-digest", tags=["Distributed Locking"], dependencies=[Depends(rate_limiter)])
async def trigger_daily_digest():
    lock = AsyncDistributedLock("daily_digest", expire=10)
    
    # Try to acquire lock
    acquired = await lock.acquire()
    if not acquired:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Lock conflict: Another worker is currently processing the daily digest report."
        )
        
    try:
        # Simulate processing work
        logger.info("[LOCK EXECUTION] Acquired lock: lock:daily_digest. Executing digest...")
        await asyncio.sleep(2.0)
        logger.info("[LOCK EXECUTION] Digest processing complete. Releasing lock...")
    finally:
        # Atomically release lock via Lua script
        released = await lock.release()
        if released:
            logger.info("[LOCK EXECUTION] Lock released successfully.")
        else:
            logger.warning("[LOCK EXECUTION] Lock release failed or was already released (timeout).")
            
    return {"status": "success", "message": "Daily digest processed and compiled."}


# ----------------------------------------------------
# 10. Approximate Analytics (DAU) & Bitmaps Attendance
# ----------------------------------------------------
@app.post("/attendance/track", tags=["Attendance & Analytics"], dependencies=[Depends(rate_limiter)])
async def track_attendance(user_id: str = Depends(get_current_user)):
    await track_user_activity(user_id)
    return {"status": "success", "user_id": user_id, "tracked": True}

@app.get("/analytics/dau", tags=["Attendance & Analytics"], dependencies=[Depends(rate_limiter)])
async def get_daily_active_users(date: Optional[str] = Query(None, description="Target date in YYYY-MM-DD format")):
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    # PFCOUNT gets unique count in HyperLogLog
    count = await redis_client.pfcount(f"analytics:dau:{date}")
    return {"date": date, "unique_active_users": count}

@app.get("/attendance/{user_id}/count", tags=["Attendance & Analytics"], dependencies=[Depends(rate_limiter)])
async def get_attendance_count(user_id: str, month: Optional[str] = Query(None, description="Target month in YYYY-MM format")):
    if not month:
        month = datetime.now().strftime("%Y-%m")
        
    user_exists = await redis_client.exists(f"user:{user_id}")
    if not user_exists:
        raise HTTPException(status_code=404, detail="User profile not found")
        
    # BITCOUNT returns number of bits set to 1 in the bitmap
    active_days_count = await redis_client.bitcount(f"attendance:{user_id}:{month}")
    return {
        "user_id": user_id,
        "month": month,
        "active_days_count": active_days_count
    }

@app.get("/attendance/{user_id}/check/{day}", tags=["Attendance & Analytics"], dependencies=[Depends(rate_limiter)])
async def check_attendance_day(user_id: str, day: int = Path(..., ge=1, le=31), month: Optional[str] = Query(None, description="Target month in YYYY-MM format")):
    if not month:
        month = datetime.now().strftime("%Y-%m")
        
    user_exists = await redis_client.exists(f"user:{user_id}")
    if not user_exists:
        raise HTTPException(status_code=404, detail="User profile not found")
        
    # GETBIT returns bit value at offset
    bit_value = await redis_client.getbit(f"attendance:{user_id}:{month}", day)
    return {
        "user_id": user_id,
        "month": month,
        "day": day,
        "active": bool(bit_value)
    }


# ----------------------------------------------------
# 11. Geospatial Awareness
# ----------------------------------------------------
@app.post("/geo/location", tags=["Geospatial Awareness"], dependencies=[Depends(rate_limiter)])
async def update_geo_location(payload: UserLocationRequest):
    user_exists = await redis_client.exists(f"user:{payload.user_id}")
    if not user_exists:
        raise HTTPException(status_code=404, detail="User profile not found")
        
    # GEOADD stores coordinates
    # Parameters order: key, longitude, latitude, member
    await redis_client.geoadd("geo:active_users", (payload.longitude, payload.latitude, payload.user_id))
    return {
        "status": "success",
        "user_id": payload.user_id,
        "coordinates": {"longitude": payload.longitude, "latitude": payload.latitude}
    }

@app.get("/geo/nearby", tags=["Geospatial Awareness"], dependencies=[Depends(rate_limiter)])
async def get_nearby_users(
    longitude: float = Query(..., description="Query central longitude"),
    latitude: float = Query(..., description="Query central latitude"),
    radius: float = Query(10.0, description="Radius distance"),
    unit: str = Query("km", regex="^(km|m|mi|ft)$", description="Distance unit")
):
    # GEOSEARCH queries elements in range
    results = await redis_client.geosearch(
        "geo:active_users",
        longitude=longitude,
        latitude=latitude,
        radius=radius,
        unit=unit,
        withdist=True,
        withcoord=True
    )
    
    formatted = []
    if results:
        for item in results:
            # redis-py returns a list: [member, distance, (long, lat)]
            user_id = item[0]
            distance = item[1]
            coords = item[2]
            
            formatted.append({
                "user_id": user_id,
                "distance": distance,
                "unit": unit,
                "coordinates": {"longitude": coords[0], "latitude": coords[1]}
            })
            
    return formatted


# ----------------------------------------------------
# 12. Background Job Queue
# ----------------------------------------------------
@app.post("/jobs", tags=["Background Job Queue"], dependencies=[Depends(rate_limiter)])
async def enqueue_job(payload: JobQueueRequest):
    job_id = str(uuid.uuid4())
    job_payload = {
        "id": job_id,
        "job_type": payload.job_type,
        "payload": payload.payload,
        "enqueued_at": datetime.now().isoformat()
    }
    
    # LPUSH enqueues job onto Redis List
    await redis_client.lpush("jobs:queue", json.dumps(job_payload))
    return {"status": "success", "job_id": job_id}


# Helper import to trigger async locks in FastAPI
import asyncio
