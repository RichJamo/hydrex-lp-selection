"""Tests for weekly_bootstrap_update.get_hydx_price.

Invariants under test:
  - Only pairs on Base whose base token address is HYDX_ADDRESS are priced.
  - Among valid pairs, the highest-liquidity one sets the price.
  - No valid pair -> RuntimeError (never a guessed price).

Run: python -m unittest discover tests
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import weekly_bootstrap_update as wbu  # noqa: E402

FAKE_ADDRESS = "0x8D52483efD150738C6F11D450f46FB62B7d0594E"


def _pair(chain, address, price, liquidity, symbol="HYDX"):
    return {
        "chainId": chain,
        "baseToken": {"address": address, "symbol": symbol},
        "priceUsd": str(price) if price is not None else None,
        "liquidity": {"usd": liquidity} if liquidity is not None else None,
    }


def _mock_response(pairs):
    resp = mock.Mock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = pairs
    return resp


class GetHydxPriceTest(unittest.TestCase):
    def _price(self, pairs):
        with mock.patch.object(wbu.requests, "get", return_value=_mock_response(pairs)) as get:
            price = wbu.get_hydx_price()
        requested_url = get.call_args.args[0]
        self.assertTrue(requested_url.endswith(wbu.HYDX_ADDRESS))
        return price

    def test_ignores_copycat_symbol_and_picks_deepest_real_pair(self):
        pairs = [
            _pair("base", FAKE_ADDRESS, 0.1618, 80_000_000),      # fake HYDX, huge liquidity
            _pair("ethereum", wbu.HYDX_ADDRESS, 0.15, 75_000_000),  # right address, wrong chain
            _pair("base", wbu.HYDX_ADDRESS.lower(), 0.02502, 7_885),
            _pair("base", wbu.HYDX_ADDRESS, 0.02486, 696_289),
        ]
        self.assertEqual(self._price(pairs), 0.02486)

    def test_skips_zero_or_missing_price(self):
        pairs = [
            _pair("base", wbu.HYDX_ADDRESS, None, 1_000_000),
            _pair("base", wbu.HYDX_ADDRESS, 0, 900_000),
            _pair("base", wbu.HYDX_ADDRESS, 0.0247, None),
        ]
        self.assertEqual(self._price(pairs), 0.0247)

    def test_raises_when_no_valid_pair(self):
        for pairs in ([], None, [_pair("base", FAKE_ADDRESS, 0.16, 1_000_000)]):
            with self.subTest(pairs=pairs):
                with self.assertRaises(RuntimeError):
                    self._price(pairs)


if __name__ == "__main__":
    unittest.main()
