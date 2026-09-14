# Auth hardening + admin UI — design

Phase 1b of the multi-user program. Phase 1a (cross-user isolation) shipped in
`fix(security): close cross-user isolation defects`.

## Why

The platform is going from one user to a handful of trusted people, each
connecting their own real broker account. Today's auth is: bcrypt passwords,
opaque DB session tokens, open registration, no email, no second factor, and a
logout that kills every session on every device. That is thin for accounts that
will eventually place real-money orders.

Posture chosen: **invite-only, mandatory TOTP, Resend for mail.**

Invite-only fits the actual user list (people the operator knows) and removes
a class of problems — no account enumeration surface worth defending, no
email-verification-as-spam-control, no open registration endpoint.

## Scope

Split into two shippable passes. TOTP changes the login path for every user, so
it gets its own focused pass rather than riding along with five other changes.

**1b-i** — email layer, invites, admin UI, session/device management, rate limiting
**1b-ii** — TOTP enrolment and enforcement, recovery codes, password reset

Excluded (say so if wanted): WebAuthn/passkeys, OAuth social login, per-user API
tokens, email-change flow.

## 1. Email layer

`app/services/email/` with an `EmailSender` protocol and two backends:

- `ResendSender` — HTTP POST to the Resend API. Config: `RESEND_API_KEY`,
  `EMAIL_FROM`.
- `ConsoleSender` — logs subject, recipient and any link. Default when no API
  key is set.

The console default matters: every flow below is testable end-to-end locally
before a sending domain is verified. Templates are plain text.

Sending is best-effort and must never block the request that triggered it —
a failed invite email leaves a valid invite row the admin can re-send, not a
failed API call.

## 2. Invite-only registration

`POST /auth/register` no longer creates accounts from nothing. It requires a
valid, unconsumed, unexpired invite token, and sets the password for the
invited email.

New table `invites`:

| column | notes |
|---|---|
| id | uuid pk |
| email | the invited address, lowercased |
| token_hash | sha256 of the token; the raw token only ever exists in the email |
| invited_by | fk users.id |
| created_at / expires_at | 7 day default |
| consumed_at | null until used; enforces single use |

Flow: admin creates invite → email with signed link to the web app → invitee
sets a password → account created.

Clicking the emailed link proves control of the address, so an invited account
is email-verified by construction. No separate verification flow.

## 3. Mandatory TOTP (1b-ii)

`pyotp`, standard authenticator apps. New columns on `users`: `totp_secret_enc`
(Fernet, reusing `encrypt_secret`), `totp_enabled_at`.

**Enforcement.** A user without TOTP enabled can reach only the enrolment
endpoints and logout; every other authenticated route returns 403
`totp_setup_required`. The frontend routes such users to a forced setup screen.

**Login becomes two-step.** Password verifies → server returns a short-lived
(5 min) challenge token, not a session → client submits the TOTP code with the
challenge → session issued. The challenge token is not a credential for
anything else.

**Recovery codes.** Ten single-use codes, bcrypt-hashed in a `recovery_codes`
table, displayed exactly once at enrolment. Mandatory 2FA without recovery
means a lost phone is a manual-SQL lockout, so this is not optional. An admin
can additionally force re-enrolment (`reset-2fa`), which clears the secret and
invalidates outstanding codes.

## 4. Password reset (1b-ii)

`POST /auth/password/forgot` always returns 202 regardless of whether the
address exists — no enumeration. Emailed single-use token, 30 minute expiry,
stored hashed. `POST /auth/password/reset` consumes it, sets the new password,
and revokes every session for that user (a reset is the response to a suspected
compromise; leaving old sessions alive defeats it).

## 5. Sessions as devices

`sessions` gains `user_agent`, `ip`, `last_seen_at`, `revoked_at`.

- `GET /auth/sessions` — list this user's sessions, current one flagged.
- `DELETE /auth/sessions/{id}` — revoke one (ownership-scoped, per Phase 1a).
- `POST /auth/logout` — revokes **only the current session**.

The last point is a bug fix: today logout issues
`DELETE FROM sessions WHERE user_id = ...`, so signing out on a phone signs you
out on your desktop.

## 6. Login rate limiting

Redis fixed-window counters, 5 failures per 15 minutes, keyed on both email and
client IP. Exceeding returns 429. A successful login clears the counter.

The same limiter guards password-reset requests and TOTP verification. TOTP
codes are six digits — rate limiting is the control that makes a second factor
meaningful rather than a formality.

## 7. Admin UI

A minimal slice pulled forward from Phase 6, because admin bootstrap is a
dependency of everything above: invites need an inviter, mandatory 2FA needs a
reset path, suspension needs an operator.

Backend, all behind the existing `CurrentAdmin` dependency:

| endpoint | purpose |
|---|---|
| `GET /admin/users` | list users with status, 2FA state, last seen |
| `POST /admin/invites` | create + send an invite |
| `GET /admin/invites` | outstanding invites |
| `DELETE /admin/invites/{id}` | revoke an unconsumed invite |
| `POST /admin/users/{id}/suspend` / `/unsuspend` | toggle `is_active`; suspend also revokes sessions |
| `POST /admin/users/{id}/reset-2fa` | clear TOTP, force re-enrolment |
| `PATCH /admin/users/{id}/admin` | grant/revoke operator |

Frontend: an `/admin` page (user table, invite form, per-user actions), hidden
from navigation for non-admins.

**Self-lockout guards.** An admin may not suspend themselves or remove their own
operator flag, and the last remaining admin may not be demoted or suspended by
anyone. Without these, one click makes the instance unadministrable — and the
global kill switch, which is operator-only as of Phase 1a, unreachable.

## Data changes

Migration `0006`: `invites`, `recovery_codes`, new `sessions` columns, new
`users` TOTP columns. Follows the existing `IF NOT EXISTS` convention (0001
bootstraps from model metadata, so fresh databases already have the tables).

## Testing

TDD throughout, matching the existing pure-unit style (fakeredis, no Postgres).
Cases that must exist:

- invite is single-use and expires; a consumed or expired token creates nothing
- registration without an invite is refused
- TOTP enforcement blocks a non-enrolled user from a normal route, and permits
  the enrolment routes
- a recovery code works exactly once
- rate limiter trips at the threshold, and clears on successful login
- logout revokes the current session only, leaving others alive
- password reset revokes all sessions and the token cannot be replayed
- self-suspension, self-demotion and last-admin-demotion are all refused

## Operational notes

Requires `RESEND_API_KEY` and a verified sending domain for `EMAIL_FROM`.
Neither blocks development: the console backend is the default and prints the
links that would have been emailed.

Migration 0005's lesson applies here too — check that an upgrade path leaves at
least one usable admin account. Mandatory TOTP on an existing instance means
existing users, including the operator, are routed to enrolment on next login.
