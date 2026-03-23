"""
core/position_redeemer.py

Auto-redeem winning Polymarket conditional token positions after market settlement.

Redemption flow
───────────────
1.  Bot calls redeemer.queue(market) whenever a new window is opened.
2.  Background loop waits for window_close_ts + SETTLEMENT_DELAY seconds
    (default 5 min) so the Gnosis CTF contract has time to record the payout.
3.  Checks payoutDenominator(conditionId) on-chain → if 0, market not settled yet.
4.  Calls balanceOf(holder, tokenId) for UP and DOWN tokens.
5.  For each non-zero balance, calls redeemPositions via the appropriate path.

Signature-type dispatch (POLY_SIGNATURE_TYPE in .env)
──────────────────────────────────────────────────────
  0  EOA              — EOA sends redeemPositions directly from its own address.
                        Tokens must be in the EOA's wallet.
  1  POLY_PROXY       — Same tx path as EOA but tokens are in the proxy wallet.
                        The proxy must be callable by the EOA (operator registration).
                        NOTE: if the proxy does not forward arbitrary calls, this will
                        return 0 USDC (no error, just an empty redemption).
  2  POLY_GNOSIS_SAFE — Tokens are in the Gnosis Safe (POLY_FUNDER_ADDRESS).
                        EOA owner signs a Safe EIP-712 transaction and calls
                        Safe.execTransaction() to make the Safe call redeemPositions.
                        Requires: EOA is a Safe owner, threshold = 1.

Dependencies
────────────
    pip install web3 eth-keys
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import config

log = logging.getLogger(__name__)

# ── Polygon contract addresses ─────────────────────────────────────────────────
CTF_ADDRESS    = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"  # Gnosis ConditionalTokens
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e collateral
ZERO_BYTES32   = b"\x00" * 32                                   # parentCollectionId

# Tried in order; first responsive one is used.
POLYGON_RPCS = [
    "https://rpc.ankr.com/polygon",
    "https://polygon-rpc.com",
    "https://polygon.llamarpc.com",
    "https://matic-mainnet.chainstacklabs.com",
    "https://rpc-mainnet.matic.network",
]

# ── Minimal ABIs ───────────────────────────────────────────────────────────────
CTF_ABI = [
    {
        "name": "redeemPositions",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "collateralToken",    "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId",        "type": "bytes32"},
            {"name": "indexSets",          "type": "uint256[]"},
        ],
        "outputs": [],
    },
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "id",      "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "payoutDenominator",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "conditionId", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

# Gnosis Safe — only functions needed for sig_type=2
SAFE_ABI = [
    {
        "name": "nonce",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "getTransactionHash",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "to",             "type": "address"},
            {"name": "value",          "type": "uint256"},
            {"name": "data",           "type": "bytes"},
            {"name": "operation",      "type": "uint8"},
            {"name": "safeTxGas",      "type": "uint256"},
            {"name": "baseGas",        "type": "uint256"},
            {"name": "gasPrice",       "type": "uint256"},
            {"name": "gasToken",       "type": "address"},
            {"name": "refundReceiver", "type": "address"},
            {"name": "nonce",          "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bytes32"}],
    },
    {
        "name": "execTransaction",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {"name": "to",             "type": "address"},
            {"name": "value",          "type": "uint256"},
            {"name": "data",           "type": "bytes"},
            {"name": "operation",      "type": "uint8"},
            {"name": "safeTxGas",      "type": "uint256"},
            {"name": "baseGas",        "type": "uint256"},
            {"name": "gasPrice",       "type": "uint256"},
            {"name": "gasToken",       "type": "address"},
            {"name": "refundReceiver", "type": "address"},
            {"name": "signatures",     "type": "bytes"},
        ],
        "outputs": [{"name": "success", "type": "bool"}],
    },
]


# ── Data ───────────────────────────────────────────────────────────────────────

@dataclass
class RedeemTask:
    condition_id:  str    # hex string (with or without 0x prefix)
    up_token_id:   str    # decimal string (from MarketInfo)
    down_token_id: str
    market_slug:   str
    close_ts:      float  # unix timestamp of window close
    queued_at:     float = field(default_factory=time.time)
    attempts:      int   = 0


# ── Main class ─────────────────────────────────────────────────────────────────

class PositionRedeemer:
    """
    Background service that redeems winning conditional tokens after settlement.

    Usage in BotOrchestrator:
        self.redeemer = PositionRedeemer()
        # wire up to market watcher:
        self.watcher.on_new_window(self._on_new_window)   # already done
        # in _on_new_window, also queue the previous market:
        self.redeemer.queue(previous_market)
        # in run():
        asyncio.create_task(self.redeemer.run())
    """

    SETTLEMENT_DELAY = 300   # seconds after close_ts before first redeem attempt
    MAX_ATTEMPTS     = 5     # give up after this many retries
    RETRY_DELAY      = 120   # seconds between retries when not yet settled

    def __init__(self):
        self._sig_type  = config.POLY_SIGNATURE_TYPE
        self._funder    = config.POLY_FUNDER_ADDRESS or ""
        self._key       = config.POLY_PRIVATE_KEY or ""
        self._w3        = None
        self._ctf       = None
        self._safe      = None
        self._queue: list[RedeemTask] = []
        self._done:  set[str]         = set()
        self._running = False

    # ── Public ────────────────────────────────────────────────────────────────

    def queue(self, market) -> None:
        """
        Enqueue a market window for post-settlement redemption.
        Call this when a market window closes (on_new_window fires for the NEXT window).
        """
        cid = getattr(market, "condition_id", "") or ""
        if not cid or cid in self._done:
            return
        # Avoid duplicate entries
        existing = {t.condition_id for t in self._queue}
        if cid in existing:
            return
        task = RedeemTask(
            condition_id  = cid,
            up_token_id   = market.up_token_id,
            down_token_id = market.down_token_id,
            market_slug   = market.slug,
            close_ts      = float(market.window_close_ts),
        )
        self._queue.append(task)
        log.debug("Queued for redeem: %s (close_ts=%d)", market.slug, market.window_close_ts)

    async def run(self) -> None:
        """Background loop — polls every 30 s."""
        self._running = True
        log.info("PositionRedeemer started (sig_type=%d)", self._sig_type)

        while self._running:
            try:
                self._ensure_connected()
                if self._w3:
                    await self._process_queue()
            except Exception as e:
                log.error("PositionRedeemer loop error: %s", e)
            await asyncio.sleep(30)

    def stop(self) -> None:
        self._running = False

    # ── Connection ────────────────────────────────────────────────────────────

    def _ensure_connected(self) -> None:
        if self._w3 and self._w3.is_connected():
            return
        try:
            from web3 import Web3
        except ImportError:
            log.error("web3 not installed — run: pip install web3")
            return

        for url in POLYGON_RPCS:
            try:
                w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 12}))
                if w3.is_connected():
                    self._w3  = w3
                    self._ctf = w3.eth.contract(
                        address=w3.to_checksum_address(CTF_ADDRESS),
                        abi=CTF_ABI,
                    )
                    if self._sig_type == 2 and self._funder:
                        self._safe = w3.eth.contract(
                            address=w3.to_checksum_address(self._funder),
                            abi=SAFE_ABI,
                        )
                    log.info("PositionRedeemer connected: %s", url)
                    return
            except Exception:
                pass
        log.warning("PositionRedeemer: no Polygon RPC reachable — will retry")

    # ── Queue processing ──────────────────────────────────────────────────────

    async def _process_queue(self) -> None:
        now   = time.time()
        ready = [
            t for t in self._queue
            if now >= t.close_ts + self.SETTLEMENT_DELAY
            and t.condition_id not in self._done
        ]
        for task in ready:
            self._queue.remove(task)
            task.attempts += 1
            try:
                result = await asyncio.get_event_loop().run_in_executor(
                    None, self._attempt_redeem, task
                )
            except Exception as e:
                log.error("Redeem error %s: %s", task.market_slug, e)
                result = "error"

            if result == "redeemed":
                log.info("✓ Redeemed: %s", task.market_slug)
                self._done.add(task.condition_id)
            elif result == "no_balance":
                log.info("No redeemable balance: %s", task.market_slug)
                self._done.add(task.condition_id)
            elif result == "not_settled":
                if task.attempts < self.MAX_ATTEMPTS:
                    # Delay next attempt by scheduling back into the queue
                    task.close_ts = now - self.SETTLEMENT_DELAY + self.RETRY_DELAY
                    self._queue.append(task)
                    log.info("Market not settled yet — retry %d/%d: %s",
                             task.attempts, self.MAX_ATTEMPTS, task.market_slug)
                else:
                    log.warning("Gave up on redeem after %d attempts: %s",
                                task.attempts, task.market_slug)
                    self._done.add(task.condition_id)

    # ── Redemption ────────────────────────────────────────────────────────────

    def _attempt_redeem(self, task: RedeemTask) -> str:
        """
        Returns:
          "redeemed"    — tx sent and confirmed
          "no_balance"  — holder has no tokens (nothing to redeem)
          "not_settled" — payoutDenominator == 0, market not resolved on-chain yet
          "error"       — tx failed
        """
        cid_bytes = _parse_bytes32(task.condition_id)

        # Check on-chain settlement
        try:
            denom = self._ctf.functions.payoutDenominator(cid_bytes).call()
        except Exception as e:
            log.warning("payoutDenominator call failed: %s", e)
            return "not_settled"

        if denom == 0:
            return "not_settled"

        # Determine holder address (who owns the tokens)
        from web3 import Web3
        if self._sig_type in (1, 2) and self._funder:
            holder = Web3.to_checksum_address(self._funder)
        else:
            holder = self._eoa_address()

        # Check balances and redeem each non-zero token
        # Binary market: UP → indexSet=1 (outcome 0), DOWN → indexSet=2 (outcome 1)
        redeemed_any = False
        for token_id_str, index_set, label in [
            (task.up_token_id,   1, "UP"),
            (task.down_token_id, 2, "DOWN"),
        ]:
            try:
                balance = self._ctf.functions.balanceOf(holder, int(token_id_str)).call()
            except Exception as e:
                log.warning("balanceOf(%s) failed: %s", label, e)
                continue

            if balance == 0:
                continue

            log.info("%s %s: balance=%d — redeeming (indexSet=%d)",
                     task.market_slug, label, balance, index_set)
            ok = self._send_redeem(cid_bytes, [index_set])
            if ok:
                redeemed_any = True
            else:
                log.error("Redeem tx failed for %s %s", task.market_slug, label)
                return "error"

        return "redeemed" if redeemed_any else "no_balance"

    def _send_redeem(self, cid_bytes: bytes, index_sets: list[int]) -> bool:
        """Route to the correct signing path."""
        try:
            if self._sig_type == 2:
                return self._redeem_gnosis_safe(cid_bytes, index_sets)
            else:
                return self._redeem_eoa(cid_bytes, index_sets)
        except Exception as e:
            log.error("_send_redeem error: %s", e)
            return False

    # ── sig_type 0/1 — EOA direct ─────────────────────────────────────────────

    def _redeem_eoa(self, cid_bytes: bytes, index_sets: list[int]) -> bool:
        """
        EOA calls redeemPositions directly.

        For sig_type=0: tokens must be in the EOA's address.
        For sig_type=1: tokens are in the proxy; the EOA is the operator.
            The ConditionalTokens contract's redeemPositions redeems from
            msg.sender (the EOA), so this path works only if the EOA holds
            tokens directly.  Use sig_type=2 (Gnosis Safe) if the proxy is
            a Gnosis Safe that holds the tokens.
        """
        from eth_account import Account
        from web3 import Web3

        w3   = self._w3
        acct = Account.from_key(self._key)

        txn = self._ctf.functions.redeemPositions(
            Web3.to_checksum_address(USDC_E_ADDRESS),
            ZERO_BYTES32,
            cid_bytes,
            index_sets,
        ).build_transaction({
            "from":     acct.address,
            "nonce":    w3.eth.get_transaction_count(acct.address),
            "gas":      300_000,
            "gasPrice": _gas_price(w3),
        })
        signed   = acct.sign_transaction(txn)
        tx_hash  = w3.eth.send_raw_transaction(signed.raw_transaction)
        log.info("EOA redeem tx: 0x%s", tx_hash.hex())
        receipt  = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        ok       = receipt["status"] == 1
        log.info("EOA redeem %s  tx=0x%s", "✓" if ok else "✗ REVERTED", tx_hash.hex())
        return ok

    # ── sig_type 2 — Gnosis Safe ───────────────────────────────────────────────

    def _redeem_gnosis_safe(self, cid_bytes: bytes, index_sets: list[int]) -> bool:
        """
        Execute redeemPositions through a Gnosis Safe (threshold = 1 assumed).

        EIP-712 signing flow:
          1.  Encode redeemPositions calldata (target = CTF contract).
          2.  Fetch Safe.nonce() to make the tx hash unique.
          3.  Call Safe.getTransactionHash() — returns the EIP-712 bytes32 hash
              the Safe will verify on-chain (already includes domain separator).
          4.  Sign the raw bytes32 hash with the EOA private key via eth_keys.
              Do NOT add an EIP-191 prefix — the Safe uses raw ecrecover.
              Signature format: r(32 bytes) + s(32 bytes) + v(1 byte, 27 or 28).
          5.  Call Safe.execTransaction() from the EOA (pays gas in MATIC).

        Safe signature encoding (from GnosisSafe.sol):
          - v == 27 or 28 → standard ECDSA on raw hash  ← we use this
          - v == 31 or 32 → EIP-191 prefixed signature (subtract 4 from v internally)
          - v >= 2        → contract/EOA approval (different scheme)
        """
        from eth_account import Account
        from eth_keys import keys as eth_keys
        from web3 import Web3

        w3   = self._w3
        safe = self._safe
        acct = Account.from_key(self._key)

        if safe is None:
            log.error("Gnosis Safe contract not initialised — "
                      "set POLY_FUNDER_ADDRESS to the Safe address")
            return False

        # 1. Encode CTF.redeemPositions calldata
        calldata: bytes = self._ctf.encode_abi(
            "redeemPositions",
            args=[
                Web3.to_checksum_address(USDC_E_ADDRESS),
                ZERO_BYTES32,
                cid_bytes,
                index_sets,
            ],
        )

        # 2. Safe transaction parameters
        to              = Web3.to_checksum_address(CTF_ADDRESS)
        value           = 0
        operation       = 0    # CALL (not DELEGATECALL = 1)
        safe_tx_gas     = 0    # let the executor decide; Safe forwards remaining gas
        base_gas        = 0
        gas_price_safe  = 0
        gas_token       = "0x0000000000000000000000000000000000000000"
        refund_receiver = "0x0000000000000000000000000000000000000000"
        nonce           = safe.functions.nonce().call()

        # 3. Get the EIP-712 hash the Safe will verify (includes domain separator)
        safe_tx_hash: bytes = safe.functions.getTransactionHash(
            to, value, calldata, operation,
            safe_tx_gas, base_gas, gas_price_safe,
            gas_token, refund_receiver, nonce,
        ).call()

        # 4. Sign the raw hash WITHOUT EIP-191 prefix (Safe uses raw ecrecover)
        raw_key = bytes.fromhex(self._key.removeprefix("0x"))
        pkey    = eth_keys.PrivateKey(raw_key)
        sig_obj = pkey.sign_msg_hash(safe_tx_hash)
        # eth_keys returns v as 0 or 1; Gnosis Safe expects 27 or 28
        v_byte  = bytes([sig_obj.v + 27])
        r_bytes = sig_obj.r.to_bytes(32, "big")
        s_bytes = sig_obj.s.to_bytes(32, "big")
        packed_sig: bytes = r_bytes + s_bytes + v_byte   # 65 bytes total

        # 5. Execute through the Safe (EOA pays MATIC gas)
        exec_txn = safe.functions.execTransaction(
            to, value, calldata, operation,
            safe_tx_gas, base_gas, gas_price_safe,
            gas_token, refund_receiver,
            packed_sig,
        ).build_transaction({
            "from":     acct.address,
            "nonce":    w3.eth.get_transaction_count(acct.address),
            "gas":      500_000,
            "gasPrice": _gas_price(w3),
        })
        signed_tx  = acct.sign_transaction(exec_txn)
        tx_hash    = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        log.info("Safe redeem tx: 0x%s (nonce=%d)", tx_hash.hex(), nonce)
        receipt    = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        ok         = receipt["status"] == 1
        log.info("Safe redeem %s  tx=0x%s", "✓" if ok else "✗ REVERTED", tx_hash.hex())
        return ok

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _eoa_address(self) -> str:
        from eth_account import Account
        return Account.from_key(self._key).address


# ── Module helpers ─────────────────────────────────────────────────────────────

def _parse_bytes32(hex_str: str) -> bytes:
    """Convert a hex condition ID (with or without 0x) to a 32-byte value."""
    h = hex_str.removeprefix("0x")
    return bytes.fromhex(h.zfill(64))


def _gas_price(w3) -> int:
    """Return gas price with a 20 % tip for faster Polygon confirmation."""
    base = w3.eth.gas_price
    return int(base * 1.2)
