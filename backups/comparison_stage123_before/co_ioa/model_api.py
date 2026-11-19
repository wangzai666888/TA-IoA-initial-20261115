"""Real configurable Chat Completions transport; no provider keys in source."""
import json
import math
import os
import sys
import time
from urllib.parse import urlparse
import httpx
from .planner import LLMPlanner, RuleBasedPlanner
from .errors import ModelServiceError


class ModelAPI:
    def __init__(self, audit, *, environment=None, transport=None):
        env = os.environ if environment is None else environment
        self.base = env.get("CO_IOA_API_BASE", "").rstrip("/")
        self.model = env.get("CO_IOA_MODEL", "")
        self.key = env.get("CO_IOA_API_KEY", "")
        parsed = urlparse(self.base)
        if not self.model or parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("LLM mode requires CO_IOA_API_BASE and CO_IOA_MODEL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("API base must not contain credentials, query or fragment")
        if not self.key and env.get("CO_IOA_ALLOW_UNAUTHENTICATED", "false").lower() != "true":
            raise ValueError("Set CO_IOA_API_KEY, or explicitly allow an unauthenticated local model")
        self.timeout = float(env.get("CO_IOA_MODEL_TIMEOUT", "120"))
        self.max_timeout = float(env.get("CO_IOA_MODEL_MAX_TIMEOUT", str(self.timeout * 2)))
        self.max_retries = int(env.get("CO_IOA_MODEL_RETRIES", "2"))
        self.retry_backoff = float(env.get("CO_IOA_MODEL_RETRY_BACKOFF", "1"))
        if (not math.isfinite(self.timeout) or self.timeout <= 0
                or not math.isfinite(self.max_timeout) or self.max_timeout < self.timeout
                or not 0 <= self.max_retries <= 5
                or not math.isfinite(self.retry_backoff) or not 0 <= self.retry_backoff <= 30):
            raise ValueError("invalid model timeout/retry configuration")
        self.temperature = float(env.get("CO_IOA_TEMPERATURE", "0"))
        self.trust_env = env.get("CO_IOA_MODEL_TRUST_ENV", "true").lower() == "true"
        self.transport, self.audit = transport, audit

    @property
    def request_budget_seconds(self):
        # Cover all transport attempts and backoff in the enclosing local HTTP
        # control request. Read timeouts increase only after a timed-out attempt.
        phases = min(10, self.timeout) + min(30, self.timeout) + min(10, self.timeout)
        return (self.max_retries + 1) * (self.max_timeout + phases) + 30 * self.max_retries

    def __call__(self, prompt, context, feedback):
        headers = {"Authorization": "Bearer " + self.key} if self.key else {}
        payload = {"model": self.model, "temperature": self.temperature,
                   "response_format": {"type": "json_object"},
                   "messages": [{"role": "system", "content": prompt},
                                {"role": "user", "content": json.dumps(context, ensure_ascii=False) +
                                 ("\nCorrection required: " + feedback if feedback else "")}]}
        read_timeout = self.timeout
        for attempt in range(self.max_retries + 1):
            started = time.monotonic()
            row = {"model": self.model, "provider_base": self.base, "status": "started", "usage": None,
                   "agent_id": context.get("agent_id"), "sim_time": context.get("simulation_time"),
                   "transport_attempt": attempt + 1, "read_timeout_seconds": read_timeout,
                   "temperature": self.temperature}
            self.audit.append(row)
            retry_after = 0.0
            error = None
            timed_out = False
            try:
                timeout = httpx.Timeout(read_timeout, connect=min(10, self.timeout),
                                        write=min(30, self.timeout), pool=min(10, self.timeout))
                with httpx.Client(timeout=timeout, trust_env=self.trust_env, transport=self.transport) as client:
                    response = client.post(self.base + "/chat/completions", headers=headers, json=payload)
                if response.status_code >= 400:
                    row.update(status="http_error", http_status=response.status_code)
                    try:
                        retry_after = min(30.0, max(0.0, float(response.headers.get("retry-after", "0"))))
                    except ValueError:
                        pass
                    raise ModelServiceError(f"Model API returned HTTP {response.status_code}",
                        retryable=response.status_code in {408, 429, 500, 502, 503, 504})
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                if not isinstance(content, str):
                    row["status"] = "invalid_response"
                    raise ModelServiceError("Model API did not return text JSON content")
                row.update(status="success", usage=data.get("usage"))
                return content
            except ModelServiceError as exc:
                error = exc
            except httpx.HTTPError as exc:
                timed_out = isinstance(exc, httpx.TimeoutException)
                row.update(status="transport_or_response_error", error_type=type(exc).__name__)
                error = ModelServiceError("Model API request/response failed: " + type(exc).__name__,
                    retryable=isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)))
            except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
                row.update(status="invalid_response", error_type=type(exc).__name__)
                error = ModelServiceError("Model API response invalid: " + type(exc).__name__, retryable=True)
            finally:
                row["elapsed_wall_seconds"] = round(time.monotonic() - started, 4)
            retry = error.retryable and attempt < self.max_retries
            row["retry_scheduled"] = retry
            if not retry:
                raise error from None
            if timed_out:
                read_timeout = min(self.max_timeout, read_timeout * 2)
            delay = min(30.0, max(retry_after, self.retry_backoff * 2 ** attempt))
            row["retry_delay_seconds"] = delay
            print(f"[LLM] {error}; retry {attempt + 2}/{self.max_retries + 1}, "
                  f"read timeout {read_timeout:g}s", file=sys.stderr, flush=True)
            time.sleep(delay)


def make_planner(sim, catalog, scenario):
    mode = scenario.get("planner", "rule")
    if mode == "rule":
        return RuleBasedPlanner(scenario["requirements"], scenario.get("revisions", []))
    if mode != "llm":
        raise ValueError("planner must be rule or llm")
    planner = LLMPlanner(catalog, ModelAPI(sim.model_calls), audit=sim.planning_attempts)
    planner.PROMPT += "\nPublic capability templates: " + json.dumps(catalog.templates, ensure_ascii=False)
    planner.PROMPT += "\nInitial task specification: " + json.dumps(scenario["requirements"], ensure_ascii=False)
    planner.PROMPT += ("\nUse the initial specification for first planning; subsequent changes must obey "
                       "the current hard constraints and permissions. No executor selection or invented shorter durations.")
    return planner
