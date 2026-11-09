import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import auth
from estate import routes as estate_routes
from starlette.requests import Request


class AuthRouteTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(auth.router)
        self.client = TestClient(app)
        self.public_url = patch.dict(os.environ, {"DATATRUST_PUBLIC_URL": "https://datatrust.example"})
        self.public_url.start()
        self.addCleanup(self.public_url.stop)

    def test_signup_sends_one_time_verification_link(self):
        with patch.object(auth.store, "register_email_user", return_value=("new@example.com", True)) as register, \
                patch.object(auth.store, "issue_auth_token", return_value="single-use-token") as issue, \
                patch.object(auth, "_send_email") as send_email:
            response = self.client.post("/api/auth/signup", json={
                "email": " New@Example.com ",
                "password": "a-long-test-password",
            })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"], auth.PUBLIC_MESSAGE)
        register.assert_called_once_with("new@example.com", "a-long-test-password")
        issue.assert_called_once_with("new@example.com", "verify_email", minutes=30)
        self.assertIn("https://datatrust.example/api/auth/verify?token=single-use-token",
                      send_email.call_args.args[2])

    def test_signup_rejects_short_password_before_creating_account(self):
        with patch.object(auth.store, "register_email_user") as register:
            response = self.client.post("/api/auth/signup", json={
                "email": "new@example.com",
                "password": "short",
            })

        self.assertEqual(response.status_code, 400)
        register.assert_not_called()

    def test_unverified_password_login_is_rejected(self):
        with patch.object(auth.store, "login", return_value={"unverified": True}):
            response = self.client.post("/api/auth/login", json={
                "email": "new@example.com",
                "password": "valid-password",
            })

        self.assertEqual(response.status_code, 403)
        self.assertIn("Verify your email", response.json()["detail"])

    def test_verified_password_login_sets_http_only_session_cookie(self):
        user = {"username": "new@example.com", "role": "viewer", "session_version": 2}
        with patch.object(auth.store, "login", return_value=user), \
                patch.object(auth.store, "make_session", return_value="signed-session") as make_session:
            response = self.client.post("/api/auth/login", json={
                "email": "new@example.com",
                "password": "valid-password",
            })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["role"], "viewer")
        self.assertEqual(response.cookies["dt_session"], "signed-session")
        self.assertIn("httponly", response.headers["set-cookie"].lower())
        make_session.assert_called_once_with("new@example.com", "viewer", 2)

    def test_password_login_accepts_admin_username(self):
        user = {"username": "admin", "role": "admin", "session_version": 1}
        with patch.object(auth.store, "login", return_value=user) as store_login, \
                patch.object(auth.store, "make_session", return_value="admin-session"):
            response = self.client.post("/api/auth/login", json={
                "email": "admin",
                "password": "admin",
            })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"], {"username": "admin", "role": "admin"})
        store_login.assert_called_once_with("admin", "admin")

    def test_verification_link_is_rejected_after_token_is_consumed(self):
        with patch.object(auth.store, "consume_auth_token", side_effect=["new@example.com", None]), \
                patch.object(auth.store, "audit"):
            first = self.client.get("/api/auth/verify", params={"token": "one-time"})
            replay = self.client.get("/api/auth/verify", params={"token": "one-time"})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 400)

    def test_password_reset_rejects_short_password(self):
        with patch.object(auth.store, "reset_user_password") as reset:
            response = self.client.post("/api/auth/reset", json={
                "token": "x" * 24,
                "password": "short",
            })

        self.assertEqual(response.status_code, 422)
        reset.assert_not_called()

    def test_password_reset_rejects_replayed_token(self):
        with patch.object(auth.store, "reset_user_password", side_effect=["new@example.com", None]):
            first = self.client.post("/api/auth/reset", json={
                "token": "x" * 24,
                "password": "a-long-test-password",
            })
            replay = self.client.post("/api/auth/reset", json={
                "token": "x" * 24,
                "password": "a-long-test-password",
            })

        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 400)

    def test_google_sign_in_reports_missing_configuration(self):
        with patch.dict(os.environ, {"GOOGLE_CLIENT_ID": "", "GOOGLE_CLIENT_SECRET": ""}):
            response = self.client.get("/api/auth/google")

        self.assertEqual(response.status_code, 503)

    def test_google_callback_rejects_unverified_email(self):
        with patch.object(auth.oauth.google, "authorize_access_token",
                          new=AsyncMock(return_value={"userinfo": {
                              "email": "new@example.com",
                              "email_verified": False,
                              "sub": "google-subject",
                          }})), patch.object(auth.store, "register_google_user") as register:
            response = self.client.get("/api/auth/google/callback", follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login?error=google_email")
        register.assert_not_called()

    def test_google_callback_creates_session_for_verified_account(self):
        user = {"username": "new@example.com", "role": "viewer", "session_version": 0}
        with patch.object(auth.oauth.google, "authorize_access_token",
                          new=AsyncMock(return_value={"userinfo": {
                              "email": "new@example.com",
                              "email_verified": True,
                              "sub": "google-subject",
                          }})), patch.object(auth.store, "register_google_user", return_value=user), \
                patch.object(auth.store, "make_session", return_value="google-session"):
            response = self.client.get("/api/auth/google/callback", follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/app")
        self.assertEqual(response.cookies["dt_session"], "google-session")

    def test_auth_disabled_uses_local_admin_identity(self):
        request = Request({
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/estate/session",
            "raw_path": b"/api/estate/session",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8000),
        })
        with patch.object(auth.store, "auth_enabled", return_value=False):
            user = estate_routes.current_user(request)
            session = estate_routes.session(request)

        self.assertEqual(user, {"username": "local", "role": "admin"})
        self.assertEqual(session, {"user": user})

    def test_dashboard_stays_public_while_estate_api_requires_sign_in(self):
        import server

        with patch.object(server.estate_store, "auth_enabled", return_value=True), \
                patch.object(server.estate_store, "read_session", return_value=None):
            app_client = TestClient(server.app, follow_redirects=False)
            dashboard = app_client.get("/")
            estate_session = app_client.get("/api/estate/session")
            estate_connections = app_client.get("/api/estate/connections")

        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(estate_session.status_code, 200)
        self.assertIsNone(estate_session.json()["user"])
        self.assertEqual(estate_connections.status_code, 401)


if __name__ == "__main__":
    unittest.main()
