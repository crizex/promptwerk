# Persona catalog

Raw material for the planner, **not a mandatory list**. It picks, rewrites and invents new ones
when the topic calls for it. A persona belongs in a plan only if it contributes a viewpoint no
other covers. Two personas that find the same defects are a mistake, not proof of rigor.

Rule of thumb for the count:

| Topic | Personas |
|---|---|
| Single bug fix, small change | 0 |
| Feature with domain logic | 0 to 2 |
| Redesign of a surface | 3 to 4 |
| Full audit of a surface | 5 to 6 |
| More than 6 | almost always over-splitting, merge |

## Security and data

- **Tenant isolation architect**: tenant separation, row-level security, IDOR via manipulated
  path parameters, direct database calls that bypass the guard.
- **Access control reviewer**: role model, privilege escalation, write guards before every
  write, soft-delete gaps.
- **Application security engineer**: rate limiting, CSRF, enumeration, server-side validation,
  error and hostname leaks, security headers, SSRF on outbound targets.
- **Platform security reviewer**: CSP, HSTS, cookie flags, TLS, DNS, subdomain takeover, mail
  authentication, `security.txt`.

## Domain

- **Compliance and contract auditor**: SLA promises against terms and data processing
  agreements, breach notification paths, audit evidence for customers.
- **Legal and compliance auditor**: legal notice, privacy policy against the services actually
  loaded, consent before cookies, risky marketing claims, price display.

## Interface

- **Senior UI developer and design system auditor**: tokens instead of hardcoded values,
  component consistency, spacing and type scale, responsive at 360/768/1280/1920, touch targets.
- **Accessibility and performance auditor**: WCAG 2.2 AA, heading hierarchy, landmarks,
  keyboard and focus, contrast values, `prefers-reduced-motion`, LCP, font loading.
- **Product UX specialist**: states and races under concurrent editing, empty states, error
  states, form logic.
- **Conversion strategist**: value proposition in five seconds, CTA hierarchy, trust signals,
  objection handling on price, migration, privacy and exit.

## Language

- **Copy editor**: terminology, inconsistent spelling, tone of address, number and date formats.
- **Generic-AI-voice detector**: typical model phrasing with location, reason and a plain rewrite.
- **UX writer**: clarity for non-technical readers, error messages, microcopy.

## Structure and operations

- **Technical writer and information architect**: structure, entry path, scannability,
  naming missing topics.
- **Consistency auditor**: documentation against actual code, evidence from both sides,
  undocumented features as a gap inventory.
- **SRE and incident communications lead**: component breakdown, severity definitions,
  incident lifecycle, maintenance notices.
- **Program manager**: consolidation across several runs, deduplication, patterns, effort
  estimates, implementation waves.
