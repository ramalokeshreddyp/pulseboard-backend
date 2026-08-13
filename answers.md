# PulseBoard Questionnaire Responses

### 1. High-Level Architecture
We chose a **decoupled, multi-service architecture** composed of:
1. **API Server (FastAPI)**: Serves REST requests, runs auth check middleware, writes profile hashes and sets, and publishes events or pushes background tasks.
2. **Background Worker Service (Python Asyncio)**: A service executing three concurrent task loops for background processing: polling job queues via `BRPOP`, processing stream consumer group events via `XREADGROUP`, and logging Pub/Sub messages via `psubscribe`.
3. **Operational Data Store (Redis)**: Serves as the high-velocity coordinator and in-memory cache.

**Why this is suitable:**
- **Separation of Concerns**: High-latency or CPU-bound tasks (like mock emails, logs, and digests) are processed in the background worker, ensuring the web API remains highly responsive.
- **Independent Scaling**: If background jobs or stream ingestion experiences spikes, we can horizontally scale the background worker containers independently without increasing the API footprint.
- **Event-Driven decoupling**: Redis Pub/Sub and Streams decouple the API inputs from operational processing, providing clean boundaries.

---

### 2. Redis Key Naming Strategy & Trade-offs
We adopted the standard namespace convention `namespace:object_type:id:attribute` (e.g., `user:usr_alice`, `workspace:ws_infra:members`, `session:token_uuid`). This prevents naming collisions and simplifies operations.

#### Selected Feature 1: Presence (Redis Set - `online_users`)
- **Justification**: A Set is perfect because membership must be unique (a user is either online or offline). Set operations like `SADD` (go online), `SREM` (go offline), and `SISMEMBER` (check status) execute in $O(1)$ time complexity. We retrieve all active users instantly via `SMEMBERS`.
- **Trade-offs / Alternative**: If a user's client crashes, they remain in the Set indefinitely until an explicit logout. An alternative is using a **Sorted Set (ZSET)** where the member is `user_id` and the score is the epoch heartbeat timestamp. We could run a periodic task to purge members with scores older than 30 seconds (`ZREVRANGEBYSCORE`). We chose a **Set** for simplicity and low operational overhead, relying on explicit logout and session expiry actions.

#### Selected Feature 2: Activity Feed (Redis List - `feed:{user_id}`)
- **Justification**: We use a List as a capped chronological log. We push new events to the head using `LPUSH` ($O(1)$) and trim the list length to 100 items using `LTRIM` ($O(1)$ on continuous caps). Retrieve recent feeds quickly via `LRANGE` ($O(K)$ where $K$ is the limit).
- **Trade-offs / Alternative**: Fetching or inserting elements in the middle of a List is slow ($O(N)$). If we needed to query specific events by ID or edit feed items, a **Hash** or **Sorted Set** would be better. Since feeds are strictly read-only and chronologically ordered (newest-first), a capped List is the most memory-efficient structure.

---

### 3. Scaling the Redis Layer (10x Traffic)
To handle 10x traffic, we would implement the following sequential strategies:

1. **Read/Write Splitting (Primary-Replica)**: Since reads (like querying profiles, presence, and feeds) dominate operational traffic, we would direct writes to a Redis Primary and distribute read queries across multiple read replicas.
2. **Redis Cluster (Sharding)**: For write-heavy scalability and memory partitioning, we would transition to a Redis Cluster.
   - **Impact on Key Design**: Redis Cluster hashes keys into 16,384 slots. Commands operating on multiple keys (like `SINTER` for common workspaces: `SINTER user:{user1}:workspaces user:{user2}:workspaces`, or pipeline `MULTI`/`EXEC` blocks) will fail if the keys partition to different slots.
   - **Remediation**: To resolve this, we would use **Redis Hash Tags** (e.g., `user:{usr_alice}:workspaces`) to force specific related keys to hash to the same cluster slot. For cross-user queries (like intersecting Alice's and Bob's workspaces), tagging everything to one slot destroys cluster balance. Therefore, we would change the intersection logic to retrieve sets and intersect them in the API application layer (in-memory Python `set.intersection`).

---

### 4. Handling Redis Connection Failures
- **Impact**: If Redis experiences a connection failure, the API server will fail to validate session tokens, verify rate limits, store locations, and retrieve feeds, throwing `HTTP 500` or connection errors to the client.
- **Resilience Mechanisms**:
  - **Connection Pool Backoff**: Configure the Redis client connection pool to use retry strategies with exponential backoff.
  - **Circuit Breaker Pattern**: If connection attempts fail consecutively, trip the circuit breaker. Instead of hanging or waiting for network timeouts, immediately return a user-friendly `HTTP 503 Service Unavailable` or run in a degraded mode.
  - **Stale/Local Fallbacks**: For user profiles, fallback to a local in-memory cache (like Cachetools or an LRU cache). For rate-limiting, fail-open (log warning and allow request) to prevent locking out valid users due to helper service outages.
  - **Fallback Database**: If a primary relational database exists, queries could fallback to direct DB lookup (at higher latency) while Redis is recovering.

---

### 5. Background Job Queue (Lists vs. Streams)
- **Why we chose Lists (`jobs:queue`)**: We used Redis Lists with `LPUSH` (enqueue) and `BRPOP` (blocking dequeue).
  - **Pros**: Highly performant, extremely simple to implement, and blocking pops eliminate CPU busy-waiting loops.
  - **Cons (Trade-off)**: **No Acknowledgement Guarantee**. Once `BRPOP` pops a job, it is deleted from Redis. If the worker container crashes midway through processing, the job is permanently lost.
- **Comparison with Streams**:
  - If we used **Streams** with `XADD` and `XREADGROUP`, we would gain **At-Least-Once Processing Guarantees**.
  - Dequeued stream messages remain in the consumer group's Pending Entries List (PEL). If a worker crashes before sending `XACK`, another worker can claim the pending message, preventing job losses.
  - **Downside**: Streams require complex housekeeping (periodically querying `XPENDING` to claim dead tasks and managing consumer offsets), which was unnecessary for PulseBoard's ephemeral notifications but vital for transaction-heavy queues.
