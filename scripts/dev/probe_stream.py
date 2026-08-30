"""Probe: consume the SSE run-agent-stream over HTTP and report what actually arrives.

Evidence tool for Item 2 (pipeline stalling). Counts per-case data events, records
the final {done} summary, and cross-checks /api/metrics afterward. Run repeatedly.
"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:5000"


def post(path):
    req = urllib.request.Request(BASE + path, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=120) as r:
        return json.loads(r.read().decode())


def consume_stream():
    case_events = 0
    final = None
    statuses = {}
    req = urllib.request.Request(BASE + "/api/run-agent-stream")
    with urllib.request.urlopen(req, timeout=300) as r:
        buf = ""
        for chunk in r:
            buf += chunk.decode("utf-8", "replace")
            while "\n\n" in buf:
                block, buf = buf.split("\n\n", 1)
                block = block.strip()
                if not block.startswith("data:"):
                    continue
                obj = json.loads(block[5:].strip())
                if obj.get("done"):
                    final = obj
                else:
                    case_events += 1
                    st = obj.get("final_status")
                    statuses[st] = statuses.get(st, 0) + 1
    return case_events, final, statuses


def main():
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    for i in range(1, runs + 1):
        reset = post("/api/reset")
        case_events, final, statuses = consume_stream()
        metrics = get("/api/metrics")["agent"]
        print(f"--- RUN {i} ---")
        print(f"  reset: {reset}")
        print(f"  stream case events: {case_events}")
        print(f"  stream per-status: {statuses}")
        print(f"  final done event: {final}")
        print(f"  /api/metrics after: recovered_cases={metrics['recovered_cases']} "
              f"escalated_cases={metrics['escalated_cases']} "
              f"total_cases={metrics['total_cases']} "
              f"amount_recovered={metrics['amount_recovered']}")


if __name__ == "__main__":
    main()
