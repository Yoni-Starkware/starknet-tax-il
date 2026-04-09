"""Classifier invariants: prefer UNKNOWN over wrong tax labels."""
from __future__ import annotations

from decimal import Decimal

from starknet_tax.classifier import EventType, classify
from starknet_tax.config import (
    NEW_POOL_MEMBER_SELECTOR,
    POOL_MEMBER_EXIT_ACTION_SELECTOR,
    POOL_MEMBER_REWARD_CLAIMED_SELECTOR,
    STAKING_CONTRACT,
)
from tests.conftest import make_ptx, token_flow


def _dex() -> str:
    from starknet_tax.config import DEX_CONTRACTS

    return sorted(DEX_CONTRACTS)[0]


def _defi_income_contract() -> str:
    from starknet_tax.config import DEFI_INCOME_CONTRACTS

    return sorted(DEFI_INCOME_CONTRACTS)[0]


class TestStakingAndUnknown:
    def test_staking_receive_without_claim_or_exit_is_unknown(self) -> None:
        """Do not treat ambiguous staking receipts as income or DEFI_INCOME."""
        ptx = make_ptx(
            tokens_in=[token_flow("STRK", "10")],
            touched={STAKING_CONTRACT},
            raw_events=[],  # no PoolMemberRewardClaimed / StakerRewardClaimed / ExitAction
        )
        ev = classify(ptx)
        assert ev.event_type == EventType.UNKNOWN
        assert "review manually" in ev.notes.lower()

    def test_staking_reward_claim_is_staking_income(self) -> None:
        ptx = make_ptx(
            tokens_in=[token_flow("STRK", "1")],
            touched={STAKING_CONTRACT},
            raw_events=[{"keys": [POOL_MEMBER_REWARD_CLAIMED_SELECTOR], "data": []}],
        )
        assert classify(ptx).event_type == EventType.STAKING_INCOME

    def test_pool_member_exit_action_is_withdrawal(self) -> None:
        ptx = make_ptx(
            tokens_in=[token_flow("STRK", "5")],
            touched={STAKING_CONTRACT},
            raw_events=[{"keys": [POOL_MEMBER_EXIT_ACTION_SELECTOR], "data": []}],
        )
        assert classify(ptx).event_type == EventType.STAKE_WITHDRAWAL


class TestTokensInOnly:
    def test_tokens_in_only_touching_defi_contract_is_defi_income(self) -> None:
        """Receiving only from a known yield protocol => DEFI_INCOME (not RECEIVE)."""
        from starknet_tax.config import DEFI_INCOME_CONTRACTS

        zk = next(iter(sorted(DEFI_INCOME_CONTRACTS)))
        ptx = make_ptx(
            tokens_in=[token_flow("USDC", "100")],
            touched={zk},
            raw_events=[],
        )
        assert classify(ptx).event_type == EventType.DEFI_INCOME


class TestTokensOutOnly:
    def test_send_when_no_protocol_contract(self) -> None:
        ptx = make_ptx(
            tokens_out=[token_flow("ETH", "0.5")],
            touched=set(),
        )
        assert classify(ptx).event_type == EventType.SEND

    def test_protocol_out_without_new_pool_member_or_strk_is_unknown(self) -> None:
        """WBTC (or other) to staking path without delegation signals -> UNKNOWN, not SEND."""
        ptx = make_ptx(
            tokens_out=[token_flow("WBTC", "0.01")],
            touched={STAKING_CONTRACT},
            raw_events=[],
        )
        ev = classify(ptx)
        assert ev.event_type == EventType.UNKNOWN
        assert "review manually" in ev.notes.lower()

    def test_strk_only_to_staking_is_stake_deposit(self) -> None:
        ptx = make_ptx(
            tokens_out=[token_flow("STRK", "100")],
            touched={STAKING_CONTRACT},
            raw_events=[],
        )
        assert classify(ptx).event_type == EventType.STAKE_DEPOSIT

    def test_new_pool_member_makes_stake_deposit_for_non_strk(self) -> None:
        ptx = make_ptx(
            tokens_out=[token_flow("WBTC", "0.001")],
            touched={STAKING_CONTRACT},
            raw_events=[{"keys": [NEW_POOL_MEMBER_SELECTOR, "0x0", "0x0"], "data": []}],
        )
        ev = classify(ptx)
        assert ev.event_type == EventType.STAKE_DEPOSIT
        assert "WBTC" in ev.notes


class TestLiquidStaking:
    def test_liquid_stake_strk_to_xstrk(self) -> None:
        ptx = make_ptx(
            tokens_in=[token_flow("xSTRK", "50")],
            tokens_out=[token_flow("STRK", "40")],
        )
        assert classify(ptx).event_type == EventType.LIQUID_STAKE

    def test_liquid_unstake_xstrk_to_strk(self) -> None:
        ptx = make_ptx(
            tokens_in=[token_flow("STRK", "40")],
            tokens_out=[token_flow("xSTRK", "50")],
        )
        assert classify(ptx).event_type == EventType.LIQUID_UNSTAKE


class TestSwapsAndMultiToken:
    def test_multi_token_in_and_out_is_unknown(self) -> None:
        ptx = make_ptx(
            tokens_in=[token_flow("ETH", "1"), token_flow("STRK", "10")],
            tokens_out=[token_flow("USDC", "3000")],
        )
        assert classify(ptx).event_type == EventType.UNKNOWN

    def test_single_token_bridge_swap_without_dex(self) -> None:
        ptx = make_ptx(
            tokens_in=[token_flow("STRK", "10")],
            tokens_out=[token_flow("ETH", "0.2")],
            touched=set(),
        )
        assert classify(ptx).event_type == EventType.SWAP

    def test_dex_swap_different_symbols(self) -> None:
        ptx = make_ptx(
            tokens_in=[token_flow("STRK", "10")],
            tokens_out=[token_flow("ETH", "0.2")],
            touched={_dex()},
        )
        assert classify(ptx).event_type == EventType.SWAP

    def test_dex_same_symbol_in_and_out_is_unknown(self) -> None:
        """Liquidity-style same-asset flow is not classified as SWAP."""
        ptx = make_ptx(
            tokens_in=[token_flow("ETH", "1")],
            tokens_out=[token_flow("ETH", "0.9")],
            touched={_dex()},
        )
        assert classify(ptx).event_type == EventType.UNKNOWN


class TestFeeOnly:
    def test_fee_only(self) -> None:
        ptx = make_ptx(
            fee_amount=Decimal("0.0001"),
        )
        assert classify(ptx).event_type == EventType.FEE_ONLY
