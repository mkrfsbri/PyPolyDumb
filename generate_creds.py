"""
Jalankan sekali untuk men-derive API credentials dari Polymarket.

Usage:
    python generate_creds.py

Hasilkan output berupa 3 baris yang bisa langsung di-paste ke .env
"""

import os
from dotenv import load_dotenv

load_dotenv()

PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY", "")
FUNDER = os.getenv("POLY_FUNDER_ADDRESS", "")
SIG_TYPE = int(os.getenv("POLY_SIGNATURE_TYPE", "1"))

if not PRIVATE_KEY:
    raise SystemExit("ERROR: POLY_PRIVATE_KEY belum diisi di .env")
if not FUNDER:
    raise SystemExit("ERROR: POLY_FUNDER_ADDRESS belum diisi di .env")

from py_clob_client.client import ClobClient

# Inisialisasi Level 1 (tanpa API creds) untuk derive
client = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=137,
    key=PRIVATE_KEY,
    signature_type=SIG_TYPE,
    funder=FUNDER,
)

print(f"Derivasi credentials untuk:")
print(f"  EOA address : {client.get_address()}")
print(f"  Funder/Proxy: {FUNDER}")
print(f"  Sig type    : {SIG_TYPE} ({'EOA' if SIG_TYPE == 0 else 'Proxy' if SIG_TYPE == 1 else 'Gnosis'})")
print()

creds = client.create_or_derive_api_creds()

if creds is None:
    raise SystemExit("ERROR: Gagal derive credentials. Pastikan private key dan funder address benar.")

print("Paste baris berikut ke .env kamu:")
print("=" * 50)
print(f"POLY_API_KEY={creds.api_key}")
print(f"POLY_API_SECRET={creds.api_secret}")
print(f"POLY_API_PASSPHRASE={creds.api_passphrase}")
print("=" * 50)
