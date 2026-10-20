"""The API client numbers a fairminter deploy at its deploy block, whatever
its later status: it reads the block's NEW_FAIRMINTER events, never the
derived state's `/v2/blocks/{h}/fairminters` (which moves a deploy to the
block of its latest status change)."""
from counters.counterparty import CounterpartyClient

DEPLOY = "5dfbc6ffaae2939838c411edcb99952c768f9375a3fa090f42ebe6ecfef4d464"


class _Events(CounterpartyClient):
    """Core as of a later block: the deploy (block 964,250) has since closed
    (965,043). Only the events endpoint is answered."""

    def __init__(self):
        self.paths = []

    def _get(self, path, params=None):
        self.paths.append(path)
        if path == "/v2/blocks/964250/events/NEW_FAIRMINTER":
            return {"result": [{
                "event_index": 20486760, "event": "NEW_FAIRMINTER", "tx_hash": DEPLOY,
                "params": {"asset": "MEMENOME", "asset_longname": None, "block_index": 964250,
                           "description": "(()=>{})()", "mime_type": "text/javascript",
                           "source": "bc1qsource", "status": "pending",
                           "tx_hash": DEPLOY, "tx_index": 3176664},
            }], "next_cursor": None}
        if path.endswith("/events/NEW_FAIRMINTER"):
            return {"result": [], "next_cursor": None}
        raise AssertionError(f"unexpected path {path}")


def test_deploy_is_read_at_its_deploy_block():
    cp = _Events()
    rows = cp.get_block_fairminters(964250)
    assert [(r["asset"], r["tx_hash"], r["tx_index"], r["block_index"]) for r in rows] == [
        ("MEMENOME", DEPLOY, 3176664, 964250)]
    assert rows[0]["description"] and rows[0]["mime_type"] == "text/javascript"
    # The close block holds no deploy.
    assert cp.get_block_fairminters(965043) == []
    assert all("/fairminters" not in p for p in cp.paths)
