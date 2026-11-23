from __future__ import annotations

import importlib
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.billing import BillingStore, hash_lookup_secret, utc_now
from app.config import get_settings
from app.wewe_store import WeWeStore


@pytest.fixture()
def auth_app(mysql_settings, monkeypatch):
    for key, value in {
        "MYSQL_HOST": mysql_settings.mysql_host,
        "MYSQL_PORT": str(mysql_settings.mysql_port),
        "MYSQL_USER": mysql_settings.mysql_user,
        "MYSQL_PASSWORD": mysql_settings.mysql_password,
        "MYSQL_DATABASE": mysql_settings.mysql_database,
        "MYSQL_UNIX_SOCKET": mysql_settings.mysql_unix_socket,
        "BOOTSTRAP_ADMIN_EMAIL": "auth-bootstrap@example.com",
        "BOOTSTRAP_ADMIN_PASSWORD": "test-only-password",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    main = importlib.import_module("app.main")
    main.billing_store = BillingStore(mysql_settings)
    main.wewe_store = WeWeStore(mysql_settings)
    main.app.dependency_overrides.clear()

    class ForbiddenExternalWork:
        def __getattr__(self, name):
            raise AssertionError(f"external work started before authorization: {name}")

    main.pipeline = SimpleNamespace(
        embedding=ForbiddenExternalWork(),
        indexer=ForbiddenExternalWork(),
        chat=ForbiddenExternalWork(),
    )
    yield main
    main.app.dependency_overrides.clear()
    get_settings.cache_clear()


def _session_client(main, user_id: int) -> TestClient:
    client = TestClient(main.app)
    client.cookies.set("session_token", main.billing_store.create_session(user_id))
    return client


def test_sms_slider_challenge_is_bound_to_original_phone(auth_app):
    main = auth_app
    main.settings.app_sms_enabled = False
    phone_a = "13800138000"
    phone_b = "13900139000"
    challenge_id = "sl_cross_phone_must_fail"
    main.billing_store.create_slider_challenge(
        challenge_id=challenge_id,
        target_hash=hash_lookup_secret(f"{challenge_id}:100"),
        expires_at=utc_now() + timedelta(minutes=3),
        ip_address="testclient",
        phone=phone_a,
    )
    client = TestClient(main.app)

    response = client.post("/api/auth/sms/send", json={
        "phone": phone_b,
        "challenge_id": challenge_id,
        "slider_offset": 100,
    })

    assert response.status_code == 400
    with main.billing_store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sms_verification_codes").fetchone()[0] == 0

    # A wrong-phone attempt must not consume the legitimate user's challenge.
    response = client.post("/api/auth/sms/send", json={
        "phone": phone_a,
        "challenge_id": challenge_id,
        "slider_offset": 100,
    })
    assert response.status_code == 200
    assert len(response.json()["debug_code"]) == 6


def test_sms_send_is_serialized_per_phone(auth_app, monkeypatch):
    main = auth_app
    main.settings.app_sms_enabled = True
    main.settings.app_sms_provider = "tencent"
    phone = "13800138000"
    send_count = 0
    send_count_lock = threading.Lock()

    class SlowTencentClient:
        def __init__(self, _settings):
            pass

        def send_code(self, **_kwargs):
            nonlocal send_count
            with send_count_lock:
                send_count += 1
            time.sleep(0.25)
            return SimpleNamespace(ok=True, provider_message_id="msg-test", error="")

    monkeypatch.setattr(main, "TencentSmsClient", SlowTencentClient)
    challenge_ids = ["sl_concurrent_send_a", "sl_concurrent_send_b"]
    for challenge_id in challenge_ids:
        main.billing_store.create_slider_challenge(
            challenge_id=challenge_id,
            target_hash=hash_lookup_secret(f"{challenge_id}:100"),
            expires_at=utc_now() + timedelta(minutes=3),
            ip_address="testclient",
            phone=phone,
        )

    barrier = threading.Barrier(2)

    def send(challenge_id):
        barrier.wait(timeout=10)
        with TestClient(main.app) as client:
            return client.post("/api/auth/sms/send", json={
                "phone": phone,
                "challenge_id": challenge_id,
                "slider_offset": 100,
            })

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(send, challenge_ids))

    assert [response.status_code for response in responses].count(200) == 1
    assert any(
        response.status_code == 429 and "正在发送" in response.json()["detail"]
        for response in responses
    )
    assert send_count == 1
    with main.billing_store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sms_verification_codes").fetchone()[0] == 1


def test_slider_challenge_requires_a_valid_phone(auth_app):
    client = TestClient(auth_app.app)
    assert client.get("/api/auth/slider-challenge").status_code == 400
    assert client.get("/api/auth/slider-challenge?phone=not-a-phone").status_code == 400


def test_docs_openapi_and_debug_require_authentication(auth_app):
    main = auth_app
    user = main.billing_store.create_user("docs-reader@example.com", "password-123")
    anonymous = TestClient(main.app)
    for path in ("/docs", "/api/debug", "/openapi.json"):
        response = anonymous.get(path)
        assert response.status_code == 401, path

    client = _session_client(main, int(user["id"]))
    for path in ("/docs", "/api/debug", "/openapi.json"):
        response = client.get(path)
        assert response.status_code == 200, path
    assert "openapi" in client.get("/openapi.json").json()


def test_every_business_api_route_declares_authentication(auth_app):
    main = auth_app
    public = {
        "/api/auth/register",
        "/api/auth/register-phone",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/slider-challenge",
        "/api/auth/sms/send",
        "/api/billing/plans",
    }
    allowed_dependencies = {
        main.current_user, main.current_admin, main.current_api_key, main.current_wewe_image_user
    }

    def calls(dependant):
        for dependency in dependant.dependencies:
            yield dependency.call
            yield from calls(dependency)

    unchecked = []
    for route in main.app.routes:
        if not isinstance(route, APIRoute):
            continue
        if not (route.path.startswith("/api/") or route.path == "/process"):
            continue
        if route.path in public:
            continue
        if not allowed_dependencies.intersection(calls(route.dependant)):
            unchecked.append(f"{sorted(route.methods)} {route.path}")
    assert not unchecked, "API routes without authentication: " + ", ".join(unchecked)


def test_cross_user_knowledge_and_rag_access_is_denied_before_external_work(auth_app):
    main = auth_app
    owner = main.billing_store.create_user("owner@example.com", "password-123")
    stranger = main.billing_store.create_user("stranger@example.com", "password-123")
    kb = main.billing_store.ensure_default_knowledge_base(int(owner["id"]))
    document = main.billing_store.record_knowledge_document(
        int(owner["id"]), int(kb["id"]), "owner-run", "private.pdf", "pdf",
        "completed", 1, 1, 0, "artifacts/owner-run",
    )
    stranger_session = _session_client(main, int(stranger["id"]))
    owner_kb = int(kb["id"])

    for path in (
        f"/api/knowledge-bases/{owner_kb}/documents",
        f"/api/rag/sources?knowledge_base_id={owner_kb}",
    ):
        assert stranger_session.get(path).status_code == 404
    for path, payload in (
        ("/api/rag/search", {"query": "private", "knowledge_base_id": owner_kb}),
        ("/api/rag/answer", {"query": "private", "knowledge_base_id": owner_kb}),
    ):
        assert stranger_session.post(path, json=payload).status_code == 404
    assert stranger_session.delete(
        f"/api/knowledge-bases/{owner_kb}/documents/{document['id']}"
    ).status_code == 404

    raw_key, _ = main.billing_store.create_api_key(int(stranger["id"]), "stranger-key")
    api_client = TestClient(main.app, headers={"X-API-Key": raw_key})
    assert api_client.get(f"/api/v1/knowledge-bases/{owner_kb}/documents").status_code == 404
    assert api_client.get(f"/api/v1/knowledge-bases/{owner_kb}/sources").status_code == 404
    assert api_client.post(
        f"/api/v1/knowledge-bases/{owner_kb}/search", json={"query": "private"}
    ).status_code == 404
    assert api_client.post(
        f"/api/v1/knowledge-bases/{owner_kb}/answer", json={"query": "private"}
    ).status_code == 404
    assert api_client.delete(
        f"/api/v1/knowledge-bases/{owner_kb}/documents/{document['id']}"
    ).status_code == 404
    owner_view = _session_client(main, int(owner["id"]))
    assert owner_view.get(f"/api/knowledge-bases/{owner_kb}/documents").json()["count"] == 1


def test_admin_fake_session_and_artifact_owner_boundaries(auth_app, tmp_path, monkeypatch):
    main = auth_app
    owner = main.billing_store.create_user("artifact-owner@example.com", "password-123")
    stranger = main.billing_store.create_user("artifact-stranger@example.com", "password-123")
    anonymous = TestClient(main.app)
    assert anonymous.get("/api/admin/users").status_code == 401
    assert anonymous.get(
        "/api/v1/knowledge-bases", headers={"X-API-Key": "dp_forged-invalid-key"}
    ).status_code == 401
    anonymous.cookies.set("session_token", "sess_forged-invalid-token")
    assert anonymous.get("/api/auth/me").status_code == 401
    stranger_client = _session_client(main, int(stranger["id"]))
    assert stranger_client.get("/api/admin/users").status_code == 403
    assert stranger_client.get("/api/config").status_code == 403

    artifacts = tmp_path / "artifacts"
    work = artifacts / "owner-run"
    work.mkdir(parents=True)
    (work / "private.txt").write_text("owner-only", encoding="utf-8")
    main._write_artifact_owner(work, int(owner["id"]), None)
    monkeypatch.setattr(main, "ARTIFACTS_DIR", artifacts)
    assert anonymous.get("/artifacts/owner-run/private.txt").status_code == 401
    assert stranger_client.get("/artifacts/owner-run/private.txt").status_code == 403
    owner_client = _session_client(main, int(owner["id"]))
    response = owner_client.get("/artifacts/owner-run/private.txt")
    assert response.status_code == 200
    assert response.text == "owner-only"


def test_wewe_image_supports_api_key_without_cross_user_or_cache_leak(auth_app, tmp_path, monkeypatch):
    main = auth_app
    owner = main.billing_store.create_user("image-owner@example.com", "password-123")
    stranger = main.billing_store.create_user("image-stranger@example.com", "password-123")
    kb = main.billing_store.ensure_default_knowledge_base(int(owner["id"]))
    source_id = main.wewe_store.add_source(
        int(owner["id"]), int(kb["id"]), "web", "图片来源", target_url="https://example.com"
    )
    article_id, _ = main.wewe_store.upsert_article(
        user_id=int(owner["id"]), source_id=source_id, knowledge_base_id=int(kb["id"]),
        upstream_article_id="image-article", title="图片文章",
    )
    image_dir = tmp_path / str(owner["id"]) / str(article_id) / "images"
    image_dir.mkdir(parents=True)
    image_path = image_dir / "img.png"
    image_path.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    ))
    image_id = main.wewe_store.upsert_image(
        article_id, int(owner["id"]), "https://example.com/image.png",
        local_path=str(image_path), mime_type="image/png", byte_size=image_path.stat().st_size,
        description="受控图片", status="described",
    )
    monkeypatch.setattr(main.settings, "wewe_artifact_dir", str(tmp_path))
    owner_key, _ = main.billing_store.create_api_key(int(owner["id"]), "image-owner-key")
    stranger_key, _ = main.billing_store.create_api_key(int(stranger["id"]), "image-stranger-key")
    url = f"/api/wewe/images/{image_id}/file"

    assert TestClient(main.app).get(url).status_code == 401
    assert TestClient(main.app, headers={"X-API-Key": stranger_key}).get(url).status_code == 404
    response = TestClient(main.app, headers={"X-API-Key": owner_key}).get(url)
    assert response.status_code == 200, response.text
    assert response.content == image_path.read_bytes()
    assert response.headers["Cache-Control"] == "private, no-store"
    assert _session_client(main, int(owner["id"])).get(url).status_code == 200
