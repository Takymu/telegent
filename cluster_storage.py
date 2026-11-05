"""Placement plans for immutable attachments. No network, filesystem mutations or deletions."""
from dataclasses import dataclass
import hashlib
import math
import re
from typing import Optional, Tuple


HOT_SECONDS = 21 * 86400
MIN_FREE_BYTES = 300 * 1024 * 1024


@dataclass(frozen=True)
class Node:
    id: str
    free_bytes: int
    observed_at: float
    online: bool = True
    draining: bool = False
    failure_domain: str = ''


@dataclass(frozen=True)
class Replica:
    node: str
    size: int
    sha256: str
    verified: bool = True


@dataclass(frozen=True)
class Blob:
    id: str
    size: int
    created_at: float
    sha256: str
    replicas: Tuple[Replica, ...] = ()


@dataclass(frozen=True)
class Transfer:
    target: str
    source: Optional[str]
    blocked: Optional[str] = None


@dataclass(frozen=True)
class Placement:
    blob: str
    hot: bool
    required: int
    desired: Tuple[str, ...]
    available: Tuple[str, ...]
    transfers: Tuple[Transfer, ...]
    removable: Tuple[str, ...]

    @property
    def missing_copies(self):
        return max(0, self.required - len(self.available))


@dataclass(frozen=True)
class Plan:
    placements: Tuple[Placement, ...]
    remaining_bytes: Tuple[Tuple[str, int], ...]


def _identity(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9._-]{1,128}', value)) and value not in ('.', '..')


def _count(value):
    return type(value) is int and 0 <= value < 2**63


def _timestamp(value):
    return type(value) in (int, float) and math.isfinite(value)


def _digest(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[0-9a-f]{64}', value))


def _score(blob, node, weight):
    # Weighted rendezvous hashing is stable across process restarts and input ordering.
    raw = hashlib.sha256((blob.id+'\0'+blob.sha256+'\0'+node.id).encode('ascii')).digest()
    u = min(1 - 2**-53, (int.from_bytes(raw[:8], 'big') + 0.5) / 2**64)
    return -math.log(u) / max(1, weight)


def plan_storage(blobs, nodes, now, archive_copies=2, reserve_bytes=MIN_FREE_BYTES,
                 heartbeat_ttl=90, hot_seconds=HOT_SECONDS, rebalance=False):
    """Plan copies; a later inventory must verify targets before proposing old-copy removal.

    Healthy copies are retained by default. Explicit rebalancing uses free space plus
    verified managed bytes as fixed capacity weights, avoiding oscillation caused by
    counting a moved file against its destination's free-space weight next time.
    `removable` is a proposal, not a deletion permit: execution must recheck inventory
    generation, leadership/fencing and placement under the cluster's commit protocol.
    """
    blobs, nodes = tuple(blobs), tuple(nodes)
    if (not _timestamp(now) or not _count(reserve_bytes) or type(archive_copies) is not int
            or archive_copies < 2 or not _timestamp(heartbeat_ttl) or heartbeat_ttl <= 0
            or not _timestamp(hot_seconds) or hot_seconds <= 0):
        raise ValueError('некорректная политика размещения вложений')
    by_node, by_blob = {}, {}
    for node in nodes:
        if (not isinstance(node, Node) or not _identity(node.id) or node.id in by_node
                or not _count(node.free_bytes) or not _timestamp(node.observed_at)
                or type(node.online) is not bool or type(node.draining) is not bool
                or not isinstance(node.failure_domain, str)):
            raise ValueError('некорректный или повторяющийся узел хранения')
        by_node[node.id] = node
    for blob in blobs:
        if (not isinstance(blob, Blob) or not _identity(blob.id) or blob.id in by_blob
                or not _count(blob.size) or not _timestamp(blob.created_at) or not _digest(blob.sha256)):
            raise ValueError('некорректное или повторяющееся вложение')
        seen = set()
        for replica in blob.replicas:
            if (not isinstance(replica, Replica) or not _identity(replica.node) or replica.node in seen
                    or not _count(replica.size) or not _digest(replica.sha256) or type(replica.verified) is not bool):
                raise ValueError('некорректная или повторяющаяся копия вложения')
            seen.add(replica.node)
        by_blob[blob.id] = blob

    def healthy(node):
        return node.online and 0 <= now - node.observed_at <= heartbeat_ttl

    def valid(blob, replica):
        return replica.verified and (replica.size, replica.sha256) == (blob.size, blob.sha256)

    held = {node.id: 0 for node in nodes}
    for blob in blobs:
        for replica in blob.replicas:
            if replica.node in held and valid(blob, replica): held[replica.node] += blob.size
    left = {node.id: max(0, node.free_bytes - reserve_bytes) for node in nodes}
    weights = {node.id: left[node.id] + (held[node.id] if rebalance else 0) for node in nodes}
    active = sorted((node for node in nodes if not node.draining), key=lambda node: node.id)
    candidates = [node for node in active if healthy(node)]
    placements = []
    # Prioritize fresh files and bound all copies in this batch by the same capacity ledger.
    ordered = sorted(blobs, key=lambda blob: (now-blob.created_at > hot_seconds, -blob.size, blob.id))
    for blob in ordered:
        hot = now - blob.created_at <= hot_seconds
        available = {replica.node for replica in blob.replicas if valid(blob, replica)
                     and replica.node in by_node and healthy(by_node[replica.node])}
        desired, domains = [], set()
        required = max(1, len(active)) if hot else archive_copies

        def fits(node):
            return node.id in available or (node.free_bytes >= reserve_bytes and left[node.id] >= blob.size)

        def choose(pool):
            return min(pool, key=lambda node: (
                (node.failure_domain or node.id) in domains,
                not rebalance and node.id not in available,
                _score(blob, node, weights[node.id]), node.id))

        if hot:
            # Offline enrolled devices remain desired and catch up when they return.
            desired = [node.id for node in active]
        else:
            pool = [node for node in candidates if fits(node)]
            while pool and len(desired) < required:
                node = choose(pool)
                desired.append(node.id); domains.add(node.failure_domain or node.id)
                pool.remove(node)

        transfers = []
        sources = sorted(available)
        for nid in desired:
            if nid in available: continue
            node = by_node[nid]
            blocked = ('node_offline' if not healthy(node) else 'source_unavailable' if not sources
                       else 'disk_space' if not fits(node) else None)
            transfers.append(Transfer(nid, sources[0] if sources else None, blocked))
            if blocked is None: left[nid] -= blob.size
        # Never count an intended/in-flight copy as durable, or remove hot/offline copies.
        removable = (tuple(sorted(available-set(desired)))
                     if not hot and len(desired) >= required and set(desired) <= available else ())
        placements.append(Placement(blob.id, hot, required, tuple(desired), tuple(sources), tuple(transfers), removable))
    return Plan(tuple(placements), tuple(sorted(left.items())))
