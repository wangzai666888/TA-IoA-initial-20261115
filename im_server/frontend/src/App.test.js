import React from "react";
import { render, screen, waitFor, fireEvent, act } from "@testing-library/react";
import App from "./App";

test("loads scenario list and submits a real API job", async () => {
  global.fetch = jest.fn((path, options) => Promise.resolve({ ok: true, json: () => Promise.resolve(
    path === "/api/scenarios" ? [{ name: "delayed_contact" }] : options?.method === "POST" ? { run_id: "job1", scenario: "delayed_contact", status: "queued", planner: "rule" } : []) }));
  render(<App />);
  await waitFor(() => expect(screen.getByRole("option", { name: "delayed_contact" })).toBeInTheDocument());
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "运行实验" })); });
  await waitFor(() => expect(global.fetch).toHaveBeenCalledWith("/api/runs", expect.objectContaining({ method: "POST" })));
});

test("shows completed task timing, windows and event replay", async () => {
  const job = { run_id: "complete", scenario: "delayed_contact", status: "completed", planner: "rule" };
  const result = { outcome: "success", planner: "RuleBasedPlanner", evaluated_until: 700, tasks: {
    inspect: { status: "COMPLETED", actual_start_time: 300, actual_end_time: 660, result_received_at: 660, history: [] }
  }, executions: {} };
  global.fetch = jest.fn(path => Promise.resolve({ ok: true,
    json: () => Promise.resolve(path === "/api/scenarios" ? [{ name: "delayed_contact" }] : path === "/api/runs" ? [job] : path.endsWith("/result") ? result : { objective: "Inspect site", max_time: 700, windows: [], events: [] }),
    text: () => Promise.resolve(JSON.stringify({ event_id: "e1", sim_time: 120, agent_id: "USV-1", event_type: "phase_changed", phase_after: "checking" })) }));
  render(<App />);
  await waitFor(() => expect(screen.getByText("completed · rule")).toBeInTheDocument());
  await act(async () => { fireEvent.click(screen.getByText("completed · rule")); });
  await waitFor(() => expect(screen.getByRole("heading", { name: "终局任务结果" })).toBeInTheDocument());
  expect(screen.getByRole("img", { name: "有向通信窗口" })).toBeInTheDocument();
  expect(screen.getByRole("slider", { name: "仿真时间" })).toBeInTheDocument();
  expect(screen.getByText("COMPLETED")).toBeInTheDocument();
});
