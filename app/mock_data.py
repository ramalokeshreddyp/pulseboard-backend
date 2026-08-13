import json
import logging
from datetime import datetime
from app.redis_client import sync_redis_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("pulseboard-seeder")

def seed_data():
    logger.info("Starting mock data seeding into Redis...")
    
    # 1. Clear existing data to ensure clean state
    # We can do this field-by-field or FLUSHDB. To be safe, we will delete key patterns related to our app
    keys_to_delete = [
        "online_users", "trending:channels", "reputation:users", "geo:active_users", "jobs:queue", "stream:events"
    ]
    # Delete pattern-based keys
    for pattern in ["user:*", "workspace:*", "feed:*", "session:*", "attendance:*", "analytics:*", "rate_limit:*"]:
        found_keys = sync_redis_client.keys(pattern)
        keys_to_delete.extend(found_keys)
        
    keys_to_delete = list(set(keys_to_delete)) # deduplicate
    if keys_to_delete:
        sync_redis_client.delete(*keys_to_delete)
        logger.info(f"Cleaned up {len(keys_to_delete)} existing keys.")

    # 2. Seed User Profiles (Hashes)
    users = {
        "usr_alice": {
            "id": "usr_alice",
            "email": "alice@pulseboard.io",
            "name": "Alice Vance",
            "role": "Lead SRE",
            "created_at": datetime.now().isoformat()
        },
        "usr_bob": {
            "id": "usr_bob",
            "email": "bob@pulseboard.io",
            "name": "Bob Smith",
            "role": "Security Engineer",
            "created_at": datetime.now().isoformat()
        },
        "usr_charlie": {
            "id": "usr_charlie",
            "email": "charlie@pulseboard.io",
            "name": "Charlie Dev",
            "role": "Backend Developer",
            "created_at": datetime.now().isoformat()
        }
    }
    
    for user_id, profile in users.items():
        sync_redis_client.hset(f"user:{user_id}", mapping=profile)
        logger.info(f"Seeded user profile: {user_id}")

    # 3. Seed Workspaces (Hashes) and Memberships (Sets)
    workspaces = {
        "ws_infra": "Infrastructure Operations",
        "ws_security": "Security Operations",
        "ws_apps": "Application Delivery"
    }
    
    for ws_id, ws_name in workspaces.items():
        # Workspace metadata
        sync_redis_client.hset(f"workspace:{ws_id}:meta", mapping={
            "id": ws_id,
            "name": ws_name,
            "created_at": datetime.now().isoformat()
        })
        logger.info(f"Seeded workspace metadata: {ws_id} ({ws_name})")

    # Add memberships (using MULTI / Transaction syntax via pipeline)
    # Alice is in ws_infra and ws_apps
    # Bob is in ws_infra and ws_security
    # Charlie is in ws_apps only
    memberships = [
        ("ws_infra", "usr_alice"),
        ("ws_infra", "usr_bob"),
        ("ws_security", "usr_bob"),
        ("ws_apps", "usr_alice"),
        ("ws_apps", "usr_charlie"),
    ]
    
    pipe = sync_redis_client.pipeline()
    for ws_id, user_id in memberships:
        pipe.sadd(f"workspace:{ws_id}:members", user_id)
        pipe.sadd(f"user:{user_id}:workspaces", ws_id)
    pipe.execute()
    logger.info("Seeded workspace memberships.")

    # 4. Seed Activity Feeds (Lists)
    for user_id in users.keys():
        feed_items = [
            {"event_id": f"evt_1_{user_id}", "event_type": "system_signup", "description": "Welcome to PulseBoard!", "timestamp": datetime.now().isoformat()},
            {"event_id": f"evt_2_{user_id}", "event_type": "profile_update", "description": "Profile details initialized.", "timestamp": datetime.now().isoformat()}
        ]
        for item in feed_items:
            sync_redis_client.lpush(f"feed:{user_id}", json.dumps(item))
        # Cap feed
        sync_redis_client.ltrim(f"feed:{user_id}", 0, 99)
    logger.info("Seeded initial activity feeds.")

    # 5. Seed Presence (Sets)
    # Alice and Bob are online, Charlie is offline
    sync_redis_client.sadd("online_users", "usr_alice", "usr_bob")
    logger.info("Seeded online presence status.")

    # 6. Seed Trending Channels (Sorted Sets)
    trending_channels = {
        "channel_incidents": 89,
        "channel_ops": 45,
        "channel_deployments": 23,
        "channel_general": 12
    }
    for channel_id, score in trending_channels.items():
        sync_redis_client.zincrby("trending:channels", score, channel_id)
    logger.info("Seeded trending channels sorted set.")

    # 7. Seed User Reputation (Sorted Sets)
    reputations = {
        "usr_alice": 1500,
        "usr_bob": 1200,
        "usr_charlie": 1000
    }
    for user_id, score in reputations.items():
        sync_redis_client.zadd("reputation:users", {user_id: score})
    logger.info("Seeded user reputation scores.")

    # 8. Seed Geospatial Data (Geo Index)
    # Alice: SF Downtown (approx)
    # Bob: SF Mission (approx, ~2km away)
    # Charlie: NY Times Square (far away)
    sync_redis_client.geoadd("geo:active_users", (-122.4194, 37.7749, "usr_alice"))
    sync_redis_client.geoadd("geo:active_users", (-122.4014, 37.7879, "usr_bob"))
    sync_redis_client.geoadd("geo:active_users", (-73.9851, 40.7580, "usr_charlie"))
    logger.info("Seeded geospatial active user locations.")

    # 9. Seed DAU HyperLogLog and Attendance Bitmaps for testing
    date_str = datetime.now().strftime("%Y-%m-%d")
    month_str = datetime.now().strftime("%Y-%m")
    day_of_month = datetime.now().day
    
    # Track Alice and Bob as active today
    sync_redis_client.pfadd(f"analytics:dau:{date_str}", "usr_alice", "usr_bob")
    sync_redis_client.setbit(f"attendance:usr_alice:{month_str}", day_of_month, 1)
    sync_redis_client.setbit(f"attendance:usr_bob:{month_str}", day_of_month, 1)
    
    # Also mark Alice active 5 days ago for history simulation
    sync_redis_client.setbit(f"attendance:usr_alice:{month_str}", max(1, day_of_month - 5), 1)
    sync_redis_client.setbit(f"attendance:usr_alice:{month_str}", max(1, day_of_month - 2), 1)

    logger.info("Seeded analytics HLL and attendance bitmaps.")
    logger.info("Data seeding completed successfully!")

if __name__ == "__main__":
    seed_data()
