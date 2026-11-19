"""Shared capture physics and method boundary, without model/API calls."""
import json
from pathlib import Path

import pytest

from common_capture import AgentState, CaptureEnvironment, CaptureLocalTools, SimConfig, load_scene
from common_capture.protocol import ROOT, load_frozen_scene, validate_protocol
from common_capture.rules import RULES


SCENES = ROOT / 'experiments/capture_v1/scenes'


def test_all_ten_scene_bytes_and_effective_rules_are_frozen():
    report = validate_protocol()
    assert report['scene_count'] == 10 and not report['methods_connected']
    for path in sorted(SCENES.glob('scene_*.json')):
        scene = load_scene(path)
        assert len(scene.candidate_catalog()) == 9
        raw = json.loads(path.read_text())
        assert raw['config']['observation_period'] == 5
        # Select exactly 2/1/2 without using target distance.
        ids = [row['id'] for row in scene.candidate_catalog() if row['role'] == 'observer'][:2]
        ids += [row['id'] for row in scene.candidate_catalog() if row['role'] == 'leader'][:1]
        ids += [row['id'] for row in scene.candidate_catalog() if row['role'] == 'marker'][:2]
        env = scene.new_episode(ids)
        assert env.config.observation_period == 3
        assert env.config.authorization_required and not env.config.support_required
        assert env.experiment_rules == RULES
        assert env.scenario_sha256 == report_sha(path)
    assert load_frozen_scene('scene_01.json').scenario_id == 'random_fixed_01'
    with pytest.raises(ValueError, match='unknown frozen'):
        load_frozen_scene('../scene_01.json')


def report_sha(path):
    from common_capture.protocol import sha256
    return sha256(path)


def test_team_is_method_selected_and_initial_sensors_are_private():
    scene = load_scene(SCENES / 'scene_01.json')
    catalog = scene.candidate_catalog()
    assert all('target_position' not in row for row in catalog)
    far_observers = [row['id'] for row in catalog if row['role'] == 'observer'
                     and scene.local_initial_observation(row['id'])['observation']['target_position'] is None]
    assert len(far_observers) >= 2
    ids = far_observers[:2]
    ids += [row['id'] for row in catalog if row['role'] == 'leader'][:1]
    ids += [row['id'] for row in catalog if row['role'] == 'marker'][:2]
    env = scene.new_episode(ids)
    assert all(env.observe(name)['target_position'] is None for name in far_observers[:2])
    with pytest.raises(ValueError, match='quotas'):
        scene.new_episode([row['id'] for row in catalog[:5]])
    with pytest.raises(ValueError, match='unknown candidate'):
        scene.local_initial_observation('outside')


def test_local_tools_do_not_issue_team_decisions_or_accept_forged_delivery():
    env = CaptureEnvironment(
        config=SimConfig(observation_period=3, support_required=False,
                         authorization_required=True),
        agents=[AgentState('o1', 'UAV', 'observer', (0, 40)),
                AgentState('o2', 'USV', 'observer', (80, 80)),
                AgentState('lead', 'USV', 'leader', (0, 40)),
                AgentState('m1', 'UUV', 'marker', (0, 40)),
                AgentState('m2', 'UAV', 'marker', (80, 80))],
        target_route=[(0, 40), (12, 40), (12, 55), (101, 55)])
    tools = CaptureLocalTools(env, verify_delivery=lambda r: r['message_id'] == 'actual-ack')
    observer, marker = tools.for_agent('o1'), tools.for_agent('m1')
    assert observer.observe()['target_position'] == [0.0, 40.0]
    assert not hasattr(marker, 'select_team') and not hasattr(marker, 'authorize')
    assert marker.move_to(0, 40)['accepted']
    with pytest.raises(PermissionError):
        tools.for_agent('lead').observe()
    with pytest.raises(PermissionError):
        tools.for_agent('lead').try_tag('g1')
    for _ in range(3):
        tools.advance_one_second()
    assert observer.observe()['observed_at'] == 3
    assert env.trace[-1]['target_position'] == [3.0, 40.0]
    grant = {'authorization_id': 'g1', 'episode_id': 'ep1', 'leader_id': 'lead',
             'marker_id': 'm1', 'observer_id': 'o1', 'observed_at': 3,
             'target_position': [3.0, 40.0], 'position_at': 3,
             'marker_position': [0.0, 40.0], 'issued_at': 3, 'expires_at': 8,
             'estimated_distance': 3.0, 'rules_version': RULES['version']}
    assert marker.try_tag('g1')['reason'] == 'authorization_not_delivered'
    with pytest.raises(PermissionError, match='verified transport'):
        tools.register_delivered_authorization({'grant': grant, 'message_id': 'made-up', 'delivered_at': 3})
    tools.register_delivered_authorization({'grant': grant, 'message_id': 'actual-ack', 'delivered_at': 3})
    assert marker.try_tag('g1')['accepted']
    assert env.result()['capture_time'] == 3
