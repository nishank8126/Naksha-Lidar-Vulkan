# Verify NKPC density matches measured for 1.14B projection sanity
nkpc_bytes_m56 = 9.11 * 1024**3
pts_m56 = 268_809_706
bytes_per_pt = nkpc_bytes_m56 / pts_m56
# 268M pts -> the block stores per-point: xyz(int32*3=12) cls(u8=1) intensity(u16=2) rgb(u16*3=6) sid(u64=8) rn(u8=1) psi(u16=2) = 30 bytes raw
# + block header/directory overhead (small) + the 15.5M overview extra points
print(f"MANDI_56 NKPC bytes/pt = {bytes_per_pt:.1f}")
print(f"  (raw per-pt attrs = 30 bytes: xyz12+cls1+intensity2+rgb6+sid8+rn1+psi2)")
print(f"  (includes {284_330_057 - 268_809_706:,} overview extra pts + block headers)")
total = 1_140_436_759
proj = bytes_per_pt * total
print(f"  1.14B projected NKPC = {proj/1024**3:.1f} GiB ({proj/1e9:.2f} GB)"); print(f"  6 files = {proj/1024**3*6:.1f} GiB")
print(f"  available = 75.27 GiB  -> NKPC-along exceeds disk by {proj/1024**3*6 - 75.27:.1f} GiB")
