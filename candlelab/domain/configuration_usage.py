"""Deletion checks for saved configurations, combinations and embedded snapshots."""


def base_strategies(definition):
    """Match the frontend's conservative comparison of copies without origin IDs."""
    ids = set()
    pending = [definition]
    while pending:
        current = pending.pop()
        if current.get("strategy_id"):
            ids.add(current["strategy_id"])
        for member in current.get("members") or []:
            copy = member.get("combination")
            if isinstance(copy, dict):
                pending.append(copy)
            elif member.get("strategy_id"):
                ids.add(member["strategy_id"])
    return ids


def embedded_configuration_usage(definition, target):
    """Nested copies have no origin ID; an edited copy remains ambiguous."""
    pending = list(definition.get("members") or [])
    uncertain = False
    while pending:
        member = pending.pop()
        copy = member.get("combination")
        if not isinstance(copy, dict):
            continue
        same_base = False
        if copy.get("kind") == target["kind"]:
            if copy["kind"] == "custom":
                same_base = copy.get("name") == target["name"] or base_strategies(
                    copy
                ) == base_strategies(target)
            else:
                same_base = copy.get("strategy_id") == target.get("strategy_id")
        if same_base:
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
