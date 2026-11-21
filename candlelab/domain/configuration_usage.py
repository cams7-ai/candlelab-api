"""Deletion checks for saved individual configurations and embedded snapshots."""


def embedded_configuration_usage(definition, target):
    """Nested copies have no origin ID; an edited copy remains ambiguous."""
    pending = list(definition.get("members") or [])
    uncertain = False
    while pending:
        member = pending.pop()
        copy = member.get("combination")
        if not isinstance(copy, dict):
            continue
        if copy.get("kind") == "strategy" and copy.get("strategy_id") == target["strategy_id"]:
            if copy.get("name") == target["name"]:
                return "linked"
            uncertain = True
        pending.extend(copy.get("members") or [])
    return "uncertain" if uncertain else None


def configuration_usage(target, combinations, runs):
    usage = {"runs": [], "combinations": [], "uncertain_copies": []}
    for combination in combinations:
        if combination["id"] == target["id"]:
            continue
        relation = embedded_configuration_usage(combination, target)
        if relation == "linked":
            usage["combinations"].append(combination["name"])
        elif relation:
            usage["uncertain_copies"].append(f"Combinação: {combination['name']}")
    for run in runs:
        snapshot = run.get("combination_snapshot")
        if not snapshot:
            continue
        if snapshot.get("id") == target["id"]:
            usage["runs"].append(run["id"])
            continue
        relation = embedded_configuration_usage(snapshot, target)
        if relation == "linked":
            usage["runs"].append(run["id"])
        elif relation:
            usage["uncertain_copies"].append(f"Processamento: {run['id']}")
    return usage
