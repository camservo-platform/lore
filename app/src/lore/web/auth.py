"""Who's playing: GitHub sign-in or a password, both ending in a session cookie.

- GitHub: OAuth web flow; only GitHub logins listed in LORE_GITHUB_USERS
  ("github-login:lore-name,...") may sign in, as that Lore name.
- Passwords: the htpasswd file the ingress logins live in (lore-users), checked here
  with bcrypt. A browser that answers the Basic prompt gets a session; scripts can also
  send Basic credentials on every request.
- LORE_DEV_USER (local development only) signs everyone in as that name, and then lets
  an X-Lore-User header pick someone else, for testing multiple players.

Sessions live in Redis (lore:session:<id>), so admins can list and revoke them.
"""

import base64
import hashlib
import json
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import bcrypt
import httpx
from redis.asyncio import Redis
from starlette.requests import HTTPConnection

COOKIE = "lore_session"
SESSION_TTL = 30 * 24 * 3600
STATE_TTL = 600
GITHUB_AUTHORIZE = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN = "https://github.com/login/oauth/access_token"
GITHUB_USER = "https://api.github.com/user"


def parse_user_map(raw: str) -> dict[str, str]:
    """"camservo:cameron, other:dana" -> {"camservo": "cameron", "other": "dana"} (logins lowercased)."""
    mapping = {}
    for pair in raw.split(","):
        if ":" in pair:
            login, name = (part.strip() for part in pair.split(":", 1))
            if login and name:
                mapping[login.lower()] = name
    return mapping


def handle(session_id: str) -> str:
    """A stable, non-secret name for a session (shown to admins, used to revoke it)."""
    return hashlib.sha256(session_id.encode()).hexdigest()[:12]


class Passwords:
    """Checks username/password against an htpasswd file of bcrypt entries."""

    def __init__(self, path: str | None):
        self._path = Path(path) if path else None
        self._mtime = None
        self._entries: dict[str, bytes] = {}

    def _load(self) -> None:
        if not self._path or not self._path.exists():
            self._entries = {}
            return
        mtime = self._path.stat().st_mtime
        if mtime != self._mtime:
            entries = {}
            for line in self._path.read_text().splitlines():
                name, _, digest = line.strip().partition(":")
                if name and digest.startswith(("$2y$", "$2b$", "$2a$")):
                    # htpasswd writes $2y$; bcrypt verifies it as the equivalent $2b$.
                    entries[name] = ("$2b$" + digest[4:]).encode()
            self._entries, self._mtime = entries, mtime

    def check(self, username: str, password: str) -> bool:
        self._load()
        digest = self._entries.get(username)
        return bool(digest) and bcrypt.checkpw(password.encode(), digest)

    @property
    def available(self) -> bool:
        self._load()
        return bool(self._entries)


class Auth:
    def __init__(
        self, redis: Redis, *, public_url: str, passwords_file: str | None, github_client_id: str | None,
        github_client_secret: str | None, github_users: dict[str, str], dev_user: str | None,
    ):
        self._redis = redis
        self._public_url = public_url.rstrip("/")
        self.passwords = Passwords(passwords_file)
        self._github = (github_client_id, github_client_secret) if github_client_id and github_client_secret else None
        self._github_users = github_users
        self.dev_user = dev_user

    @property
    def github_enabled(self) -> bool:
        return self._github is not None

    # --- identifying requests ------------------------------------------------------

    async def user(self, conn: HTTPConnection) -> str | None:
        if self.dev_user:
            return conn.headers.get("x-lore-user") or self.dev_user
        session = await self.session(conn)
        if session:
            return session["user"]
        return self.basic_user(conn)

    async def session(self, conn: HTTPConnection) -> dict[str, Any] | None:
        session_id = conn.cookies.get(COOKIE)
        if not session_id:
            return None
        raw = await self._redis.get(f"lore:session:{session_id}")
        return json.loads(raw) if raw else None

    def basic_user(self, conn: HTTPConnection) -> str | None:
        scheme, _, encoded = conn.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "basic":
            return None
        try:
            username, _, password = base64.b64decode(encoded).decode().partition(":")
        except ValueError:
            return None
        return username if self.passwords.check(username, password) else None

    # --- sessions ------------------------------------------------------------------

    async def start_session(self, user: str, provider: str, conn: HTTPConnection) -> str:
        session_id = secrets.token_urlsafe(32)
        await self._redis.set(f"lore:session:{session_id}", json.dumps({
            "user": user, "provider": provider, "created": time.time(),
            "agent": conn.headers.get("user-agent", "")[:200],
        }), ex=SESSION_TTL)
        return session_id

    async def end_session(self, conn: HTTPConnection) -> None:
        session_id = conn.cookies.get(COOKIE)
        if session_id:
            await self._redis.delete(f"lore:session:{session_id}")

    async def sessions(self) -> list[dict[str, Any]]:
        out = []
        async for key in self._redis.scan_iter("lore:session:*"):
            raw = await self._redis.get(key)
            if raw:
                info = json.loads(raw)
                out.append({"id": handle(key.split(":", 2)[2]), "user": info["user"], "provider": info["provider"],
                            "created": info["created"], "agent": info.get("agent", "")})
        return sorted(out, key=lambda s: -s["created"])

    async def revoke(self, session_handle: str) -> bool:
        async for key in self._redis.scan_iter("lore:session:*"):
            if handle(key.split(":", 2)[2]) == session_handle:
                await self._redis.delete(key)
                return True
        return False

    def cookie_args(self) -> dict[str, Any]:
        return {"httponly": True, "samesite": "lax", "secure": self._public_url.startswith("https://"),
                "max_age": SESSION_TTL, "path": "/"}

    # --- GitHub --------------------------------------------------------------------

    @property
    def github_callback(self) -> str:
        return f"{self._public_url}/auth/github/callback"

    async def github_start(self) -> str:
        """URL to send the browser to; remembers a one-time state value against CSRF."""
        state = secrets.token_urlsafe(24)
        await self._redis.set(f"lore:oauth-state:{state}", "1", ex=STATE_TTL)
        client_id, _ = self._github
        return f"{GITHUB_AUTHORIZE}?" + urlencode({
            "client_id": client_id, "redirect_uri": self.github_callback, "state": state, "allow_signup": "false",
        })

    async def github_finish(self, code: str, state: str) -> tuple[str | None, str]:
        """Returns (lore name, github login); the name is None if that login isn't allowed."""
        if not state or not await self._redis.getdel(f"lore:oauth-state:{state}"):
            raise AuthError("That sign-in link expired. Please try again.")
        client_id, client_secret = self._github
        async with httpx.AsyncClient(timeout=15) as http:
            token = (await http.post(GITHUB_TOKEN, headers={"Accept": "application/json"}, data={
                "client_id": client_id, "client_secret": client_secret, "code": code,
                "redirect_uri": self.github_callback,
            })).json().get("access_token")
            if not token:
                raise AuthError("GitHub didn't accept the sign-in. Please try again.")
            profile = (await http.get(GITHUB_USER, headers={
                "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            })).json()
        login = profile.get("login", "")
        return self._github_users.get(login.lower()), login


class AuthError(Exception):
    pass
