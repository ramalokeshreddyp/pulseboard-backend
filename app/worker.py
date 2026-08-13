import asyncio
import json
import logging
import signal
from app.redis_client import redis_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("pulseboard-worker")

async def job_queue_worker():
    """Worker task that polls jobs:queue via BRPOP and processes them."""
    logger.info("Background Job Worker started. Listening on 'jobs:queue'...")
    while True:
        try:
            # BRPOP returns (key, value) or None
            result = await redis_client.brpop("jobs:queue", timeout=2)
            if result:
                _, payload_str = result
                try:
                    payload = json.loads(payload_str)
                    logger.info(f"[JOB WORKER] Processing job: {payload}")
                    # Simulate processing (e.g. sending a mock email)
                    await asyncio.sleep(0.5)
                    logger.info(f"[JOB WORKER] Successfully completed job: {payload.get('id', 'unknown')}")
                except json.JSONDecodeError:
                    logger.error(f"[JOB WORKER] Invalid JSON payload: {payload_str}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[JOB WORKER] Error: {e}")
            await asyncio.sleep(2)

async def stream_worker():
    """Worker task that reads from stream:events using a consumer group and acknowledges events."""
    stream_key = "stream:events"
    group_name = "worker_group"
    consumer_name = "worker_1"

    # Ensure stream and group exist
    try:
        await redis_client.xgroup_create(stream_key, group_name, id="0", mkstream=True)
        logger.info(f"Created Redis Stream consumer group '{group_name}' for stream '{stream_key}'.")
    except Exception as e:
        # Group already exists or stream initialization handled it
        if "BUSYGROUP" in str(e):
            logger.info(f"Consumer group '{group_name}' already exists.")
        else:
            logger.warning(f"Consumer group creation warning: {e}")

    logger.info("Stream Worker started. Reading from 'stream:events'...")
    while True:
        try:
            # Read new messages (using ">" to get messages never delivered to other consumers)
            streams = await redis_client.xreadgroup(
                group_name,
                consumer_name,
                {stream_key: ">"},
                count=5,
                block=2000
            )
            if streams:
                for _, messages in streams:
                    for message_id, payload in messages:
                        logger.info(f"[STREAM WORKER] Received event [{message_id}]: {payload}")
                        # Simulate processing
                        await asyncio.sleep(0.2)
                        # Acknowledge the event
                        await redis_client.xack(stream_key, group_name, message_id)
                        logger.info(f"[STREAM WORKER] Acknowledged event [{message_id}]")
        except asyncio.CancelledError:
            break
        except Exception as e:
            if "NOGROUP" in str(e):
                logger.warning("[STREAM WORKER] Consumer group or stream is missing. Re-creating...")
                try:
                    await redis_client.xgroup_create(stream_key, group_name, id="0", mkstream=True)
                    logger.info(f"[STREAM WORKER] Re-created consumer group '{group_name}'.")
                except Exception as cg_err:
                    if "BUSYGROUP" not in str(cg_err):
                        logger.error(f"[STREAM WORKER] Failed to re-create group: {cg_err}")
            else:
                logger.error(f"[STREAM WORKER] Error reading from stream: {e}")
            await asyncio.sleep(2)

async def pubsub_worker():
    """Worker task that subscribes to channel messaging & typing Pub/Sub topics and logs them."""
    logger.info("Pub/Sub Subscriber started. Subscribing to channel updates...")
    pubsub = redis_client.pubsub()
    await pubsub.psubscribe("channel:*:messages", "channel:*:typing")
    
    try:
        async for message in pubsub.listen():
            if message["type"] == "pmessage":
                channel = message["channel"]
                data = message["data"]
                logger.info(f"[PUBSUB WORKER] Channel [{channel}] broadcast event: {data}")
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"[PUBSUB WORKER] Error: {e}")
    finally:
        await pubsub.unsubscribe()
        await pubsub.close()

async def main():
    # Create running background tasks
    tasks = [
        asyncio.create_task(job_queue_worker()),
        asyncio.create_task(stream_worker()),
        asyncio.create_task(pubsub_worker())
    ]
    
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def shutdown_handler():
        logger.info("Received SIGINT/SIGTERM shutdown signal. Gracefully stopping workers...")
        stop_event.set()

    # Register OS signals
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown_handler)
        except NotImplementedError:
            pass

    # Wait until container sends a stop signal
    await stop_event.wait()
    
    logger.info("Cancelling running worker tasks...")
    for task in tasks:
        task.cancel()
        
    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("All background tasks shut down. Graceful exit completed.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Worker stopped by KeyboardInterrupt.")
