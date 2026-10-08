import base64
import subprocess

import pytest
from starlette.requests import Request

from lore.web.auth import COOKIE, Auth, AuthError, handle


def request(headers=None, cookies=None):
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if cookies:
        raw.append((b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()))
    return Request({"type": "http", "headers": raw})


def basic(user, password):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


@pytest.fixture
def passwords_file(tmp_path):
    entry = subprocess.run(["htpasswd", "-nbB", "alice", "s3cret"], capture_output=True, text=True, check=True).stdout
    path = tmp_path / "users"
    path.write_text(entry)
    return str(path)


def make_auth(redis, passwords_file=None, dev_user=None):
    return Auth(redis, public_url="https://lore.example", passwords_file=passwords_file, github_client_id="id",
                github_client_secret="secret", github_users={"octocat": "alice"}, dev_user=dev_user)


async def test_password_and_forged_header(redis, passwords_file):
    auth = make_auth(redis, passwords_file)
    assert await auth.user(request(basic("alice", "s3cret"))) == "alice"
    assert await auth.user(request(basic("alice", "wrong"))) is None
    # Outside dev mode a client can't name itself.
    assert await auth.user(request({"X-Lore-User": "alice"})) is None


async def test_sessions_can_be_listed_and_revoked(redis, passwords_file):
    auth = make_auth(redis, passwords_file)
    session_id = await auth.start_session("alice", "password", request({"User-Agent": "test"}))
    assert await auth.user(request(cookies={COOKIE: session_id})) == "alice"
    listed = [s for s in await auth.sessions() if s["id"] == handle(session_id)]
    assert listed and listed[0]["user"] == "alice" and listed[0]["agent"] == "test"
    assert await auth.revoke(handle(session_id))
    assert await auth.user(request(cookies={COOKIE: session_id})) is None
    assert not await auth.revoke(handle(session_id))


async def test_dev_user_and_override(redis):
    auth = make_auth(redis, dev_user="tester")
    assert await auth.user(request()) == "tester"
    assert await auth.user(request({"X-Lore-User": "dana"})) == "dana"


async def test_github_state_is_single_use(redis):
    auth = make_auth(redis)
    url = await auth.github_start()
    assert url.startswith("https://github.com/login/oauth/authorize?") and "redirect_uri=https%3A%2F%2Flore.example" in url
    with pytest.raises(AuthError, match="expired"):
        await auth.github_finish("code", "forged-state")


def test_cookie_is_secure_on_https(redis):
    assert make_auth(redis).cookie_args()["secure"] is True
