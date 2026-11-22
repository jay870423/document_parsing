from __future__ import annotations

import threading
from dataclasses import replace
from datetime import timedelta

import pytest

from app.billing import BillingStore, utc_now


def grant_primary_subscription(store: BillingStore, user_id: int, quota: int):
    # Signup also grants a 365-day trial; make the measured plan the active one.
    return store.grant_subscription(
        user_id, "personal_light", call_quota=quota,
        ends_at=utc_now() + timedelta(days=400),
    )


def test_quota_reservation_is_atomic_under_concurrency(mysql_settings):
    store = BillingStore(mysql_settings)
    user = store.create_user("atomic-quota@example.com", "password-123")
    grant_primary_subscription(store, user["id"], 5)
    barrier = threading.Barrier(20)
    successes = []
    failures = []
    lock = threading.Lock()

    def reserve():
        barrier.wait(timeout=10)
        try:
            reservation = store.reserve_call(user["id"])
            with lock:
                successes.append(reservation)
        except PermissionError as exc:
            with lock:
                failures.append(str(exc))

    threads = [threading.Thread(target=reserve) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert not any(thread.is_alive() for thread in threads)
    assert len(successes) == 5
    assert len(failures) == 15
    assert store.get_active_subscription(user["id"])["remaining_calls"] == 0
    for reservation in successes:
        store.consume_call(
            user["id"], None, "/api/rag/answer", "POST", 200, 1,
            reservation=reservation,
        )
    assert store.get_active_subscription(user["id"])["used_calls"] == 5
    with pytest.raises(PermissionError):
        store.reserve_call(user["id"])


def test_failed_call_releases_reservation_exactly_once(mysql_settings):
    store = BillingStore(mysql_settings)
    user = store.create_user("refund-quota@example.com", "password-123")
    grant_primary_subscription(store, user["id"], 1)
    reservation = store.reserve_call(user["id"])
    assert store.get_active_subscription(user["id"])["remaining_calls"] == 0

    store.consume_call(
        user["id"], None, "/api/rag/answer", "POST", 500, 1,
        call_count=0, reservation=reservation,
    )

    assert store.get_active_subscription(user["id"])["remaining_calls"] == 1
    with pytest.raises(RuntimeError, match="settled"):
        store.consume_call(
            user["id"], None, "/api/rag/answer", "POST", 500, 1,
            call_count=0, reservation=reservation,
        )
    assert store.get_active_subscription(user["id"])["remaining_calls"] == 1


def test_legacy_successful_consume_cannot_overdraw(mysql_settings):
    store = BillingStore(mysql_settings)
    user = store.create_user("legacy-quota@example.com", "password-123")
    grant_primary_subscription(store, user["id"], 1)
    store.consume_call(user["id"], None, "/legacy", "POST", 200, 1)
    with pytest.raises(PermissionError):
        store.consume_call(user["id"], None, "/legacy", "POST", 200, 1)
    assert store.get_active_subscription(user["id"])["used_calls"] == 1


def test_concurrent_failure_settlement_refunds_exactly_once(mysql_settings):
    store = BillingStore(mysql_settings)
    user = store.create_user("concurrent-refund@example.com", "password-123")
    grant_primary_subscription(store, user["id"], 2)
    failed_reservation = store.reserve_call(user["id"])
    successful_reservation = store.reserve_call(user["id"])
    store.consume_call(
        user["id"], None, "/api/rag/answer", "POST", 200, 1,
        reservation=successful_reservation,
    )
    barrier = threading.Barrier(12)
    outcomes = []
    lock = threading.Lock()

    def settle_failed_copy(copy):
        barrier.wait(timeout=10)
        try:
            store.consume_call(
                user["id"], None, "/api/rag/answer", "POST", 500, 1,
                call_count=0, reservation=copy,
            )
            outcome = "settled"
        except RuntimeError:
            outcome = "already-settled"
        with lock:
            outcomes.append(outcome)

    threads = [
        threading.Thread(target=settle_failed_copy, args=(replace(failed_reservation),))
        for _ in range(12)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert not any(thread.is_alive() for thread in threads)
    assert outcomes.count("settled") == 1
    assert outcomes.count("already-settled") == 11
    subscription = store.get_active_subscription(user["id"])
    assert subscription["used_calls"] == 1
    assert subscription["remaining_calls"] == 1


def test_abandoned_persistent_reservation_is_refunded_by_another_store(mysql_settings):
    crashed_store = BillingStore(mysql_settings)
    user = crashed_store.create_user("crashed-reservation@example.com", "password-123")
    grant_primary_subscription(crashed_store, user["id"], 1)
    reservation = crashed_store.reserve_call(user["id"])
    assert crashed_store.get_active_subscription(user["id"])["remaining_calls"] == 0
    with crashed_store.connect() as conn:
        conn.execute(
            "UPDATE call_reservations SET created_at = ? WHERE reservation_id = ?",
            ("2000-01-01T00:00:00+00:00", reservation.reservation_id),
        )

    recovery_store = BillingStore(mysql_settings)
    assert recovery_store.reconcile_abandoned_reservations() == 1
    assert recovery_store.reconcile_abandoned_reservations() == 0
    subscription = recovery_store.get_active_subscription(user["id"])
    assert subscription["used_calls"] == 0
    assert subscription["remaining_calls"] == 1
    with recovery_store.connect() as conn:
        row = conn.execute(
            "SELECT state FROM call_reservations WHERE reservation_id = ?",
            (reservation.reservation_id,),
        ).fetchone()
    assert row["state"] == "expired"


def test_quota_precheck_reconciles_expired_reservation(mysql_settings):
    store = BillingStore(mysql_settings)
    user = store.create_user("precheck-recovery@example.com", "password-123")
    grant_primary_subscription(store, user["id"], 1)
    reservation = store.reserve_call(user["id"])
    assert store.get_active_subscription(user["id"])["remaining_calls"] == 0
    with store.connect() as conn:
        conn.execute(
            "UPDATE call_reservations SET created_at = ? WHERE reservation_id = ?",
            ("2000-01-01T00:00:00+00:00", reservation.reservation_id),
        )

    subscription = store.ensure_quota(user["id"])
    assert subscription["remaining_calls"] == 1
    with store.connect() as conn:
        row = conn.execute(
            "SELECT state FROM call_reservations WHERE reservation_id = ?",
            (reservation.reservation_id,),
        ).fetchone()
    assert row["state"] == "expired"
