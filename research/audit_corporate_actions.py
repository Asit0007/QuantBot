"""Scan the whole NSE panel for split/bonus events and unexplained jumps."""
import os, sys
_R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _R); sys.path.insert(0, os.path.join(_R, "research"))
from nse_cross_sectional import load_panel                      # noqa: E402
from markets.adapters.corporate_actions import audit            # noqa: E402

p = load_panel("2015-01-01", "2026-09-11")
a = audit(p)
print(f"panel {len(p):,} rows, {p['symbol'].nunique():,} symbols")
if a.empty:
    print("no events"); raise SystemExit
print(f"\nevents detected: {len(a):,} across {a['symbol'].nunique():,} symbols")
print(a["kind"].value_counts().to_string())
print("\nmost common clean ratios:")
print(a[a.kind == "split"]["ratio"].round(3).value_counts().head(10).to_string())
u = a[a.kind == "unexplained"]
print(f"\nunexplained (NOT adjusted, need a human): {len(u)}")
print(u.head(10).to_string(index=False))
