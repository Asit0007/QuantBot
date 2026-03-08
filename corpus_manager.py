"""
corpus_manager.py — QuantBot Corpus Management Module

Standalone module that can be imported by bot.py OR called manually
to refresh corpus at any time (e.g. on the 10th of each month).

Usage from bot.py:
    from corpus_manager import CorpusManager
    cm = CorpusManager(initial_balance=100, base_monthly_dca=10)
    cm.on_trade_complete(pnl, is_loss)
    cm.on_monthly_refresh(current_balance, year_number)

Usage as standalone script (manual refresh):
    python corpus_manager.py --balance 1250.50 --year 2026
"""

from datetime import datetime
import json
import os
from dotenv import load_dotenv
load_dotenv()

_DATA_DIR = os.getenv("DATA_DIR", ".")
os.makedirs(_DATA_DIR, exist_ok=True)
CORPUS_STATE_FILE = os.path.join(_DATA_DIR, "corpus_state.json")


class CorpusManager:
    """
    Manages corpus sizing with:
      - Ratchet UP:   After every 10 completed trades with net gain,
                      corpus = current balance (locks in profits)
      - Ratchet DOWN: After 10 consecutive losses, corpus shrinks to
                      current balance (stops throwing good money after bad)
      - Monthly DCA:  Adds monthly contribution to balance + corpus
                      on the 10th of each month
      - Annual DCA increment: Base monthly amount grows 10% each January
    """

    def __init__(self,
                 initial_balance:  float = 100.0,
                 base_monthly_dca: float = 10.0,
                 dca_annual_growth: float = 0.10,
                 ratchet_up_every:  int   = 10,
                 ratchet_down_after: int  = 10):

        self.corpus             = initial_balance
        self.peak_corpus        = initial_balance
        self.base_monthly_dca   = base_monthly_dca
        self.dca_annual_growth  = dca_annual_growth
        self.ratchet_up_every   = ratchet_up_every
        self.ratchet_down_after = ratchet_down_after

        # Counters
        self.trade_count        = 0     # total trades since last ratchet up
        self.consecutive_losses = 0
        self.net_since_ratchet  = 0.0   # cumulative PnL since last ratchet

        # DCA tracking
        self.last_dca_month     = -1
        self.total_dca_added    = 0.0

        # Log
        self.events             = []

    def get_monthly_dca(self, year: int, start_year: int = 2019) -> float:
        """Returns monthly DCA amount for a given year with 10% annual compounding."""
        years_elapsed = max(0, year - start_year)
        return round(self.base_monthly_dca * ((1 + self.dca_annual_growth) ** years_elapsed), 2)

    def on_trade_complete(self, pnl: float, balance: float) -> dict:
        """
        Call after every completed trade.
        Returns dict with action taken and new corpus value.
        """
        self.trade_count        += 1
        self.net_since_ratchet  += pnl

        if pnl > 0:
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1

        action = "none"

        # Ratchet DOWN: 10 consecutive losses → shrink corpus to protect capital
        if self.consecutive_losses >= self.ratchet_down_after:
            old = self.corpus
            self.corpus             = balance   # reset to current (lower) balance
            self.consecutive_losses = 0
            self.trade_count        = 0
            self.net_since_ratchet  = 0.0
            action = f"ratchet_down: {old:.2f} → {self.corpus:.2f}"
            self.events.append({'type': 'ratchet_down', 'from': old, 'to': self.corpus})

        # Ratchet UP: every 10 trades with net positive PnL
        elif (self.trade_count >= self.ratchet_up_every and
              self.net_since_ratchet > 0 and
              balance > self.corpus):
            old = self.corpus
            self.corpus            = balance
            self.peak_corpus       = max(self.peak_corpus, self.corpus)
            self.trade_count       = 0
            self.net_since_ratchet = 0.0
            action = f"ratchet_up: {old:.2f} → {self.corpus:.2f}"
            self.events.append({'type': 'ratchet_up', 'from': old, 'to': self.corpus})

        elif self.trade_count >= self.ratchet_up_every:
            # Completed 10 trades but net negative — reset counter, keep corpus
            self.trade_count       = 0
            self.net_since_ratchet = 0.0
            action = "cycle_reset_no_ratchet"

        return {
            'corpus':             self.corpus,
            'consecutive_losses': self.consecutive_losses,
            'action':             action,
        }

    def on_monthly_refresh(self, balance: float,
                           year: int,
                           month: int,
                           start_year: int = 2019) -> dict:
        """
        Call on the 10th of each month.
        Adds DCA contribution to balance and corpus.
        Returns new balance and contribution amount.
        """
        if month == self.last_dca_month:
            return {'contribution': 0, 'new_balance': balance, 'total_dca': self.total_dca_added}

        contribution           = self.get_monthly_dca(year, start_year)
        self.last_dca_month    = month
        self.total_dca_added  += contribution

        # Add to corpus too — new money should be deployable immediately
        self.corpus           += contribution
        self.peak_corpus       = max(self.peak_corpus, self.corpus)

        new_balance = balance + contribution
        self.events.append({
            'type': 'dca', 'year': year, 'month': month,
            'amount': contribution, 'total': self.total_dca_added
        })

        return {
            'contribution': contribution,
            'new_balance':  new_balance,
            'corpus':       self.corpus,
            'total_dca':    self.total_dca_added,
        }

    def save_state(self, filepath: str = CORPUS_STATE_FILE):
        """Save corpus state to JSON — used by bot.py to persist between restarts."""
        state = {
            'corpus':             self.corpus,
            'peak_corpus':        self.peak_corpus,
            'trade_count':        self.trade_count,
            'consecutive_losses': self.consecutive_losses,
            'net_since_ratchet':  self.net_since_ratchet,
            'last_dca_month':     self.last_dca_month,
            'total_dca_added':    self.total_dca_added,
            'saved_at':           datetime.now().isoformat(),
        }
        with open(filepath, 'w') as f:
            json.dump(state, f, indent=2)
        print(f"✅ Corpus state saved → {filepath}")
        print(f"   Corpus: ${self.corpus:.2f} | DCA total: ${self.total_dca_added:.2f}")

    def load_state(self, filepath: str = CORPUS_STATE_FILE) -> bool:
        """Load corpus state from JSON."""
        if not os.path.exists(filepath):
            return False
        with open(filepath) as f:
            state = json.load(f)
        self.corpus             = state['corpus']
        self.peak_corpus        = state['peak_corpus']
        self.trade_count        = state['trade_count']
        self.consecutive_losses = state['consecutive_losses']
        self.net_since_ratchet  = state['net_since_ratchet']
        self.last_dca_month     = state['last_dca_month']
        self.total_dca_added    = state['total_dca_added']
        print(f"✅ Corpus state loaded from {filepath}")
        print(f"   Corpus: ${self.corpus:.2f} | DCA total: ${self.total_dca_added:.2f}")
        return True

    def summary(self) -> str:
        lines = [
            f"  Corpus:              ${self.corpus:.2f}",
            f"  Peak Corpus:         ${self.peak_corpus:.2f}",
            f"  Total DCA Added:     ${self.total_dca_added:.2f}",
            f"  Consecutive Losses:  {self.consecutive_losses}",
            f"  Trades This Cycle:   {self.trade_count}/{self.ratchet_up_every}",
            f"  Net This Cycle:      ${self.net_since_ratchet:+.2f}",
        ]
        return "\n".join(lines)


# ─── Standalone manual refresh ────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Manual corpus refresh tool")
    parser.add_argument('--balance', type=float, required=True,
                        help='Current account balance')
    parser.add_argument('--year',    type=int,
                        default=datetime.now().year,
                        help='Current year (for DCA calculation)')
    parser.add_argument('--month',   type=int,
                        default=datetime.now().month,
                        help='Current month')
    parser.add_argument('--load',    action='store_true',
                        help='Load existing corpus state')
    parser.add_argument('--save',    action='store_true',
                        help='Save corpus state after refresh')
    args = parser.parse_args()

    cm = CorpusManager()
    if args.load:
        cm.load_state()

    result = cm.on_monthly_refresh(args.balance, args.year, args.month)

    print(f"\n📊 Monthly Corpus Refresh")
    print(f"  Balance before:  ${args.balance:.2f}")
    print(f"  DCA added:       ${result['contribution']:.2f}")
    print(f"  Balance after:   ${result['new_balance']:.2f}")
    print(f"  New corpus:      ${result['corpus']:.2f}")
    print(f"  Total DCA ever:  ${result['total_dca']:.2f}")

    if args.save:
        cm.save_state()