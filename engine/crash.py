"""Crash injector. CRASH_AT=<point>:<adapter>:<operation>, e.g.
CRASH_AT=sent_before_receipt:aws_iam:create_access_key"""
import os


def maybe_crash(point: str, adapter: str, operation: str) -> None:
    if os.environ.get("CRASH_AT") == f"{point}:{adapter}:{operation}":
        print(f"[crash] injected at {point}:{adapter}:{operation}", flush=True)
        os._exit(137)
