#!/usr/bin/env python3
"""
Push a job to the Redis toWorker queue (same format the REST server uses).
Useful for testing the worker without going through the REST API.
Example: after uploading an MP3 via REST /apiv1/separate, the hash is returned.
You can also push a job with a songhash that already exists in the queue bucket.
"""
import os
import sys
import json
import redis

REDIS_HOST = os.getenv("REDIS_HOST") or "localhost"
REDIS_PORT = int(os.getenv("REDIS_PORT") or "6379")
# Match sample-requests.py / REST port (macOS: 5000 is often AirPlay)
REST = os.getenv("REST") or "localhost:5001"
CALLBACK_BASE = os.getenv("CALLBACK_BASE") or f"http://{REST}"

redis_client = redis.StrictRedis(host=REDIS_HOST, port=REDIS_PORT, db=0)

# Usage: python3 send-request.py <songhash>
# Env: SONGHASH, REDIS_HOST, REDIS_PORT, REST=localhost:5001, CALLBACK_BASE=http://...
if __name__ == "__main__":
    songhash = os.getenv("SONGHASH") or (sys.argv[1] if len(sys.argv) > 1 else None)
    if not songhash:
        print("Usage: SONGHASH=abc123 python send-request.py   OR   python send-request.py <songhash>")
        sys.exit(1)
    job = {
        "songhash": songhash,
        "model": "htdemucs",
        "callback": {"url": CALLBACK_BASE, "data": {"songhash": songhash}},
    }
    redis_client.rpush("toWorker", json.dumps(job))
    print(f"Pushed job for songhash={songhash}")
