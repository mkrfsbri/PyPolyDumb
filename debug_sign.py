"""
Diagnostik signing order — menggunakan alur ClobClient yang sama persis dengan bot.
Jalankan: python debug_sign.py

Bagian 1 : verifikasi lokal (tidak POST ke API)
Bagian 2 : intercept POST body untuk melihat persis apa yang dikirim
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
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType, PartialCreateOrderOptions
from py_order_utils.builders.order_builder import OrderBuilder as UtilsOrderBuilder
from py_order_utils.signer import Signer as UtilsSigner

# Token dari log yang gagal terakhir
TOKEN_ID = "38112870951064265909502943213812026902920948580072601239040959889927748868 62".replace(" ", "")
PRICE    = 0.50   # harga dummy
SIZE     = 5.0    # shares (minimum Polymarket)
SIDE     = "BUY"

eoa     = Account.from_key(KEY).address
fund_cs = to_checksum_address(FUND)

print("=" * 65)
print("DIAGNOSTIK ORDER SIGNING — REAL ClobClient FLOW")
print("=" * 65)
print(f"  EOA   : {eoa}")
print(f"  Funder: {fund_cs}")
print(f"  SigType: {SIG_T}")
print(f"  EOA == Funder: {eoa.lower() == fund_cs.lower()} (harus False untuk sig_type=1)")
print()

# ── Level 1: hanya butuh key+funder, tidak perlu API creds ───────────────
client_l1 = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=137,
    key=KEY,
    signature_type=SIG_T,
    funder=FUND,
)

print(f"Token  : {TOKEN_ID[:20]}...")
print(f"Side   : {SIDE}  Price: {PRICE}  Size: {SIZE} shares")
print()

# ── Bagian 1: create_order (tanpa POST) ──────────────────────────────────
print("── Bagian 1: create_order lokal ─────────────────────────────────")
try:
    order_args = OrderArgs(
        token_id=TOKEN_ID,
        price=PRICE,
        size=SIZE,
        side=SIDE,
        fee_rate_bps=0,
    )
    options = PartialCreateOrderOptions(neg_risk=False)
    signed = client_l1.create_order(order_args, options)
    od = signed.dict()

    print(f"  maker          : {od['maker']}")
    print(f"  signer         : {od['signer']}")
    print(f"  tokenId        : {od['tokenId'][:20]}...")
    print(f"  makerAmount    : {od['makerAmount']}")
    print(f"  takerAmount    : {od['takerAmount']}")
    print(f"  feeRateBps     : {od['feeRateBps']}")
    print(f"  side           : {od['side']}")
    print(f"  signatureType  : {od['signatureType']}")
    print(f"  nonce          : {od['nonce']}")
    print(f"  expiration     : {od['expiration']}")
    print(f"  signature[:20] : {od['signature'][:22]}...")
    print()

    # Verifikasi recover signer lokal
    neg_risk = client_l1.get_neg_risk(TOKEN_ID)
    from py_clob_client.config import get_contract_config
    cfg = get_contract_config(137, neg_risk)
    print(f"  neg_risk       : {neg_risk}  → exchange: {cfg.exchange}")

    utils_signer = UtilsSigner(key=KEY)
    tmp_builder  = UtilsOrderBuilder(cfg.exchange, 137, utils_signer)
    struct_hash  = tmp_builder._create_struct_hash(signed.order)
    recovered    = Account._recover_hash(struct_hash,
                       signature=bytes.fromhex(od["signature"][2:]))

    sig_ok = recovered.lower() == eoa.lower()
    print(f"  Recovered EOA  : {recovered}")
    print(f"  Signature valid: {'✓ OK' if sig_ok else '✗ MISMATCH!'}")

except Exception as e:
    print(f"  ERROR saat create_order: {e}")
    import traceback; traceback.print_exc()

# ── Bagian 2: intercept POST body ────────────────────────────────────────
print()
print("── Bagian 2: intercept POST body (tidak benar-benar dikirim) ────")

if not all([AKEY, ASEC, APASS]):
    print("  API creds tidak lengkap — skip intercept.")
else:
    import httpx

    _orig_post = httpx.post

    captured = {}

    def _fake_post(url, **kwargs):
        captured["url"]  = url
        captured["body"] = kwargs.get("data") or kwargs.get("content") or ""
        captured["hdrs"] = {k: v for k, v in (kwargs.get("headers") or {}).items()
                            if k.lower() in ("poly-address", "poly-signature",
                                             "poly-timestamp", "poly-nonce",
                                             "content-type")}
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
        order_args2 = OrderArgs(
            token_id=TOKEN_ID,
            price=PRICE,
            size=SIZE,
            side=SIDE,
            fee_rate_bps=0,
        )
        signed2  = client_l2.create_order(order_args2, PartialCreateOrderOptions(neg_risk=False))
        client_l2.post_order(signed2, OrderType.GTC)
    except RuntimeError as e:
        if "__INTERCEPTED__" in str(e):
            print(f"  URL  : {captured.get('url')}")
            print(f"  Headers: {json.dumps(captured.get('hdrs', {}), indent=4)}")
            body_str = captured.get("body", "")
            try:
                body_obj = json.loads(body_str)
                ord_body = body_obj.get("order", {})
                print(f"  Body.owner          : {body_obj.get('owner')}")
                print(f"  Body.orderType      : {body_obj.get('orderType')}")
                print(f"  Body.order.maker    : {ord_body.get('maker')}")
                print(f"  Body.order.signer   : {ord_body.get('signer')}")
                print(f"  Body.order.sig_type : {ord_body.get('signatureType')}")
                print(f"  Body.order.feeRate  : {ord_body.get('feeRateBps')}")
                print(f"  Body.order.maker_amt: {ord_body.get('makerAmount')}")
                print(f"  Body.order.taker_amt: {ord_body.get('takerAmount')}")
                sig_b = ord_body.get("signature", "")
                print(f"  Body.order.sig[:20] : {sig_b[:22]}...")
            except Exception:
                print(f"  Raw body: {body_str[:500]}")
        else:
            print(f"  ERROR: {e}")
    except Exception as e:
        print(f"  ERROR (non-intercept): {e}")
        import traceback; traceback.print_exc()
    finally:
        httpx.post = _orig_post

print()
print("=" * 65)
