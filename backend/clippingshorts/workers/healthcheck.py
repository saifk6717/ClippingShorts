"""Docker healthcheck for a worker: healthy when its heartbeat in Redis is fresh.

    python -m clippingshorts.workers.healthcheck stream|ai|renderer
"""
import sys
import time

from .. import redis_bus as rb


def main():
    name = sys.argv[1]
    hb = rb.get_json(rb.k("worker", name, "hb"))
    if not hb or time.time() - hb.get("t", 0) > 30:
        print(f"{name}: no fresh heartbeat")
        sys.exit(1)
    print(f"{name}: ok")


if __name__ == "__main__":
    main()
