"""Conservative async driver for the native simulation clock.

The required settle callback is the transport/event integration point for
stages 6-7. It must finish all messages and external events due at the current
second before the driver may advance; this module never fabricates delivery.
"""
import asyncio
from time import monotonic

import httpx

from .native_clock import ClockConflict


class ClockWaitTimeout(RuntimeError):
    """Wall-clock infrastructure/model timeout, not a simulated task failure."""


class NativeClockDriver:
    def __init__(self, *, status, step, settle, poll_seconds=0.01,
                 max_wall_wait_seconds=1200, max_delivery_ack_wait_seconds=60,
                 stop_when=None):
        if (settle is None or poll_seconds <= 0 or max_wall_wait_seconds <= 0
                or max_delivery_ack_wait_seconds <= 0):
            raise ValueError('driver needs a settling callback and positive wall timers')
        self.status = status
        self.step = step
        self.settle = settle
        self.poll_seconds = poll_seconds
        self.max_wall_wait_seconds = max_wall_wait_seconds
        self.max_delivery_ack_wait_seconds = max_delivery_ack_wait_seconds
        self.stop_when = stop_when

    async def run_until(self, until):
        if type(until) is not int or until < 0:
            raise ValueError('until must be a nonnegative integer simulation second')
        last_progress = monotonic()
        last_state = None
        unacknowledged_since = None
        while True:
            state = await self.status()
            if state.get('fault'):
                raise RuntimeError('native clock stopped by technical fault: '
                                   + str(state['fault'].get('reason', state['fault'])))
            if self.stop_when is not None and self.stop_when(state):
                return state
            now = state['sim_time']
            fingerprint = (now, state['active_decisions'], state['waiting_tools'],
                           state['pending_wakes'], state.get('pending_deliveries', 0))
            if fingerprint != last_state:
                last_state, last_progress = fingerprint, monotonic()
            if (state.get('pending_deliveries', 0) and not state['active_decisions']
                    and not state['pending_wakes']):
                if unacknowledged_since is None:
                    unacknowledged_since = monotonic()
                elif monotonic() - unacknowledged_since > self.max_delivery_ack_wait_seconds:
                    raise ClockWaitTimeout(
                        f'native delivery was not acknowledged at simulation t={now}')
            else:
                unacknowledged_since = None
            if monotonic() - last_progress > self.max_wall_wait_seconds:
                raise ClockWaitTimeout(f'native action or delivery stalled at simulation t={now}')
            if state['active_decisions'] or state['pending_wakes'] or state.get('pending_deliveries', 0):
                await asyncio.sleep(self.poll_seconds)
                continue
            # Stage 6 must supply a callback that drains native messages at
            # this second. It can take arbitrary wall time without advancing.
            try:
                await asyncio.wait_for(self.settle(now), timeout=self.max_wall_wait_seconds)
            except asyncio.TimeoutError as exc:
                raise ClockWaitTimeout(f'settling stalled at simulation t={now}') from exc
            fresh = await self.status()
            if fresh.get('fault'):
                raise RuntimeError('native clock stopped by technical fault: '
                                   + str(fresh['fault'].get('reason', fresh['fault'])))
            if self.stop_when is not None and self.stop_when(fresh):
                return fresh
            if fresh['sim_time'] != now or fresh['active_decisions'] or fresh['pending_wakes'] or fresh.get('pending_deliveries', 0):
                continue
            if now >= until:
                return fresh
            try:
                stepped = await self.step()
            except ClockConflict:
                # A native action can begin between the last status read and
                # the step request. The clock refuses the step; retry it.
                continue
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 409:
                    continue
                raise
            if stepped['sim_time'] == now and stepped.get('next_boundary') is None:
                return stepped


class HttpClockControl:
    """Driver-token view of the same ASGI service used by native tool Agents."""
    def __init__(self, *, base_url, control_token, client=None):
        if not base_url or not control_token:
            raise ValueError('HTTP clock control needs URL and driver token')
        self.base_url = base_url.rstrip('/')
        self.headers = {'Authorization': 'Bearer ' + control_token}
        self.client = client

    async def _request(self, method, route):
        if self.client is not None:
            response = await self.client.request(method, self.base_url + route, headers=self.headers)
        else:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.request(method, self.base_url + route, headers=self.headers)
        response.raise_for_status()
        return response.json()

    async def status(self):
        return await self._request('GET', '/clock/status')

    async def step(self):
        return await self._request('POST', '/clock/step')

    async def settle(self, _now):
        return await self._request('POST', '/transport/settle')

    async def evaluate(self):
        return await self._request('GET', '/evaluation')
