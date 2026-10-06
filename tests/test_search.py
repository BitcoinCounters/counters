"""Search (counters/search.py, Store.find): a typed string becomes counters.

Pinned here:
  1. classify() reads every identifier shape without a database — number,
     inscription id, bare txid / sha256, address, block, URL, name — and the
     shapes cannot be confused;
  2. a family name (DEGENT) finds every DEGENT.x and says it is a family;
  3. ranking is exact, then family, then prefix, then substring, by number;
  4. lookups are case-insensitive, a bare txid finds its first event, a
     content hash finds its carriers;
  5. find() (/counter/, /c/) accepts the same spellings.

Zero-dependency runner: python tests/test_search.py   (or via pytest)
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counters import search  # noqa: E402
from counters.config import Config  # noqa: E402
from counters.store import CounterRecord, Store  # noqa: E402

TX = {k: (k * 2 + "0" * 64)[:64] for k in "abcdef12"}      # eight distinct (hex) txids
SHA_PIC = "11" * 32
SHA_TXT = "22" * 32
ADDR = "bc1qznsu8jwtaslycnpv7lvu6aqfamy6utdyuhztad"
ADDR2 = "1BoatSLRHtKNngkdXEeobR76b53LETtpyT"


def rec(asset, longname=None, *, txid, msg=0, sha=SHA_PIC, source=ADDR, block=900000):
    return CounterRecord(
        asset=asset, asset_id="1", asset_longname=longname, kind="issuance",
        content_type="image/png", content_type_raw=None, content_sha256=sha,
        content_length=10, is_pointer_like=False, mint_txid=txid, msg_index=msg,
        block_index=block, cp_tx_index=1, source=source,
    )


def make_store(tmp) -> Store:
    cfg = Config()
    cfg.data_dir = tmp
    store = Store(cfg)
    rows = [
        rec("XDUALS", txid=TX["a"], sha=SHA_TXT),                       # 0 named asset
        rec("A2938794432705199034", "DEGENT.0", txid=TX["b"]),           # 1
        rec("A16412480202985984110", "DEGENT.1", txid=TX["c"]),          # 2
        rec("A17292507019270585600", "DEGENT.2", txid=TX["d"], source=ADDR2),  # 3
        rec("DEGENTS", txid=TX["e"]),                                   # 4 prefix, not family
        rec("RARE", "RARE.DEGENT", txid=TX["f"]),                       # 5 substring in a long name
        rec("TWINS", txid=TX["1"], msg=0),                              # 6 two events, one tx
        rec("TWINS", txid=TX["1"], msg=1, sha=SHA_TXT),                 # 7
    ]
    for n, r in enumerate(rows):
        store.add_counter(n, r)
    store.db.commit()
    return store


def numbers(payload):
    return [r["number"] for r in payload["results"]]


def serialize(row):
    return {"number": row["number"], "asset": row["asset_longname"] or row["asset"]}


# --- 1. classify -------------------------------------------------------------

def test_classify_shapes():
    c = search.classify
    assert c("").kind == "empty" and c("   ").kind == "empty"
    assert (c("204").kind, c("204").token) == ("number", "204")
    assert c("#204").token == "204" and c("0007").token == "7"
    q = c(TX["a"] + "i3");  assert (q.kind, q.token, q.msg_index) == ("id", TX["a"], 3)
    assert c(TX["a"].upper()).kind == "hex" and c(TX["a"].upper()).token == TX["a"]
    assert c(ADDR.upper()).kind == "address" and c(ADDR.upper()).token == ADDR
    assert c(ADDR2).kind == "address" and c(ADDR2).token == ADDR2       # base58 keeps its case
    assert (c("block 966500").kind, c("block 966500").token) == ("block", "966500")
    assert c("height:12").token == "12"
    q = c("degent 6");  assert q.kind == "name" and q.variants == ["DEGENT 6", "DEGENT.6"]
    assert c("$DEGENT").variants == ["DEGENT"]
    digits64 = "1" * 64
    assert c(digits64).kind == "hex"                                     # a hash, even all digits
    assert c("9" * 30).kind == "number" and c("9" * 30).token == "9" * 30  # not a hash, not a counter


def test_classify_unwraps_urls():
    c = search.classify
    assert c("https://ordinals.com/inscription/" + TX["a"] + "i0").kind == "id"
    assert c("https://explore.block.space/tx/" + TX["a"]).token == TX["a"]
    assert c("https://xchain.io/asset/DEGENT.6").variants[0] == "DEGENT.6"
    assert c("https://counters.gallery/c/204").token == "204"
    assert c("https://counters.gallery/#/c/DEGENT.0").variants[0] == "DEGENT.0"
    assert c("https://counters.gallery/#/b/966500").kind == "block"
    assert c("www.counters.gallery/c/7").token == "7"
    assert c("https://mempool.space/block/966500").kind == "block"


# --- 2-4. run ----------------------------------------------------------------

def test_family_name_finds_every_member_and_says_so():
    with tempfile.TemporaryDirectory() as tmp:
        s = make_store(tmp)
        p = search.run(s, "DEGENT", 50, serialize)
        assert p["kind"] == "name"
        assert p["collection"] == {"name": "DEGENT", "count": 3}
        # family first (by number), then the prefix hit, then the substring hit
        assert numbers(p) == [1, 2, 3, 4, 5]
        assert p["total"] == 5
        assert p["exact"] is None            # no counter is named exactly DEGENT


def test_ranking_exact_then_family_prefix_substring():
    with tempfile.TemporaryDirectory() as tmp:
        s = make_store(tmp)
        p = search.run(s, "degent.1", 50, serialize)       # lowercase, exact long name
        assert p["exact"]["number"] == 2
        assert numbers(p)[0] == 2
        p = search.run(s, "degents", 50, serialize)
        assert p["exact"]["number"] == 4 and numbers(p) == [4]
        p = search.run(s, "degent 2", 50, serialize)       # space stands in for the dot
        assert p["exact"]["number"] == 3
        p = search.run(s, "RARE", 50, serialize)
        assert p["collection"] == {"name": "RARE", "count": 1}
        assert p["exact"]["number"] == 5                   # asset RARE is an exact spelling


def test_limit_and_total_are_independent():
    with tempfile.TemporaryDirectory() as tmp:
        s = make_store(tmp)
        p = search.run(s, "DEGENT", 2, serialize)
        assert numbers(p) == [1, 2] and p["total"] == 5


def test_number_id_txid_sha_address():
    with tempfile.TemporaryDirectory() as tmp:
        s = make_store(tmp)
        assert search.run(s, "3", 9, serialize)["exact"]["number"] == 3
        assert search.run(s, "99", 9, serialize)["results"] == []
        assert search.run(s, "9" * 30, 9, serialize)["results"] == []     # no overflow, no hit
        assert s.find("9" * 30) is None
        assert search.run(s, TX["1"] + "i1", 9, serialize)["exact"]["number"] == 7
        p = search.run(s, TX["1"].upper(), 9, serialize)    # bare txid: both events, first is exact
        assert (p["kind"], numbers(p), p["exact"]["number"]) == ("hex", [6, 7], 6)
        p = search.run(s, SHA_TXT, 9, serialize)            # content hash: its carriers
        assert numbers(p) == [0, 7] and p["exact"] is None and "sha256" in p["note"]
        p = search.run(s, ADDR, 9, serialize)
        assert (p["kind"], numbers(p), p["total"]) == ("address", [0, 1, 2, 4, 5, 6, 7], 7)
        assert p["note"] == search.ADDRESS_NOTE
        p = search.run(s, "block 900000", 9, serialize)
        assert (p["kind"], p["token"], p["results"]) == ("block", "900000", [])
        assert search.run(s, "", 9, serialize)["kind"] == "empty"


def test_like_wildcards_are_literal():
    with tempfile.TemporaryDirectory() as tmp:
        s = make_store(tmp)
        assert search.run(s, "%", 9, serialize)["results"] == []
        assert search.run(s, "_", 9, serialize)["results"] == []
        assert search.run(s, "DEGENT.%", 9, serialize)["results"] == []


# --- 5. find() ---------------------------------------------------------------

def test_find_accepts_every_spelling():
    with tempfile.TemporaryDirectory() as tmp:
        s = make_store(tmp)
        assert s.find("2")["number"] == 2
        assert s.find(" DEGENT.1 ")["number"] == 2
        assert s.find("degent.1")["number"] == 2            # case-insensitive fallback
        assert s.find("xduals")["number"] == 0
        assert s.find(TX["1"])["number"] == 6                # bare txid: first event
        assert s.find(TX["1"].upper() + "i1")["number"] == 7
        assert s.find(SHA_TXT)["number"] == 0                # content hash: lowest carrier
        assert s.find("DEGENT") is None                     # a family is not a counter
        assert s.find("NOPE") is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("ok", name)
