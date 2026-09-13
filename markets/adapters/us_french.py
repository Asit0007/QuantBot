"""
markets/adapters/us_french.py — US market returns, free, back to 1926.

The Ken French data library (Dartmouth) publishes daily Fama-French factors
built from CRSP. `Mkt-RF + RF` is the total return of the US equity market.
No API key, no rate limit, no bot wall — the three things that blocked every
other free US source: Yahoo returns 429, Stooq serves a SHA-256 proof-of-work
challenge, Tiingo 403, Alpaca 401.

WHAT THIS IS GOOD FOR, AND WHAT IT IS NOT.

  Good for: anything close-only over a very long history. 100 years is an
  extraordinary sample next to BTC's 6.9 — and sample size is this project's
  binding constraint, not ideas.

  NOT good for: anything needing intraday range. These are RETURNS, so a
  price series can be reconstructed by compounding but there is no genuine
  open/high/low. ATR stops, Parabolic SAR and intrabar liquidation checks all
  need high/low and CANNOT be computed here. Synthesising a high/low from a
  close would be inventing data — the same error as "repairing" a price move
  that was never a split. So this adapter reports close only, and any
  strategy needing range must wait for a real OHLCV feed (Alpaca).

  Also not individual stocks, and not SPX/NDX/DJI specifically — it is the
  whole US market, cap-weighted.
"""
from __future__ import annotations

import io
import os
import zipfile

import pandas as pd
import requests

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE = os.path.join(_ROOT, "data_cache", "us_french")
URL = ("https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
       "F-F_Research_Data_Factors_daily_CSV.zip")
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 Chrome/120.0 Safari/537.36"}


def load_us_market(force: bool = False) -> pd.DataFrame:
    """Daily US total-market return and a compounded price index.

    Columns: ret (decimal), rf (decimal), close (index, base 100).
    """
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "ff_daily.csv")
    if force or not os.path.exists(path):
        r = requests.get(URL, headers=HEADERS, timeout=90)
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            raw = z.read(z.namelist()[0]).decode("latin-1")
        # The file has a prose header and a copyright footer; the data block
        # is the run of lines beginning with an 8-digit date.
        rows = [ln for ln in raw.splitlines()
                if len(ln) > 8 and ln[:8].isdigit()]
        df = pd.read_csv(io.StringIO("date,mkt_rf,smb,hml,rf\n" + "\n".join(rows)))
        df.to_csv(path, index=False)
    else:
        df = pd.read_csv(path)

    df["date"] = pd.to_datetime(df["date"].astype(str), format="%Y%m%d")
    df = df.set_index("date").sort_index()
    # Values are PERCENT in the source file.
    df["ret"] = (df["mkt_rf"] + df["rf"]) / 100.0
    df["rf"] = df["rf"] / 100.0
    df["close"] = 100.0 * (1.0 + df["ret"]).cumprod()
    return df[["ret", "rf", "close"]]
