"""Normalize CO-IoA stores for the shared external evaluator."""
from copy import deepcopy

from common_environment.evaluator import evaluate


def evaluate_co(simulation, *, allow_early_failure=False):
    accepted = {(row['agent_id'], row['assignment_id']): row['sim_time']
                for row in simulation.events if row['event_type'] == 'task_accepted'}
    delivered = {row['message_id']: row['sim_time'] for row in simulation.events
                 if row['event_type'] == 'message_delivered'}
    physical = []
    messages = []
    for agent in simulation.agents.values():
        for assignment_id, row in agent.store.executions.items():
            physical.append(dict(task_id=row['assignment'].task_id,
                assignment_id=assignment_id, executor_id=agent.agent_id,
                status=row['status'], actual_end_time=row['actual_end_time'],
                result=deepcopy(row['result']),
                accepted_at=accepted.get((agent.agent_id, assignment_id)),
                requirement_snapshot=deepcopy(row['requirement'].__dict__)))
        for message in agent.store.messages:
            if message['message_type'] != 'TASK_RESULT' or message['message_id'] not in delivered:
                continue
            payload = message['payload']
            messages.append(dict(kind='TASK_RESULT', task_id=payload.get('task_id'),
                assignment_id=payload.get('assignment_id'), sender_id=message['sender_id'],
                receiver_id=agent.agent_id, received_at=delivered[message['message_id']],
                created_at=message['created_at'],
                actual_end_time=payload.get('actual_end_time'), result=deepcopy(payload.get('result')),
                message_id=message['message_id']))
    revisions = [dict(at=row['sim_time'], requirements=row['requirements'])
                 for row in simulation.events if row['event_type'] == 'plan_validated'
                 and 'requirements' in row]
    coordinator = simulation.coordinator().controller.collab
    technical_fault = coordinator.reason if coordinator.outcome == 'error' else None
    return evaluate(simulation.scenario, physical=physical, deliveries=messages,
                    revisions=revisions, evaluated_until=simulation.now,
                    technical_fault=technical_fault,
                    allow_early_failure=allow_early_failure)
