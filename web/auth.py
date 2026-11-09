"""Site-wide email verification, password recovery, and Google OpenID Connect."""
import os
import re
import smtplib
from email.message import EmailMessage
from urllib.parse import quote, urlsplit

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from estate import store

router = APIRouter(prefix="/api/auth")
oauth = OAuth()
oauth.register(
    name="google",
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PUBLIC_MESSAGE = "If this email can be used, we sent instructions to its inbox."


class Credentials(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(min_length=1, max_length=256)


class EmailAddress(BaseModel):
    email: str = Field(max_length=320)


class PasswordReset(BaseModel):
    token: str = Field(min_length=20, max_length=256)
    password: str = Field(min_length=12, max_length=256)


def normalize_email(email: str) -> str:
    value = email.strip().lower()
    if len(value) > 254 or not EMAIL_PATTERN.fullmatch(value):
        raise HTTPException(400, "Enter a valid email address.")
    return value


def _send_email(recipient: str, subject: str, text: str) -> None:
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    try:
        port = int(os.getenv("SMTP_PORT", "587"))
    except ValueError as exc:
        raise RuntimeError("SMTP_PORT must be a number.") from exc
    username = os.getenv("SMTP_USER", "")
    password = os.getenv("SMTP_PASSWORD", "")
    sender = os.getenv("SMTP_FROM", username)
    if not username or not password or not sender:
        raise RuntimeError("Email delivery is not configured. Set SMTP_USER, SMTP_PASSWORD, and SMTP_FROM.")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content(text)
    with smtplib.SMTP(host, port, timeout=15) as client:
        client.ehlo()
        client.starttls()
        client.ehlo()
        client.login(username, password)
        client.send_message(message)


def _public_url(request: Request, path: str) -> str:
    base = os.getenv("DATATRUST_PUBLIC_URL", "").strip().rstrip("/")
    if not base:
        if request.url.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise HTTPException(503, "Set DATATRUST_PUBLIC_URL to the public HTTPS origin before sending email.")
        base = str(request.base_url).rstrip("/")
    parsed = urlsplit(base)
    if not parsed.hostname or parsed.query or parsed.fragment:
        raise HTTPException(500, "DATATRUST_PUBLIC_URL must be an origin without a query or fragment.")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")):
        raise HTTPException(500, "DATATRUST_PUBLIC_URL must use HTTPS except for localhost development.")
    return f"{base}{path}"


def _mail_or_error(recipient: str, subject: str, text: str) -> None:
    try:
        _send_email(recipient, subject, text)
    except (OSError, smtplib.SMTPException, RuntimeError) as exc:
        print(f"Authentication email delivery failed: {type(exc).__name__}")
        raise HTTPException(503, "Email delivery is unavailable. Check the email configuration and try again.") from exc


def _set_session(response: Response, user: dict) -> None:
    response.set_cookie(
        "dt_session",
        store.make_session(user["username"], user["role"], user.get("session_version", 0)),
        httponly=True,
        samesite="lax",
        secure=os.getenv("PORTAL_COOKIE_SECURE", "false").lower() == "true",
        max_age=store.SESSION_HOURS * 3600,
        path="/",
    )


@router.get("/session")
def session(request: Request):
    user = store.read_session(request.cookies.get("dt_session", ""))
    return {"user": {"username": user["username"], "role": user["role"]} if user else None}


@router.post("/signup")
def signup(body: Credentials, request: Request):
    email = normalize_email(body.email)
    if len(body.password) < 12:
        raise HTTPException(400, "Use a password with at least 12 characters.")
    username, needs_verification = store.register_email_user(email, body.password)
    if username and needs_verification:
        token = store.issue_auth_token(username, "verify_email", minutes=30)
        link = _public_url(request, "/api/auth/verify?token=" + quote(token))
        _mail_or_error(email, "Verify your DataTrust account",
                       f"Use this one-time link within 30 minutes to verify your email:\n\n{link}\n\nIf you did not request this account, ignore this message.")
    return {"message": PUBLIC_MESSAGE}


@router.post("/login")
def login(body: Credentials, response: Response):
    identifier = body.email.strip().lower()
    if not identifier or len(identifier) > 254:
        raise HTTPException(400, "Enter your email address or username.")
    if "@" in identifier:
        identifier = normalize_email(identifier)
    user = store.login(identifier, body.password)
    if not user:
        raise HTTPException(401, "Username/email or password is incorrect.")
    if user.get("unverified"):
        raise HTTPException(403, "Verify your email before signing in. Check your inbox for the verification link.")
    _set_session(response, user)
    return {"user": {"username": user["username"], "role": user["role"]}}


@router.get("/verify")
def verify(token: str):
    username = store.consume_auth_token(token, "verify_email")
    if not username:
        return HTMLResponse(
            "<!doctype html><title>Link expired</title><h1>This verification link is invalid or expired.</h1>"
            "<p><a href='/login'>Return to DataTrust sign in</a></p>",
            status_code=400,
        )
    store.audit(username, "EMAIL_VERIFIED", "portal", "email address verified")
    return HTMLResponse(
        "<!doctype html><title>Email verified</title><h1>Email verified</h1>"
        "<p>Your DataTrust account is ready.</p><p><a href='/login'>Continue to sign in</a></p>"
    )


@router.post("/forgot")
def forgot(body: EmailAddress, request: Request):
    email = normalize_email(body.email)
    with store.db() as conn:
        row = conn.execute("""SELECT username FROM portal.users
                              WHERE lower(email) = %s AND email_verified AND active""", (email,)).fetchone()
    if row:
        token = store.issue_auth_token(row["username"], "reset_password", minutes=30)
        link = _public_url(request, "/login#reset_token=" + quote(token))
        _mail_or_error(email, "Reset your DataTrust password",
                       f"Use this one-time link within 30 minutes to reset your password:\n\n{link}\n\nIf you did not request a reset, ignore this message.")
    return {"message": PUBLIC_MESSAGE}


@router.post("/reset")
def reset_password(body: PasswordReset):
    if len(body.password) < 12:
        raise HTTPException(400, "Use a password with at least 12 characters.")
    username = store.reset_user_password(body.token, body.password)
    if not username:
        raise HTTPException(400, "This password-reset link is invalid or expired. Request a new one.")
    return {"message": "Password changed. You can now sign in."}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie("dt_session", path="/")
    return {"ok": True}


@router.get("/google")
async def google_login(request: Request):
    if not os.getenv("GOOGLE_CLIENT_ID") or not os.getenv("GOOGLE_CLIENT_SECRET"):
        raise HTTPException(503, "Google sign-in is not configured.")
    callback = _public_url(request, "/api/auth/google/callback")
    return await oauth.google.authorize_redirect(request, callback)


@router.get("/google/callback")
async def google_callback(request: Request):
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception as exc:
        print(f"Google sign-in failed: {type(exc).__name__}")
        return RedirectResponse("/login?error=google", status_code=303)
    claims = token.get("userinfo") or {}
    raw_email = claims.get("email")
    subject = claims.get("sub")
    if not isinstance(raw_email, str) or not isinstance(subject, str) or claims.get("email_verified") is not True:
        return RedirectResponse("/login?error=google_email", status_code=303)
    try:
        email = normalize_email(raw_email)
    except HTTPException:
        return RedirectResponse("/login?error=google_email", status_code=303)
    user = store.register_google_user(email, subject)
    if not user:
        return RedirectResponse("/login?error=google_account", status_code=303)
    response = RedirectResponse("/app", status_code=303)
    _set_session(response, user)
    return response
