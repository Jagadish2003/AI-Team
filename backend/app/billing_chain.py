"""R-1.9.1-L2 / T4 (AT-696) — tamper-evidence for billing telemetry.

Each billing event is stamped AT EMISSION with a per-org monotonic sequence
number (``seq``). Because ``seq`` is assigned before the event is written and is
monotonic per org, a usage report over a period sees a CONTIGUOUS block of seqs —
so deleting an event from the store before generation leaves a GAP: the report's
sequenced-event count no longer matches its seq range, and the deletion is
detectable (AC4). The report additionally carries a hash chain over the covered
events (their seq + identity, folded), so the covered set + ordering are bound and
independently re-verifiable by CloudFulcrum.

This is tamper-EVIDENCE, not tamper-proofing: a determined operator with DB + the
report_key can still forge, but casual deletion of a billing row is detectable as
a seq gap / count mismatch. The whole report is signed (T3), so the chain and
counts cannot be altered after generation.

  * :func:`next_seq` — atomically advance the per-org counter at emission (serialised
    with a per-key advisory lock; never raises — returns None so a stamping failure
    degrades the event to "unsequenced" rather than losing it).
  * :func:`build_tamper_evidence` — assemble the report's tamper-evidence block from
    the covered events (report-time; pure).
  * :func:`verify_tamper_evidence` — re-derive contiguity + chain root from a report's
    tamper-evidence block and report whether it is consistent (CloudFulcrum-side; pure).
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Optional

from . import db

logger = logging.getLogger(__name__)

SEQ_KEY_PREFIX = "billing_seq:"
TAMPER_ALGORITHM = "sha256/seq-hash-chain"


# ---------------------------------------------------------------------------
# Emission-time: per-org monotonic sequence counter (durable, atomic)
# ---------------------------------------------------------------------------
def next_seq(org_id: str) -> Optional[int]:
    """Atomically advance and return the org's next billing sequence number.

    Persisted in the shared ``kv`` table under ``billing_seq:{org_id}`` and
    serialised with a per-key PostgreSQL advisory lock, so concurrent same-org
    billing emissions can never collide (a collision would be a false tamper
    alarm — a duplicate seq or a phantom gap). Never raises: on any failure it
    returns ``None`` and the caller still emits the event (unsequenced), so a
    counter hiccup degrades tamper-evidence coverage rather than dropping a
    billing record.
    """
    key = SEQ_KEY_PREFIX + org_id
    con = None
    try:
        con = db.connect()
        cur = con.cursor()
        # Serialise increments for THIS key across connections/requests. Released
        # at transaction end (commit/rollback below).
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))
        cur.execute("SELECT payload FROM kv WHERE key = %s", (key,))
        row = cur.fetchone()
        last = 0
        if row and row[0] is not None:
            payload = row[0] if isinstance(row[0], dict) else json.loads(row[0])
            try:
                last = int(payload.get("last_seq", 0))
            except (TypeError, ValueError):
                last = 0
        seq = last + 1
        cur.execute(
            "INSERT INTO kv (key, payload) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET payload = EXCLUDED.payload",
            (key, json.dumps({"last_seq": seq})),
        )
        con.commit()
        return seq
    except Exception:  # pragma: no cover — a counter failure must never break a run
        logger.warning("billing seq advance failed for org %s", org_id, exc_info=True)
        if con is not None:
            try:
                con.rollback()
            except Exception:
                pass
        return None
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Report-time: hash chain over the covered events (pure)
# ---------------------------------------------------------------------------
def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def entry_hash(seq: int, event_type: str, core: dict) -> str:
    """Hash of one billing event's chain entry: binds its seq, type, and identity/content."""
    return hashlib.sha256(f"{seq}\n{event_type}\n{_canonical(core)}".encode("utf-8")).hexdigest()


def _fold(prev_chain_hash: str, this_entry_hash: str) -> str:
    return hashlib.sha256(f"{prev_chain_hash}\n{this_entry_hash}".encode("utf-8")).hexdigest()


def build_tamper_evidence(events: list[dict], *, total_event_count: int) -> dict:
    """Build the report's tamper-evidence block from the covered billing events.

    ``events`` is a list of ``{"seq": int|None, "event_type": str, "core": dict}``
    for every billing event in the report. Events that carry a ``seq`` are ordered
    by it and folded into a hash chain; the block records the count, seq range,
    the expected (gap-free) count, the ordered chain, its root, and a
    ``consistent`` verdict (no gap + chain re-folds). ``total_event_count`` is the
    report's overall billing-event count (T3's ``event_count``), carried here too
    so a count-vs-range mismatch is self-evident.
    """
    sequenced = [e for e in events if isinstance(e.get("seq"), int)]
    sequenced.sort(key=lambda e: e["seq"])

    chain: list[dict] = []
    prev = ""
    for e in sequenced:
        eh = entry_hash(e["seq"], str(e.get("event_type") or ""), e.get("core") or {})
        ch = _fold(prev, eh)
        chain.append({"seq": e["seq"], "entry_hash": eh, "chain_hash": ch})
        prev = ch

    seq_min = sequenced[0]["seq"] if sequenced else None
    seq_max = sequenced[-1]["seq"] if sequenced else None
    expected_count = (seq_max - seq_min + 1) if sequenced else 0
    chain_root = chain[-1]["chain_hash"] if chain else ""

    # No gap iff the sequenced events exactly fill their [min, max] range and no
    # seq repeats — i.e. the count of sequenced events equals the span. An empty
    # report has no events and so is trivially contiguous (nothing was deleted).
    if sequenced:
        seq_contiguous = (len(sequenced) == expected_count) and (
            len({e["seq"] for e in sequenced}) == len(sequenced)
        )
    else:
        seq_contiguous = True
    # Unsequenced billing events (a counter hiccup at emission) are visible, not
    # silently dropped — a report with any is not fully chain-covered.
    unsequenced_count = total_event_count - len(sequenced)

    return {
        "algorithm": TAMPER_ALGORITHM,
        "event_count": total_event_count,
        "sequenced_count": len(sequenced),
        "unsequenced_count": unsequenced_count,
        "seq_min": seq_min,
        "seq_max": seq_max,
        "expected_count": expected_count,
        "chain": chain,
        "chain_root": chain_root,
        "consistent": seq_contiguous and unsequenced_count == 0,
    }


def verify_tamper_evidence(tamper: dict) -> dict:
    """Independently re-verify a report's tamper-evidence block (CloudFulcrum-side).

    Recomputes the chain root by folding the block's ``entry_hash`` values in
    order, and checks the seqs are contiguous over [seq_min, seq_max] with no
    gap/duplicate and no unsequenced events. A billing event deleted from the
    store before generation shows here as a seq gap (``gap_detected``), so the
    report is flagged inconsistent (AC4).

    Returns ``{"consistent": bool, "gap_detected": bool, "chain_root_matches":
    bool, "reason": str|None}``. Pure — no DB, no network.
    """
    chain = tamper.get("chain") or []
    seqs = [c.get("seq") for c in chain]

    # Re-fold the chain from the stored entry hashes.
    prev = ""
    for c in chain:
        prev = _fold(prev, str(c.get("entry_hash") or ""))
    chain_root_matches = (prev == (tamper.get("chain_root") or "")) if chain else (
        (tamper.get("chain_root") or "") == ""
    )

    gap_detected = False
    reason: Optional[str] = None
    if seqs:
        seq_min, seq_max = seqs[0], seqs[-1]
        ordered = seqs == sorted(seqs)
        span = (seq_max - seq_min + 1)
        no_gap = ordered and len(seqs) == span and len(set(seqs)) == len(seqs)
        if not no_gap:
            gap_detected = True
            reason = "seq gap: covered events are not contiguous — event(s) missing"

    unsequenced = int(tamper.get("unsequenced_count") or 0)
    if unsequenced > 0 and reason is None:
        reason = f"{unsequenced} billing event(s) carry no seq — not chain-covered"
    if not chain_root_matches and reason is None:
        reason = "chain root does not re-fold — the chain was altered"

    consistent = (not gap_detected) and chain_root_matches and unsequenced == 0
    return {
        "consistent": consistent,
        "gap_detected": gap_detected,
        "chain_root_matches": chain_root_matches,
        "reason": reason,
    }
