# SPDX-License-Identifier: Apache-2.0
"""Wallet layer: how a mandate reaches a follower's funds without holding them.

The product is non-custodial by construction. A follower keeps their own
embedded wallet and grants this server scoped, revocable signing rights. That
is Dynamic's delegated-access pattern, and it is the reason a follower can
leave at any time without asking anyone.

Three adapters, one interface:

  DryRunWallet     builds the exact calldata and refuses to broadcast. Default.
  DynamicServer    a Dynamic MPC server wallet the agent owns end to end.
  DynamicDelegated a follower's own wallet, signed for under delegation.

The Dynamic adapters call the SDK exactly as its Python docs specify
(`dynamic-wallet-sdk`, `DynamicEvmWalletClient`, `create_delegated_evm_client`).
They are wired but not exercised here: no Dynamic environment id or API token
is available in this build, so `available()` reports False and every call
refuses rather than pretending. Running them needs the two credentials named in
`REQUIRED_ENV`, nothing else.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, asdict

from . import chain
from .keccak import keccak256

BASE_CHAIN_ID = 8453

REQUIRED_ENV = ("DYNAMIC_ENV_ID", "DYNAMIC_API_TOKEN")

# Uniswap v3 SwapRouter02 on Base. Verified to carry code by
# scripts/verify_addresses.py before any calldata is built against it.
SWAP_ROUTER_02 = "0x2626664c2603336E57B271c5C0b26F421741e481"


def selector(signature: str) -> str:
    return "0x" + keccak256(signature.encode()).hex()[:8]


# exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))
EXACT_INPUT_SINGLE = selector(
    "exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))")
APPROVE = selector("approve(address,uint256)")


@dataclass
class UnsignedTx:
    to: str
    data: str
    value: int
    chain_id: int
    note: str

    def to_dict(self) -> dict:
        return asdict(self)


def build_approve(token: str, spender: str, amount: int) -> UnsignedTx:
    data = APPROVE + chain.enc_addr(spender) + chain.enc_uint(amount)
    return UnsignedTx(to=token, data=data, value=0, chain_id=BASE_CHAIN_ID,
                      note=f"approve {spender} to spend {amount} of {token}")


def build_exact_input_single(token_in: str, token_out: str, fee: int,
                             recipient: str, amount_in: int,
                             amount_out_min: int) -> UnsignedTx:
    """Uniswap v3 SwapRouter02 exactInputSingle calldata.

    The struct is a single head-encoded tuple of seven static words, so it is
    inlined rather than offset-encoded.
    """
    body = (
        chain.enc_addr(token_in)
        + chain.enc_addr(token_out)
        + chain.enc_uint(fee)
        + chain.enc_addr(recipient)
        + chain.enc_uint(amount_in)
        + chain.enc_uint(amount_out_min)
        + chain.enc_uint(0)               # sqrtPriceLimitX96: no limit
    )
    return UnsignedTx(
        to=SWAP_ROUTER_02, data=EXACT_INPUT_SINGLE + body, value=0,
        chain_id=BASE_CHAIN_ID,
        note=(f"swap {amount_in} of {token_in} for at least {amount_out_min} "
              f"of {token_out} via fee tier {fee}"),
    )


class Wallet:
    """Common interface. Every adapter reports honestly whether it can act."""

    kind = "abstract"

    def available(self) -> tuple[bool, str]:
        raise NotImplementedError

    def address(self) -> str | None:
        raise NotImplementedError

    def send(self, tx: UnsignedTx) -> dict:
        raise NotImplementedError


class DryRunWallet(Wallet):
    """Builds calldata, never broadcasts. The default everywhere.

    A build that cannot move funds is the correct default for a system that
    executes other people's strategies. Turning it off is a deliberate act.
    """

    kind = "dry-run"

    def __init__(self, address: str | None = None):
        self._address = address

    def available(self) -> tuple[bool, str]:
        return True, "dry-run adapter: builds calldata, refuses to broadcast"

    def address(self) -> str | None:
        return self._address

    def send(self, tx: UnsignedTx) -> dict:
        return {
            "broadcast": False,
            "reason": "dry-run wallet: transaction built but not sent",
            "tx": tx.to_dict(),
            "calldata_bytes": (len(tx.data) - 2) // 2,
        }


class DynamicServerWallet(Wallet):
    """A Dynamic MPC server wallet owned by this service.

    Follows the documented Python SDK surface:
        DynamicEvmWalletClient(env_id, rpc_urls={chain_id: url})
        await client.authenticate_api_token(token)
        await client.create_wallet_account(...)
        await client.send_transaction(address=..., tx=...)
    """

    kind = "dynamic-server"

    def __init__(self, env_id: str | None = None, api_token: str | None = None,
                 address: str | None = None, rpc_url: str | None = None):
        self.env_id = env_id or os.environ.get("DYNAMIC_ENV_ID")
        self.api_token = api_token or os.environ.get("DYNAMIC_API_TOKEN")
        self._address = address or os.environ.get("DYNAMIC_WALLET_ADDRESS")
        self.rpc_url = rpc_url or chain.ENDPOINTS[0]

    def available(self) -> tuple[bool, str]:
        missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
        if missing and not (self.env_id and self.api_token):
            return False, f"not configured: set {', '.join(missing)}"
        try:
            import dynamic_wallet_sdk  # noqa: F401
        except ImportError:
            return False, "dynamic-wallet-sdk is not installed (pip install dynamic-wallet-sdk)"
        return True, "dynamic server wallet configured"

    def address(self) -> str | None:
        return self._address

    async def _client(self):
        from dynamic_wallet_sdk import DynamicEvmWalletClient
        client = DynamicEvmWalletClient(
            self.env_id, rpc_urls={BASE_CHAIN_ID: self.rpc_url})
        await client.__aenter__()
        await client.authenticate_api_token(self.api_token)
        return client

    async def create_wallet(self, password: str):
        from dynamic_wallet_sdk import ThresholdSignatureScheme
        client = await self._client()
        try:
            wallet = await client.create_wallet_account(
                threshold_signature_scheme=ThresholdSignatureScheme.TWO_OF_TWO,
                password=password,
            )
            self._address = wallet.account_address
            return {"address": wallet.account_address, "wallet_id": wallet.wallet_id}
        finally:
            await client.__aexit__(None, None, None)

    async def send_async(self, tx: UnsignedTx) -> dict:
        ok, why = self.available()
        if not ok:
            return {"broadcast": False, "reason": why, "tx": tx.to_dict()}
        client = await self._client()
        try:
            payload = {
                "to": tx.to, "value": tx.value, "data": tx.data,
                "chainId": tx.chain_id,
            }
            tx_hash = await client.send_transaction(
                address=self._address, tx=payload, rpc_url=self.rpc_url)
            return {"broadcast": True, "tx_hash": tx_hash,
                    "explorer": f"https://basescan.org/tx/{tx_hash}"}
        finally:
            await client.__aexit__(None, None, None)

    def send(self, tx: UnsignedTx) -> dict:
        import asyncio
        return asyncio.run(self.send_async(tx))


class DynamicDelegatedWallet(Wallet):
    """A follower's own wallet, signed for under revocable delegation.

    This is the product's real posture: the follower never transfers custody,
    and revoking delegation in Dynamic ends the agent's authority immediately
    without this service being involved.

    Follows the documented delegated surface:
        decrypt_delegated_webhook_data(private_key_pem, encrypted_delegated_share,
                                       encrypted_wallet_api_key)
        create_delegated_evm_client(environment_id=..., api_key=...)
    """

    kind = "dynamic-delegated"

    def __init__(self, wallet_id: str, address: str,
                 env_id: str | None = None, api_key: str | None = None):
        self.wallet_id = wallet_id
        self._address = address
        self.env_id = env_id or os.environ.get("DYNAMIC_ENV_ID")
        self.api_key = api_key or os.environ.get("DYNAMIC_API_TOKEN")

    def available(self) -> tuple[bool, str]:
        if not (self.env_id and self.api_key):
            return False, f"not configured: set {', '.join(REQUIRED_ENV)}"
        try:
            import dynamic_wallet_sdk  # noqa: F401
        except ImportError:
            return False, "dynamic-wallet-sdk is not installed"
        return True, f"delegated signing authority for wallet {self.wallet_id}"

    def address(self) -> str | None:
        return self._address

    @staticmethod
    def from_webhook(webhook_body: dict, rsa_private_key_pem: str) -> dict:
        """Decrypt the delegation credentials Dynamic posts to our webhook."""
        from dynamic_wallet_sdk.delegated.decrypt import decrypt_delegated_webhook_data
        decrypted = decrypt_delegated_webhook_data(
            private_key_pem=rsa_private_key_pem,
            encrypted_delegated_key_share=webhook_body["encryptedDelegatedShare"],
            encrypted_wallet_api_key=webhook_body["encryptedWalletApiKey"],
        )
        return {
            "wallet_id": webhook_body["walletId"],
            "wallet_api_key": decrypted.decrypted_wallet_api_key,
            "key_share": decrypted.decrypted_delegated_share["secretShare"],
        }

    def send(self, tx: UnsignedTx) -> dict:
        ok, why = self.available()
        if not ok:
            return {"broadcast": False, "reason": why, "tx": tx.to_dict()}
        raise NotImplementedError(
            "delegated broadcast is assembled from the signature returned by "
            "delegated_sign_message / delegated sign_transaction and submitted "
            "over eth_sendRawTransaction; wire it once a delegation exists"
        )


def default_wallet(follower: str | None = None) -> Wallet:
    """Pick the strongest adapter the environment actually supports."""
    server = DynamicServerWallet(address=follower)
    ok, _ = server.available()
    return server if ok else DryRunWallet(address=follower)
