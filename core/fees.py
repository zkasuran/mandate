# SPDX-License-Identifier: Apache-2.0
"""Strategist fee accounting.

A strategist earns on followed volume, not on a follower's profits, and never
by holding their money. The fee is computed per fill, in USDC, and owed from
the follower to the strategist at settlement.

Charging on volume rather than performance is a deliberate choice: a
performance fee on an unverifiable mark is precisely the incentive this product
exists to remove, and volume is the one quantity both sides can read off the
chain.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

# Nobody pays the platform more than the strategist. The cap is in the code so
# it cannot drift in a config file.
MAX_FEE_BPS = 2_000
PROTOCOL_SHARE_BPS = 1_000        # 10% of the strategist's fee


@dataclass
class FeeSplit:
    gross_usdc: int
    strategist_usdc: int
    protocol_usdc: int
    fee_bps: int
    volume_usdc: int

    def to_dict(self) -> dict:
        d = asdict(self)
        d["gross_usd"] = self.gross_usdc / 1e6
        d["strategist_usd"] = self.strategist_usdc / 1e6
        d["protocol_usd"] = self.protocol_usdc / 1e6
        d["volume_usd"] = self.volume_usdc / 1e6
        return d


def fee_on_volume(volume_usdc: int, fee_bps: int) -> FeeSplit:
    """Split the fee on one clip. Integer maths only, remainder to the strategist."""
    if fee_bps < 0 or fee_bps > MAX_FEE_BPS:
        raise ValueError(f"fee_bps must be within 0..{MAX_FEE_BPS}")
    gross = volume_usdc * fee_bps // 10_000
    protocol = gross * PROTOCOL_SHARE_BPS // 10_000
    return FeeSplit(gross_usdc=gross, strategist_usdc=gross - protocol,
                    protocol_usdc=protocol, fee_bps=fee_bps,
                    volume_usdc=volume_usdc)


def accrue(fills: list, fee_bps: int) -> dict:
    """Total owed across a set of executed clips."""
    splits = [fee_on_volume(f["quote_in"], fee_bps)
              for f in fills if f.get("quote_in")]
    return {
        "clips": len(splits),
        "volume_usdc": sum(s.volume_usdc for s in splits),
        "gross_usdc": sum(s.gross_usdc for s in splits),
        "strategist_usdc": sum(s.strategist_usdc for s in splits),
        "protocol_usdc": sum(s.protocol_usdc for s in splits),
        "fee_bps": fee_bps,
        "basis": "charged on followed volume, never on an unverifiable mark",
    }
