"""
Static configuration: token addresses, protocol contracts, event selectors.

All StarkNet addresses are stored as canonical minimal-hex felt252 strings
(no leading zeros after 0x), consistent with fetcher._norm().
Verify addresses at: https://github.com/starknet-io/starknet-addresses
"""
from __future__ import annotations


def _norm_felt(addr: str) -> str:
    """Normalize a felt252 address: strip leading zeros (0x049d... → 0x49d...)."""
    try:
        return "0x" + hex(int(addr, 16))[2:].lower()
    except (ValueError, TypeError):
        return addr.lower()

# ── Tokens ──────────────────────────────────────────────────────────────────

# Lowercase contract address → symbol
ADDRESS_TO_TOKEN: dict[str, str] = {
    "0x049d36570d4e46f48e99674bd3fcc84644ddd6b96f7c741b1562b82f9e004dc7": "ETH",
    "0x04718f5a0fc34cc1af16a1cdee98ffb20c31f5cd61d6ab07201858f4287c938d": "STRK",
    # StarkGate-bridged USDC (Cairo-0, old bridge) — CoinGecko: bridged-usd-coin-starkgate
    "0x053c91253bc9682c04929ca02ed00b3e423f6710d2ee7e0d5ebb06f3ecf368a8": "USDCe",
    # Circle-native USDC on StarkNet (Cairo-1, deployed by Circle) — CoinGecko: usd-coin
    "0x033068f6539f8e6e6b131e6b2b814e6c34a5224bc66947c47dab9dfee93b35fb": "USDC",
    "0x068f5c6a61780768455de69077e07e89787839bf8166decfbf92b645209c0fb8": "USDT",
    "0x05574eb6b8789a91466f902c380d978e472db68170ff82a5b650b95a58ddf4ad": "DAI",
    "0x03fe2b97c1fd336e750087d68b9b867997fd64a2661ff3ca5a7c771641e8e7ac": "WBTC",
    "0x042b8f0484674ca266ac5d08e4ac6a3fe65bd3129795def2dca5c34ecc5f96d2": "wstETH",
    # Endur liquid-staked STRK (verified from on-chain Transfer events)
    "0x028d709c875c0ceac3dce7065bec5328186dc89fe254527084d1689910954b0a": "xSTRK",
}

TOKEN_DECIMALS: dict[str, int] = {
    "ETH": 18,
    "STRK": 18,
    "USDCe": 6,
    "USDC": 6,
    "USDT": 6,
    "DAI": 18,
    "WBTC": 8,
    "wstETH": 18,
    "xSTRK": 18,
}

COINGECKO_IDS: dict[str, str] = {
    "ETH": "ethereum",
    "STRK": "starknet",
    "USDCe": "bridged-usd-coin-starkgate",
    "USDC": "usd-coin",
    "USDT": "tether",
    "DAI": "dai",
    "WBTC": "wrapped-bitcoin",
    "wstETH": "wrapped-steth",
    # xSTRK has no CoinGecko listing — priced via on-chain vault rate (see below)
}

# Liquid staking tokens whose price is derived on-chain rather than from a price feed.
# Format: symbol → (parent_symbol, vault_contract)
# Price = parent_price × vault.convert_to_assets(1e18) / 1e18
# The exchange rate is sampled at two points (earliest-tx block + latest block) and
# linearly interpolated for intermediate dates.
LIQUID_STAKING_SOURCES: dict[str, tuple[str, str]] = {
    "xSTRK": (
        "STRK",
        "0x028d709c875c0ceac3dce7065bec5328186dc89fe254527084d1689910954b0a",
    ),
}

STABLECOINS: set[str] = {"USDC", "USDCe", "USDT", "DAI"}

# Contracts that emit ERC-20 Transfer events but whose tokens you explicitly do not
# want to track (e.g. LP tokens, internal reward tokens, dust).
# Every Transfer from a contract NOT in ADDRESS_TO_TOKEN and NOT here will cause the
# report to fail with an error.  You must make a conscious choice for every token.
IGNORED_TOKEN_CONTRACTS: set[str] = set()

# ── Protocol contracts ───────────────────────────────────────────────────────

# Native STRK staking contract (delegators call claim_rewards here)
STAKING_CONTRACT = "0x00ca1702e64c81d9a07b86bd2c540188d92a2c73cf5cc0e508d949015e7e84a7"

# Well-known DEX core/router contracts — swaps routed through these
DEX_CONTRACTS: set[str] = {
    "0x00000005dd3d2f4429af886cd1a3b08289dbcea99a294197e9eb43b0e0325b4b",  # Ekubo core
    "0x041fd22b238fa21cfcf5dd45a8548974d8263b3a531a60388411c5e230f97023",  # JediSwap v1 router
    "0x03b6bc2bd047a2f0a2a5e7c0c72bc0f12fca0b8fdedb6d07c5c44ffe17e0d13",  # JediSwap v2
    "0x010884171baf1914edc28d7afb619b40a4051cfae78a094a55d230f19e944a28",  # MySwap
    "0x07a6f98c03379b9513ca84cca1373ff452a7462a3b61598f0af5bb27ad7f76d1",  # 10KSwap
    "0x04270219d365d6b017231b52e92b3fb5d7c8378b05e9abc97724537a80e93b0f",  # AVNU exchange router
}

# DeFi protocols that pay yield/interest — receiving tokens from these = income
DEFI_INCOME_CONTRACTS: set[str] = {
    "0x04c0a5193d58f74fbace4b74dcf65481e734ed1714121bdc571da345540efa05",  # zkLend market
    "0x028d709c875c0ceac3dce7065bec5328186dc89fe254527084d1689910954b0a",  # Endur vault
    # Add Nostra pool contracts as they become known
}

# Normalize all addresses to canonical form so they match fetcher._norm() output.
ADDRESS_TO_TOKEN = {_norm_felt(k): v for k, v in ADDRESS_TO_TOKEN.items()}
DEX_CONTRACTS = {_norm_felt(a) for a in DEX_CONTRACTS}
DEFI_INCOME_CONTRACTS = {_norm_felt(a) for a in DEFI_INCOME_CONTRACTS}
IGNORED_TOKEN_CONTRACTS = {_norm_felt(a) for a in IGNORED_TOKEN_CONTRACTS}
STAKING_CONTRACT = _norm_felt(STAKING_CONTRACT)

ALL_PROTOCOL_CONTRACTS: set[str] = (
    DEX_CONTRACTS | DEFI_INCOME_CONTRACTS | {STAKING_CONTRACT}
)

# ── Event selectors ──────────────────────────────────────────────────────────
# StarkNet selector = keccak256(ascii_name) & ((1 << 250) - 1)

# ERC-20 Transfer — the single most important event for tax tracking
TRANSFER_SELECTOR = "0x99cd8bde557814842a3121e8ddfd433a539b8c9f14bf31ebf108d12e6196e9"


def _sn_keccak(name: str) -> str:
    """Compute a StarkNet event/function selector from its ASCII name."""
    from Crypto.Hash import keccak as _keccak
    k = _keccak.new(digest_bits=256)
    k.update(name.encode("ascii"))
    value = int(k.hexdigest(), 16) & ((1 << 250) - 1)
    return hex(value)


# Staking contract reward claim events (computed at import time)
POOL_MEMBER_REWARD_CLAIMED_SELECTOR = _sn_keccak("PoolMemberRewardClaimed")
STAKER_REWARD_CLAIMED_SELECTOR = _sn_keccak("StakerRewardClaimed")
DELEGATION_POOL_MEMBER_EXIT_INTENT_SELECTOR = _sn_keccak("PoolMemberExitIntent")
POOL_MEMBER_EXIT_ACTION_SELECTOR = _sn_keccak("PoolMemberExitAction")

# ── API endpoints ────────────────────────────────────────────────────────────

# Truly public RPC endpoints (no key required)
PUBLIC_RPC_URLS = [
    "https://starknet-mainnet.public.blastapi.io",
    "https://free-rpc.nethermind.io/mainnet-juno",
    "https://rpc.starknet.lava.build",
]

# ── Israeli tax constants ────────────────────────────────────────────────────

ISRAEL_CGT_RATE = 0.25          # Standard capital-gains tax rate
ISRAEL_SURTAX_THRESHOLD = 721_560  # NIS — surtax kicks in above this
ISRAEL_SURTAX_RATE = 0.03       # Section 121B(f); some CPAs argue 5% (121B(b))
                                 # for crypto — consult your CPA
