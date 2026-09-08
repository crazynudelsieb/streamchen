# Security Policy

## Supported Versions

Only the latest code on `main` is supported for security fixes.

## Reporting a Vulnerability

Do not report vulnerabilities in public issues.

Use GitHub Security Advisories for this repository, or contact the maintainer
directly at `appchen@outlook.at`.

Please include:

- A clear description of the issue
- Steps to reproduce
- Impact and affected components
- Suggested mitigation if available

Target response times:

- Initial acknowledgment: within 48 hours
- Status update: within 7 days
- Critical fix target: within 30 days

## Security Model (streamchen)

The app is intentionally account-free. Security is based on:

- Random room tokens for room access
- Random host bearer secrets, stored server-side as Argon2 hashes
- Anonymous session cookies and CSRF double-submit tokens
- Per-room moderation controls and server-side rate limits

## Built-in Protections

- CSRF enforcement for non-safe API methods
- HTTPOnly session cookies with SameSite=Lax
- Optional `Secure` cookies (`SECURE_COOKIES=true` in production)
- Strict cache behavior (`no-store` on mutable views)
- Input validation with Pydantic and constrained route guards
- SQLAlchemy ORM usage (no string-concatenated SQL paths)
- Automatic shadow-ban escalation for repeated abuse
- Non-root containers for API and worker images

## Operational Security Checklist

Before publishing or deploying:

- Set strong values for `POSTGRES_PASSWORD`, `ICECAST_SOURCE_PASSWORD`, and
  `ICECAST_ADMIN_PASSWORD`
- Set `SECURE_COOKIES=true` behind HTTPS
- Keep `TRUSTED_PROXY_COUNT` aligned with your proxy chain
- Keep `.env` private (commit `.env.example` only)
- Keep base images and Python dependencies up to date. CI audits the Python
  dependencies on every push (`pip-audit`) and flags newly introduced
  advisories on pull requests (`dependency-review`), but both report rather
  than gate: a CVE is published on somebody else's schedule, and a red build on
  an unchanged tree would block the fix for whatever *is* broken from shipping.
  A green run therefore does not mean no known CVEs — read the two scan jobs.
  Nothing in CI scans the built images, so base-image CVEs are yours to track.
- Restrict infrastructure access to Postgres/Redis/Icecast private networks

## Known Tradeoffs

- No user accounts by design: anyone with a room link can join that room
- Host control is possession-based: anyone with a host secret can moderate
- Redis state is intentionally disposable; do not use it for durable audit data

## Scope Notes

This policy applies to the current FastAPI + worker + Icecast architecture.
Legacy references to unrelated stacks (for example Flask-specific guidance,
SMTP-specific controls, or `SECRET_KEY` requirements) are intentionally out of
scope here.
