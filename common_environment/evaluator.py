"""Method-neutral external judgement from physical work and received results.

Only the evaluation process reads these records. No Agent or planner endpoint
exposes them. A discussion conclusion is never a result delivery.
"""
from copy import deepcopy
import json


JUDGE_VERSION = 'external-v1'


def _requirements(scenario, until, revisions):
    requirements = {row['task_id']: deepcopy(row) for row in scenario['requirements']}
    for event in scenario.get('events', []):
        if event['time'] <= until and event['type'] == 'objective_update':
            replacement = event['payload'].get('requirements')
            if replacement is not None:
                requirements = {row['task_id']: deepcopy(row) for row in replacement}
    allowed = scenario.get('revisions', []) if scenario.get('adjustment_policy', {}).get('allow_revision') else []
    for revision in sorted(revisions, key=lambda row: row['at']):
        if revision['at'] > until:
            continue
        candidate = revision['requirements']
        candidate = list(candidate.values()) if isinstance(candidate, dict) else candidate
        if candidate in allowed:
            requirements = {row['task_id']: deepcopy(row) for row in candidate}
    return requirements


def _early_failure_certificate(scenario, requirements, task_results, physical, now):
    """Prove the final mission result and physical-completion ratio are fixed.

    This uses private judge evidence only. It deliberately ignores team membership,
    forecasts and the strategy's apparent intentions. A completed but unreported
    task or any active physical work keeps the run open.
    """
    if scenario.get('adjustment_policy', {}).get('allow_revision'):
        return None
    if any(event['time'] > now and event['type'] == 'objective_update'
           for event in scenario.get('events', [])):
        return None
    if any(row.get('status') in {'ACCEPTED', 'RUNNING', 'CANCEL_PENDING'}
           for row in physical):
        return None
    required = [row for row in requirements.values() if row.get('required', True)]
    if not required or not any(task_results[row['task_id']]['status'] == 'not_completed_on_time'
                               for row in required):
        return None
    bounds = []
    for row in required:
        status = task_results[row['task_id']]['status']
        if status == 'received':
            continue
        if status != 'not_completed_on_time' or now + row['duration'] <= row['deadline']:
            return None
        bounds.append({'task_id': row['task_id'], 'deadline': row['deadline'],
                       'minimum_duration': row['duration'],
                       'latest_possible_start': row['deadline'] - row['duration']})
    return {'rule': 'physical_deadline_v1', 'certified_at': now,
            'reason': 'Every outstanding required task has missed its latest possible physical start',
            'impossible_tasks': bounds,
            'received_tasks': [row['task_id'] for row in required
                               if task_results[row['task_id']]['status'] == 'received'],
            'active_physical_work': False, 'future_objective_change': False,
            'authorized_revision_possible': False}


def evaluate(scenario, *, physical, deliveries, revisions=(), evaluated_until,
             technical_fault=None, allow_early_failure=False):
    """Score all strategies with the same evidence rules and scenario objective.

    ``physical`` contains trusted tool/executor records. ``deliveries`` must
    contain only messages that actually entered an eligible receiver's inbox;
    native callers must require WebSocket ACK. Both adapters normalize their
    strategy-specific wire formats before entering this function.
    """
    if type(evaluated_until) is not int or evaluated_until < 0:
        raise ValueError('evaluated_until must be a nonnegative simulation second')
    requirements = _requirements(scenario, evaluated_until, revisions)
    collectors = {agent['id'] for agent in scenario['agents']
                  if {'INITIATOR', 'COORDINATOR'} & set(agent.get('roles', []))}
    required = [row for row in requirements.values() if row.get('required', True)]
    task_results = {}
    completed_count = 0
    for requirement in required:
        task_id = requirement['task_id']
        candidates = [row for row in physical if row.get('task_id') == task_id
                      and row.get('status') == 'COMPLETED'
                      and type(row.get('actual_end_time')) is int
                      and row['actual_end_time'] <= requirement['deadline']
                      and row.get('accepted_at') is not None
                      and row.get('requirement_snapshot') ==
                          _requirements(scenario, row['accepted_at'], revisions).get(task_id)
                      and row.get('result') is not None]
        candidates.sort(key=lambda row: (row['actual_end_time'], row['assignment_id']))
        completed_count += bool(candidates)
        matches = []
        for work in candidates:
            for delivery in deliveries:
                received_at = delivery.get('received_at')
                if (delivery.get('kind') != 'TASK_RESULT'
                    or delivery.get('task_id') != task_id
                    or delivery.get('assignment_id') != work['assignment_id']
                    or delivery.get('sender_id') != work['executor_id']
                    or delivery.get('receiver_id') not in collectors
                    or type(received_at) is not int
                    or not work['actual_end_time'] <= received_at <= evaluated_until
                    or type(delivery.get('created_at')) is not int
                    or delivery['created_at'] < work['actual_end_time']
                    or delivery.get('actual_end_time') != work['actual_end_time']
                    or delivery.get('result') != work['result']):
                    continue
                matches.append((received_at, work['assignment_id'], delivery.get('message_id')))
        matches.sort(key=lambda row: (row[0], row[1], str(row[2])))
        first = matches[0] if matches else None
        physical_at = candidates[0]['actual_end_time'] if candidates else None
        task_results[task_id] = {
            'deadline': requirement['deadline'],
            'physical_completed_at': physical_at,
            'result_received_at': first[0] if first else None,
            'assignment_id': first[1] if first else None,
            'message_id': first[2] if first else None,
            'status': 'received' if first else ('unreported' if candidates else 'not_completed_on_time'),
        }
    receipt_times = [row['result_received_at'] for row in task_results.values()]
    success_at = max(receipt_times) if all(time is not None for time in receipt_times) else None
    if not required:
        success_at = 0
    cancel_times = [event['time'] for event in scenario.get('events', [])
                    if event['time'] <= evaluated_until and event['type'] == 'objective_update'
                    and event['payload'].get('cancel')]
    cancelled_at = min(cancel_times) if cancel_times else None
    terminal_certificate = None
    if (allow_early_failure and technical_fault is None and cancelled_at is None
            and success_at is None and evaluated_until < scenario.get('max_time', 1200)):
        terminal_certificate = _early_failure_certificate(
            scenario, requirements, task_results, physical, evaluated_until)
    if technical_fault is not None:
        outcome, ended_at = 'error', None
    elif cancelled_at is not None and (success_at is None or cancelled_at <= success_at):
        outcome, ended_at = 'cancelled', cancelled_at
    elif success_at is not None:
        outcome, ended_at = 'success', success_at
    elif terminal_certificate is not None:
        outcome, ended_at = 'failed', None
    elif evaluated_until >= scenario.get('max_time', 1200):
        outcome, ended_at = 'failed', None
    else:
        outcome, ended_at = 'incomplete', None
    return {
        'judge_version': JUDGE_VERSION, 'outcome': outcome, 'ended_at': ended_at,
        'all_required_results_received_at': success_at,
        'evaluated_until': evaluated_until, 'technical_fault': technical_fault,
        'terminal_certificate': terminal_certificate,
        'required_task_count': len(required), 'tasks': task_results,
        'on_time_execution_completion_rate': None if technical_fault is not None else
            (completed_count / len(required) if required else 1.0),
        'collaboration_success_rate': None if technical_fault is not None else float(outcome == 'success'),
        'pending_remote_work': [row['assignment_id'] for row in physical
                                if row.get('status') in {'ACCEPTED', 'RUNNING'}],
    }


def evaluate_native(environment, router, clock, *, allow_early_failure=False):
    """Read private native tool state and ACKed group copies at the driver boundary."""
    acknowledged = {event['transit_id']: event['sim_time'] for event in router.events
                    if event['event_type'] == 'native_delivery_acknowledged'}
    physical = [dict(task_id=row['task_id'], assignment_id=row['assignment_id'],
                     executor_id=row['agent_id'], status=row['status'],
                     actual_end_time=row['actual_end_time'], result=deepcopy(row['result']),
                     accepted_at=row['accepted_at'],
                     requirement_snapshot=deepcopy(row['requirement_snapshot']))
                for row in environment.tasks.values()]
    revisions = [dict(at=row['accepted_at'],
                      requirements=environment.scenario['revisions'][row['revision_index']])
                 for row in environment.tasks.values() if row.get('revision_index') is not None
                 and row['accepted_at'] is not None]
    deliveries = []
    for transit in router.transits:
        if transit.kind != 'group' or transit.status != 'DELIVERED' or transit.transit_id not in acknowledged:
            continue
        message = transit.payload['message']
        origin = transit.payload.get('origin', transit.sender)
        if message.get('type') != 7 or message.get('sender') != origin:
            continue
        try:
            conclusion = json.loads(message.get('task_conclusion', ''))
        except (TypeError, ValueError):
            continue
        if not isinstance(conclusion, dict) or conclusion.get('status') != 'COMPLETED':
            continue
        deliveries.append(dict(kind='TASK_RESULT', task_id=conclusion.get('task_id'),
            assignment_id=conclusion.get('assignment_id'), sender_id=origin,
            receiver_id=transit.receiver, received_at=acknowledged[transit.transit_id],
            created_at=transit.payload.get('origin_created_at', transit.created_at),
            actual_end_time=conclusion.get('actual_end_time'),
            result=conclusion.get('result'), message_id=transit.transit_id))
    return evaluate(environment.scenario, physical=physical, deliveries=deliveries,
                    revisions=revisions, evaluated_until=environment.now,
                    technical_fault=clock.fault,
                    allow_early_failure=allow_early_failure)
