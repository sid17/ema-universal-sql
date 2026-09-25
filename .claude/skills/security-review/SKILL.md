---
name: security-review
description: "Use when reviewing code for security vulnerabilities. Trigger when the user says 'security review', 'check for vulnerabilities', 'is this secure', 'audit security', or before any production deployment, even if they don't explicitly mention security."
argument-hint: "[backend|mobile|all]"
allowed-tools: Read Grep Glob
---

# Security Review

Check the codebase for common security issues.

## Step 1: Determine scope

- `backend` — review backend routes, models, database queries
- `mobile` — review network calls, storage, deep links
- `all` or no arguments — review both

## Step 2: Backend review

Read `docs/architecture/backend.md` then check:

### Injection
- [ ] MongoDB queries use parameterized inputs (not string concatenation)
- [ ] No raw user input in `$regex` patterns without escaping
- [ ] File paths from user input are sanitized

### Authentication & Authorization
- [ ] Sensitive endpoints require auth (if applicable)
- [ ] No hardcoded API keys or secrets in source code
- [ ] `.env` files are in `.gitignore`

### Input validation
- [ ] All endpoints use Pydantic models (not raw `dict = Body(...)`)
- [ ] File uploads validate type and size
- [ ] Query parameters have bounds (`ge=1`, `le=100`)

### Error handling
- [ ] No stack traces leaked to clients
- [ ] Sensitive data not in error messages
- [ ] Failed operations don't leave partial state

### Rate limiting
- [ ] Endpoints that create resources have rate limits
- [ ] No unbounded queries (pagination or limit with max cap)

### CORS
- [ ] `allow_origins=["*"]` is only for development
- [ ] Production should restrict origins

## Step 3: Mobile review

Read `docs/architecture/mobile.md` then check:

### Network
- [ ] API URLs use HTTPS in production
- [ ] No secrets in JavaScript bundle
- [ ] API responses validated before use

### Storage
- [ ] No sensitive data in AsyncStorage/MMKV without encryption
- [ ] SQLite database not storing credentials

### Deep links
- [ ] Share extension URL parsing validates input
- [ ] No code injection via deep link parameters

### Dependencies
- [ ] No known vulnerable packages (`yarn audit`)
- [ ] Native dependencies are from trusted sources

## Step 4: Report

Create a findings table:

| Severity | File | Line | Issue | Recommendation |
|----------|------|------|-------|----------------|
| HIGH | backend/routes/instagram.py | 45 | Raw regex from user input | Escape special chars |
| MEDIUM | mobile/src/config.ts | 7 | Hardcoded default URL | Use env variable |
| LOW | backend/app.py | 5 | CORS allow all origins | Restrict in production |

Categorize: HIGH (fix now), MEDIUM (fix before ship), LOW (improve later).
