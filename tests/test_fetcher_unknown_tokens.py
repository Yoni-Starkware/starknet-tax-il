"""Fetcher: unknown ERC-20 contracts must abort with clear instructions."""
from __future__ import annotations

import pytest

from starknet_tax.fetcher import _assert_no_unknown_transfers


class TestAssertNoUnknownTransfers:
    def test_empty_list_does_not_raise(self) -> None:
        _assert_no_unknown_transfers([])

    def test_unknown_pair_raises_runtime_error(self) -> None:
        with pytest.raises(RuntimeError) as exc_info:
            _assert_no_unknown_transfers(
                [
                    ("0xaaa", "0xtoken1"),
                    ("0xbbb", "0xtoken1"),
                ]
            )
        msg = str(exc_info.value)
        assert "UNKNOWN TOKEN CONTRACTS" in msg
        assert "--ignore-unknown-tokens" in msg

    def test_message_lists_contract_and_sample_txs(self) -> None:
        with pytest.raises(RuntimeError) as exc_info:
            _assert_no_unknown_transfers([("0xtxhashdeadbeef", "0xdeadbeef0001")])
        msg = str(exc_info.value)
        assert "0xdeadbeef0001" in msg
        assert "0xtxhashdeadbeef" in msg
