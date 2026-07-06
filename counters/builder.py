"""Build the COUNT inscription tapscript and derive its commit address.

Mirrors the parser in envelope.py and the canonical format in build ref §4.
The leaf is ord-style: the key check sits first, then the skipped-no-op
envelope:

    <reveal_xonly> OP_CHECKSIG
    OP_FALSE OP_IF
      PUSH "COUNT" PUSH 0x01 PUSH <content_type> OP_0 <body chunks...>
    OP_ENDIF

The commit output is a P2TR whose internal key is the reveal key and whose
single tapleaf is this script (same construction Counterparty uses for its own
taproot envelopes). The reveal spends it via the script path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from . import tap
from .config import ASSET_TAG, CONTENT_TYPE_TAG, COUNT_MARKER

OP_FALSE = 0x00
OP_IF = 0x63
OP_ENDIF = 0x68
OP_CHECKSIG = 0xAC
OP_0 = 0x00

MAX_PUSH = 520  # taproot per-push cap


def chunk_body(body: bytes, size: int = MAX_PUSH) -> list[bytes]:
    return [body[i : i + size] for i in range(0, len(body), size)] or []


def build_envelope(content_type: bytes, body: bytes, asset: bytes = b"") -> bytes:
    """The OP_FALSE OP_IF ... OP_ENDIF envelope (no key check).

    If `asset` is given, emit the reinscription target tag (0x02) after the
    content_type field: the counter attaches to that existing asset and the tx
    carries no Counterparty message.
    """
    script = bytes([OP_FALSE, OP_IF])
    script += tap.push_data(COUNT_MARKER)
    # content_type tag: a 1-byte 0x01 data push, then the MIME push.
    script += tap.push_data(bytes([CONTENT_TYPE_TAG]))
    script += tap.push_data(content_type)
    # optional reinscription target: 1-byte 0x02 data push, then the asset push.
    if asset:
        script += tap.push_data(bytes([ASSET_TAG]))
        script += tap.push_data(asset)
    script += bytes([OP_0])  # empty separator: fields end, body begins
    for chunk in chunk_body(body):
        script += tap.push_data(chunk)
    script += bytes([OP_ENDIF])
    return script


def build_leaf(reveal_xonly: bytes, content_type: bytes, body: bytes,
               asset: bytes = b"") -> bytes:
    return (tap.push_data(reveal_xonly) + bytes([OP_CHECKSIG])
            + build_envelope(content_type, body, asset))


@dataclass
class Inscription:
    reveal_seckey: bytes      # 32 bytes; signs the reveal's script-path input
    reveal_xonly: bytes       # internal key == reveal key
    content_type: bytes
    body: bytes
    leaf: bytes               # the tapscript
    merkle_root: bytes        # = tapleaf hash (single leaf)
    output_xonly: bytes       # tweaked output key
    commit_address: str
    control_block: bytes

    @property
    def commit_script_pubkey(self) -> bytes:
        return tap.p2tr_script_pubkey(self.output_xonly)


def build_inscription(content_type: bytes, body: bytes,
                      seckey: bytes | None = None, hrp: str = "bc",
                      asset: bytes = b"") -> Inscription:
    seckey = seckey or os.urandom(32)
    reveal_xonly = tap.xonly_pubkey(seckey)
    leaf = build_leaf(reveal_xonly, content_type, body, asset)
    merkle_root = tap.tapleaf_hash(leaf)
    _, output_xonly = tap.taproot_tweak_pubkey(reveal_xonly, merkle_root)
    return Inscription(
        reveal_seckey=seckey,
        reveal_xonly=reveal_xonly,
        content_type=content_type,
        body=body,
        leaf=leaf,
        merkle_root=merkle_root,
        output_xonly=output_xonly,
        commit_address=tap.p2tr_address(output_xonly, hrp=hrp),
        control_block=tap.control_block(reveal_xonly, merkle_root),
    )
