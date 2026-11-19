"""Deterministic scenario construction; JSON snapshots are the CLI inputs."""
from copy import deepcopy


def window(sender, receiver, start, end):
    return {"sender_id": sender, "receiver_id": receiver, "start": start, "end": end}


def base():
    agents = [
        {"id": "USV-1", "platform": "USV", "roles": ["SERVER", "COORDINATOR"], "capabilities": ["coordinate"]},
        {"id": "UAV-1", "platform": "UAV", "roles": ["INITIATOR"], "capabilities": []},
        {"id": "AUV-1", "platform": "AUV", "roles": ["EXECUTOR"], "capabilities": ["sonar"]},
        {"id": "AUV-2", "platform": "AUV", "roles": ["EXECUTOR"], "capabilities": ["sonar"]},
    ]
    return {"name": "baseline", "seed": 0, "max_time": 1150, "perfect_prediction": True, "latency": 0,
        "objective": "Inspect the underwater site and return the sonar result", "server_id": "USV-1",
        "capability_templates": {"coordinate": {"duration": 1, "aliases": ["coordination"]},
            "sonar": {"duration": 360, "aliases": ["水下声呐检查"]},
            "survey": {"duration": 400, "aliases": ["surface survey"]},
            "analysis": {"duration": 30, "aliases": ["analyse sonar"]}},
        "agents": agents, "initial_registry": {a["id"]: a["capabilities"] for a in agents},
        "hard_constraints": {"deadline": 900}, "adjustment_policy": {"max_retries": 3, "allow_revision": False},
        "requirements": [{"task_id": "inspect", "capability_description": "水下声呐检查",
            "earliest_start": 0, "deadline": 900, "duration": 360, "dependencies": [], "parameters": {}, "required": True}],
        "windows": [window("UAV-1", "USV-1", 0, 1000), window("USV-1", "UAV-1", 0, 1000),
                    window("USV-1", "AUV-1", 180, 200), window("USV-1", "AUV-2", 300, 320),
                    window("AUV-1", "USV-1", 660, 700), window("AUV-1", "USV-1", 1080, 1120),
                    window("AUV-2", "USV-1", 660, 700), window("AUV-2", "USV-1", 1080, 1120)], "events": []}


def change(time, intervals, observers=("USV-1",), sender="USV-1", receiver="AUV-1", observed=True):
    physical = {"time": time, "type": "contact_change", "sender": sender, "receiver": receiver, "intervals": intervals}
    events = [physical]
    if observed:
        events.append({**physical, "type": "prediction_update", "observers": list(observers)})
    return events


def suite():
    scenarios = {"baseline": base()}
    delayed = deepcopy(base())
    delayed["events"] = change(120, [[720, 740]])
    scenarios["delayed_contact"] = delayed
    local = deepcopy(delayed)
    local["initial_group"] = ["AUV-2"]
    scenarios["local_alternative"] = local
    feasible = deepcopy(base())
    feasible["events"] = change(120, [[240, 260]])
    scenarios["feasible_shift"] = feasible
    too_late = deepcopy(base())
    too_late["windows"] = [w for w in too_late["windows"] if not (w["sender_id"] == "USV-1" and w["receiver_id"].startswith("AUV"))]
    too_late["windows"] += [window("USV-1", "AUV-1", 720, 740), window("USV-1", "AUV-2", 750, 770)]
    scenarios["hard_deadline_failure"] = too_late
    revision = deepcopy(too_late)
    revision["hard_constraints"] = {"deadline": 1200}
    revision["adjustment_policy"].update(allow_revision=True, mutable_fields=["deadline"])
    updated = deepcopy(revision["requirements"])
    updated[0]["deadline"] = 1200
    revision["revisions"] = [updated]
    scenarios["authorized_revision"] = revision
    disconnected = deepcopy(base())
    disconnected["events"] = change(200, [], observed=True)
    scenarios["accepted_then_disconnected"] = disconnected
    missed = deepcopy(base())
    missed["events"] = change(120, [[720, 740]], observed=False)
    scenarios["unobserved_window_change"] = missed
    lost_ack = deepcopy(base())
    # Either sonar executor can be selected. Neither can return its result to
    # the Server/coordinator, while assignment downlinks remain unchanged.
    lost_ack["windows"] = [w for w in lost_ack["windows"] if not (
        w["sender_id"] in {"AUV-1", "AUV-2"} and w["receiver_id"] == "USV-1")]
    scenarios["lost_ack_result"] = lost_ack
    dependency = deepcopy(base())
    dependency["agents"][0]["capabilities"].append("analysis")
    dependency["initial_registry"]["USV-1"].append("analysis")
    dependency["requirements"].append({"task_id": "analyse", "capability_description": "analysis",
        "earliest_start": 0, "deadline": 900, "duration": 30, "dependencies": ["inspect"], "parameters": {}, "required": True})
    scenarios["dependency"] = dependency
    cleanup = deepcopy(base())
    cleanup["agents"][2]["uncancellable"] = True
    cleanup["events"] = [{"time": 200, "type": "objective_update", "payload": {"cancel": True}}]
    cleanup["max_time"] = 400
    cleanup["windows"].append(window("USV-1", "AUV-1", 200, 220))
    scenarios["cancel_pending_cleanup"] = cleanup
    remote = deepcopy(delayed)
    remote["agents"][0]["capabilities"] = []
    remote["initial_registry"]["USV-1"] = []
    coordinator = {"id": "COORD-1", "platform": "UAV", "roles": ["COORDINATOR", "EXECUTOR"], "capabilities": ["coordinate", "survey"]}
    remote["agents"].append(coordinator)
    remote["initial_registry"]["COORD-1"] = coordinator["capabilities"][:]
    # Repoint task-dispatch opportunities to the actual coordinator.
    for w in remote["windows"]:
        if w["receiver_id"].startswith("AUV"):
            w["sender_id"] = "COORD-1"
        elif w["sender_id"].startswith("AUV"):
            w["receiver_id"] = "COORD-1"
    remote["windows"] += [window("UAV-1", "COORD-1", 0, 1000), window("COORD-1", "UAV-1", 0, 1000),
        window("COORD-1", "USV-1", 0, 10), window("USV-1", "COORD-1", 0, 10),
        window("COORD-1", "USV-1", 250, 1000), window("USV-1", "COORD-1", 250, 1000)]
    remote["events"] = change(120, [[720, 740]], observers=("COORD-1", "USV-1"), sender="COORD-1")
    remote["requirements"].append({"task_id": "parallel", "capability_description": "survey", "earliest_start": 0,
        "deadline": 900, "duration": 400, "dependencies": [], "parameters": {}, "required": True})
    scenarios["remote_server_parallel"] = remote
    objective = deepcopy(base())
    objective["agents"][0]["capabilities"] += ["survey", "analysis"]
    objective["initial_registry"]["USV-1"] += ["survey", "analysis"]
    objective["requirements"].append({"task_id": "parallel", "capability_description": "survey",
        "earliest_start": 0, "deadline": 900, "duration": 400, "dependencies": [], "parameters": {}, "required": True})
    objective["adjustment_policy"].update(allow_revision=True, allow_add=True)
    revised = deepcopy(objective["requirements"])
    revised.append({"task_id": "extra", "capability_description": "analysis", "earliest_start": 120,
        "deadline": 900, "duration": 30, "dependencies": [], "parameters": {}, "required": True})
    objective["events"] = [{"time": 120, "type": "objective_update", "payload": {"requirements": revised}}]
    scenarios["objective_revision_parallel"] = objective
    for name, data in scenarios.items():
        data["name"] = name
    return scenarios
