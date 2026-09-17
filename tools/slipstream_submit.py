#!/usr/bin/env python3
"""Submit one signed transaction to MARA Slipstream, the way that actually works.

`counters wallet inscribe --slipstream` submits once and believes the answer.
That is enough for a small reveal and wrong for a large one: Slipstream's origin
sits behind Cloudflare, and a multi-megabyte upload routinely finishes and then
dies with a 502/524 while the origin is still validating it. The answer is
AMBIGUOUS — the submission may well have landed — so a plain client either gives
up on a transaction that was accepted, or re-uploads megabytes every retry.

The method here is buffer777's (see ~/inscriptions/buffer777/NOTES.md,
"Submitting to Slipstream"), which carried reveals up to 749,931 vB:

  1. PROBE, then submit into the window. POST a throwaway body ("00") every 6 s
     until the origin answers 400 "Failed to deserialize" in under 2 s, twice in
     a row — proof it is alive right now — then send the real transaction
     immediately, with a 300 s timeout.
  2. OUR NODE IS THE ONLY AUTHORITY. A Slipstream submission is invisible to
     every node until MARA mines it, so the chain decides, never the API. The
     chain is checked before each retry (never resubmit something already mined;
     stop if an input was spent by something else), and after acceptance we watch
     for a confirmation. Slipstream's own status endpoint has reported "not
     found" for transactions that later mined, so it is logged, never obeyed.
  3. A 524 AFTER THE FULL UPLOAD (>90 s) is what every accepted submission looked
     like. Treat it as "probably accepted": watch the chain and re-upload at most
     every 30 minutes, 6 times — not every couple of minutes.

One more thing this fixes: urllib's and requests' default User-Agent trips
Cloudflare error 1010 on slipstream.mara.com. Every request here sends its own.

Usage:
  tools/slipstream_submit.py REVEAL.hex                  # submit, then watch
  tools/slipstream_submit.py REVEAL.hex --watch-only     # already uploaded
  tools/slipstream_submit.py REVEAL.hex --no-watch       # submit and exit

An inscription reveal cannot be fee-bumped or replaced (its envelope key is
ephemeral and discarded), so this never rebuilds or re-signs anything: the hex
you pass is the only transaction that can ever spend that commit.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counters.bitcoind import BitcoindClient, BitcoindError  # noqa: E402
from counters.config import Config  # noqa: E402

SLIPSTREAM = "https://slipstream.mara.com"
USER_AGENT = "counters/1.0"        # urllib's default trips Cloudflare error 1010

PROBE_INTERVAL = 6                 # seconds between probes
PROBE_STREAK = 2                   # consecutive live answers before a real submit
PROBE_CAP = 1200                   # ~2 h of probing before giving up
SUBMIT_TIMEOUT = 300               # a multi-MB upload plus the origin's think time
WATCH_INTERVAL = 30                # seconds between chain checks after submission
WATCH_CAP = 24 * 3600              # stop watching after a day; it may still mine
SS_STATUS_EVERY = 600              # ask Slipstream's (untrusted) status this often
RESUBMIT_EVERY = 1800              # after an ambiguous 524, re-upload no more often
RESUBMIT_MAX = 6                   # ...and no more than this many times

KEY_PATHS = (
    "/home/node/inscriptions/buffer777/.env",
    "/home/node/tatiana/mint/tomint/mint-transactions/91011/.env",
)


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


def slipstream_key() -> str | None:
    """The API key, if there is one. It only buys a discount multiplier when MARA
    has issued one — its absence never blocks a submission."""
    if os.environ.get("SLIPSTREAM_API_KEY"):
        return os.environ["SLIPSTREAM_API_KEY"]
    for path in KEY_PATHS:
        if os.path.exists(path):
            m = re.search(r"^\s*(?:export\s+)?SLIPSTREAM_API_KEY\s*=\s*(.+)\s*$",
                          open(path).read(), re.M)
            if m:
                return m.group(1).strip().strip("\"'")
    return None


def _post(hexstr: str, timeout: float, key: str | None) -> tuple[int | None, str, float]:
    """POST to /api/transactions. Returns (http_code_or_None, body, seconds).
    Never raises: a failed upload is data here, not an exception."""
    body = json.dumps({"tx_hex": hexstr, "client_code": key}).encode()
    req = urllib.request.Request(
        f"{SLIPSTREAM}/api/transactions", method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT,
                 "Content-Length": str(len(body))})
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    started = time.time()
    try:
        with urllib.request.urlopen(req, body, timeout=timeout) as r:
            return r.status, r.read().decode()[:600], time.time() - started
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:600].strip(), time.time() - started
    except Exception as e:                      # timeout, connection reset, DNS…
        return None, f"{type(e).__name__}: {e}", time.time() - started


def _status(txid: str, key: str | None, timeout: float = 30) -> dict:
    """Slipstream's status endpoint, on a short leash: it has taken 30 s+ and has
    reported "not found" for transactions that later mined. Decoration only."""
    req = urllib.request.Request(
        f"{SLIPSTREAM}/api/transactions/status?tx_id={txid}",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def _chain_state(btc: BitcoindClient, txid: str, first_in: tuple[str, int]):
    """What our node knows: ("confirmed", conf, height) | ("mempool",) |
    ("conflict",) when the first input was spent by something else | ("absent",)."""
    try:
        d = btc._call("getrawtransaction", [txid, True])
    except BitcoindError:
        d = None
    if d:
        conf = d.get("confirmations") or 0
        if conf > 0:
            height = None
            try:
                height = btc._call("getblockheader", [d["blockhash"]])["height"]
            except Exception:
                pass
            return ("confirmed", conf, height)
        return ("mempool",)
    try:
        if btc._call("gettxout", [first_in[0], first_in[1], False]) is None:
            return ("conflict",)
    except BitcoindError:
        return ("conflict",)
    return ("absent",)


def _describe(state) -> str:
    if state[0] == "confirmed":
        return f"confirmed in block {state[2]} ({state[1]} confirmation(s))"
    return state[0]


def submit(btc: BitcoindClient, raw: str, txid: str, first_in: tuple[str, int],
           key: str | None) -> tuple[bool, bool]:
    """Probe until the origin is alive, then upload.

    Returns (ok, ambiguous): `ok` is False only when the transaction can never
    confirm or we gave up; `ambiguous` marks the 524-after-full-upload case,
    where the watcher should keep the slow re-upload armed. A clean acceptance
    is not ambiguous and must never be re-uploaded.
    """
    probes = streak = attempts = 0

    while True:
        state = _chain_state(btc, txid, first_in)
        if state[0] == "confirmed":
            log(f"already on chain: {_describe(state)}")
            return True, False
        if state[0] == "mempool":
            log("visible in our own mempool — no need to submit")
            return True, False
        if state[0] == "conflict":
            log("FAILED: the commit output was spent by a different transaction; "
                "this reveal can never confirm")
            return False, False

        code, msg, dt = _post("00", 20, key)
        probes += 1
        alive = (code == 400 and dt < 2.0)
        if alive != (streak > 0) or probes % 10 == 1:
            log(f"probe {probes}: http={code} {dt:.2f}s "
                f"{'ALIVE' if alive else 'no answer from the origin'}")
        streak = streak + 1 if alive else 0

        if streak >= PROBE_STREAK:
            attempts += 1
            log(f"window open — submitting {len(raw) // 2:,} bytes (attempt {attempts})")
            code2, msg2, dt2 = _post(raw, SUBMIT_TIMEOUT, key)
            log(f"submit http={code2} in {dt2:.1f}s: {msg2[:200]}")
            low = (msg2 or "").lower()
            if code2 in (200, 201):
                log("accepted by Slipstream")
                return True, False
            if code2 == 400 and ("already" in low or "known" in low):
                log("Slipstream says it already has it — treating as submitted")
                return True, False
            if code2 == 400 and "not found" not in low:
                log(f"FAILED: Slipstream rejected it: {msg2[:300]}")
                return False, False
            if code2 == 524 and dt2 > 90:
                # Cloudflare gave up while the origin was still chewing on the
                # body. Every accepted large submission looked exactly like this.
                log(f"524 after the full upload ({dt2:.0f}s) — probably accepted. "
                    f"Watching the chain, re-uploading at most every "
                    f"{RESUBMIT_EVERY // 60} min in case it did not take")
                return True, True
            log("no definitive answer — back to probing (the chain is checked "
                "before every retry, so a landed submission is never repeated)")
            streak = 0

        if probes >= PROBE_CAP:
            log(f"giving up: no healthy window in {probes} probes "
                f"(~{probes * PROBE_INTERVAL // 60} min). The commit stays "
                f"spendable by this reveal — try again later.")
            return False, False
        time.sleep(PROBE_INTERVAL)


def watch(btc: BitcoindClient, raw: str, txid: str, first_in: tuple[str, int],
          key: str | None, ambiguous: bool) -> int:
    """Poll our own node until the transaction confirms. Slipstream is invisible
    until MARA mines it, so only the chain can answer."""
    log("watching our node: a Slipstream submission is invisible to every node "
        "until MARA mines it, so only the chain can say it worked")
    t0 = last_ss = last_up = time.time()
    resubmits = 0

    while time.time() - t0 < WATCH_CAP:
        state = _chain_state(btc, txid, first_in)
        if state[0] == "confirmed":
            log(f"CONFIRMED in block {state[2]} ({state[1]} confirmation(s))")
            return 0
        if state[0] == "conflict":
            log("FAILED: the commit output was spent by a different transaction")
            return 1

        # `raw` is empty once the verdict is settled (see main): a clean
        # acceptance can never be re-uploaded, so there is nothing to send.
        if (ambiguous and raw and state[0] == "absent" and resubmits < RESUBMIT_MAX
                and time.time() - last_up > RESUBMIT_EVERY):
            code, _msg, dt = _post("00", 20, key)
            if code == 400 and dt < 2.0:
                resubmits += 1
                last_up = time.time()
                log(f"still not on chain after {int((time.time() - t0) // 60)} min — "
                    f"re-uploading (resubmit {resubmits} of {RESUBMIT_MAX})")
                code2, msg2, dt2 = _post(raw, SUBMIT_TIMEOUT, key)
                log(f"resubmit http={code2} in {dt2:.1f}s: {(msg2 or '')[:160]}")
                low = (msg2 or "").lower()
                if code2 in (200, 201) or (code2 == 400 and ("already" in low
                                                             or "known" in low)):
                    ambiguous = False
                    log("definitive: Slipstream has it")
                elif code2 == 400 and "not found" not in low:
                    log(f"FAILED: Slipstream rejected it: {msg2[:300]}")
                    return 1

        if time.time() - last_ss > SS_STATUS_EVERY:
            last_ss = time.time()
            try:
                st = _status(txid, key)
                log(f"Slipstream status (not trusted): "
                    f"{(st.get('message') or json.dumps(st))[:120]}")
            except Exception as e:
                log(f"Slipstream status unavailable: {str(e)[:80]}")
        time.sleep(WATCH_INTERVAL)

    log("stopped watching after 24 h; the submission stands — check the chain later")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Submit a signed transaction to MARA "
                                             "Slipstream (probe, submit, watch)")
    ap.add_argument("hexfile", help="file holding the signed raw transaction hex")
    ap.add_argument("--watch-only", action="store_true",
                    help="skip submission; it was uploaded already, just watch the chain")
    ap.add_argument("--no-watch", action="store_true",
                    help="submit and exit without watching for the confirmation")
    args = ap.parse_args()

    raw = open(args.hexfile).read().strip()
    if not re.fullmatch(r"[0-9a-fA-F]+", raw or ""):
        print(f"{args.hexfile} does not hold raw transaction hex", file=sys.stderr)
        return 1

    btc = BitcoindClient(Config())
    try:
        dec = btc._call("decoderawtransaction", [raw])
    except BitcoindError as e:
        print(f"our node cannot decode it: {e}", file=sys.stderr)
        return 1
    txid = dec["txid"]
    first_in = (dec["vin"][0]["txid"], dec["vin"][0]["vout"])
    key = slipstream_key()

    log(f"txid      : {txid}")
    log(f"size      : {dec['size']:,} bytes, {dec['vsize']:,} vB, {dec['weight']:,} WU")
    log(f"spends    : {first_in[0]}:{first_in[1]}")
    log(f"api key   : {'yes' if key else 'none (no discount multiplier; not required)'}")
    log(f"chain     : {_describe(_chain_state(btc, txid, first_in))}")

    ambiguous = False
    if args.watch_only:
        # Resumed after an earlier upload: assume the ambiguous case, so the
        # watcher keeps the slow re-upload armed if it never reaches the chain.
        log("watch-only: not uploading now")
        ambiguous = True
    else:
        ok, ambiguous = submit(btc, raw, txid, first_in, key)
        if not ok:
            return 1

    if args.no_watch:
        return 0
    # Only the ambiguous case can ever re-upload, and the watch runs for hours.
    # Holding a multi-megabyte hex string resident all that time for a re-upload
    # that cannot happen is how this gets killed under memory pressure, so a
    # settled verdict drops it here.
    return watch(btc, raw if ambiguous else "", txid, first_in, key, ambiguous)


if __name__ == "__main__":
    raise SystemExit(main())
