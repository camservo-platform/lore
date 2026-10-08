import base64
import json

import httpx
import pytest

from lore.web import users
from lore.web.auth import Passwords
from lore.web.users import LOCKED_USER, FileUsers, SecretUsers, UserError

from test_auth import make_auth, passwords_file, request  # noqa: F401  (fixture)


async def test_file_store_adds_changes_and_deletes(tmp_path):
    path = tmp_path / "users"
    store = FileUsers(str(path))
    assert await store.names() == []

    text = await store.set_password("Dana", "first-pass", create=True)  # names are lowercased
    assert await store.names() == ["dana"]
    assert users.parse(text)["dana"].startswith("$2y$")  # the htpasswd form Traefik reads
    with pytest.raises(UserError, match="already"):
        await store.set_password("dana", "another-pass", create=True)

    checker = Passwords(None)
    checker.use(await store.set_password("dana", "second-pass", create=False))
    assert checker.check("dana", "second-pass") and not checker.check("dana", "first-pass")

    # The last real user leaves a placeholder (an empty list breaks Traefik's basic auth).
    text = await store.delete("dana")
    assert list(users.parse(text)) == [LOCKED_USER]
    assert await store.names() == []
    with pytest.raises(UserError):
        await store.delete(LOCKED_USER)


@pytest.mark.parametrize("name,password,error", [
    ("Not Valid", "long-enough", "Usernames"),
    (LOCKED_USER, "long-enough", "Usernames"),
    ("ok", "short", "at least"),
    ("ok", "has:colon", "colon"),
    ("ok", "x" * 73, "at most"),
])
async def test_bad_names_and_passwords_are_refused(tmp_path, name, password, error):
    with pytest.raises(UserError, match=error):
        await FileUsers(str(tmp_path / "users")).set_password(name, password, create=True)


async def test_unknown_users_cant_be_changed(tmp_path):
    store = FileUsers(str(tmp_path / "users"))
    with pytest.raises(UserError, match="no user"):
        await store.set_password("ghost", "long-enough", create=False)


class FakeApi:
    """The Kubernetes API for one Secret, with resourceVersion conflicts."""

    def __init__(self, text: str, conflicts: int = 0):
        self.data = base64.b64encode(text.encode()).decode()
        self.version = 1
        self.conflicts = conflicts  # times another writer sneaks in before our patch
        self.tokens: list[str] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.tokens.append(req.headers["authorization"])
        if req.method == "GET":
            return httpx.Response(200, json={"metadata": {"resourceVersion": str(self.version)},
                                             "data": {"users": self.data}})
        body = json.loads(req.content)
        assert req.headers["content-type"] == "application/merge-patch+json"
        if self.conflicts:
            self.conflicts -= 1
            self.version += 1
            return httpx.Response(409, json={})
        if body["metadata"]["resourceVersion"] != str(self.version):
            return httpx.Response(409, json={})
        self.data, self.version = body["data"]["users"], self.version + 1
        return httpx.Response(200, json={})

    @property
    def text(self) -> str:
        return base64.b64decode(self.data).decode()


def secret_store(api: FakeApi, tokens=iter(f"token-{i}" for i in range(100))) -> SecretUsers:
    return SecretUsers("lore-users", "lore", lambda: next(tokens), api="https://k8s.test",
                       transport=httpx.MockTransport(api))


async def test_secret_store_retries_when_someone_else_wrote_first():
    api = FakeApi(f"{LOCKED_USER}:$2y$10$x\n", conflicts=2)
    store = secret_store(api)
    await store.set_password("dana", "long-enough", create=True)
    assert list(users.parse(api.text)) == ["dana"]  # placeholder dropped once there's a real user
    assert len(set(api.tokens)) == len(api.tokens)    # token re-read for every call (rotation)


async def test_secret_store_gives_up_after_repeated_conflicts():
    store = secret_store(FakeApi("", conflicts=50))
    with pytest.raises(UserError, match="kept changing"):
        await store.set_password("dana", "long-enough", create=True)


async def test_secret_store_explains_missing_permission():
    store = SecretUsers("lore-users", "lore", lambda: "t", api="https://k8s.test",
                        transport=httpx.MockTransport(lambda req: httpx.Response(403, json={})))
    with pytest.raises(UserError, match="isn't allowed"):
        await store.names()


async def test_revoke_user_signs_out_only_that_users_sessions(redis, passwords_file):  # noqa: F811
    auth = make_auth(redis, passwords_file)
    req = request()
    dana_pw = await auth.start_session("dana", "password", req)
    dana_gh = await auth.start_session("dana", "github:dana-gh", req)
    other = await auth.start_session("erin", "password", req)
    assert await auth.revoke_user("dana", provider="password") == 1
    assert not await redis.exists(f"lore:session:{dana_pw}")
    assert await redis.exists(f"lore:session:{dana_gh}")
    assert await auth.revoke_user("dana") == 1
    assert await redis.exists(f"lore:session:{other}")
    await redis.delete(f"lore:session:{other}")
