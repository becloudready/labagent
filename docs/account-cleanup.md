# Cleaning up an existing account

For accounts that were never tagged consistently (shared accounts, years of workshop leftovers). The tag-driven cohort
`sweep` cannot help there, because it only finds what carries your tag. `audit` and `cleanup` work from a **ruleset**:
an explicit, reviewable statement of what may be deleted.

## Safety model: delete only what a rule says

Decision order for every discovered resource:

1. a `keep` rule matches: **keep** (always wins, regardless of rule order)
2. built-in protected name prefixes (CloudTrail, Config, CDK, ...): **keep**
3. a `delete` rule matches **and** `labagent` has a deleter for the type: **delete**
4. anything else: **review**. It is reported and never deleted.

So a forgotten resource type or a typo makes labagent delete *less*, not more. Empty `match` blocks and `delete` rules
without a type are rejected when the ruleset is loaded. A rule with `"enabled": false` stays in the file but has no effect.

## Ruleset

```json
{
  "version": 1, "name": "legacy-cleanup",
  "rules": [
    {"id": "keep-admin", "action": "keep",   "match": {"type": ["iam/user"], "name": ["admin_*"]}},
    {"id": "old-keys",   "action": "delete", "match": {"type": ["ec2/key-pair"], "older_than_days": 30}}
  ]
}
```

Match keys (all must match; a list means any entry): `type` (`service/type`), `region`, `name` (glob, case-insensitive),
`id`, `name_regex`, `tags` (`{"Key": "glob"}`), `tags_absent`, `older_than_days`, `unused_days`, `attrs` (exact match on
discovered attributes such as `enabled`, `attachments`, `aliases`). See `labagent/rules.py` for the full contract and
`examples/rulesets/legacy-cleanup.example.json` for a starting point.

**Keep your real rulesets out of this public repo.** They contain your resource names. Store them in the private runner
repo (or the orchestrator) and pass them to the job.

## Workflow

| Step | Command | Changes AWS? |
|---|---|---|
| 1. Audit | job `{"command":"audit","options":{"regions":["us-east-1"]}}` with your ruleset | no |
| 2. Plan | `{"command":"cleanup","mode":"plan"}` | no. Returns a plan with an id and hash |
| 3. Review | read `items`: `delete`, `keep`, `review`, each with the rule that decided it | no |
| 4. Apply | `{"command":"cleanup","mode":"apply","plan":<plan>,"approved_hash":"<hash>"}` | yes: exactly the plan |
| 5. Repeat | `pending` items (CloudFront, CloudFormation, throttled calls) return a `retry_after_s`. Re-run apply later | yes |
| 6. Re-audit | step 1 again. Remaining `review` items become new rules, or stay | no |

Locally: `labagent --config lab.json run --job job.json --ruleset rules.json --output result.json`.
In CI: the same job through the private runner workflow (`docs/examples/runner-workflow.example.yml`); the orchestrator
supplies the ruleset in the job.

## Supported deleters

CloudFormation stacks, CloudFront, API Gateway (REST, HTTP), Lambda, EC2 instances, Elastic IPs, volumes, key pairs,
security groups, S3 (versions included), DynamoDB, CloudWatch log groups, SNS, SQS, Secrets Manager (7 day recovery),
ECR (force), IAM users, groups, roles and policies (detached from everything first).

Discovered but not deletable yet (reported as `review`, even if a rule matches): VPCs, NAT gateways, load balancers,
RDS, EKS/ECS, KMS aliases, Route 53.

## Order and dependencies

Applied in dependency order: stacks, CloudFront, APIs, Lambda, EC2, key pairs, security groups, S3, DynamoDB,
logs, messaging, secrets, ECR, then IAM users, groups, roles, policies. In-use resources return `pending` and a retry
hint; they never fail the run.
