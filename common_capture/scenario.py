"""Immutable nine-candidate scene; a method supplies the active roster.

The source JSON is copied byte-for-byte from the earlier capture project.
Its observation_period=5 is an inert saved field in the window experiment:
the effective authorized-capture rule sets it to 3 at episode reset.
"""
from dataclasses import dataclass
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from .physics import AgentState, CaptureEnvironment, SimConfig, _point, distance
from .rules import RULES, frozen_rules


ROLE_QUOTAS = {'observer': 2, 'leader': 1, 'marker': 2}


@dataclass(frozen=True)
class CaptureScenario:
    scenario_id: str
    source_sha256: str
    _source_data: dict

    def candidate_catalog(self):
        """Public identity/capability catalog; target truth stays private."""
        return [{'id': row['id'], 'carrier_type': row['carrier_type'],
                 'role': row['role']} for row in self._source_data['candidates']]

    def local_initial_observation(self, agent_id):
        """Candidate-local t=0 position and sensor sample, before team choice."""
        rows = [row for row in self._source_data['candidates'] if row['id'] == agent_id]
        if len(rows) != 1:
            raise ValueError('unknown candidate')
        row = rows[0]
        result = {'time': 0, 'agent_id': agent_id, 'carrier_type': row['carrier_type'],
                  'role': row['role'], 'position': deepcopy(row['position'])}
        if row['role'] == 'observer':
            target = self._source_data['target']['route'][0]
            visible = distance(row['position'], target) <= (
                self._source_data['config']['observation_radius'] + 1e-9)
            result['observation'] = {'observer_id': agent_id, 'observed_at': 0,
                                     'target_position': deepcopy(target) if visible else None}
        return result

    def new_episode(self, selected_ids, *, selection_record=None):
        """Create the same physical world for any method-selected 2/1/2 team."""
        if (not isinstance(selected_ids, (list, tuple)) or len(selected_ids) != 5
                or len(set(selected_ids)) != 5):
            raise ValueError('selected team must contain five distinct candidates')
        by_id = {row['id']: row for row in self._source_data['candidates']}
        if any(agent_id not in by_id for agent_id in selected_ids):
            raise ValueError('selected team contains an unknown candidate')
        selected = [by_id[agent_id] for agent_id in selected_ids]
        if {role: sum(row['role'] == role for row in selected)
                for role in ROLE_QUOTAS} != ROLE_QUOTAS:
            raise ValueError('selected team violates the 2/1/2 role quotas')
        effective_config = dict(self._source_data['config'])
        effective_config.update({key: RULES[key] for key in
                                 ('observation_period', 'marker_radius', 'hold_seconds',
                                  'support_required', 'authorization_required')})
        env = CaptureEnvironment(
            config=SimConfig(**effective_config),
            agents=[AgentState(row['id'], row['carrier_type'], row['role'],
                               deepcopy(row['position'])) for row in selected],
            target_route=deepcopy(self._source_data['target']['route']),
            scenario_id=self.scenario_id,
            scenario_sha256=self.source_sha256,
            team_selection=deepcopy(selection_record),
            generation=deepcopy(self._source_data.get('generation')),
            experiment_rules=frozen_rules())
        return env


def load_scene(path):
    path = Path(path)
    raw = path.read_bytes()
    data = json.loads(raw)
    if (set(data.get('selection', {}).get('role_counts', {})) != set(ROLE_QUOTAS)
            or data['selection']['role_counts'] != ROLE_QUOTAS
            or data['selection']['radius'] is not None):
        raise ValueError('capture v1 requires the saved whole-map 2/1/2 quotas')
    rows = data['candidates']
    if (len(rows) != 9 or len({row['id'] for row in rows}) != 9
            or any(sum(row['role'] == role for row in rows) != 3 for role in ROLE_QUOTAS)):
        raise ValueError('capture v1 requires three candidates per role')
    config = SimConfig(**data['config'])
    for row in rows:
        x, y = _point(row['position'], row['id'] + '.position')
        if not (0 <= x <= config.width and 0 <= y <= config.height):
            raise ValueError('candidate starts outside the map: ' + row['id'])
    initial_target = data['target']['route'][0]
    if not any(row['role'] == 'observer'
               and distance(row['position'], initial_target) <= config.observation_radius + 1e-9
               for row in rows):
        raise ValueError('scene lacks any t=0 observer discovery')
    # Structural validation and a physical clock smoke without selecting a
    # team by target distance. A fixed ID subset is only a validation fixture.
    validation = CaptureScenario(data['scenario_id'], hashlib.sha256(raw).hexdigest(), data)
    selected = [next(row['id'] for row in rows if row['role'] == role)
                for role in ('leader',)]
    selected += [row['id'] for row in rows if row['role'] == 'observer'][:2]
    selected += [row['id'] for row in rows if row['role'] == 'marker'][:2]
    validation.new_episode(selected)
    return validation
