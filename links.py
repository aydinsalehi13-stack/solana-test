"""The only place FOMO URLs are built (spec 3.4). Never shorten, wrap or edit what these return."""
import logging

log = logging.getLogger("desk.links")

CHAIN_ID = {"solana": 1399811149, "robinhood": 4663, "base": 8453, "bnb": 56}
SIGNUP_URL = "https://fomo.family"


def coin_url(address, chain="solana"):  # unknown chain: log it, return SIGNUP_URL
    if chain not in CHAIN_ID:
        log.warning("unknown chain %r, returning signup url", chain)
        return SIGNUP_URL
    return f"https://fomo.family/coin?address={address}&chainId={CHAIN_ID[chain]}&source=share_link"


def profile_url(handle):
    return f"https://fomo.family/profile/{handle.lstrip('@')}?source=share_link"
