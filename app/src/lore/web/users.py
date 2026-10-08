"""Password logins, managed from the admin page.

They live as htpasswd lines (bcrypt) in one place that everything else reads: in the
cluster the users Secret (lore-users), which Traefik's basic auth and the web pod's
mounted file both use, and which `deploy.sh add-user` also edits; locally, the
LORE_PASSWORDS_FILE itself. Writes are read-modify-write with the Secret's
resourceVersion, so a concurrent edit (another admin, deploy.sh) is retried rather than
lost.
"""

import base64
import os
import re
import secrets
from collections.abc import Callable
from pathlib import Path

import bcrypt
import httpx

NAME = re.compile(r"^[a-z0-9_-]{1,32}$")
MIN_PASSWORD = 8
MAX_PASSWORD_BYTES = 72  # bcrypt ignores anything longer
# Traefik drops a basic-auth middleware with no users (its routes then 404), so an empty
# list keeps one placeholder whose password was never kept (as deploy.sh does).
LOCKED_USER = "_locked"
SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
SECRET_KEY = "users"


class UserError(Exception):
    pass


def parse(text: str) -> dict[str, str]:
    """htpasswd text -> {name: digest}, in file order."""
    users = {}
    for line in text.splitlines():
        name, sep, digest = line.strip().partition(":")
        if name and sep:
            users[name] = digest
    return users


def render(users: dict[str, str]) -> str:
    real = {name: digest for name, digest in users.items() if name != LOCKED_USER}
    if not real:
        real = {LOCKED_USER: digest_for(secrets.token_urlsafe(32))}
    return "".join(f"{name}:{digest}\n" for name, digest in real.items())


def digest_for(password: str) -> str:
    # $2y$ is what htpasswd writes and every reader accepts (Traefik included); it's the
    # same algorithm as the $2b$ the bcrypt library produces.
    return "$2y$" + bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=10)).decode()[4:]


def new_password() -> str:
    return secrets.token_urlsafe(18)


def check_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not NAME.match(name) or name == LOCKED_USER:
        raise UserError("Usernames are 1-32 lowercase letters, digits, _ and -.")
    return name


def check_password(password: str) -> str:
    if len(password) < MIN_PASSWORD:
        raise UserError(f"Passwords need at least {MIN_PASSWORD} characters.")
    if len(password.encode()) > MAX_PASSWORD_BYTES:
        raise UserError(f"Passwords can be at most {MAX_PASSWORD_BYTES} bytes.")
    if ":" in password or "\n" in password:
        raise UserError("Passwords can't contain a colon or a line break.")
    return password


class UserStore:
    """Reads and edits the htpasswd text; subclasses say where it's kept."""

    writable = True

    async def _read(self) -> tuple[str, str | None]:
        """(text, version) where version guards the next write."""
        raise NotImplementedError

    async def _write(self, text: str, version: str | None) -> bool:
        """False if someone else changed it since that version was read."""
        raise NotImplementedError

    async def names(self) -> list[str]:
        text, _ = await self._read()
        return [name for name in parse(text) if name != LOCKED_USER]

    async def _change(self, edit) -> str:
        for _ in range(5):
            text, version = await self._read()
            users = parse(text)
            edit(users)
            new_text = render(users)
            if await self._write(new_text, version):
                return new_text
        raise UserError("The user list kept changing underneath us; try again.")

    async def set_password(self, name: str, password: str, *, create: bool) -> str:
        """Adds a user (create) or changes an existing one's password; returns the new text."""
        name, password = check_name(name), check_password(password)
        digest = digest_for(password)

        def edit(users: dict[str, str]) -> None:
            if create and name in users:
                raise UserError(f"There's already a user called {name}.")
            if not create and name not in users:
                raise UserError(f"There's no user called {name}.")
            users[name] = digest
        return await self._change(edit)

    async def delete(self, name: str) -> str:
        def edit(users: dict[str, str]) -> None:
            if name not in users or name == LOCKED_USER:
                raise UserError(f"There's no user called {name}.")
            del users[name]
        return await self._change(edit)


class FileUsers(UserStore):
    """Local development: the htpasswd file itself."""

    def __init__(self, path: str | None):
        self._path = Path(path) if path else None
        self.writable = self._path is not None

    async def _read(self) -> tuple[str, str | None]:
        if not self._path or not self._path.exists():
            return "", None
        return self._path.read_text(), None

    async def _write(self, text: str, version: str | None) -> bool:
        if not self._path:
            raise UserError("No passwords file is configured (LORE_PASSWORDS_FILE).")
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(text)
        tmp.replace(self._path)
        return True


class SecretUsers(UserStore):
    """In the cluster: the users Secret, through the Kubernetes API (the pod's service
    account may only get and update that one Secret)."""

    def __init__(self, secret: str, namespace: str, token: Callable[[], str], *,
                 api: str = "https://kubernetes.default.svc", verify: str | bool = True,
                 transport: httpx.AsyncBaseTransport | None = None):
        self._url = f"{api}/api/v1/namespaces/{namespace}/secrets/{secret}"
        self._token = token  # read each time: service account tokens are rotated
        self._client = httpx.AsyncClient(verify=verify, timeout=10, transport=transport)

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}"}

    async def _read(self) -> tuple[str, str | None]:
        response = await self._client.get(self._url, headers=self._auth())
        if response.status_code == 403:
            raise UserError("Lore isn't allowed to read the users secret (check the chart's Role).")
        response.raise_for_status()
        body = response.json()
        encoded = (body.get("data") or {}).get(SECRET_KEY, "")
        return base64.b64decode(encoded).decode(), body["metadata"]["resourceVersion"]

    async def _write(self, text: str, version: str | None) -> bool:
        # A merge patch carrying resourceVersion fails with 409 if the Secret changed.
        response = await self._client.patch(
            self._url, headers={**self._auth(), "Content-Type": "application/merge-patch+json"},
            json={"metadata": {"resourceVersion": version},
                  "data": {SECRET_KEY: base64.b64encode(text.encode()).decode()}},
        )
        if response.status_code == 409:
            return False
        if response.status_code == 403:
            raise UserError("Lore isn't allowed to change the users secret (check the chart's Role).")
        response.raise_for_status()
        return True


def user_store(secret: str | None, passwords_file: str | None) -> UserStore:
    """The Secret when running in the cluster with one configured, else the local file."""
    if secret and (SERVICE_ACCOUNT / "token").exists():
        return SecretUsers(
            secret,
            namespace=(SERVICE_ACCOUNT / "namespace").read_text().strip(),
            token=lambda: (SERVICE_ACCOUNT / "token").read_text().strip(),
            api=f"https://{os.environ.get('KUBERNETES_SERVICE_HOST', 'kubernetes.default.svc')}:"
                f"{os.environ.get('KUBERNETES_SERVICE_PORT', '443')}",
            verify=str(SERVICE_ACCOUNT / "ca.crt"),
        )
    return FileUsers(passwords_file)
