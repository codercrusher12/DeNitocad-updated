"""
Unit tests for db.py's wallet-auth (nonces/sessions) and credits
storage - the pieces backing wallet_auth.py's Sign-In-With-Wallet flow
and the pay-as-you-go/bulk-pack credit system that replaced per-call
BOT payments. These test db.py directly (no HTTP, no FastAPI) since
the properties that matter here - single-use nonces, session expiry,
and above all the atomic credit decrement - are storage-layer
guarantees, not routing behavior. See test_api.py for the HTTP-level
equivalents.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


WALLET = "0xAbC1230000000000000000000000000000000D"


class TestNonces:
    def test_nonce_is_embedded_in_its_own_message(self, tmp_db):
        nonce, message = tmp_db.create_nonce(WALLET)
        assert nonce in message

    def test_wrong_address_cannot_consume_someone_elses_nonce(self, tmp_db):
        nonce, _ = tmp_db.create_nonce(WALLET)
        other = "0x0000000000000000000000000000000000dEaD"
        assert tmp_db.consume_nonce(other, nonce, ttl_seconds=600) is None

    def test_correct_address_consumes_and_gets_the_exact_message_back(self, tmp_db):
        nonce, message = tmp_db.create_nonce(WALLET)
        result = tmp_db.consume_nonce(WALLET, nonce, ttl_seconds=600)
        assert result == message

    def test_a_nonce_cannot_be_replayed(self, tmp_db):
        nonce, _ = tmp_db.create_nonce(WALLET)
        assert tmp_db.consume_nonce(WALLET, nonce, ttl_seconds=600) is not None
        assert tmp_db.consume_nonce(WALLET, nonce, ttl_seconds=600) is None

    def test_an_expired_nonce_is_rejected(self, tmp_db):
        nonce, _ = tmp_db.create_nonce(WALLET)
        time.sleep(1.1)
        assert tmp_db.consume_nonce(WALLET, nonce, ttl_seconds=1) is None


class TestSessions:
    def test_a_fresh_session_is_retrievable(self, tmp_db):
        session_id, _ = tmp_db.create_session(WALLET, ttl_seconds=600)
        session = tmp_db.get_session(session_id)
        assert session is not None
        assert session["wallet_address"] == WALLET.lower()

    def test_an_expired_session_returns_none_and_is_cleaned_up(self, tmp_db):
        session_id, _ = tmp_db.create_session(WALLET, ttl_seconds=0)
        time.sleep(1.1)
        assert tmp_db.get_session(session_id) is None
        # Lazily deleted, not just treated as invalid - confirm the row
        # is actually gone, not just failing the expiry check forever.
        with tmp_db.get_conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        assert row is None

    def test_deleting_a_session_invalidates_it(self, tmp_db):
        session_id, _ = tmp_db.create_session(WALLET, ttl_seconds=600)
        tmp_db.delete_session(session_id)
        assert tmp_db.get_session(session_id) is None


class TestCredits:
    def test_fresh_wallet_has_zero_credits(self, tmp_db):
        assert tmp_db.get_credit_balance(WALLET) == 0

    def test_consuming_with_zero_balance_fails(self, tmp_db):
        assert tmp_db.consume_credit(WALLET) is False

    def test_add_credits_is_additive_across_purchases(self, tmp_db):
        tmp_db.add_credits(WALLET, 1000)
        balance = tmp_db.add_credits(WALLET, 1)
        assert balance == 1001

    def test_consume_credit_decrements_by_the_requested_amount(self, tmp_db):
        tmp_db.add_credits(WALLET, 10)
        assert tmp_db.consume_credit(WALLET) is True
        assert tmp_db.get_credit_balance(WALLET) == 9

    def test_boundary_at_exactly_one_credit(self, tmp_db):
        tmp_db.add_credits(WALLET, 1)
        assert tmp_db.consume_credit(WALLET) is True
        assert tmp_db.get_credit_balance(WALLET) == 0
        assert tmp_db.consume_credit(WALLET) is False

    def test_add_credits_rejects_non_positive_amounts(self, tmp_db):
        import pytest

        with pytest.raises(ValueError):
            tmp_db.add_credits(WALLET, 0)
        with pytest.raises(ValueError):
            tmp_db.add_credits(WALLET, -5)

    def test_concurrent_spenders_cannot_overdraw_a_single_credit(self, tmp_db):
        """The property that actually matters for real money: N
        concurrent /generate calls for a wallet sitting at exactly 1
        credit must produce exactly 1 winner, never 0 and never more
        than 1. This is what makes db.consume_credit's UPDATE ... WHERE
        balance >= ? a real guarantee rather than a check-then-act race."""
        import threading

        tmp_db.add_credits(WALLET, 1)
        results = []

        def spend():
            results.append(tmp_db.consume_credit(WALLET))

        threads = [threading.Thread(target=spend) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(results) == 1
        assert tmp_db.get_credit_balance(WALLET) == 0
