"""
Diagnostik signing order secara lokal.
Jalankan: python debug_sign.py

Script ini TIDAK mengirim order ke API — hanya memverifikasi bahwa
EIP-712 signature yang dibuat lokal valid (signer bisa di-recover).
"""

import os
import json
from dotenv import load_dotenv

load_dotenv()

KEY   = os.getenv("POLY_PRIVATE_KEY", "")
FUND  = os.getenv("POLY_FUNDER_ADDRESS", "")
AKEY  = os.getenv("POLY_API_KEY", "")
SIG_T = int(os.getenv("POLY_SIGNATURE_TYPE", "1"))

if not KEY or not FUND:
    raise SystemExit("ERROR: POLY_PRIVATE_KEY / POLY_FUNDER_ADDRESS belum diisi di .env")

from eth_account import Account
from eth_utils import keccak, to_checksum_address

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs
from py_clob_client.config import get_contract_config

# ── 1. Verifikasi dasar ────────────────────────────────────────────────────
eoa = Account.from_key(KEY).address
fund_cs = to_checksum_address(FUND)

print("=" * 60)
print("DIAGNOSTIK ORDER SIGNING")
print("=" * 60)
print(f"  EOA (dari private key)  : {eoa}")
print(f"  Funder (POLY_FUNDER_ADDRESS) : {fund_cs}")
print(f"  Signature type          : {SIG_T}")
print(f"  API Key prefix          : {AKEY[:8]}..." if AKEY else "  API Key: NOT SET")
print()

if SIG_T == 1:
    if eoa.lower() == fund_cs.lower():
        print("⚠️  MASALAH: EOA == Funder!")
        print("   Untuk sig_type=1 (proxy), POLY_FUNDER_ADDRESS harus berisi")
        print("   alamat PROXY CONTRACT dari polymarket.com/profile, BUKAN EOA.")
    else:
        print("✓  EOA != Funder (benar untuk sig_type=1)")
elif SIG_T == 0:
    if eoa.lower() != fund_cs.lower():
        print("⚠️  MASALAH: Untuk sig_type=0 (EOA), POLY_FUNDER_ADDRESS harus")
        print("   sama dengan EOA address (derived dari POLY_PRIVATE_KEY).")
    else:
        print("✓  EOA == Funder (benar untuk sig_type=0)")

# ── 2. Buat order dummy dan verifikasi signature lokal ─────────────────────
print()
print("Membuat order dummy untuk verifikasi signing...")

# Token BTC YES 5m (dummy — tidak dikirim ke API)
DUMMY_TOKEN = "20237547420640173970854278910041369523140337546333913520867861759044515484018"

from py_clob_client.order_builder.builder import OrderBuilder
from py_clob_client.signer import Signer as ClobSigner
from py_order_utils.signer import Signer as UtilsSigner
from py_order_utils.builders.order_builder import OrderBuilder as UtilsOrderBuilder
from py_order_utils.model.order import OrderData

# Ambil contract config (neg_risk=True untuk market BTC binary)
for neg_risk_val in [False, True]:
    cfg = get_contract_config(137, neg_risk_val)
    label = "NEG_RISK" if neg_risk_val else "NORMAL"

    signer = UtilsSigner(key=KEY)

    maker = to_checksum_address(FUND)
    sig_signer = signer.address()

    data = OrderData(
        maker=maker,
        taker="0x0000000000000000000000000000000000000000",
        tokenId=DUMMY_TOKEN,
        makerAmount="1000000",   # 1 USDC.e (6 decimals)
        takerAmount="2000000",
        side="0",               # BUY
        feeRateBps="0",
        nonce="0",
        signer=sig_signer,
        expiration="0",
        signatureType=SIG_T,
    )

    builder = UtilsOrderBuilder(
        cfg.exchange,
        137,
        signer,
    )

    try:
        signed = builder.build_signed_order(data)
        order_dict = signed.dict()
        sig = order_dict["signature"]

        # Recover signer dari signature
        struct_hash = builder._create_struct_hash(signed.order)
        recovered = Account._recover_hash(struct_hash, signature=bytes.fromhex(sig[2:]))

        sig_ok = recovered.lower() == eoa.lower()
        print(f"\n  [{label}] Exchange: {cfg.exchange}")
        print(f"    maker (funder)  : {order_dict['maker']}")
        print(f"    signer (EOA)    : {order_dict['signer']}")
        print(f"    signatureType   : {order_dict['signatureType']}")
        print(f"    signature[:16]  : {sig[:18]}...")
        print(f"    Recovered signer: {recovered}")
        print(f"    Signature valid : {'✓ OK' if sig_ok else '✗ MISMATCH — private key tidak sesuai!'}")
        if not sig_ok:
            print(f"    Expected EOA    : {eoa}")

    except Exception as e:
        print(f"\n  [{label}] ERROR: {e}")

# ── 3. Cek konsistensi API key ─────────────────────────────────────────────
print()
print("=" * 60)
if AKEY:
    print("Untuk memverifikasi API key cocok dengan proxy ini, jalankan:")
    print("  python generate_creds.py")
    print("Jika API Key berbeda dengan yang di .env, update .env!")
else:
    print("⚠️  POLY_API_KEY tidak diset — jalankan python generate_creds.py")
print("=" * 60)
