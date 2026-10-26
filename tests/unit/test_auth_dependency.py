"""
EAOC Unit Test Suite - JWT Verification & RBAC Dependency (Part 2.16-P)

Tests all security requirements for Part 2.16-P:
1. Valid signed test token.
2. Invalid signature.
3. Expired token.
4. Invalid issuer.
5. Invalid audience.
6. Missing subject, tenant ID, roles, or expiration.
7. alg: none and algorithm substitution.
8. Missing or malformed bearer header.
9. Missing signing secret or required configuration.
10. Authorized and unauthorized role checks.
11. Spoofed identity headers do not change the authenticated principal.
12. Safe, non-sensitive error responses.
"""

import base64
import json
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import jwt
from fastapi import Depends, FastAPI, status
from fastapi.testclient import TestClient

from eaoc.api.dependencies.auth import get_trusted_identity, require_roles
from eaoc.config import Settings, get_settings
from eaoc.schemas.auth import AuthenticatedUser

TEST_SECRET = "super-secret-test-signing-key-for-eaoc-tests-32bytes-min"
TEST_ISSUER = "eaoc-test-issuer"
TEST_AUDIENCE = "eaoc-test-audience"


def make_test_token(
    secret_key: str = TEST_SECRET,
    algorithm: str = "HS256",
    sub: Optional[str] = "usr-test-123",
    tenant_id: Optional[str] = "tenant-alpha",
    roles: Optional[List[str]] = None,
    exp_offset_seconds: int = 3600,
    iss: Optional[str] = TEST_ISSUER,
    aud: Optional[str] = TEST_AUDIENCE,
    omit_claims: Optional[List[str]] = None,
    extra_claims: Optional[Dict[str, Any]] = None,
) -> str:
    """Helper to generate signed test JWT tokens for unit testing."""
    now = datetime.now(timezone.utc)
    payload: Dict[str, Any] = {
        "sub": sub,
        "tenant_id": tenant_id,
        "roles": ["approver", "finance_user"] if roles is None else roles,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=exp_offset_seconds)).timestamp()),
    }
    if iss is not None:
        payload["iss"] = iss
    if aud is not None:
        payload["aud"] = aud
    if extra_claims:
        payload.update(extra_claims)
    if omit_claims:
        for claim in omit_claims:
            payload.pop(claim, None)

    return jwt.encode(payload, secret_key, algorithm=algorithm)


class TestAuthDependency(unittest.TestCase):
    """Test suite for Part 2.16-P authentication and RBAC dependency."""

    def setUp(self):
        # Configure test settings
        self.test_settings = Settings(
            ENVIRONMENT="testing",
            JWT_SECRET_KEY=TEST_SECRET,
            JWT_ALGORITHM="HS256",
            JWT_ISSUER=TEST_ISSUER,
            JWT_AUDIENCE=TEST_AUDIENCE,
        )

        # Create isolated test app with protected routes
        self.app = FastAPI(title="Test Auth App")

        @self.app.get("/protected")
        async def protected_route(
            user: AuthenticatedUser = Depends(get_trusted_identity),
        ):
            return {
                "user_id": user.user_id,
                "tenant_id": user.tenant_id,
                "roles": user.roles,
            }

        @self.app.get("/rbac-approver")
        async def rbac_approver_route(
            user: AuthenticatedUser = Depends(require_roles("approver", "admin")),
        ):
            return {"status": "authorized", "user_id": user.user_id}

        @self.app.get("/rbac-admin-only")
        async def rbac_admin_route(
            user: AuthenticatedUser = Depends(require_roles("admin")),
        ):
            return {"status": "authorized", "user_id": user.user_id}

        self.app.dependency_overrides[get_settings] = lambda: self.test_settings
        self.client = TestClient(self.app)

    def tearDown(self):
        self.app.dependency_overrides.clear()

    def test_1_valid_signed_test_token(self):
        """1. Verify valid signed test token authenticates successfully."""
        token = make_test_token(sub="usr-valid", tenant_id="tenant-1", roles=["approver"])
        response = self.client.get(
            "/protected",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["user_id"], "usr-valid")
        self.assertEqual(data["tenant_id"], "tenant-1")
        self.assertEqual(data["roles"], ["approver"])

    def test_2_invalid_signature(self):
        """2. Verify token signed with wrong secret is rejected with HTTP 401."""
        tampered_token = make_test_token(secret_key="completely-wrong-secret-key-32bytes")
        response = self.client.get(
            "/protected",
            headers={"Authorization": f"Bearer {tampered_token}"},
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn("signature", response.json()["detail"].lower())

    def test_3_expired_token(self):
        """3. Verify expired token is rejected with HTTP 401."""
        expired_token = make_test_token(exp_offset_seconds=-3600)
        response = self.client.get(
            "/protected",
            headers={"Authorization": f"Bearer {expired_token}"},
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn("expired", response.json()["detail"].lower())

    def test_4_invalid_issuer(self):
        """4. Verify token with untrusted/mismatched issuer is rejected with HTTP 401."""
        invalid_iss_token = make_test_token(iss="untrusted-external-idp")
        response = self.client.get(
            "/protected",
            headers={"Authorization": f"Bearer {invalid_iss_token}"},
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn("issuer", response.json()["detail"].lower())

    def test_5_invalid_audience(self):
        """5. Verify token with mismatched audience is rejected with HTTP 401."""
        invalid_aud_token = make_test_token(aud="foreign-api-service")
        response = self.client.get(
            "/protected",
            headers={"Authorization": f"Bearer {invalid_aud_token}"},
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn("audience", response.json()["detail"].lower())

    def test_6_missing_subject_tenant_roles_or_expiration(self):
        """6. Verify tokens missing sub, tenant_id, roles, or exp are rejected with HTTP 401."""
        # a. Missing sub
        t_no_sub = make_test_token(omit_claims=["sub"])
        r = self.client.get("/protected", headers={"Authorization": f"Bearer {t_no_sub}"})
        self.assertEqual(r.status_code, status.HTTP_401_UNAUTHORIZED)

        # b. Empty sub string
        t_empty_sub = make_test_token(sub="   ")
        r = self.client.get("/protected", headers={"Authorization": f"Bearer {t_empty_sub}"})
        self.assertEqual(r.status_code, status.HTTP_401_UNAUTHORIZED)

        # c. Missing tenant_id
        t_no_tenant = make_test_token(omit_claims=["tenant_id"])
        r = self.client.get("/protected", headers={"Authorization": f"Bearer {t_no_tenant}"})
        self.assertEqual(r.status_code, status.HTTP_401_UNAUTHORIZED)

        # d. Empty tenant_id string
        t_empty_tenant = make_test_token(tenant_id="  ")
        r = self.client.get("/protected", headers={"Authorization": f"Bearer {t_empty_tenant}"})
        self.assertEqual(r.status_code, status.HTTP_401_UNAUTHORIZED)

        # e. Missing roles
        t_no_roles = make_test_token(omit_claims=["roles"])
        r = self.client.get("/protected", headers={"Authorization": f"Bearer {t_no_roles}"})
        self.assertEqual(r.status_code, status.HTTP_401_UNAUTHORIZED)

        # f. Invalid roles type (string instead of list)
        t_str_roles = make_test_token(extra_claims={"roles": "approver"})
        r = self.client.get("/protected", headers={"Authorization": f"Bearer {t_str_roles}"})
        self.assertEqual(r.status_code, status.HTTP_401_UNAUTHORIZED)

        # g. Missing exp
        t_no_exp = make_test_token(omit_claims=["exp"])
        r = self.client.get("/protected", headers={"Authorization": f"Bearer {t_no_exp}"})
        self.assertEqual(r.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_7_alg_none_and_algorithm_substitution(self):
        """7. Verify alg: none and algorithm substitutions are rejected with HTTP 401."""
        now = datetime.now(timezone.utc)
        payload = {
            "sub": "attacker",
            "tenant_id": "tenant-evil",
            "roles": ["admin"],
            "iss": TEST_ISSUER,
            "aud": TEST_AUDIENCE,
            "exp": int((now + timedelta(hours=1)).timestamp()),
        }

        # 7a. Unsigned token with alg: none
        h_none = base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode()).decode().rstrip("=")
        b_none = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        token_none = f"{h_none}.{b_none}."

        r_none = self.client.get("/protected", headers={"Authorization": f"Bearer {token_none}"})
        self.assertEqual(r_none.status_code, status.HTTP_401_UNAUTHORIZED)

        # 7b. Algorithm substitution: alg: RS256 when server expects HS256
        h_rs256 = base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()).decode().rstrip("=")
        token_rs256 = f"{h_rs256}.{b_none}.fake-rs256-signature"

        r_rs256 = self.client.get("/protected", headers={"Authorization": f"Bearer {token_rs256}"})
        self.assertEqual(r_rs256.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn("algorithm", r_rs256.json()["detail"].lower())

    def test_8_missing_or_malformed_bearer_header(self):
        """8. Verify missing or malformed Authorization header is rejected with HTTP 401."""
        # a. Missing header entirely
        r1 = self.client.get("/protected")
        self.assertEqual(r1.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn("missing", r1.json()["detail"].lower())

        # b. Basic auth scheme instead of Bearer
        r2 = self.client.get("/protected", headers={"Authorization": "Basic dXNlcjpwYXNz"})
        self.assertEqual(r2.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn("scheme", r2.json()["detail"].lower())

        # c. Bearer without token
        r3 = self.client.get("/protected", headers={"Authorization": "Bearer"})
        self.assertEqual(r3.status_code, status.HTTP_401_UNAUTHORIZED)

        # d. Bearer with whitespace only
        r4 = self.client.get("/protected", headers={"Authorization": "Bearer    "})
        self.assertEqual(r4.status_code, status.HTTP_401_UNAUTHORIZED)

        # e. Multiple token parts
        r5 = self.client.get("/protected", headers={"Authorization": "Bearer tok1 tok2"})
        self.assertEqual(r5.status_code, status.HTTP_401_UNAUTHORIZED)

        # f. Non-JWT garbage token
        r6 = self.client.get("/protected", headers={"Authorization": "Bearer not-a-jwt-token"})
        self.assertEqual(r6.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_9_missing_signing_secret_or_required_config(self):
        """9. Verify missing secret or unsupported algorithm fails closed with HTTP 500."""
        # a. Missing JWT_SECRET_KEY
        unconfigured_settings = Settings(
            ENVIRONMENT="testing",
            JWT_SECRET_KEY=None,
            JWT_ALGORITHM="HS256",
        )
        self.app.dependency_overrides[get_settings] = lambda: unconfigured_settings
        token = make_test_token()
        r = self.client.get("/protected", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertIn("improperly configured", r.json()["detail"])
        # Ensure secret is not in the error
        self.assertNotIn(TEST_SECRET, r.text)

        # b. Unsupported JWT_ALGORITHM
        bad_alg_settings = Settings(
            ENVIRONMENT="testing",
            JWT_SECRET_KEY=TEST_SECRET,
            JWT_ALGORITHM="UNSUPPORTED_BLOWFISH",
        )
        self.app.dependency_overrides[get_settings] = lambda: bad_alg_settings
        r_alg = self.client.get("/protected", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r_alg.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertIn("improperly configured", r_alg.json()["detail"])
        self.assertNotIn(TEST_SECRET, r_alg.text)

    def test_10_authorized_and_unauthorized_role_checks(self):
        """10. Verify RBAC permits authorized roles (200) and denies unauthorized roles (403)."""
        # a. User with 'approver' role on /rbac-approver -> 200 OK
        t_approver = make_test_token(roles=["approver"])
        r1 = self.client.get("/rbac-approver", headers={"Authorization": f"Bearer {t_approver}"})
        self.assertEqual(r1.status_code, status.HTTP_200_OK)
        self.assertEqual(r1.json()["status"], "authorized")

        # b. User with 'viewer' role on /rbac-approver -> 403 Forbidden
        t_viewer = make_test_token(roles=["viewer"])
        r2 = self.client.get("/rbac-approver", headers={"Authorization": f"Bearer {t_viewer}"})
        self.assertEqual(r2.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("lacks required role", r2.json()["detail"])

        # c. User with 'approver' role on /rbac-admin-only -> 403 Forbidden
        r3 = self.client.get("/rbac-admin-only", headers={"Authorization": f"Bearer {t_approver}"})
        self.assertEqual(r3.status_code, status.HTTP_403_FORBIDDEN)

        # d. User with 'admin' role on /rbac-admin-only -> 200 OK
        t_admin = make_test_token(roles=["admin"])
        r4 = self.client.get("/rbac-admin-only", headers={"Authorization": f"Bearer {t_admin}"})
        self.assertEqual(r4.status_code, status.HTTP_200_OK)

    def test_11_spoofed_identity_headers_ignored(self):
        """11. Verify spoofed headers (X-User-Roles, X-User-ID, X-Tenant-ID) are ignored."""
        # Principal has 'viewer' role only in verified JWT
        token = make_test_token(sub="legit-user-42", tenant_id="legit-tenant", roles=["viewer"])

        # Attacker injects spoofing headers attempting privilege escalation
        spoofed_headers = {
            "Authorization": f"Bearer {token}",
            "X-User-Roles": "admin,approver,root",
            "X-User-ID": "admin-super-id",
            "X-Tenant-ID": "master-root-tenant",
        }

        # Verify identity matches token claims exactly, ignoring headers
        r_protected = self.client.get("/protected", headers=spoofed_headers)
        self.assertEqual(r_protected.status_code, status.HTTP_200_OK)
        data = r_protected.json()
        self.assertEqual(data["user_id"], "legit-user-42")
        self.assertEqual(data["tenant_id"], "legit-tenant")
        self.assertEqual(data["roles"], ["viewer"])

        # Verify RBAC denies access despite spoofed X-User-Roles
        r_rbac = self.client.get("/rbac-approver", headers=spoofed_headers)
        self.assertEqual(r_rbac.status_code, status.HTTP_403_FORBIDDEN)

    def test_12_safe_non_sensitive_error_responses(self):
        """12. Verify error messages are sanitized and never expose signing keys or raw secrets."""
        token = make_test_token()
        headers = {"Authorization": f"Bearer {token}"}

        # Test error paths
        for path in ["/protected", "/rbac-approver"]:
            resp = self.client.get(path, headers={"Authorization": "Bearer bad-token"})
            self.assertNotIn(TEST_SECRET, resp.text)
            self.assertNotIn("Traceback", resp.text)

            resp_empty = self.client.get(path)
            self.assertNotIn(TEST_SECRET, resp_empty.text)


if __name__ == "__main__":
    unittest.main()
