"""Failed generations must not cost the user anything."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

WALLET = "0x" + "ab" * 20


def test_purchased_credit_is_refunded(tmp_db):
    tmp_db.add_credits(WALLET, 5)
    assert tmp_db.charge_generation(WALLET) == "purchased"
    assert tmp_db.get_credit_balance(WALLET) == 4
    tmp_db.refund_generation(WALLET, "purchased")
    assert tmp_db.get_credit_balance(WALLET) == 5


def test_subscription_credit_is_refunded(tmp_db):
    tmp_db.upsert_subscription(
        WALLET, stripe_customer_id="cus_1", stripe_subscription_id="sub_1",
        status="active", tier="starter", subscription_credits=3,
        current_period_end=(datetime.now(timezone.utc) + timedelta(days=20)).isoformat(),
    )
    assert tmp_db.charge_generation(WALLET) == "subscription_credit"
    assert tmp_db.get_subscription(WALLET)["subscription_credits"] == 2
    tmp_db.refund_generation(WALLET, "subscription_credit")
    assert tmp_db.get_subscription(WALLET)["subscription_credits"] == 3


def test_failed_jobs_do_not_count_toward_the_daily_cap(tmp_db):
    tmp_db.record_job(description="x", success=False, user_id=WALLET, error="e")
    assert tmp_db.count_generations_last_24h(WALLET) == 0
    tmp_db.record_job(description="x", success=True, user_id=WALLET, part_type="shaft", parameters={})
    assert tmp_db.count_generations_last_24h(WALLET) == 1


def test_failed_generate_call_refunds_the_credit(client, wallet_with_credits, monkeypatch):
    import web_app

    monkeypatch.setattr(
        web_app.generator, "generate_from_text",
        lambda *a, **k: {"success": False, "error": "boom", "error_type": "ParseError", "parameters": None},
    )
    headers = wallet_with_credits["auth_headers"]
    before = client.get("/credits/balance", headers=headers).json()["balance"]
    body = client.post("/generate", json={"description": "x", "use_deepseek": False}, headers=headers).json()
    after = client.get("/credits/balance", headers=headers).json()["balance"]
    assert body["success"] is False and after == before
    assert "not charged" in body["error"]
