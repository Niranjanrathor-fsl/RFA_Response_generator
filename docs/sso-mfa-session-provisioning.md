# Microsoft SSO hardening: MFA, session controls, managed provisioning

Applies to `AUTH_MODE=microsoft`. Code: `app/auth.py`, `app/routes/auth_routes.py`,
`app/config.py`, `app/main.py`. Tests: `tests/test_auth_microsoft.py`.

## 1. MFA enforcement

- `microsoft_user()` rejects a sign-in unless the verified ID token's `amr` claim
  contains `mfa` (`MS_REQUIRE_MFA=true`, the default). The rejection happens before
  any Graph call and returns 403 with an MFA message; no session is created.
- Set `MS_REQUIRE_MFA=false` only if MFA is not yet enabled in the tenant.
- Defence in depth: also create a Conditional Access policy requiring MFA for this
  app registration, so users are prompted rather than just rejected.

## 2. Session controls

| Control | Behaviour | Setting |
|---|---|---|
| Absolute lifetime | Session ends after this time regardless of activity (60-86400 s) | `SESSION_MAX_AGE_SECONDS` (default 28800) |
| Idle timeout | Session ends after inactivity; each request refreshes it | `SESSION_IDLE_TIMEOUT_SECONDS` (default 1800, must be 60..max age) |
| Server-side registry | Each login gets a random `sid` stored in server memory; the cookie is valid only while its `sid` is registered | - |
| Revocation | Logout removes the `sid`, so a copied cookie stops working. `auth.revoke_user_sessions(object_id)` ends all sessions of one user (e.g. a leaver) | - |
| Cookie flags | Signed, HTTP-only, SameSite=lax; `Secure` is required for HTTPS deployments (startup fails if `SESSION_HTTPS_ONLY` does not match `PUBLIC_BASE_URL`) | `SESSION_HTTPS_ONLY` |

Limitation: the registry is in-process. A restart signs everyone out, and multiple
instances/workers would each hold their own registry. Run a single instance, or move
`_sessions` to a shared store (e.g. Redis) before scaling out.

## 3. Managed user provisioning

Instead of editing `access/d_and_above_dso.json`, access can be managed in Entra:

- `MS_ALLOWED_GROUP_IDS` - comma-separated group object IDs (validated as GUIDs).
  Requires the app registration's token configuration to emit the `groups` claim
  (Token configuration -> Add groups claim -> Security groups, "Groups assigned to
  the application").
- `MS_REQUIRED_APP_ROLES` - comma-separated app role values (emitted in `roles`).
  Preferred: no group-overage risk.

If either is set, a user is admitted when they hold one of the groups or roles; the
JSON allowlist is neither read nor required. Tenant, MFA, Graph `userType=Member`
and `oid` binding checks still apply. If neither is set, the legacy allowlist file
behaves exactly as before.

Group overage (token carries `_claim_names` instead of `groups`) is denied and
logged; use app roles or restrict group assignment.

Joiner/leaver flow: add/remove users in the Entra group or role assignment (set
"Assignment required" on the enterprise application as well). Removal takes effect
at the next sign-in; to cut off a live session immediately, call
`revoke_user_sessions` or lower `SESSION_MAX_AGE_SECONDS`/idle timeout. Automatic
SCIM push provisioning is not implemented, as the app has no user database.

## Tests added

MFA missing/disabled, idle timeout and activity extension, logout revoking a stolen
cookie, admin revoke, group and app-role admission, denial for unassigned/overage,
and config validation.
