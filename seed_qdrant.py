"""
seed_qdrant.py
==============
Creates the `devops_knowledge` collection in Qdrant and upserts a set of
DevOps runbook entries that the KnowledgeAgent can retrieve.

Run once from the project root before starting the pipeline:

    python seed_qdrant.py

Requirements (already in your env):
    pip install qdrant-client sentence-transformers
"""

import sys
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
from sentence_transformers import SentenceTransformer

# ── Config ────────────────────────────────────────────────────────────────────
QDRANT_HOST      = "localhost"
QDRANT_PORT      = 6333
COLLECTION_NAME  = "devops_knowledge"
EMBEDDING_MODEL  = "sentence-transformers/all-MiniLM-L6-v2"
VECTOR_SIZE      = 384

# ── Knowledge documents ───────────────────────────────────────────────────────
# Each entry will become one searchable vector in Qdrant.
# Add / edit freely — the KnowledgeAgent will surface the closest matches.
DOCUMENTS = [
    # ── SQLAlchemy / connection pool ──────────────────────────────────────────
    {
        "id"      : 1,
        "text"    : (
            "SQLAlchemy connection pool exhaustion fix: set pool_size=20 and "
            "max_overflow=10 on create_engine(). The default pool_size=5 is too "
            "small for production workloads. Also set pool_pre_ping=True so stale "
            "connections are recycled automatically."
        ),
        "source"  : "runbook/sqlalchemy",
        "tags"    : ["sqlalchemy", "connection-pool", "python"],
    },
    {
        "id"      : 2,
        "text"    : (
            "OperationalError: QueuePool limit overflow. Symptoms: requests hang or "
            "raise 'TimeoutError: QueuePool limit of size X overflow Y reached'. "
            "Root cause: pool_size too small or connections not closed. Fix: increase "
            "pool_size, ensure every engine.connect() is used as a context manager, "
            "and never silence exceptions with a bare except clause."
        ),
        "source"  : "runbook/sqlalchemy",
        "tags"    : ["sqlalchemy", "connection-pool", "error"],
    },
    {
        "id"      : 3,
        "text"    : (
            "Python bare except anti-pattern: `except:` catches BaseException including "
            "KeyboardInterrupt and SystemExit, hiding all errors silently. Always catch "
            "specific exceptions, e.g. `except sqlalchemy.exc.OperationalError as e` "
            "and log or re-raise."
        ),
        "source"  : "runbook/python-best-practices",
        "tags"    : ["python", "exception-handling"],
    },

    # ── PostgreSQL ────────────────────────────────────────────────────────────
    {
        "id"      : 4,
        "text"    : (
            "PostgreSQL max_connections default is 100. Each SQLAlchemy pool_size slot "
            "holds one persistent connection. If multiple services share the same DB, "
            "use PgBouncer as a connection pooler to avoid hitting the server limit."
        ),
        "source"  : "runbook/postgresql",
        "tags"    : ["postgresql", "connection-pool"],
    },

    # ── Kubernetes / pod restarts ─────────────────────────────────────────────
    {
        "id"      : 5,
        "text"    : (
            "Pod CrashLoopBackOff caused by OOMKilled: increase memory limits in the "
            "Deployment manifest. Check `kubectl describe pod <name>` for Last State "
            "exit code 137 (OOM). Typical fix: set resources.limits.memory to at least "
            "512Mi for Python services."
        ),
        "source"  : "runbook/kubernetes",
        "tags"    : ["kubernetes", "oom", "crashloop"],
    },
    {
        "id"      : 6,
        "text"    : (
            "Kubernetes liveness probe failing: service starts slowly and probe fires "
            "before the app is ready. Fix: add initialDelaySeconds=30 to the "
            "livenessProbe spec, or switch to a startupProbe for slow-starting containers."
        ),
        "source"  : "runbook/kubernetes",
        "tags"    : ["kubernetes", "liveness-probe"],
    },

    # ── Redis ─────────────────────────────────────────────────────────────────
    {
        "id"      : 7,
        "text"    : (
            "Redis NOAUTH Authentication required: the client is connecting without a "
            "password but requirepass is set in redis.conf. Fix: pass the password in "
            "the connection URL redis://:password@host:6379 or set REDIS_PASSWORD env var."
        ),
        "source"  : "runbook/redis",
        "tags"    : ["redis", "auth"],
    },
    {
        "id"      : 8,
        "text"    : (
            "Redis connection timeout in high-traffic services: increase the connection "
            "pool size in the client library (e.g. redis-py: ConnectionPool(max_connections=50)). "
            "Also enable TCP keepalive to detect dead connections early."
        ),
        "source"  : "runbook/redis",
        "tags"    : ["redis", "connection-pool", "timeout"],
    },

    # ── General microservices ─────────────────────────────────────────────────
    {
        "id"      : 9,
        "text"    : (
            "HTTP 503 Service Unavailable from downstream API: implement exponential "
            "backoff with jitter (initial=0.5s, max=30s, multiplier=2). Use a circuit "
            "breaker (e.g. pybreaker) to stop cascading failures when the downstream "
            "is consistently unavailable."
        ),
        "source"  : "runbook/microservices",
        "tags"    : ["http", "retry", "circuit-breaker"],
    },
    {
        "id"      : 10,
        "text"    : (
            "High CPU usage in Python service: profile with `py-spy top --pid <pid>`. "
            "Common culprits: N+1 database queries (fix with eager loading), unbounded "
            "loops, or missing indexes. Use EXPLAIN ANALYZE in PostgreSQL to find slow queries."
        ),
        "source"  : "runbook/performance",
        "tags"    : ["performance", "cpu", "python"],
    },
]

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"Connecting to Qdrant at {QDRANT_HOST}:{QDRANT_PORT} …")
    client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)

    print(f"Loading embedding model: {EMBEDDING_MODEL} …")
    model = SentenceTransformer(EMBEDDING_MODEL)

    # Create (or recreate) the collection
    existing = [c.name for c in client.get_collections().collections]
    if COLLECTION_NAME in existing:
        print(f"Collection '{COLLECTION_NAME}' already exists — recreating …")
        client.delete_collection(COLLECTION_NAME)

    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
    )
    print(f"Collection '{COLLECTION_NAME}' created (size={VECTOR_SIZE}, distance=COSINE)")

    # Embed and upsert
    print(f"Embedding and upserting {len(DOCUMENTS)} documents …")
    points = []
    for doc in DOCUMENTS:
        vector = model.encode(doc["text"]).tolist()
        points.append(PointStruct(
            id      = doc["id"],
            vector  = vector,
            payload = {
                "text"  : doc["text"],
                "source": doc["source"],
                "tags"  : doc["tags"],
            },
        ))

    client.upsert(collection_name=COLLECTION_NAME, points=points)

    # Verify
    info = client.get_collection(COLLECTION_NAME)
    count = info.points_count
    print(f"\n✔  Upserted {count} vectors into '{COLLECTION_NAME}'")
    print("    Run `python run_pipeline.py` now — the KnowledgeAgent will find results.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n✘  Error: {e}", file=sys.stderr)
        print("    Is Qdrant running?  docker ps | grep qdrant", file=sys.stderr)
        sys.exit(1)