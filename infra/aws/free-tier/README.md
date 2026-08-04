# Helix on AWS for $0

One `t3.micro`, nothing else. Everything used here is inside the 12-month free tier for a new AWS account.

```bash
cd infra/aws/free-tier
terraform init
terraform apply -var="alert_email=you@example.com"
terraform output next_steps
```

---

## What makes it free

| Resource | Free-tier allowance | What we use |
|---|---|---|
| EC2 `t3.micro` | 750 h/month | 1 instance, 24/7 = 730 h |
| EBS gp3 | 30 GB | 30 GB |
| Elastic IP | free while attached | 1 |
| Data transfer out | 100 GB/month | far less |
| AWS Budgets | first 2 free | 1 spend alarm |

**Deliberately avoided, because none of these are free:** ALB (~$16/mo), Fargate, NAT gateway (~$32/mo), and RDS/ElastiCache beyond their free hours. That is why this is a plain EC2 instance and not the Fargate stack in [`../`](../).

## How it fits in 1 GB

Postgres and Redis are not deployed as containers. Instead the backend uses two paths it already supports:

- **SQLite** instead of Postgres — the same Alembic migrations run against it.
- **The in-process cache** instead of Redis. On a *single* instance this is not a downgrade: the semantic cache, rate-limit windows, session memory and trace pub/sub only need to be shared *between replicas*, and there is exactly one replica.

Measured with `docker stats` on the actual stack:

| Container | Memory |
|---|---|
| backend | **186 MB** |
| frontend | **4.3 MB** |
| **total** | **~191 MB** |

So 1 GB is comfortable at runtime. **The swap file is for the build**, not steady state — compiling the Python dependencies and training the classifier during `docker build` is what would otherwise get OOM-killed on a 1 GB box. `user-data.sh` creates 4 GB of swap before it builds anything.

Verified locally with `docker-compose.free.yml`: **10/10 smoke checks pass** on SQLite + in-process cache, all three agent pods working.

## When to stop using this

Move to the [Fargate stack](../) or the full `docker-compose.yml` the moment any of these is true:

- **You need a second instance.** SQLite does not survive concurrent writers, and two in-process caches will disagree with each other — rate limits become per-instance and the semantic cache stops sharing.
- **You have real users.** One instance is one availability zone, with no failover and no managed backups.
- **You need the data to survive.** `terraform destroy` deletes the volume. Take your own backups.

## After `terraform apply`

The instance builds both images on first boot — give it **5–10 minutes**.

```bash
# Watch it come up
curl -fsS http://<public-ip>:8000/health

# Or watch the bootstrap log
ssh ec2-user@<public-ip> 'tail -f /var/log/helix-boot.log'

# Verify everything
python scripts/smoke_test.py http://<public-ip>:8000
```

Then open `http://<public-ip>:5173` and **sign up immediately — the first account created becomes the administrator.**

## Things that will cost you money if you ignore them

**The free tier lasts 12 months.** After that this instance is about **$7.50/month**. The Terraform creates a budget alarm that emails you at $1 of actual *or* forecast spend, which fires long before that becomes a surprise. Set `alert_email` to an address you read.

**An Elastic IP costs money when it is *not* attached to a running instance.** If you stop the instance but keep the EIP, you get billed for it. Either keep the instance running or `terraform destroy` the lot.

**Free-tier terms change.** Verify against the [AWS free tier page](https://aws.amazon.com/free/) rather than trusting this file. Also set a zero-spend budget in the AWS console as a second line of defence.

**SSH is closed by default.** Pass `-var="ssh_cidr=YOUR.IP.HERE/32"` to open it, or use EC2 Instance Connect from the console. Do not open port 22 to `0.0.0.0/0`.

**There is no HTTPS.** Traffic is plain HTTP on ports 5173 and 8000, so JWTs travel in clear text. Fine for a demo you control; not fine for anything real. Adding TLS free means putting Caddy in front with a domain name, or Cloudflare's free proxy.

## Honest alternatives

If the goal is simply "free hosting that works", AWS is not the strongest option for this workload:

- **Oracle Cloud Always Free** — 4 ARM cores and 24 GB RAM, free indefinitely rather than for 12 months. Vastly more headroom than a 1 GB `t3.micro`, and the same `docker-compose.yml` runs there unchanged (Postgres and Redis included).
- **GitHub Student Developer Pack** — you have a PES email; it bundles AWS credits, DigitalOcean credit and more. Worth claiming before spending anything.

The AWS path here is still worth having: it's the one recruiters recognise, and the Terraform demonstrates the same skills either way.

## Teardown

```bash
terraform destroy
```

Removes the instance, volume, Elastic IP, security group and budget. **The data goes with it.**
