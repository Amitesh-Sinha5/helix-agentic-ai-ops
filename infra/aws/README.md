# Helix on AWS

Two routes. **Read the cost section before you pick one** — the difference is roughly $10/month versus $50/month.

| | Option A — one EC2 box | Option B — ECS Fargate |
|---|---|---|
| Cost | **~$0–12/mo** (free tier: ~$0) | **~$50–70/mo** |
| Setup | one script, ~10 min | `terraform apply` |
| Scales | no (vertical only) | yes, 1→4 tasks automatically |
| HA | no | multi-AZ load balancing |
| Best for | portfolio, demos, learning | showing production architecture |

Both run the same images. **Option A is what I'd actually run for a portfolio project**; Option B is the one worth talking through in an interview.

---

## Why not Lambda / App Runner

Helix needs a long-lived process, for three concrete reasons:

1. **WebSockets.** `/ws/agent-status/{id}` stays open for the life of a request, and `/ws/admin/escalations` stays open indefinitely.
2. **Chroma.** The vector index is a file on disk the process opens directly. It needs a real volume, not ephemeral function storage.
3. **LangGraph state.** One question spans 5–9 model calls inside a single request. A serverless timeout mid-graph loses the work.

App Runner also rules itself out: no persistent volumes.

---

## Option A — a single EC2 instance

The whole `docker compose` stack on one box: backend, frontend, Postgres and Redis as containers.

```bash
# t4g.small, Amazon Linux 2023, arm64 (cheapest that comfortably fits the stack)
# Security group: allow 22 (your IP only), 80 and 443 from anywhere.
# Paste ec2-user-data.sh as the instance's User data.
```

[`ec2-user-data.sh`](ec2-user-data.sh) installs Docker, clones the repo, and brings the stack up on boot.

**Cost:** `t4g.small` is ~$12/mo on-demand, or free for 12 months on a new account with `t2.micro`/`t3.micro` (tight — 1 GB RAM; use `t4g.small` if it struggles). Storage ~$2/mo.

**Trade-offs, honestly:** one instance means one availability zone, no automatic failover, and manual updates. Postgres runs in a container on the same disk, so **take backups yourself** — `docker compose exec postgres pg_dump` on a cron. Fine for a demo, not for customer data.

---

## Option B — ECS Fargate

What this Terraform builds:

```
CloudFront ──► S3 (frontend, private bucket via OAC)

     ALB ──► ECS Fargate service (1–4 tasks, autoscaled)
                    │
                    ├──► RDS Postgres      (private subnet)
                    ├──► ElastiCache Redis (private subnet)
                    └──► EFS               (shared Chroma index)
```

```bash
cd infra/aws
terraform init
terraform apply \
  -var="db_password=$(openssl rand -base64 24)" \
  -var="jwt_secret=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"

terraform output deploy_commands   # prints every push/migrate/sync step
```

### Decisions worth knowing

**No NAT gateway.** A NAT gateway costs ~$32/month plus data processing — more than everything else here combined. Tasks run in public subnets with public IPs instead, and their security group accepts inbound traffic *only* from the ALB. The data stores stay in private subnets with no internet route at all, which is the isolation that actually matters.

**ALB idle timeout is 3600s, not the 60s default.** The default silently kills the agent-status WebSocket and any request whose model calls run over a minute. Both are normal here.

**No sticky sessions.** Trace events fan out through Redis pub/sub rather than in-process memory, so any task can serve any client's WebSocket. That design choice is what makes horizontal scaling work at all.

**EFS for Chroma.** Every task must see the same index — this is the AWS equivalent of the `ReadWriteMany` PVC in the Kubernetes manifests. Mounted through an access point pinned to uid 10001, matching the non-root user in the backend image.

**Autoscaling on requests-per-target, not CPU.** Agent requests spend their time waiting on the model, so CPU stays low even when the service is saturated. CPU would be a poor signal.

**Migrations run as a one-off task** (`RUN_MIGRATIONS=false` in the task definition). Several replicas starting at once must not race to migrate the same database.

### Monthly cost, roughly

| Component | On-demand | Free tier (first 12 months) |
|---|---|---|
| ALB | ~$16 | not covered |
| Fargate 0.5 vCPU / 1 GB, 1 task | ~$18 | not covered |
| RDS `db.t4g.micro` | ~$12 | **$0** (750 h) |
| ElastiCache `cache.t4g.micro` | ~$12 | **$0** (750 h) |
| EFS, S3, CloudFront, ECR | ~$1–3 | mostly $0 |
| **Total** | **~$60–70/mo** | **~$35/mo** |

The ALB and Fargate are the floor and are not free-tier eligible. If that's more than you want to spend, use Option A.

Free-tier terms change — check the [AWS free tier page](https://aws.amazon.com/free/) rather than trusting this table.

### Teardown

```bash
terraform destroy
```

RDS keeps a final snapshot when `environment=production`; nothing is retained for `staging`. Check the console afterwards for anything left — CloudWatch log groups and ECR images survive some failure modes.

---

## After either option

1. **Sign up immediately.** The first account created becomes the administrator.
2. **Register the Stripe webhook** at `<api-url>/billing/webhook` and set `STRIPE_WEBHOOK_SECRET`. Without it the endpoint accepts unsigned payloads and anyone could upgrade themselves to Pro.
3. **Set `CORS_ORIGINS`** to exactly your frontend URL, and re-apply.
4. **Verify:** `python scripts/smoke_test.py https://<api-url>` — 10 checks across every pod.

`VITE_API_URL` is baked into the frontend bundle at build time. Pointing at a different backend means a rebuild, not a restart.
