#!/usr/bin/env python3
"""Counters teleburn address: provably unspendable P2TR committing to a target.
   Q = H + t*G   where H = BIP341 NUMS point, t = TapTweak(H.x || m), m = tagged_hash("CountersTeleburn", target)
usage: teleburn.py <target>   e.g. counter:28 | inscription:<id> | asset:NAME | eth:0x...
"""
import hashlib, sys, json, subprocess
p = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
n = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)
H_X = 0x50929b74c1a04954b78b4b6035e97a5e078a5a0f28ec96d547bfee9ace803ac0   # BIP341 NUMS

def tagged(tag, msg):
    th = hashlib.sha256(tag.encode()).digest(); return hashlib.sha256(th + th + msg).digest()
def add(P, Q):
    if P is None: return Q
    if Q is None: return P
    if P[0] == Q[0] and (P[1] + Q[1]) % p == 0: return None
    l = (3*P[0]*P[0]*pow(2*P[1], -1, p)) % p if P == Q else ((Q[1]-P[1])*pow(Q[0]-P[0], -1, p)) % p
    x = (l*l - P[0] - Q[0]) % p; return (x, (l*(P[0]-x) - P[1]) % p)
def mul(k, P):
    R = None
    while k:
        if k & 1: R = add(R, P)
        P = add(P, P); k >>= 1
    return R
def lift_x(x):
    y = pow((x**3 + 7) % p, (p+1)//4, p); assert (y*y) % p == (x**3+7) % p
    return (x, y if y % 2 == 0 else p - y)

def teleburn(target: str):
    m = tagged("CountersTeleburn", target.encode("utf-8"))
    t = int.from_bytes(tagged("TapTweak", H_X.to_bytes(32, "big") + m), "big"); assert t < n
    Q = add(lift_x(H_X), mul(t, G))
    spk = "5120" + Q[0].to_bytes(32, "big").hex()
    addr = json.loads(subprocess.run(["bitcoin-cli", "decodescript", spk], capture_output=True, text=True).stdout)["address"]
    return {"target": target, "commitment_m": m.hex(), "tweak_t": hex(t), "scriptPubKey": spk, "address": addr}

if __name__ == "__main__":
    for tgt in sys.argv[1:]: print(json.dumps(teleburn(tgt)))
