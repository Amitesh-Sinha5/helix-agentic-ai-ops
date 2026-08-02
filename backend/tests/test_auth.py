"""Phase 2: signup, login, refresh rotation, RBAC."""

from __future__ import annotations

import httpx
import pytest

from app.core.security import create_access_token, hash_password, verify_password


async def test_signup_login_and_access_protected_route(client: httpx.AsyncClient):
    signup = await client.post(
        "/auth/signup", json={"email": "Alice@Example.com", "password": "correct-horse1"}
    )
    assert signup.status_code == 201
    body = signup.json()
    assert body["user"]["email"] == "alice@example.com"  # normalised
    assert "hashed_password" not in str(body)

    login = await client.post(
        "/auth/login", json={"email": "alice@example.com", "password": "correct-horse1"}
    )
    assert login.status_code == 200
    token = login.json()["tokens"]["access_token"]

    me = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == "alice@example.com"


async def test_first_account_is_admin_and_later_accounts_are_not(client: httpx.AsyncClient):
    first = await client.post("/auth/signup", json={"email": "first@x.com", "password": "passw0rd1"})
    second = await client.post("/auth/signup", json={"email": "second@x.com", "password": "passw0rd1"})
    assert first.json()["user"]["role"] == "admin"
    assert second.json()["user"]["role"] == "user"


async def test_duplicate_email_is_rejected(client: httpx.AsyncClient):
    await client.post("/auth/signup", json={"email": "dupe@x.com", "password": "passw0rd1"})
    again = await client.post("/auth/signup", json={"email": "dupe@x.com", "password": "passw0rd1"})
    assert again.status_code == 409


@pytest.mark.parametrize(
    "password,reason",
    [("short1", "too short"), ("alllettersonly", "no digits"), ("12345678901", "no letters")],
)
async def test_weak_passwords_are_rejected(client: httpx.AsyncClient, password: str, reason: str):
    response = await client.post("/auth/signup", json={"email": "weak@x.com", "password": password})
    assert response.status_code == 422, reason


async def test_wrong_password_is_indistinguishable_from_unknown_account(client: httpx.AsyncClient):
    await client.post("/auth/signup", json={"email": "real@x.com", "password": "passw0rd1"})
    wrong = await client.post("/auth/login", json={"email": "real@x.com", "password": "passw0rd2"})
    unknown = await client.post("/auth/login", json={"email": "ghost@x.com", "password": "passw0rd1"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"]


async def test_missing_and_invalid_tokens_are_rejected(client: httpx.AsyncClient):
    assert (await client.get("/auth/me")).status_code == 401
    assert (await client.get("/auth/me", headers={"Authorization": "Bearer garbage"})).status_code == 401


async def test_expired_token_is_rejected(client: httpx.AsyncClient, monkeypatch):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "access_token_expire_minutes", -1)
    signup = await client.post("/auth/signup", json={"email": "exp@x.com", "password": "passw0rd1"})
    token = signup.json()["tokens"]["access_token"]

    response = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


async def test_refresh_token_is_rotated_and_old_one_dies(client: httpx.AsyncClient):
    signup = await client.post("/auth/signup", json={"email": "rot@x.com", "password": "passw0rd1"})
    original = signup.json()["tokens"]["refresh_token"]

    rotated = await client.post("/auth/refresh", json={"refresh_token": original})
    assert rotated.status_code == 200
    new_refresh = rotated.json()["refresh_token"]
    assert new_refresh != original

    # The new one works...
    assert (await client.post("/auth/refresh", json={"refresh_token": new_refresh})).status_code == 200
    # ...and the original is now dead.
    assert (await client.post("/auth/refresh", json={"refresh_token": original})).status_code == 401


async def test_refresh_token_reuse_revokes_the_whole_family(client: httpx.AsyncClient):
    """Presenting a rotated token means it leaked, so every session is killed."""
    signup = await client.post("/auth/signup", json={"email": "theft@x.com", "password": "passw0rd1"})
    stolen = signup.json()["tokens"]["refresh_token"]

    rotated = await client.post("/auth/refresh", json={"refresh_token": stolen})
    live_token = rotated.json()["refresh_token"]

    # Attacker replays the old token: detected as reuse.
    replay = await client.post("/auth/refresh", json={"refresh_token": stolen})
    assert replay.status_code == 401
    assert "revoked" in replay.json()["detail"].lower()

    # The legitimate session was revoked too, forcing a real re-login.
    assert (await client.post("/auth/refresh", json={"refresh_token": live_token})).status_code == 401


async def test_access_token_is_not_accepted_as_a_refresh_token(client: httpx.AsyncClient):
    signup = await client.post("/auth/signup", json={"email": "mix@x.com", "password": "passw0rd1"})
    access = signup.json()["tokens"]["access_token"]
    assert (await client.post("/auth/refresh", json={"refresh_token": access})).status_code == 401


async def test_logout_revokes_one_session_and_logout_all_revokes_everything(client: httpx.AsyncClient):
    signup = await client.post("/auth/signup", json={"email": "out@x.com", "password": "passw0rd1"})
    tokens = signup.json()["tokens"]
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    second = await client.post("/auth/login", json={"email": "out@x.com", "password": "passw0rd1"})
    second_refresh = second.json()["tokens"]["refresh_token"]

    logout = await client.post(
        "/auth/logout", json={"refresh_token": tokens["refresh_token"]}, headers=headers
    )
    assert logout.status_code == 200
    assert (
        await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    ).status_code == 401
    # The other session is untouched.
    assert (await client.post("/auth/refresh", json={"refresh_token": second_refresh})).status_code == 200

    all_out = await client.post("/auth/logout", json={"all_sessions": True}, headers=headers)
    assert all_out.status_code == 200
    assert (await client.post("/auth/refresh", json={"refresh_token": second_refresh})).status_code == 401


async def test_admin_route_rejects_regular_user_with_403(user_client: httpx.AsyncClient):
    response = await user_client.get("/observability/summary")
    assert response.status_code == 403
    assert "admin" in response.json()["detail"].lower()


async def test_admin_route_allows_admin(admin_client: httpx.AsyncClient):
    response = await admin_client.get("/observability/summary")
    assert response.status_code == 200
    assert "pods" in response.json()


async def test_admin_route_requires_authentication(client: httpx.AsyncClient):
    assert (await client.get("/observability/summary")).status_code == 401


async def test_token_for_deleted_user_is_rejected(client: httpx.AsyncClient):
    """A valid signature is not enough if the subject no longer exists."""
    token, _ = create_access_token("nonexistent-user-id", role="user")
    response = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_password_hashing_round_trip():
    hashed = hash_password("s3cret-password")
    assert hashed != "s3cret-password"
    assert verify_password("s3cret-password", hashed)
    assert not verify_password("wrong", hashed)


def test_overlong_password_is_rejected_not_silently_truncated():
    """bcrypt ignores bytes past 72; accepting them would be a real auth hole."""
    with pytest.raises(ValueError):
        hash_password("x" * 100)
