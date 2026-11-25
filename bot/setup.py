"""
python -m bot.setup            # paste your Exness key details, test them, pin the live account
python -m bot.setup --check    # just test the keys already in .env

Secrets are typed into hidden prompts and written only to .env (mode 600).
Nothing here sends an order.
"""

import argparse
import getpass
import os
import re
import sys

from config.settings import CONFIG_PATH, ROOT, exness_credentials, load_config
from exness.api import ExnessApi, ExnessApiError, load_signing_key

ENV_PATH = ROOT / ".env"
KEYS = ("EXN_API_KEY", "EXN_PRIVATE_KEY", "EXN_ACCOUNT_ID", "EXN_DISCOVERY_BASE_URL")


def _read_env() -> dict:
    out = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def _write_env(values: dict):
    lines, seen = [], set()
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            k = line.split("=", 1)[0].strip()
            if "=" in line and k in values:
                lines.append(f"{k}={values[k]}")
                seen.add(k)
            else:
                lines.append(line)
    lines += [f"{k}={v}" for k, v in values.items() if k not in seen]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(ENV_PATH, 0o600)


def _ask(label: str, current: str, secret: bool = False) -> str:
    shown = "(saved, Enter keeps it)" if current else ""
    prompt = f"{label} {shown}: "
    value = (getpass.getpass(prompt) if secret else input(prompt)).strip()
    return value or current


def prompt_keys():
    env = _read_env()
    print("From Exness Personal Area > Key Management.\n")
    api_key = _ask("API key (exnsk-...)", env.get("EXN_API_KEY", ""))
    private = _ask("Private signing key (128 hex chars; hidden)", env.get("EXN_PRIVATE_KEY", ""),
                   secret=True)
    account = _ask("Trading account ID", env.get("EXN_ACCOUNT_ID", ""))
    gateway = _ask("Gateway host from the DNS widget (https://...)",
                   env.get("EXN_DISCOVERY_BASE_URL", ""))
    load_signing_key(private)  # fail now on a bad paste, before anything is written
    if not re.fullmatch(r"[1-9][0-9]*", account):
        raise ExnessApiError("The account ID should be digits only")
    _write_env({"EXN_API_KEY": api_key, "EXN_PRIVATE_KEY": private, "EXN_ACCOUNT_ID": account,
                "EXN_DISCOVERY_BASE_URL": gateway})
    for k in KEYS:  # make this process see the new values
        os.environ[k] = _read_env()[k]
    print(f"Saved to {ENV_PATH} (only you can read it).\n")


def check() -> str:
    cfg = load_config()
    creds = exness_credentials()
    api = ExnessApi(**creds, allow_trading=False,
                    user_agent=cfg.get("api", {}).get("user_agent", "Claudebot/2.0"))
    details = api.connect()
    print(f"OK  access point   {api.access_point}")
    s = details.get("settings") or {}
    acct = api.account()
    print(f"OK  account        {acct.login} {details.get('account_name', '')} | {acct.currency} | "
          f"leverage {s.get('leverage')} | {s.get('trade_mode')} / {s.get('account_status')}")
    print(f"OK  equity         {acct.equity:.2f} {acct.currency}")
    for base in cfg.get("symbols") or {}:
        try:
            name = api.resolve_symbol(base)
            spec = api.spec(name)
            bars = api.rates(name, "M1", 5)
            bid, ask, _ = api.tick(name)
            print(f"OK  {base:<7} -> {name:<10} lots {spec.volume_min}-{spec.volume_max} step "
                  f"{spec.volume_step} | bid {bid} ask {ask} | {len(bars)} M1 bars")
        except ExnessApiError as e:
            print(f"ERR {base}: {e}")
    return acct.login


def pin_live(account_id: str):
    text = CONFIG_PATH.read_text(encoding="utf-8")
    text = re.sub(r"(?m)^mode: .*$", "mode: live", text)
    text = re.sub(r"(?m)^allow_live: .*$", "allow_live: true", text)
    text = re.sub(r'(?m)^live_account_id: .*$', f'live_account_id: "{account_id}"', text)
    CONFIG_PATH.write_text(text, encoding="utf-8")
    print(f"Pinned live trading to account {account_id} in config/config.yaml.")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--check", action="store_true", help="only test the keys in .env")
    args = ap.parse_args(argv)
    try:
        if not args.check:
            prompt_keys()
        account = check()
    except (ExnessApiError, OSError) as e:
        sys.exit(f"\nSetup failed: {e}")
    if args.check:
        return
    cfg = load_config()
    print(f"\nRisk: {cfg['risk'].get('risk_per_trade_pct')}% per trade, capped at "
          f"{cfg['risk'].get('max_lots_per_trade')} lots.")
    answer = input(f"Trade REAL orders on account {account}? Type the account number to confirm: ")
    if answer.strip() == str(account):
        pin_live(account)
        print("Start it with ./run.sh")
    else:
        print("Not pinned. The bot will refuse live orders until live_account_id matches.")


if __name__ == "__main__":
    main()
