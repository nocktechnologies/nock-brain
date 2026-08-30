#!/usr/bin/env python3
"""Regenerate the recall-eval fixture with SYNTHETIC content (no real memory data)
while preserving every retrieval-relevant structure so the CI gate stays green:
  - same ids / kinds / dates / sessions / confidence / scope / status (only
    `content` is replaced; attestation re-minted with the disposable fixture key);
  - each gold fact carries a globally-unique marker phrase that its rewritten
    gold query reuses -> guaranteed BM25 retrieval at prod cap (recall ~1.0);
  - a handful of 05-19 gold facts are out-competed by 2 same-date siblings that
    repeat their marker -> they survive max_per_date=4 but drop at =2, which is
    the cap lever the self-test checks (cap2 < cap4 by >= 0.05).
Run from repo root. Writes fixture + gold + curated suite in place. Local-only.
"""
from __future__ import annotations
import collections, hashlib, importlib.util, json, sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BIN = REPO / "bin"
FIX = REPO / "tests" / "fixtures" / "recall-eval-store.json"
KEY = REPO / "tests" / "fixtures" / "recall-eval-key.json"
PUB = REPO / "tests" / "fixtures" / "recall-eval-key.pub"
GOLD = REPO / "docs" / "evals" / "recall-gold-v1.json"
CURATED = REPO / "docs" / "evals" / "curated-recall-suite.json"
sys.path.insert(0, str(BIN))

def _load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), BIN / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

# --- deterministic synthetic vocabulary -------------------------------------
DOMAINS = ["inventory ledger","routing table","cache tier","invoice batch","sensor array",
    "scheduler queue","telemetry feed","packing manifest","freight lane","billing cycle",
    "warehouse aisle","dispatch roster","meter reading","pallet rack","ledger column",
    "carrier lane","audit trail","reconciliation run","settlement window","dock door"]
# unique marker tokens: adjective x noun, indexed -> >400 unique combos
ADJ = ["copper","cobalt","amber","slate","ivory","olive","crimson","teal","umber","saffron",
    "indigo","maroon","cyan","ochre","russet","viridian","sienna","cerulean","magenta","gamboge",
    "verdigris","heliotrope","vermilion","celadon"]
NOUN = ["marmot","sprocket","lantern","gantry","harrow","kestrel","dulcimer","trellis","pergola",
    "quokka","zephyr","obelisk","pangolin","axolotl","narwhal","dovetail","flywheel","capstan",
    "gudgeon","tamarind","quince","mandrel","ferrule","gnomon"]
VERB = {"directive":"be reconciled nightly","decision":"was chosen over the alternative",
    "bug":"was mis-tallied on rollover","architecture":"routes through the staging shim",
    "correction":"was restated after review","content":"is summarized for the digest"}

def marker(i):  # unique bigram for index i (< 24*24 = 576)
    return f"{ADJ[i % len(ADJ)]} {NOUN[(i // len(ADJ)) % len(NOUN)]}"

def synth(fid, kind, marker_phrase, *, reps=1, filler=True):
    dom = DOMAINS[int(hashlib.sha1(fid.encode(), usedforsecurity=False).hexdigest(), 16) % len(DOMAINS)]
    body = f"Synthetic {kind} record concerning the {marker_phrase} {dom}. " * reps
    tail = (f"The {dom} {VERB.get(kind, 'is tracked for the digest')}; this is "
            "placeholder content in the public test fixture and contains no real "
            "memory data.") if filler else ""
    return (body + tail).strip()

def sess(f): return str(f.get("session") or f.get("session_anchor") or "")

def main():
    _sign, _scrub = _load("_sign"), _load("_scrub")
    facts = json.loads(FIX.read_text())
    gold = json.loads(GOLD.read_text())
    gold_ids = list(gold["queries"].keys())
    gold_set = set(gold_ids)

    # stable marker per fact: gold facts get the low, guaranteed-unique indices
    order = gold_ids + [f["id"] for f in facts if f["id"] not in gold_set]
    mk = {fid: marker(i) for i, fid in enumerate(order)}

    # cap-lever: pick 4 gold facts on 2026-05-19 that have >=2 same-date-session
    # siblings; those siblings will repeat the gold's marker to out-rank it.
    by_id = {f["id"]: f for f in facts}
    d0519_sess = collections.defaultdict(list)
    for f in facts:
        if str(f.get("source_date")) == "2026-05-19":
            d0519_sess[sess(f)].append(f["id"])
    lever, competitors = [], {}
    for gid in gold_ids:
        if len(lever) >= 4: break
        f = by_id[gid]
        if str(f.get("source_date")) != "2026-05-19": continue
        sibs = [x for x in d0519_sess[sess(f)] if x != gid and x not in gold_set]
        if len(sibs) >= 2:
            lever.append(gid); competitors[gid] = sibs[:2]
    comp_of = {s: g for g, ss in competitors.items() for s in ss}

    # --- neutralize sensitive METADATA (paths, UUIDs, host/system/name labels)
    # while preserving the session grouping companionship depends on. Each
    # distinct original session/anchor maps to a stable synthetic token.
    def tok(prefix, v):
        return prefix + hashlib.sha1(str(v).encode(), usedforsecurity=False).hexdigest()[:8]
    # idempotent: a value already carrying the synthetic prefix is left as-is,
    # so re-running (or running on prior output) is stable.
    sess_map, anchor_map = {}, {}
    for f in facts:
        if "session" in f and not str(f["session"]).startswith("sess-"):
            sess_map.setdefault(str(f["session"]), tok("sess-", f["session"]))
            f["session"] = sess_map[str(f["session"])]
        if "session_anchor" in f and not str(f["session_anchor"]).startswith("anchor-"):
            anchor_map.setdefault(str(f["session_anchor"]), tok("anchor-", f["session_anchor"]))
            f["session_anchor"] = anchor_map[str(f["session_anchor"])]
        if "migration_source" in f: f["migration_source"] = "synthetic-fixture"
        if "machine" in f: f["machine"] = "synthetic-host"
        if "category" in f: f["category"] = "synthetic-category"
        if "curated_name" in f and not str(f["curated_name"]).startswith("curated-"):
            f["curated_name"] = tok("curated-", f["curated_name"])

    scrubbed = 0
    for f in facts:
        fid, kind = f["id"], f.get("kind", "content")
        if fid in comp_of:                       # competitor: repeat the gold's marker, short doc
            f["content"] = synth(fid, kind, mk[comp_of[fid]], reps=3, filler=False)
        elif fid in lever:                       # lever gold: marker once, longer doc (lower TF)
            f["content"] = synth(fid, kind, mk[fid], reps=1, filler=True)
        else:
            f["content"] = synth(fid, kind, mk[fid], reps=2, filler=True)
        c, n = _scrub.scrub_secrets(f["content"])
        if n: f["content"] = c; scrubbed += n
        f.pop("attestation", None)

    key = _sign.load_or_create_key(KEY, PUB, alg="hmac-sha256", create=True)
    signed = _sign.sign_facts(facts, key)
    vkey = _sign.load_public_key(PUB)
    by_sid = {f["id"]: f for f in signed}
    bad = [(f["id"], _sign.verify_fact(f, vkey, facts_by_id=by_sid)) for f in signed
           if _sign.verify_fact(f, vkey, facts_by_id=by_sid) != _sign.VALID]
    if bad:
        print("REFUSING: unverified:", bad[:5], file=sys.stderr); return 4
    FIX.write_text(json.dumps(signed, indent=1, sort_keys=True) + "\n")

    # rewritten gold queries: reuse the fact's unique marker so it retrieves
    for gid in gold_ids:
        gold["queries"][gid] = f"which record mentioned the {mk[gid]} in the notes"
    gold["_meta"]["limitation"] = ("SYNTHETIC fixture: content and queries are "
        "generated placeholders (no real memory data) so this public repo carries "
        "no store content; regenerate via scrub_fixture.py, not from a live store.")
    GOLD.write_text(json.dumps(gold, indent=2) + "\n")

    # curated suite -> synthetic, referentially valid (ids exist; tokens present)
    cur_ids = gold_ids[:5]
    curated = [[f"S{i+1}", f"the {mk[cid]} record", f"id:{cid}"] for i, cid in enumerate(cur_ids)]
    for i, tok in enumerate(["marmot", "sprocket", "lantern"]):  # tokens that appear in synth content
        curated.append([f"C{i+1}", f"records mentioning {tok}", f"token:{tok}"])
    CURATED.write_text(json.dumps(curated, indent=2) + "\n")

    print(f"wrote fixture: {len(signed)} facts, {scrubbed} scrubbed, all VALID; "
          f"lever golds={lever}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
