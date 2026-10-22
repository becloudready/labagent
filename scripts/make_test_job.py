#!/usr/bin/env python3
"""Generate a job.json with N fake learners for an end-to-end test (no real student data).

  python scripts/make_test_job.py --command provision --cohort e2e-001 --date 01-Jan-2030 \\
      --pubkey pub.pem --count 10 > job.json
For sweep apply, pass --plan plan.json --approved-hash <hash> (the plan object from a previous sweep plan result).
"""
import argparse
import json
import sys

p = argparse.ArgumentParser()
p.add_argument("--command", required=True)
p.add_argument("--mode", default=None)
p.add_argument("--cohort", required=True)
p.add_argument("--date", required=True)
p.add_argument("--count", type=int, default=10)
p.add_argument("--pubkey")
p.add_argument("--plan", help="path to result.json from a sweep plan run; its .plan is embedded")
p.add_argument("--approved-hash")
p.add_argument("--learner-id", help="for reset")
p.add_argument("--force", action="store_true")
a = p.parse_args()

job = {
    "run_id": "00000000-0000-0000-0000-%012d" % 1,
    "command": a.command,
    "cohort": {"id": a.cohort, "created_date": a.date},
    "learners": [{"learner_id": str(i), "username": f"e2e-student-{i:02d}@example.com"}
                 for i in range(1, a.count + 1)],
    "options": {},
}
if a.mode:
    job["mode"] = a.mode
if a.pubkey:
    job["secrets_pubkey_pem"] = open(a.pubkey).read()
if a.plan:
    job["plan"] = json.load(open(a.plan))["plan"]
if a.approved_hash:
    job["approved_hash"] = a.approved_hash
if a.learner_id:
    job["options"]["learner_id"] = a.learner_id
if a.force:
    job["options"]["force"] = True
json.dump(job, sys.stdout, indent=2)
