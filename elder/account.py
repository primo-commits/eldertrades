"""
Account diagnostic.

    python -m elder.account

Prints exactly which Alpaca account these API keys belong to, and what that
account actually holds. Use it when the dashboard and the bot disagree about
equity -- the account NUMBER is the decisive check, because API keys are tied
to one specific paper account and it is easy to reset a different one.
"""
from __future__ import annotations

import sys


def main() -> int:
    from . import config as cfgmod
    from .broker import make_trading_client
    from .keys import MissingCredentials, describe_source, load_keys

    cfg = cfgmod.load()
    try:
        key, _ = load_keys()
    except MissingCredentials as e:
        print(e)
        return 1

    try:
        client = make_trading_client(cfg)
        a = client.get_account()
    except Exception as e:
        print(f"Could not reach Alpaca: {e}")
        return 1

    equity = float(a.equity)
    cash = float(a.cash)

    print("=" * 62)
    print("  WHICH ALPACA ACCOUNT ARE THESE KEYS USING?")
    print("=" * 62)
    src = describe_source()
    print(f"  API key ending ....... ...{key[-6:]}")
    print(f"  Key came from ........ {src['winner']}")
    print(f"  Endpoint ............. {'PAPER' if cfg.paper else 'LIVE'}")
    if src["shadowed"]:
        print()
        print( "  !! An environment variable is OVERRIDING alpaca_keys.txt.")
        print(f"     Env vars set: {', '.join(src['env_vars_set'])}")
        print(f"     The file {src['file_path']} is being IGNORED.")
        print( "     Clear the env var, or put the right key in it.")
    print()
    print(f"  ACCOUNT NUMBER ....... {a.account_number}")
    print(f"  Account id ........... {a.id}")
    print("=" * 62)
    print("  WHAT THAT ACCOUNT HOLDS")
    print("=" * 62)
    print(f"  Equity ............... ${equity:,.2f}")
    print(f"  Cash ................. ${cash:,.2f}")
    print(f"  Portfolio value ...... ${float(a.portfolio_value):,.2f}")
    print(f"  Buying power ......... ${float(a.buying_power):,.2f}")
    print(f"  Long market value .... ${float(a.long_market_value or 0):,.2f}")
    print(f"  Short market value ... ${float(a.short_market_value or 0):,.2f}")
    print(f"  Multiplier ........... {a.multiplier}  "
          f"({'margin' if str(a.multiplier) != '1' else 'CASH account - no shorting'})")
    print(f"  Shorting enabled ..... {a.shorting_enabled}")
    print(f"  Status ............... {str(a.status).split('.')[-1]}")
    print(f"  Created .............. {a.created_at}")
    print("=" * 62)
    print("  WHAT THE BOT WILL DO WITH THIS")
    print("=" * 62)
    r = cfg.risk
    print(f"  Sizing uses LIVE equity, not config. At ${equity:,.0f}:")
    print(f"    risk per trade ..... ${equity * r.risk_per_trade_pct:,.2f}"
          f"   ({r.risk_per_trade_pct:.2%})")
    print(f"    daily loss limit ... ${equity * r.daily_loss_limit_pct:,.2f}"
          f"   ({r.daily_loss_limit_pct:.2%})")
    print(f"    max per position ... ${equity * r.max_position_notional_pct:,.2f}"
          f"   ({r.max_position_notional_pct:.2%})")
    print(f"  config.yaml nominal_equity is ${cfg.nominal_equity:,.0f} "
          f"(documentation only -- it does not size anything)")
    print("=" * 62)

    if abs(equity - cfg.nominal_equity) / max(cfg.nominal_equity, 1) > 0.1:
        print()
        print("  MISMATCH: live equity does not match your configured nominal.")
        print()
        print("  Check these in order:")
        print()
        print(f"   1. Open the Alpaca PAPER dashboard and find the account")
        print(f"      number shown there. Does it match {a.account_number}?")
        print(f"      If NOT, you reset a different paper account than the one")
        print(f"      these API keys belong to. Alpaca allows several paper")
        print(f"      accounts and each has its own keys. Either generate keys")
        print(f"      from the account you reset, or reset this one.")
        print()
        print( "   2. If the number DOES match, the reset did not apply the")
        print( "      amount. In the dashboard, reset again and type the")
        print( "      starting balance into the cash field before confirming.")
        print( "      Leaving it untouched restores Alpaca's $100,000 default.")
        print()
        print( "   3. There is no API to set a paper balance -- it is a")
        print( "      dashboard-only action. The bot cannot do it for you.")
        print()
        print( "  Not urgent: the bot is safe at any balance. Every limit is a")
        print( "  percentage of live equity, so it simply trades smaller.")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
