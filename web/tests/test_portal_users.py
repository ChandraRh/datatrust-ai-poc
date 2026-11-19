"""Portal users (estate/store.save_user and the route): add a person, change a role or switch someone off without
resetting the password, readable errors, and the last admin is protected."""
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from estate import routes as estate_routes
from estate import store


def fake_db(current=None, other_admins=1):
    log = []

    class Conn:
        def execute(self, sql, params=None):
            log.append((" ".join(sql.split()), params))
            cur = MagicMock()
            if sql.strip().startswith("SELECT role, active"):
                cur.fetchone.return_value = current
            elif "COUNT(*)" in sql:
                cur.fetchone.return_value = {"n": other_admins}
            return cur

        def commit(self):
            log.append(("COMMIT", None))

    @contextmanager
    def db():
        yield Conn()
    return db, log


def save(*args, current=None, other_admins=1, **kw):
    db, log = fake_db(current, other_admins)
    with patch.object(store, "db", db), patch.object(store, "audit") as audit, \
            patch.object(store, "hash_password", side_effect=lambda p: "hash:" + p):
        store.save_user(*args, **kw)
    return log, audit


class SaveUserTests(unittest.TestCase):
    def test_add_a_person(self):
        log, audit = save("Priya@Company.com", "viewer", "a-long-password-1", "admin@company.com")
        insert = next(s for s, p in log if s.startswith("INSERT"))
        params = next(p for s, p in log if s.startswith("INSERT"))
        self.assertIn("session_version = portal.users.session_version + 1", insert)
        self.assertEqual(params[0], "priya@company.com")                       # emails are kept in lower case
        self.assertEqual(params[1], "hash:a-long-password-1")
        audit.assert_called_once()

    def test_change_role_or_switch_off_keeps_the_password(self):
        log, audit = save("priya@company.com", "admin", "", "admin@company.com", current={"role": "viewer", "active": True})
        self.assertTrue(any(s.startswith("UPDATE portal.users SET role") for s, _ in log))
        self.assertFalse(any(s.startswith("INSERT") for s, _ in log))
        log, audit = save("priya@company.com", "viewer", None, "admin@company.com", active=False, current={"role": "viewer", "active": True})
        self.assertIn("switched off", audit.call_args[0][3])

    def test_readable_errors(self):
        cases = [(("not-an-email", "viewer", "a-long-password-1", "a@b.co"), {}, "email address"),
                 (("p@company.com", "viewer", "short", "a@b.co"), {}, "at least 12 characters"),
                 (("p@company.com", "viewer", "", "a@b.co"), {}, "new user needs a password"),
                 (("p@company.com", "owner", "a-long-password-1", "a@b.co"), {}, "admin or viewer")]
        for args, kw, text in cases:
            with self.assertRaises(ValueError) as e:
                save(*args, **kw)
            self.assertIn(text, str(e.exception))

    def test_admins_are_protected(self):
        with self.assertRaises(ValueError) as e:                                  # yourself
            save("me@company.com", "viewer", "", "me@company.com", current={"role": "admin", "active": True})
        self.assertIn("ask another admin", str(e.exception))
        with self.assertRaises(ValueError) as e:                                  # the last active admin
            save("boss@company.com", "admin", "", "me@company.com", active=False, current={"role": "admin", "active": True}, other_admins=0)
        self.assertIn("at least one active admin", str(e.exception))


class RouteTests(unittest.TestCase):
    def client(self, role="admin"):
        app = FastAPI()
        app.include_router(estate_routes.router)
        app.dependency_overrides[estate_routes.current_user] = lambda: {"username": "admin@company.com", "role": role}
        return TestClient(app)

    def test_password_may_be_empty_for_an_existing_user(self):
        with patch.object(store, "save_user") as s:
            r = self.client().post("/api/estate/portal-users", json={"username": "p@company.com", "role": "admin", "active": False})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(s.call_args[0], ("p@company.com", "admin", "", "admin@company.com", False))

    def test_a_short_password_comes_back_as_a_sentence(self):
        db, _ = fake_db(None)
        with patch.object(store, "db", db), patch.object(store, "audit"):
            r = self.client().post("/api/estate/portal-users", json={"username": "p@company.com", "password": "short"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["detail"], "The password needs at least 12 characters.")

    def test_viewers_are_refused(self):
        self.assertEqual(self.client("viewer").post("/api/estate/portal-users", json={"username": "p@company.com"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
