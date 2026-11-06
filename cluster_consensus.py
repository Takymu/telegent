"""Equal-peer proposer orchestration over authenticated quorum RPC."""
from cluster_peer import PeerError, quorum_size
from cluster_quorum import choose_value


class Consensus:
    def __init__(self, local, exchange):
        self.local = local
        self.exchange = exchange

    def _call(self, peer, action, payload):
        if peer == self.local.node:
            if action == "prepare":
                return self.local.prepare(**payload)
            if action == "accept":
                return self.local.accept(**payload)
            if action == "commit":
                return self.local.commit(payload["slot"], payload["previous_hash"],
                                         payload["value"], payload["receipts"])
            if action == "entries":
                return {"entries": self.local.entries(payload["after"], payload.get("limit", 100))}
            raise PeerError(400, "неизвестная операция кворума")
        return self.exchange(peer, action, payload)

    def catch_up(self, peer, limit=100):
        learned = 0
        while True:
            head = self.local.state()["head"]
            response = self._call(peer, "entries", {"after": head, "limit": limit})
            entries = response.get("entries") if isinstance(response, dict) else None
            if not isinstance(entries, list):
                raise PeerError(409, "сосед вернул некорректный кворумный журнал")
            for entry in entries:
                self.local.learn(entry)
                learned += 1
            if len(entries) < limit:
                return learned

    def propose(self, value, peers=None, minimum_ballot=0):
        peers = tuple(self.local.members if peers is None else peers)
        if self.local.node not in peers or any(peer not in self.local.members for peer in peers):
            raise PeerError(400, "некорректные участники предложения")
        state = self.local.state()
        slot, previous_hash = state["head"] + 1, state["head_hash"]
        counter = self.local.next_ballot(minimum_ballot)
        prepare_payload = {"slot": slot, "previous_hash": previous_hash,
                           "counter": counter, "proposer": self.local.node}
        prepared = []
        for peer in peers:
            try:
                prepared.append(self._call(peer, "prepare", prepare_payload))
            except PeerError:
                continue
        selected = choose_value(prepared, self.local.members, value)
        accept_payload = dict(prepare_payload, value=selected)
        accepted = []
        for peer in peers:
            try:
                accepted.append(self._call(peer, "accept", accept_payload))
            except PeerError:
                continue
        if len({receipt["node"] for receipt in accepted}) < quorum_size(len(self.local.members)):
            raise PeerError(425, "для операции не собрано большинство серверов")
        commit_payload = {"slot": slot, "previous_hash": previous_hash,
                          "value": selected, "receipts": accepted}
        result = self._call(self.local.node, "commit", commit_payload)
        for peer in peers:
            if peer == self.local.node:
                continue
            try:
                self._call(peer, "commit", commit_payload)
            except PeerError:
                pass
        return dict(result, value=selected)
