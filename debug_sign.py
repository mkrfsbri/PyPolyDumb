"""
Diagnostik EIP-712 signing + on-chain operator check.
Jalankan: python debug_sign.py

Bagian 1: verifikasi lokal (tidak POST ke API)
Bagian 2: cek on-chain apakah EOA terdaftar sebagai operator untuk proxy
Bagian 3: intercept POST body untuk melihat persis apa yang dikirim
"""

import json
import os
from dotenv import load_dotenv

load_dotenv()

KEY   = os.getenv("POLY_PRIVATE_KEY", "")
FUND  = os.getenv("POLY_FUNDER_ADDRESS", "")
AKEY  = os.getenv("POLY_API_KEY", "")
ASEC  = os.getenv("POLY_API_SECRET", "")
APASS = os.getenv("POLY_API_PASSPHRASE", "")
SIG_T = int(os.getenv("POLY_SIGNATURE_TYPE", "1"))

if not KEY or not FUND:
    raise SystemExit("ERROR: POLY_PRIVATE_KEY / POLY_FUNDER_ADDRESS belum diisi di .env")

from eth_account import Account
from eth_utils import to_checksum_address

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, CreateOrderOptions, OrderArgs, OrderType
from py_clob_client.config import get_contract_config
from py_order_utils.builders.order_builder import OrderBuilder as UtilsOrderBuilder
from py_order_utils.signer import Signer as UtilsSigner

eoa     = Account.from_key(KEY).address
fund_cs = to_checksum_address(FUND)

print("=" * 65)
print("DIAGNOSTIK ORDER SIGNING + ON-CHAIN OPERATOR CHECK")
print("=" * 65)
print(f"  EOA   : {eoa}")
print(f"  Funder: {fund_cs}")
print(f"  SigType: {SIG_T}")

# Token dari log terakhir yang gagal
TOKEN_ID = "38112870951064265909502943213812026902920948580072601239040959889927748868" \
           "62".replace(" ", "")
PRICE    = 0.50
SIZE     = 5.0
SIDE     = "BUY"

# ── Bagian 1: verifikasi EIP-712 lokal ───────────────────────────────────
print()
print("── Bagian 1: EIP-712 local signing ──────────────────────────────")

client_l1 = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=137,
    key=KEY,
    signature_type=SIG_T,
    funder=FUND,
)

for neg_risk_val in [False, True]:
    label = "neg_risk=True " if neg_risk_val else "neg_risk=False"
    try:
        tick_size = client_l1.get_tick_size(TOKEN_ID)
        cfg       = get_contract_config(137, neg_risk_val)
        utils_sig = UtilsSigner(key=KEY)
        builder   = UtilsOrderBuilder(cfg.exchange, 137, utils_sig)

        from py_order_utils.model.sides import BUY as UBUY
        from py_order_utils.model.order import OrderData

        # Approximate amounts — just to test signing
        raw_maker = int(round(5.0 * 0.50 * 1e6))   # 5 shares × $0.50 in microUSDC
        raw_taker = int(round(5.0 * 1e6))

        data = OrderData(
            maker=fund_cs,
            taker="0x0000000000000000000000000000000000000000",
            tokenId=TOKEN_ID,
            makerAmount=str(raw_maker),
            takerAmount=str(raw_taker),
            side=UBUY,
            feeRateBps="0",
            nonce="0",
            signer=eoa,
            expiration="0",
            signatureType=SIG_T,
        )

        signed    = builder.build_signed_order(data)
        od        = signed.dict()
        sig       = od["signature"]
        struct_h  = builder._create_struct_hash(signed.order)
        recovered = Account._recover_hash(struct_h, signature=bytes.fromhex(sig[2:]))
        ok        = recovered.lower() == eoa.lower()

        print(f"  [{label}] exchange={cfg.exchange[:10]}... "
              f"sig_ok={'✓' if ok else '✗'}  recovered={recovered[:10]}...")
    except Exception as e:
        print(f"  [{label}] ERROR: {e}")

# ── Bagian 2: on-chain operator check ────────────────────────────────────
print()
print("── Bagian 2: on-chain operator check (Polygon RPC) ──────────────")

OPERATOR_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "operator",  "type": "address"},
            {"internalType": "address", "name": "tokenOwner","type": "address"},
        ],
        "name": "isOperator",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    }
]

POLYGON_RPC = "https://polygon-rpc.com"
EXCHANGES = {
    "Normal  (0x4bFb...)": "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
    "NegRisk (0xC5d5...)": "0xC5d563A36AE78145C45a50134d48A1215220f80a",
}

try:
    from web3 import Web3
    w3 = Web3(Web3.HTTPProvider(POLYGON_RPC, request_kwargs={"timeout": 10}))
    if not w3.is_connected():
        print("  ✗ Cannot connect to Polygon RPC — skip on-chain check")
    else:
        for name, addr in EXCHANGES.items():
            try:
                contract = w3.eth.contract(
                    address=Web3.to_checksum_address(addr),
                    abi=OPERATOR_ABI,
                )
                result = contract.functions.isOperator(eoa, fund_cs).call()
                status = "✓ AUTHORIZED" if result else "✗ NOT AUTHORIZED"
                print(f"  [{name}] isOperator({eoa[:10]}..., {fund_cs[:10]}...) = {status}")
            except Exception as e:
                print(f"  [{name}] call failed: {e}")
except ImportError:
    print("  web3 not installed — run: pip install web3")

# ── Bagian 3: intercept POST body ────────────────────────────────────────
print()
print("── Bagian 3: intercept POST body (tidak dikirim) ────────────────")

if not all([AKEY, ASEC, APASS]):
    print("  API creds tidak lengkap — skip.")
else:
    import httpx
    _orig_post = httpx.post
    captured   = {}

    def _fake_post(url, **kwargs):
        captured["url"]  = url
        captured["body"] = kwargs.get("data") or kwargs.get("content") or ""
        raise RuntimeError("__INTERCEPTED__")

    httpx.post = _fake_post
    try:
        creds = ApiCreds(api_key=AKEY, api_secret=ASEC, api_passphrase=APASS)
        client_l2 = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=KEY,
            creds=creds,
            signature_type=SIG_T,
            funder=FUND,
        )
        # Use the OFFICIAL library flow (includes __resolve_fee_rate)
        tick_size = client_l2.get_tick_size(TOKEN_ID)
        neg_risk  = client_l2.get_neg_risk(TOKEN_ID)
        order_args = OrderArgs(
            token_id=TOKEN_ID,
            price=PRICE,
            size=SIZE,
            side=SIDE,
            fee_rate_bps=0,
        )
        options = CreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
        signed2 = client_l2.builder.create_order(order_args, options)
        client_l2.post_order(signed2, OrderType.GTC)
    except RuntimeError as e:
        if "__INTERCEPTED__" in str(e):
            body_str = captured.get("body", "")
            try:
                body_obj  = json.loads(body_str)
                ord_body  = body_obj.get("order", {})
                print(f"  owner         : {body_obj.get('owner')}")
                print(f"  maker         : {ord_body.get('maker')}")
                print(f"  signer        : {ord_body.get('signer')}")
                print(f"  tokenId[:20]  : {str(ord_body.get('tokenId',''))[:20]}...")
                print(f"  signatureType : {ord_body.get('signatureType')}")
                print(f"  feeRateBps    : {ord_body.get('feeRateBps')}")
                print(f"  makerAmount   : {ord_body.get('makerAmount')}")
                print(f"  takerAmount   : {ord_body.get('takerAmount')}")
                print(f"  side          : {ord_body.get('side')}")
                print(f"  neg_risk used : {neg_risk}")
                sig_b = ord_body.get("signature", "")
                print(f"  sig[:22]      : {sig_b[:22]}...")
            except Exception:
                print(f"  Raw body: {body_str[:300]}")
        else:
            print(f"  ERROR: {e}")
    except Exception as e:
        print(f"  ERROR (non-intercept): {e}")
    finally:
        httpx.post = _orig_post

# ── Bagian 4: API secret encoding / padding check ────────────────────────
print()
print("── Bagian 4: API secret encoding / padding check ────────────────")

if not ASEC:
    print("  POLY_API_SECRET tidak ada — skip.")
else:
    import base64 as _b64
    import binascii as _binascii
    import hmac as _hmaclib
    import hashlib as _hashlib

    # ── 4a. Padding ───────────────────────────────────────────────────
    # Python's urlsafe_b64decode does NOT auto-add padding; it raises
    # binascii.Error: Incorrect padding if len(secret) % 4 != 0.
    pad_needed = (4 - len(ASEC) % 4) % 4
    padded     = ASEC + "=" * pad_needed
    print(f"  Secret length   : {len(ASEC)} chars  (pad needed: {pad_needed})")

    try:
        decoded_url = _b64.urlsafe_b64decode(padded)
        print(f"  urlsafe_b64decode: OK → {len(decoded_url)} bytes")
    except _binascii.Error as e:
        print(f"  urlsafe_b64decode: FAIL → {e}  (secret may not be base64url)")
        decoded_url = None

    # ── 4b. Standard vs URL-safe charset ─────────────────────────────
    # If the secret contains '+' or '/' it was encoded with standard base64,
    # not URL-safe.  urlsafe_b64decode will misinterpret those bytes.
    has_std_chars = any(c in ASEC for c in ("+", "/"))
    has_url_chars = any(c in ASEC for c in ("-", "_"))
    if has_std_chars:
        print(f"  WARNING: secret contains '+' or '/' — standard base64 charset!")
        print(f"           urlsafe_b64decode will decode these INCORRECTLY.")
        print(f"           The HMAC key will be WRONG → server rejects sig.")
        try:
            decoded_std = _b64.b64decode(padded)
            print(f"  Standard b64decode: OK → {len(decoded_std)} bytes")
            if decoded_url and decoded_url != decoded_std:
                print(f"  Keys differ: urlsafe vs standard → {len(decoded_url)}B vs {len(decoded_std)}B")
        except Exception as e2:
            print(f"  Standard b64decode: FAIL → {e2}")
    else:
        print(f"  Charset OK: no '+' or '/' found ({'has -/_ chars' if has_url_chars else 'alphanumeric only'})")

    # ── 4c. HMAC round-trip: does the library produce a valid sig? ────
    # Replicate exactly what build_hmac_signature does, then cross-check.
    if decoded_url is not None:
        import time as _time
        ts       = int(_time.time())
        method   = "POST"
        path     = "/order"
        test_body = '{"order":{"salt":"1","maker":"0x0","signer":"0x0","taker":"0x0",' \
                    '"tokenId":"0","makerAmount":"0","takerAmount":"0","expiration":"0",' \
                    '"nonce":"0","feeRateBps":"0","side":"BUY","signatureType":1},' \
                    '"owner":"test","orderType":"GTC","postOnly":false}'

        # Library path: urlsafe_b64decode WITHOUT padding fix
        try:
            key_lib  = _b64.urlsafe_b64decode(ASEC)   # ← exactly what the library does
            msg      = str(ts) + method + path + test_body
            sig_lib  = _b64.urlsafe_b64encode(
                _hmaclib.new(key_lib, msg.encode("utf-8"), _hashlib.sha256).digest()
            ).decode()
            print(f"  Library HMAC (no padding fix): {sig_lib[:24]}...")
        except _binascii.Error as e:
            print(f"  Library HMAC ERROR (no padding): {e}")
            print(f"  → This means every POST /order HMAC is throwing an exception!")

        # Corrected path: urlsafe_b64decode WITH padding
        try:
            key_fix  = _b64.urlsafe_b64decode(padded)
            msg      = str(ts) + method + path + test_body
            sig_fix  = _b64.urlsafe_b64encode(
                _hmaclib.new(key_fix, msg.encode("utf-8"), _hashlib.sha256).digest()
            ).decode()
            print(f"  Fixed HMAC  (with padding):    {sig_fix[:24]}...")
        except Exception as e:
            print(f"  Fixed HMAC error: {e}")

    # ── 4d. Summary ──────────────────────────────────────────────────
    print()
    if pad_needed > 0:
        print(f"  !! Secret is missing {pad_needed} padding char(s). "
              f"py_clob_client calls urlsafe_b64decode(secret) WITHOUT")
        print(f"     adding padding first.  If Python raises Incorrect padding,")
        print(f"     the exception propagates and order POST never reaches the server.")
    elif has_std_chars:
        print(f"  !! Standard-base64 chars in secret — HMAC key is wrong!")
    else:
        print(f"  API secret encoding looks correct for urlsafe_b64decode.")
        print(f"  The 400 error is NOT caused by HMAC secret encoding.")
        print(f"  Root cause is the EIP-712 order signature (neg_risk / domain).")

print()
print("=" * 65)
