# PulseBoard Project Documentation

This documentation covers the design patterns, codebase folder organization, setup instructions, testing strategies, and technical integration details of the **PulseBoard Real-Time Operations Platform**.

---

## 📂 Codebase Folder Structure

```text
pulseboard-backend/
├── app/
│   ├── __init__.py
│   ├── config.py          # Configuration loading using pydantic-settings
│   ├── main.py            # FastAPI REST routing definitions & middleware
│   ├── mock_data.py       # Initial operational database seeder
│   ├── redis_client.py    # Connection pools and Distributed Lock module
│   └── worker.py          # Background queues and stream event consumer
├── .env                   # Local configuration file (ignored by Git)
├── .env.example           # Example config configuration template
├── .gitignore             # Git ignored paths and rules
├── Dockerfile             # Multi-stage container definition
├── docker-compose.yml     # Services link configuration (Redis, API, Worker)
├── requirements.txt       # Python dependency declarations
└── verify_pulseboard.py   # E2E Automated Integration Test Suite
```

---

## 🛠️ Installation & Execution Commands

### Local Running (Development Environment)

1. **Install Dependencies**:
   Ensure you have Python 3.11+ installed. Set up a virtual environment and install packages:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Configure Settings**:
   Create a `.env` file based on the example:
   ```bash
   cp .env.example .env
   ```

3. **Run Redis locally**:
   Ensure Redis is active on port `6389` or update the `REDIS_PORT` inside `.env`.

4. **Seed Mock Data manually**:
   ```bash
   python -m app.mock_data
   ```

5. **Start services**:
   - **API Server**:
     ```bash
     uvicorn app.main:app --reload --port 8000
     ```
   - **Background Worker**:
     ```bash
     python -m app.worker
     ```

---

### Dockerized Running (Production Setup)

Run the entire suite (Redis, API, Worker) with single command:
```bash
# Build and boot the container network in background
docker compose up --build -d

# Check running statuses
docker compose ps

# Follow logs from background services
docker compose logs -f
```

---

## 🚀 Key Modules & Code Integration Details

### 1. Sessions & Authentication
- **Objective**: Maintain stateful sessions without database overhead.
- **Implementation**: On successful login (`POST /auth/login`), a random UUID token is generated. We execute `SETEX session:{token} 3600 {user_id}`. 
- **Validation**: Routes requiring auth inject `Depends(get_current_user)`. The dependency extracts the Bearer token, fetches the user ID using `GET session:{token}`, and refreshes the session's sliding-window expiration using `EXPIRE session:{token} 3600`.

### 2. API Rate Limiting
- **Objective**: Prevent API abuse from both public and authenticated routes.
- **Implementation**: Utilizes Redis atomic increments.
  - For **authenticated users**, the key is: `rate_limit:{user_id}:{minute_timestamp}`.
  - For **public endpoints**, the key is: `rate_limit:ip:{client_ip}:{minute_timestamp}`.
- **Execution Flow**:
  1. Check count using `INCR key`.
  2. If the count is 1, set the expiration: `EXPIRE key 60`.
  3. If count exceeds threshold (e.g. 60 requests/minute), raise `HTTP 429 Too Many Requests`.

### 3. Workspaces & SINTER
- **Objective**: Manage workspaces and query overlapping spaces between users.
- **Implementation**:
  - Store workspace membership: `SADD workspace:{id}:members {user_id}`.
  - Store user's workspace catalog: `SADD user:{user_id}:workspaces {id}`.
  - To check overlapping workspaces: `SINTER user:{user1_id}:workspaces user:{user2_id}:workspaces`. This performs an intersect query inside Redis, returning only common workspaces.

### 4. Background Job Queue
- **Objective**: Handle asynchronous long-running jobs (e.g. emails).
- **Implementation**:
  - **Producer**: Enqueues jobs to Redis via `LPUSH jobs:queue {payload_json}`.
  - **Consumer**: A background task executing blocking pop `BRPOP jobs:queue` in `app/worker.py`. When a task arrives, it is popped, parsed, and logged.

### 5. Event Streaming
- **Objective**: Append log records for operations telemetry.
- **Implementation**:
  - **Producer**: API server calls `XADD stream:events * data {payload_json}`.
  - **Consumer**: In `app/worker.py`, the consumer connects to the consumer group using `XREADGROUP GROUP worker_group worker_1 COUNT 5 BLOCK 2000 STREAMS stream:events >`.
  - On successful processing, the worker acknowledges the event: `XACK stream:events worker_group {message_id}`.

### 6. Geospatial Awareness
- **Objective**: Locate active users in close vicinity.
- **Implementation**:
  - Logs coordinate entries: `GEOADD geo:active_users {longitude} {latitude} {user_id}`.
  - Radius Query: `GEOSEARCH geo:active_users FROMLONLAT {longitude} {latitude} BYRADIUS {radius} km WITHDIST WITHCOORD`.

### 7. Approximate Analytics & Attendance
- **Objective**: Track Unique Daily Active Users (DAU) and individual monthly login records.
- **Implementation**:
  - **DAU HLL**: Adds user to HyperLogLog: `PFADD analytics:dau:{YYYY-MM-DD} {user_id}`. Counts uniquely via `PFCOUNT analytics:dau:{YYYY-MM-DD}`.
  - **Attendance Bitmaps**: Sets a daily bit using the day of the month as the offset: `SETBIT attendance:{user_id}:{YYYY-MM} {day_of_month} 1`. Query monthly active days using `BITCOUNT attendance:{user_id}:{YYYY-MM}`.

---

## 🧪 Testing & Validation Checks

Our automated testing suite (`verify_pulseboard.py`) performs 12 distinct assertions. Each check logs successes and errors:

| Step | Test Objective | Target Redis Commands | Validated Expected Status |
| :--- | :--- | :--- | :--- |
| **1** | User Auth Session | `SETEX`, `SADD`, `PFADD` | Returns 200 with session token, sets active DAU |
| **2** | Profile Fetching | `HGETALL`, `HMGET`, `EXISTS` | Checks and returns nested profile hash fields |
| **3** | Attendance Bitmap | `SETBIT`, `BITCOUNT`, `GETBIT` | Verifies active bits and calculated count matches |
| **4** | Presence Status | `SADD`, `SREM`, `SISMEMBER` | Verifies user registers as online and offline |
| **5** | Workspace Sets | `SADD`, `SMEMBERS`, `SINTER` | Returns membership listings and intersects workspaces |
| **6** | Activity feed | `LPUSH`, `LTRIM`, `LRANGE` | Returns events in correct chronological order |
| **7** | Pub/Sub & ZSET | `PUBLISH`, `ZINCRBY`, `ZREVRANGE`| Broadcasts chat; increments channel popularity score |
| **8** | Stream Logging | `XADD`, `XREADGROUP`, `XACK` | Enqueues and acknowledges stream telemetry |
| **9** | Mutex Locking | `SET NX EX`, Lua `EVAL` | Prevents double digests; executes safely |
| **10**| Geospatial Index | `GEOADD`, `GEOSEARCH` | Locates SF users inside a 5km radius |
| **11**| Job Queue | `LPUSH`, `BRPOP` | Enqueues job payload, Worker pops and parses |
| **12**| Rate limiting | `INCR`, `EXPIRE` | Blocks rapid spam requests with HTTP 429 |
