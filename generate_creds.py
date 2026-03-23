"""
Jalankan sekali untuk men-derive API credentials dan diagnosa koneksi Polymarket.

Usage:
    python generate_creds.py
"""

import os
from dotenv import load_dotenv

load_dotenv()

PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY", "")
FUNDER = os.getenv("POLY_FUNDER_ADDRESS", "")
SIG_TYPE = int(os.getenv("POLY_SIGNATURE_TYPE", "1"))
API_KEY = os.getenv("POLY_API_KEY", "")
API_SECRET = os.getenv("POLY_API_SECRET", "")
API_PASSPHRASE = os.getenv("POLY_API_PASSPHRASE", "")

if not PRIVATE_KEY:
    raise SystemExit("ERROR: POLY_PRIVATE_KEY belum diisi di .env")
if not FUNDER:
    raise SystemExit("ERROR: POLY_FUNDER_ADDRESS belum diisi di .env")

from eth_account import Account
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType

# ── 1. Tampilkan EOA address dari private key ──────────────────────────────
eoa = Account.from_key(PRIVATE_KEY).address
print("=" * 60)
print("DIAGNOSA POLYMARKET CREDENTIALS")
print("=" * 60)
print(f"  EOA address (dari private key) : {eoa}")
print(f"  Funder/Proxy address           : {FUNDER}")
print(f"  Signature type                 : {SIG_TYPE} ({'EOA' if SIG_TYPE == 0 else 'Proxy' if SIG_TYPE == 1 else 'Gnosis'})")
print()
print("PENTING: EOA di atas harus merupakan wallet yang sama")
print("yang kamu pakai login ke polymarket.com untuk mendapatkan proxy address tsb.")
print()

# ── 2. Level 1 client untuk derive creds ──────────────────────────────────
client_l1 = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=137,
    key=PRIVATE_KEY,
    signature_type=SIG_TYPE,
    funder=FUNDER,
)

print("[1/3] Derive API credentials...")
creds = client_l1.create_or_derive_api_creds()
if creds is None:
    raise SystemExit("ERROR: Gagal derive credentials.")

print(f"  API Key: {creds.api_key[:8]}...")
print()

# ── 3. Level 2 client untuk cek balance allowance ─────────────────────────
print("[2/3] Cek & sync balance allowance (USDC + Conditional tokens)...")
client_l2 = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=137,
    key=PRIVATE_KEY,
    creds=creds,
    signature_type=SIG_TYPE,
    funder=FUNDER,
)

try:
    r = client_l2.update_balance_allowance(
        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=SIG_TYPE)
    )
    print(f"  COLLATERAL (USDC)  : {r}")
except Exception as e:
    print(f"  COLLATERAL error   : {e}")

try:
    r = client_l2.update_balance_allowance(
        BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, signature_type=SIG_TYPE)
    )
    print(f"  CONDITIONAL tokens : {r}")
except Exception as e:
    print(f"  CONDITIONAL error  : {e}")

print()

# ── 4. Cek API keys yang terdaftar ────────────────────────────────────────
print("[3/3] API keys terdaftar untuk EOA ini:")
try:
    keys = client_l2.get_api_keys()
    if isinstance(keys, list):
        for k in keys:
            print(f"  - {k}")
    else:
        print(f"  {keys}")
except Exception as e:
    print(f"  ERROR: {e}")

print()
print("=" * 60)
print("Paste baris berikut ke .env kamu:")
print("=" * 60)
print(f"POLY_API_KEY={creds.api_key}")
print(f"POLY_API_SECRET={creds.api_secret}")
print(f"POLY_API_PASSPHRASE={creds.api_passphrase}")
print("=" * 60)
