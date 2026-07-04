# Security Policy

We take the security of the TokenLean — Token Optimisation Framework seriously. This proxy
sits in the request path of LLM traffic and handles API keys, so we appreciate
responsible disclosure of any vulnerability.

## Supported versions

The project is pre-1.0. Security fixes are applied to the latest `main` and the
most recent tagged release.

| Version | Supported |
|---------|-----------|
| `main` (latest) | ✅ |
| Older tags | ⚠️ best-effort |

## Reporting a vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Instead, report privately via one of:

1. **GitHub Security Advisories** (preferred) — use the repository's
   **"Report a vulnerability"** button under the *Security* tab. This opens a
   private advisory visible only to maintainers.
2. **Email** — send details to the maintainer security contact
   (`sumit.gupta@cbeyond.com`).

Please include:

- A description of the issue and its impact.
- Steps to reproduce (proof-of-concept if possible).
- Affected version/commit and configuration.
- Any suggested remediation.

## What to expect

- **Acknowledgement** within 3 business days.
- An initial assessment and severity rating within 7 business days.
- Coordinated disclosure: we will agree a timeline with you and credit you in the
  advisory unless you prefer to remain anonymous.

## Scope & hardening notes

When self-hosting, please remember:

- **Never commit secrets.** Provider keys, proxy keys, and Terraform state are
  gitignored by default — keep them out of the repo and out of images.
- **Proxy API keys** are validated by SHA-256 hash; rotate them if exposed.
- **Developers never receive upstream LLM provider keys** — that is the point of
  the proxy. Keep provider keys in your secret manager only.
- Restrict CORS (`CORS_ORIGINS`) and place admin endpoints behind your own auth
  in production.

Thank you for helping keep the project and its users safe.
