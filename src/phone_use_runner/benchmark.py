import json
import math
import statistics
import time
from pathlib import Path


def summarize(values: list[float]):
    if not values:
        return {"samples": 0, "p50_ms": None, "p95_ms": None}
    ordered = sorted(values)
    return {"samples": len(values), "p50_ms": round(statistics.median(ordered), 2),
            "p95_ms": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 2)}


def benchmark(transport, device_id: str, samples: int, output: Path):
    report = {"device_id": device_id, "samples_per_mode": samples, "modes": {},
              "boundary": "Client RPC uses pinned reusable HTTPS; first sample includes connection setup; collection_ms is phone-reported"}
    for mode in ("tree", "both"):
        timings, collection, failures = [], [], []
        for _ in range(samples):
            if mode == "both":
                # Android rate-limits screenshots. The sampling interval is excluded from RPC timing.
                time.sleep(0.4)
            started = time.perf_counter()
            try:
                value = transport.call(device_id, "observe", {
                    "mode": mode, "output": "decision", "compact": True, "timeout_ms": 3000,
                })
                elapsed = (time.perf_counter() - started) * 1000
                tree = value.get("tree") or {}
                shot = value.get("screenshot") or {}
                if value.get("error") or not tree.get("available") or (mode == "both" and not shot.get("available")):
                    failures.append({"error": value.get("error"), "tree_reason": tree.get("reason"),
                                     "screenshot_reason": shot.get("reason"), "rpc_ms": round(elapsed, 2)})
                else:
                    timings.append(elapsed)
                    if isinstance(value.get("collection_ms"), (int, float)):
                        collection.append(value["collection_ms"])
            except Exception as error:
                failures.append({"error_type": type(error).__name__})
        report["modes"][mode] = {"rpc": summarize(timings), "phone_collection": summarize(collection),
                                 "failures": failures, "failure_rate": len(failures) / samples}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
