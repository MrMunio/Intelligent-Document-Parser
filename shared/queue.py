from redis import Redis
from rq import Queue
from shared.config import settings

# Short timeouts for request-path use (enqueue, rate limiting, heartbeats).
redis_conn = Redis(host=settings.redis_host, port=settings.redis_port, socket_connect_timeout=5, socket_timeout=10,
                   health_check_interval=30)

# We have a normal queue 'default' and priority queue 'priority'
default_queue = Queue('default', connection=redis_conn)
priority_queue = Queue('priority', connection=redis_conn)
