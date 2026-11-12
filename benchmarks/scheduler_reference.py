"""Independent O(songs × singers) oracle for fair queue benchmarking."""

from vc_ktv.core.store import Store


def reference_queue(pool, settings, users, current, previous):
    """Reference pre-optimization scan used to validate the heap implementation."""
    ready = [s for s in pool if Store.playable(s, settings)]
    priority = [s for s in ready if s["priority_next"]]
    ready = [s for s in ready if not s["priority_next"]]
    if settings["queue_policy"] == "fcfs":
        return priority + ready
    tally = {u["id"]: u["plays"] + u["fair_offset"] for u in users}
    if current and not current["fair_exempt"]:
        tally[current["requester"]] += 1
    active = {s["requester_id"] for s in ready}
    if active:
        floor = max(0, max(tally[u] for u in active) - 2)
        for user in active:
            tally[user] = max(tally[user], floor)
    if current and not current["fair_exempt"]:
        previous = current["requester"]
    output = []
    while ready:
        head = {}
        for s in ready:
            head.setdefault(s["requester_id"], s)
        chosen = min(
            head.values(),
            key=lambda s: (
                tally[s["requester_id"]],
                s["requester_id"] == previous,
                s["requested_at"],
                s["id"],
            ),
        )
        output.append(chosen)
        tally[chosen["requester_id"]] += 1
        previous = chosen["requester_id"]
        ready.remove(chosen)
    return priority + output
