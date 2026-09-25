---
name: deploy
description: "Use when ready to deploy to production. Trigger when the user says 'deploy', 'push to prod', 'ship it', 'go live', or asks about deployment status/rollback. User-invoked only, never auto-triggered."
when_to_use: "After code review passes and tests are green. Final step of /dev workflow."
domain: dev
category: transformation
inputs: "Passing tests + reviewed code + .deploy.yml (optional)"
outputs: "Production deployment with health verification"
handoff: "none"
disable-model-invocation: true
argument-hint: "[setup|deploy|rollback|status]"
---

# Deploy

Config-driven deployment with platform auto-detection, health checks, and automatic rollback.

## Context

Deployment is user-invoked only — the agent never auto-deploys. The skill auto-detects the deployment platform from project files, with optional `.deploy.yml` override. Every deploy includes a dry-run preview, health check verification, and automatic rollback on failure.

## Instructions

### Step 1: Auto-Detect Platform

Scan project files to determine deployment method:

| File Found | Platform | Method |
|-----------|----------|--------|
| `vercel.json` or `.vercel/` | Vercel | `vercel --prod` |
| `fly.toml` | Fly.io | `fly deploy` |
| `railway.json` | Railway | `railway up` |
| `Dockerfile` + no platform config | Docker VPS | `docker compose up -d --build` |
| `package.json` (no Docker) | Node bare | `npm start` (via PM2/systemd) |
| `pyproject.toml` (no Docker) | Python bare | `gunicorn` / `uvicorn` |

If `.deploy.yml` exists, it overrides auto-detection:
```yaml
# .deploy.yml
platform: vercel | fly | railway | docker-vps | bare
host: <server>  # for VPS deployments
health_check: /api/health
deploy_path: /var/www/app
domain: app.example.com
rollback_releases: 3
```

### Step 2: Pre-Flight Checks

Before any deployment:
1. **Tests pass** — verify test suite is green
2. **Working tree clean** — `git status` must be clean
3. **Pushed to remote** — no unpushed commits
4. **Health check endpoint exists** — verify the app has one
5. **Env vars configured** — check platform for required vars

Report pre-flight status. If any check fails, STOP and report.

### Step 3: Dry-Run Preview

Show what will happen before anything irreversible:
- Platform detected: {platform}
- Deploy method: {command}
- Target: {domain/URL}
- Health check: {endpoint}
- Changes: {commit range being deployed}

**Ask for confirmation before proceeding.**

### Step 4: Deploy

Execute platform-specific deployment:

**Platform-first (preferred):**
- Vercel: `vercel --prod` (branch push = preview, merge = production)
- Fly.io: `fly deploy --ha=false`
- Railway: `railway up`

**VPS/Docker:**
1. SSH to host
2. Git pull latest
3. `docker compose up -d --build`
4. Run migrations if needed

### Step 5: Health Check Verification

After deploy completes:
1. Wait 10s for startup
2. Hit health check endpoint
3. Verify HTTP 200 response
4. Check response body for expected fields
5. Verify key pages load (if web app)

### Step 6: Rollback (on failure)

If health check fails:
- **Platform-first:** use platform rollback (Vercel instant rollback, Fly rollback)
- **VPS/Docker:** `docker compose up -d` with previous image tag
- Report failure with logs

### Actions

- `setup` — first-time configuration:
  1. Detect platform (scan project files using the table in Step 1)
  2. Verify CLI tools installed (`vercel`, `fly`, `railway`, `docker` — whichever applies)
  3. Create `.deploy.yml` with platform, health_check, and domain fields
  4. Verify health check endpoint exists in the codebase
  5. Check environment variables are configured on the platform
- `deploy` — full deployment (default)
- `rollback` — rollback to previous release
- `status` — check current deployment status and health

## Gates

- [ ] Pre-flight checks all pass
- [ ] User confirmed after dry-run preview
- [ ] Health check passes after deploy

## Anti-Patterns

- Always verify tests pass before deploying — deploying untested code risks production incidents that are harder to diagnose than pre-deploy test failures.
  - BAD: "Tests are probably fine, deploying now."
  - GOOD: Run `npm test`, verify 0 failures, then deploy.
- Push all commits before deploying — unpushed commits mean the deployed code doesn't match what's in version control, making rollbacks unreliable.
- Always show the dry-run preview — the preview is the user's last chance to catch mistakes before an irreversible production change.
- Always verify health checks after deploying — a "successful" deploy that serves errors is worse than a failed deploy because it affects real users silently.
- Only deploy when the user explicitly triggers it — auto-deployment removes human judgment from the highest-risk step in the workflow.

## Output Format

**Report:** Pre-flight status + deploy result + health check verification (or rollback report on failure)
