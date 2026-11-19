"""Real socket integration and the boundary between infrastructure and mechanism."""
import asyncio
from copy import deepcopy
import json
import subprocess
import sys
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")
pytest.importorskip("httpx")
pytest.importorskip("websockets")

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from common.ioa_transport import TransportEnvelope
from co_ioa.event_loop import Simulation
from co_ioa.ioa_infrastructure import IoAInfrastructure
from co_ioa.scenarios import suite
from im_server.co_ioa import create_co_ioa_server
from im_client.co_ioa import create_co_ioa_agent
from im_server.service import ConnectionManager


@pytest.mark.parametrize("name", ["delayed_contact", "remote_server_parallel", "objective_revision_parallel"])
def test_real_ioa_sockets_match_offline_business_events_and_results(name):
    scenario = suite()[name]
    offline = Simulation(scenario)
    expected = offline.run()
    connected = Simulation(scenario)
    with IoAInfrastructure(connected) as infrastructure:
        actual = connected.run()
        audit = infrastructure.snapshot()
        assert isinstance(infrastructure.server_app.state.connection_manager, ConnectionManager)
        assert audit["agent_http_calls"] > 0
        assert audit["cross_node_messages"] > 0
        assert not audit["errors"]
        deliveries = [e for e in connected.events if e["event_type"] == "message_delivered" and e["sender_id"] != e["receiver_id"]]
        assert {e["message_id"] for e in deliveries} == {e["message_id"] for e in audit["deliveries"]}
        assert len([e for e in audit["relay_events"] if e["event"] == "routed"]) == len(deliveries)
        assert len([e for e in audit["relay_events"] if e["event"] == "ingress_ack"]) == len(deliveries)
        assert actual == expected  # Same timing, IDs, history, ledger and metrics.
        assert connected.events == offline.events
    assert all(not node.thread.is_alive() for node in infrastructure.nodes.values())
    assert not infrastructure.relay.thread.is_alive()


def test_llm_control_timeout_covers_planning_retries(monkeypatch):
    monkeypatch.setenv("CO_IOA_API_BASE", "http://127.0.0.1:8001/v1")
    monkeypatch.setenv("CO_IOA_MODEL", "local-test-model")
    monkeypatch.setenv("CO_IOA_API_KEY", "test-only-key")
    monkeypatch.setenv("CO_IOA_MODEL_TIMEOUT", "12")
    scenario = suite()["baseline"]
    scenario["planner"] = "llm"
    sim = Simulation(scenario)
    infrastructure = IoAInfrastructure(sim)
    try:
        planner = sim.agents[sim.initiator_id].planner
        assert infrastructure.http.timeout.connect == 10
        assert infrastructure.http.timeout.read >= (planner.max_corrections + 1) * planner.transport.timeout
        assert infrastructure.http.timeout.read == planner.max_wall_seconds + 30
        assert planner.transport.max_retries == 2
    finally:
        infrastructure.http.close()
        infrastructure.loop.close()
        infrastructure.relay.socket.close()
        for node in infrastructure.nodes.values():
            node.socket.close()


def test_cleanup_error_does_not_hide_original_failure():
    from contextlib import ExitStack
    infrastructure = object.__new__(IoAInfrastructure)
    def fail_cleanup():
        raise RuntimeError("local IoA service failed to stop")
    infrastructure.close = fail_cleanup
    with pytest.raises(ValueError, match="model request failed") as raised:
        with ExitStack() as stack:
            stack.push(infrastructure)
            raise ValueError("model request failed")
    assert "local IoA service failed to stop" in raised.value.__notes__[0]


def test_sparse_clock_has_same_semantics_on_all_scenarios():
    for name, scenario in suite().items():
        offline = Simulation(scenario)
        expected = offline.run()
        sparse = Simulation(scenario)
        sparse.event_driven = True
        assert sparse.run() == expected, name
        assert sparse.events == offline.events, name


def test_agent_service_requires_control_key_and_changes_only_local_node():
    sim = Simulation(suite()["baseline"])
    node = sim.agents["UAV-1"]
    with TestClient(create_co_ioa_agent(node.runtime, "private-control")) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.post("/co-ioa/control", json={"operation": "initialize"}).status_code == 403
        assert not node.store.collaborations
        response = client.post("/co-ioa/control", json={"operation": "initialize"},
                               headers={"Authorization": "Bearer private-control"})
        assert response.status_code == 200
        assert response.json()["agent_id"] == "UAV-1"
        assert node.store.collaborations
        assert not sim.agents["USV-1"].store.collaborations


def test_relay_authentication_and_authenticated_sender_binding():
    app = create_co_ioa_server({"a": "token-a", "b": "token-b"})
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/a?token=wrong"):
                pytest.fail("invalid identity was accepted")
        with client.websocket_connect("/ws/a?token=token-a") as a:
            a.send_text(TransportEnvelope(message_id="fake", sender="b", receiver="a").model_dump_json())
            error = TransportEnvelope.model_validate_json(a.receive_text())
            assert error.kind == "error"
            assert "authenticated" in error.content
            assert not app.state.transport_audit


def test_transport_receipt_is_not_a_business_task_ack():
    app = create_co_ioa_server({"a": "token-a", "b": "token-b"})
    with TestClient(app) as client:
        with client.websocket_connect("/ws/a?token=token-a") as a, client.websocket_connect("/ws/b?token=token-b") as b:
            packet = TransportEnvelope(message_id="m", sender="a", receiver="b", content='{"business":"opaque"}')
            a.send_text(packet.model_dump_json())
            assert TransportEnvelope.model_validate_json(b.receive_text()) == packet
            b.send_text(TransportEnvelope(message_id="m", sender="b", receiver="a", kind="ingress_ack").model_dump_json())
            receipt = TransportEnvelope.model_validate_json(a.receive_text())
            assert receipt.kind == "receipt"
            assert receipt.content == ""
            assert all("TASK_ACK" not in e.values() for e in app.state.transport_audit)


def test_infrastructure_import_does_not_load_legacy_mechanism_or_milvus():
    script = """
import sys
from co_ioa.ioa_infrastructure import IoAInfrastructure
blocked = ['im_client.communication.communication_layer', 'im_client.communication.window_capture',
           'im_server.app', 'pymilvus', 'common.types.communication']
assert not any(name in sys.modules for name in blocked), [n for n in blocked if n in sys.modules]
"""
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True)
