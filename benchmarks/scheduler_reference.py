"""Independent O(songs × singers) oracle for fair queue benchmarking."""

from collections import defaultdict, deque

from vc_ktv.core.store import Store


def reference_queue(pool, settings, users, current, previous):
    """Scan original turn slots while honoring each singer's personal order."""
    ready = [s for s in pool if Store.playable(s, settings)]
    priority = [s for s in ready if s["priority_next"]]
    ready = [s for s in ready if not s["priority_next"]]
    personal = defaultdict(list)
    for song in ready:
        personal[song["requester_id"]].append(song)
    ordered = {
        user: deque(
            sorted(
                songs,
                key=lambda s: (s.get("personal_order", 0), s["requested_at"], s["id"]),
            )
        )
        for user, songs in personal.items()
    }
    if settings["queue_policy"] == "fcfs":
        return priority + [ordered[s["requester_id"]].popleft() for s in ready]
    tally = {u["id"]: u["plays"] + u["fair_offset"] for u in users}
    if current and not current["fair_exempt"]:
        tally[current["requester"]] += 1
        previous = current["requester"]
    if personal:
        floor = max(0, max(tally[u] for u in personal) - 2)
        for user in personal:
            tally[user] = max(tally[user], floor)
    output = []
    while ready:
        head = {}
        for s in ready:
            head.setdefault(s["requester_id"], s)
        slot = min(
            head.values(),
            key=lambda s: (
                tally[s["requester_id"]],
                s["requester_id"] == previous,
                s["requested_at"],
                s["id"],
            ),
        )
        previous = slot["requester_id"]
        output.append(ordered[previous].popleft())
        tally[previous] += 1
        ready.remove(slot)
    return priority + output
