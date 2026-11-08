"""Unit tests for N19 marketable limit order execution and cooldown guards."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch
import pytest

import alpaca_trader
import ai_entry_watch as ew
import ai_positions


class TestMarketableLimitPriceCalculation(unittest.TestCase):
    """Test limit price calculation with dynamic bp cap and penny floor."""

    def test_penny_cushion_on_low_priced_stock(self):
        """On a $10 stock, 5 bp is 0.5 cents ($0.005), so min_cap_dollars ($0.02) must take over."""
        with patch.object(alpaca_trader, "_can_mutate", return_value=True), \
             patch.object(alpaca_trader, "symbol_tradable", return_value=True), \
             patch.object(alpaca_trader, "_require_protective_exit", return_value=False), \
             patch.object(alpaca_trader, "want_fractional", return_value=False), \
             patch.object(alpaca_trader, "_submit_order") as mock_submit:

            mock_order = MagicMock()
            mock_order.id = "mock_ord_1"
            mock_order.status = "new"
            mock_submit.return_value = mock_order

            out = alpaca_trader.buy_marketable_limit(
                "XYZ", price=10.00, qty=10, cap_bp=5.0, min_cap_dollars=0.02
            )
            self.assertTrue(out["ok"])
            self.assertEqual(out["limit_px"], 10.02)
            submitted_req = mock_submit.call_args[0][0]
            self.assertEqual(submitted_req.limit_price, 10.02)

    def test_bp_cap_on_higher_priced_stock(self):
        """On a $200 stock, 5 bp is 10 cents ($0.10), exceeding min_cap_dollars."""
        with patch.object(alpaca_trader, "_can_mutate", return_value=True), \
             patch.object(alpaca_trader, "symbol_tradable", return_value=True), \
             patch.object(alpaca_trader, "_require_protective_exit", return_value=False), \
             patch.object(alpaca_trader, "want_fractional", return_value=False), \
             patch.object(alpaca_trader, "_submit_order") as mock_submit:

            mock_order = MagicMock()
            mock_order.id = "mock_ord_2"
            mock_order.status = "filled"
            mock_submit.return_value = mock_order

            out = alpaca_trader.buy_marketable_limit(
                "XYZ", price=200.00, qty=5, cap_bp=5.0, min_cap_dollars=0.02
            )
            self.assertTrue(out["ok"])
            self.assertEqual(out["limit_px"], 200.10)
            submitted_req = mock_submit.call_args[0][0]
            self.assertEqual(submitted_req.limit_price, 200.10)

    def test_ioc_time_in_force_is_default(self):
        """Marketable limits must use IOC to prevent resting orders from becoming trap bids."""
        from alpaca.trading.enums import TimeInForce

        with patch.object(alpaca_trader, "_can_mutate", return_value=True), \
             patch.object(alpaca_trader, "symbol_tradable", return_value=True), \
             patch.object(alpaca_trader, "_require_protective_exit", return_value=False), \
             patch.object(alpaca_trader, "want_fractional", return_value=False), \
             patch.object(alpaca_trader, "_submit_order") as mock_submit:

            mock_order = MagicMock()
            mock_order.id = "mock_ord_3"
            mock_order.status = "filled"
            mock_submit.return_value = mock_order

            out = alpaca_trader.buy_marketable_limit("XYZ", price=50.00, qty=2)
            self.assertTrue(out["ok"])
            submitted_req = mock_submit.call_args[0][0]
            self.assertEqual(submitted_req.time_in_force, TimeInForce.IOC)

    def test_swept_ioc_order_returns_not_ok_with_cap_breached_error(self):
        """When an IOC order cannot fill within the limit cap, Alpaca returns status='canceled'."""
        with patch.object(alpaca_trader, "_can_mutate", return_value=True), \
             patch.object(alpaca_trader, "symbol_tradable", return_value=True), \
             patch.object(alpaca_trader, "_require_protective_exit", return_value=False), \
             patch.object(alpaca_trader, "want_fractional", return_value=False), \
             patch.object(alpaca_trader, "_submit_order") as mock_submit:

            mock_order = MagicMock()
            mock_order.id = "mock_ord_4"
            mock_order.status = "canceled"
            mock_submit.return_value = mock_order

            out = alpaca_trader.buy_marketable_limit("XYZ", price=50.00, qty=2, cap_bp=3.0)
            self.assertFalse(out["ok"])
            self.assertEqual(out["status"], "canceled")
            self.assertEqual(out["error"], "marketable_limit_cap_breached")
            self.assertEqual(out["note"], "marketable_limit_cap_breached")


class TestMarketableLimitCooldown(unittest.TestCase):
    """Test entry watch cooldown tracking after swept orders."""

    def setUp(self):
        ew.clear_marketable_limit_cooldown()

    def tearDown(self):
        ew.clear_marketable_limit_cooldown()

    def test_cooldown_set_get_clear(self):
        now = 1000.0
        self.assertEqual(ew.get_marketable_limit_cooldown("AAPL", now=now), 0.0)

        ew.set_marketable_limit_cooldown("AAPL", cooldown_sec=900.0, now=now)
        # At now + 300s, 600s left
        self.assertAlmostEqual(ew.get_marketable_limit_cooldown("AAPL", now=now + 300.0), 600.0)
        # At now + 900s, 0s left
        self.assertAlmostEqual(ew.get_marketable_limit_cooldown("AAPL", now=now + 900.0), 0.0)
        # At now + 1000s, 0s left (no negative)
        self.assertEqual(ew.get_marketable_limit_cooldown("AAPL", now=now + 1000.0), 0.0)

        # Clear symbol
        ew.set_marketable_limit_cooldown("TSLA", cooldown_sec=900.0, now=now)
        ew.clear_marketable_limit_cooldown("AAPL")
        self.assertEqual(ew.get_marketable_limit_cooldown("AAPL", now=now), 0.0)
        self.assertGreater(ew.get_marketable_limit_cooldown("TSLA", now=now), 0.0)

        # Clear all
        ew.clear_marketable_limit_cooldown()
        self.assertEqual(ew.get_marketable_limit_cooldown("TSLA", now=now), 0.0)

    def test_blocker_label_and_post_arm_codes(self):
        self.assertEqual(ew.format_blocker("slip_cancel_cooldown"), "slip cool")
        self.assertIn("slip_cancel_cooldown", ew._POST_ARM_BLOCK_CODES)


class TestAIPositionsMarketableLimitRouting(unittest.TestCase):
    """Test ai_positions routing through buy_marketable_limit when configured."""

    def setUp(self):
        ew.clear_marketable_limit_cooldown()

    def tearDown(self):
        ew.clear_marketable_limit_cooldown()

    def test_routes_to_marketable_limit_when_cap_bp_positive(self):
        mock_cfg = {
            "ai_broker_stop_enabled": False,
            "ai_entry_order_style": "market",
            "ai_marketable_limit_cap_bp": 5.0,
            "ai_marketable_limit_min_cap_dollars": 0.02,
            "ai_marketable_limit_tif": "ioc",
            "ai_marketable_limit_cooldown_sec": 900.0,
        }

        decision = {
            "entry_low": 49.90,
            "entry_high": 50.10,
            "stop_price": 48.00,
            "target_1": 54.00,
            "desk_force": True,
        }

        with patch("config.load_config_ro", return_value=mock_cfg), \
             patch("alpaca_trader.buy_marketable_limit") as mock_limit, \
             patch("alpaca_trader.buy_market_shares") as mock_market:

            mock_limit.return_value = {
                "ok": True,
                "order_id": "ord_lim_100",
                "status": "filled",
                "limit_px": 50.03,
            }

            out = ai_positions.place_scaled_entry(
                "AAPL",
                decision,
                account_equity=25000.0,
                current_ask=50.00,
                current_bid=49.98,
            )

            self.assertTrue(out.get("ok"))
            self.assertEqual(out.get("order_type"), "MARKETABLE_LIMIT")
            self.assertEqual(out.get("tranche_a", {}).get("buy_order_id"), "ord_lim_100")
            mock_limit.assert_called_once()
            mock_market.assert_not_called()

    def test_routes_to_market_shares_when_cap_bp_zero_or_omitted(self):
        mock_cfg = {
            "ai_broker_stop_enabled": False,
            "ai_entry_order_style": "market",
            "ai_marketable_limit_cap_bp": 0.0,
        }

        decision = {
            "entry_low": 49.90,
            "entry_high": 50.10,
            "stop_price": 48.00,
            "target_1": 54.00,
            "desk_force": True,
        }

        with patch("config.load_config_ro", return_value=mock_cfg), \
             patch("alpaca_trader.buy_marketable_limit") as mock_limit, \
             patch("alpaca_trader.buy_market_shares") as mock_market:

            mock_market.return_value = {
                "ok": True,
                "order_id": "ord_mkt_200",
                "status": "filled",
            }

            out = ai_positions.place_scaled_entry(
                "AAPL",
                decision,
                account_equity=25000.0,
                current_ask=50.00,
                current_bid=49.98,
            )

            self.assertTrue(out.get("ok"))
            mock_market.assert_called_once()
            mock_limit.assert_not_called()

    def test_failed_marketable_limit_triggers_cooldown(self):
        mock_cfg = {
            "ai_broker_stop_enabled": False,
            "ai_entry_order_style": "market",
            "ai_marketable_limit_cap_bp": 3.0,
            "ai_marketable_limit_cooldown_sec": 600.0,
        }

        decision = {
            "entry_low": 49.90,
            "entry_high": 50.10,
            "stop_price": 48.00,
            "target_1": 54.00,
            "desk_force": True,
        }

        with patch("config.load_config_ro", return_value=mock_cfg), \
             patch("alpaca_trader.buy_marketable_limit") as mock_limit:

            mock_limit.return_value = {
                "ok": False,
                "order_id": "ord_lim_fail",
                "status": "canceled",
                "error": "marketable_limit_cap_breached",
                "limit_px": 50.02,
            }

            out = ai_positions.place_scaled_entry(
                "NVDA",
                decision,
                account_equity=25000.0,
                current_ask=50.00,
                current_bid=49.98,
            )

            self.assertFalse(out.get("ok"))
            # Cooldown should now be active for NVDA
            cooldown_left = ew.get_marketable_limit_cooldown("NVDA")
            self.assertGreater(cooldown_left, 550.0)
            self.assertLessEqual(cooldown_left, 600.0)


if __name__ == "__main__":
    unittest.main()
