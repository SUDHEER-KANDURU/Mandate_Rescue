"""Probe Item 3: are the rejected (spoofed) webhook customer_ids deterministic-by-design
or is the panel showing static/cached data?

We seed an ISOLATED in-memory DB with different explicit seeds, run the agent, and read
the real webhook_rejected audit rows via metrics.rejected_webhooks(). We also flip the
SPOOFED_CUSTOMER_IDS constant to prove the rejected set tracks the actual spoofed events
(i.e. the panel is data-driven, not hardcoded to CUST1007/1042/1099).
"""
import random

import db
import seed as seed_module
import agent as agent_module
import metrics as metrics_module


def run_with(seed_value, spoofed=None, total=180):
    """Seed an isolated in-memory DB, run the agent, return the rejected customer_ids."""
    orig_spoofed = seed_module.SPOOFED_CUSTOMER_IDS
    if spoofed is not None:
        seed_module.SPOOFED_CUSTOMER_IDS = tuple(spoofed)
    try:
        conn = db.get_memory_connection()
        db.init_db(conn)
        rng = random.Random(seed_value)
        records = seed_module.build_records(rng, total=total)
        for rec in records:
            db.insert_mandate_failure(conn, rec)
        conn.commit()
        agent_module.run_agent(policy=agent_module.PolicyParams(use_llm=False), conn=conn)
        rejected = metrics_module.rejected_webhooks(conn)
        conn.close()
        return sorted(r["customer_id"] for r in rejected)
    finally:
        seed_module.SPOOFED_CUSTOMER_IDS = orig_spoofed


print("SPOOFED_CUSTOMER_IDS constant =", seed_module.SPOOFED_CUSTOMER_IDS)
print()
print("seed=42  (default) rejected IDs:", run_with(42))
print("seed=7   (different) rejected IDs:", run_with(7))
print("seed=999 (different) rejected IDs:", run_with(999))
print()
print("Now flip the spoofed constant to prove the panel is DATA-DRIVEN, not hardcoded:")
print("spoofed=[CUST1000,CUST1001,CUST1002] seed=42 rejected IDs:",
      run_with(42, spoofed=["CUST1000", "CUST1001", "CUST1002"]))
