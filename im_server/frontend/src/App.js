import React, { useEffect, useMemo, useState } from "react";
import GroupList from "./components/GroupList/GroupList";
import ChatWindow from "./components/ChatWindow/ChatWindow";
import "./App.css";

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) throw new Error(`HTTP ${response.status}: ${await response.text()}`);
  return response.json();
}

function Windows({ scenario, time }) {
  let windows = [...scenario.windows];
  for (const event of scenario.events || []) {
    if (event.type === "contact_change" && event.time <= time) {
      windows = windows.filter(w => w.sender_id !== event.sender || w.receiver_id !== event.receiver);
      windows.push(...event.intervals.map(([start, end]) => ({ sender_id: event.sender, receiver_id: event.receiver, start, end })));
    }
  }
  const pairs = [...new Set(scenario.windows.map(w => `${w.sender_id} → ${w.receiver_id}`))];
  const x = t => 210 + t / (scenario.max_time || 1200) * 760;
  return <section className="panel"><h2>实际通信窗口 · t={time}s</h2>
    <svg role="img" aria-label="有向通信窗口" viewBox={`0 0 1000 ${pairs.length * 32 + 32}`}>
      {pairs.map((pair, index) => <g key={pair}>
        <text x="5" y={index * 32 + 22}>{pair}</text>
        <line x1="210" x2="970" y1={index * 32 + 18} y2={index * 32 + 18} stroke="#e2e8f0" />
        {windows.filter(w => `${w.sender_id} → ${w.receiver_id}` === pair).map((w, i) =>
          <rect key={i} x={x(w.start)} y={index * 32 + 8} width={Math.max(2, x(w.end) - x(w.start))} height="18" fill="#0d9488"><title>{`[${w.start}, ${w.end})`}</title></rect>)}
      </g>)}
      <line x1={x(time)} x2={x(time)} y1="0" y2={pairs.length * 32} stroke="#e11d48" strokeWidth="2" />
    </svg><small>评估侧真实窗口；Agent 仅使用自己已知的预测。</small>
  </section>;
}

function TaskTimeline({ result, time }) {
  const rows = Object.entries(result.tasks).flatMap(([name, record]) => record.history.map(h => ({ name, ...h })));
  const x = t => 210 + t / (result.evaluated_until || 1) * 760;
  return <section className="panel"><h2>分配历史与实际执行</h2>
    <svg role="img" aria-label="任务时间轴" viewBox={`0 0 1000 ${rows.length * 38 + 32}`}>
      {rows.map((row, index) => {
        const a = row.assignment, e = result.executions[a.assignment_id], y = index * 38;
        return <g key={a.assignment_id}>
          <text x="5" y={y + 20}>{row.name} / {a.executor_id}</text>
          <rect x={x(a.planned_start_time)} y={y + 6} width={Math.max(2, x(a.planned_end_time) - x(a.planned_start_time))} height="20" fill={row.status === "CANCELLED" ? "#cbd5e1" : "#99c6fa"}><title>{`计划 ${a.planned_start_time}–${a.planned_end_time}; ${row.status}`}</title></rect>
          {e && e.actual_start_time != null && <rect x={x(e.actual_start_time)} y={y + 12} width={Math.max(2, x(e.actual_end_time ?? result.evaluated_until) - x(e.actual_start_time))} height="8" fill="#1e40af"><title>实际执行（终局评估记录）</title></rect>}
        </g>;
      })}
      <line x1={x(time)} x2={x(time)} y1="0" y2={rows.length * 38} stroke="#e11d48" />
    </svg><small>浅蓝：计划；深蓝：实际执行；灰色：取消。包含终局评估数据。</small>
  </section>;
}

export default function App() {
  const [scenarios, setScenarios] = useState([]), [runs, setRuns] = useState([]);
  const [scenario, setScenario] = useState("delayed_contact"), [planner, setPlanner] = useState("rule");
  const [backend, setBackend] = useState("ioa"), [selected, setSelected] = useState(null);
  const [data, setData] = useState(null), [error, setError] = useState("");
  const [time, setTime] = useState(0), [playing, setPlaying] = useState(false), [controlKey, setControlKey] = useState("");
  useEffect(() => {
    let active = true;
    api("/api/scenarios").then(x => active && setScenarios(x)).catch(e => active && setError(e.message));
    const refresh = () => api("/api/runs").then(x => active && setRuns(x)).catch(e => active && setError(e.message));
    refresh(); const interval = setInterval(refresh, 1500);
    return () => { active = false; clearInterval(interval); };
  }, []);
  const jobStatus = runs.find(r => r.run_id === selected)?.status;
  const job = runs.find(r => r.run_id === selected);
  useEffect(() => {
    let active = true; setData(null); setPlaying(false);
    if (selected && jobStatus === "completed") {
      Promise.all([api(`/api/runs/${selected}/result`), api(`/api/runs/${selected}/scenario`),
        fetch(`/api/runs/${selected}/events`).then(r => { if (!r.ok) throw new Error("事件读取失败"); return r.text(); })])
        .then(([result, spec, text]) => { if (active) { setData({ result, spec, events: text.trim().split("\n").filter(Boolean).map(JSON.parse) }); setTime(result.evaluated_until); } })
        .catch(e => active && setError(e.message));
    }
    return () => { active = false; };
  }, [selected, jobStatus]);
  useEffect(() => {
    if (!playing || !data) return;
    const timer = setInterval(() => setTime(t => { if (t >= data.result.evaluated_until) { setPlaying(false); return t; } return Math.min(t + 5, data.result.evaluated_until); }), 100);
    return () => clearInterval(timer);
  }, [playing, data]);
  const visible = useMemo(() => data ? data.events.filter(e => e.sim_time <= time) : [], [data, time]);
  const phase = [...visible].reverse().find(e => e.event_type === "phase_changed")?.phase_after || "planning";
  async function launch() {
    setError("");
    try {
      const row = await api("/api/runs", { method: "POST", headers: { "Content-Type": "application/json", ...(controlKey ? { Authorization: `Bearer ${controlKey}` } : {}) }, body: JSON.stringify({ scenario, planner, backend }) });
      setRuns(old => [row, ...old]); setSelected(row.run_id);
    } catch (e) { setError(e.message); }
  }
  return <div className="app-container">
    <aside className="sidebar"><h1>CO-IoA</h1><p>通信机会约束下的协作实验</p>
      <label>场景<select value={scenario} onChange={e => setScenario(e.target.value)}>{scenarios.map(s => <option key={s.name}>{s.name}</option>)}</select></label>
      <label>规划器<select value={planner} onChange={e => setPlanner(e.target.value)}><option value="rule">规则规划</option><option value="llm">真实模型 API</option></select></label>
      <label>传输<select value={backend} onChange={e => setBackend(e.target.value)}><option value="ioa">IoA HTTP / WebSocket</option><option value="offline">离线机制验证</option></select></label>
      <label>控制密钥（若服务端配置）<input type="password" value={controlKey} onChange={e => setControlKey(e.target.value)} /></label>
      <button onClick={launch}>运行实验</button>
      <GroupList groups={runs.map(r => ({ comm_id: r.run_id, agent_names: [r.scenario], team_name: r.scenario, latest_message: `${r.status} · ${r.outcome || r.planner}` }))} onGroupSelect={setSelected} />
    </aside>
    <main><header><h1>实验记录与回放</h1><span>独立机制 · IoA 共用基础设施</span></header>
      {error && <p role="alert" className="error">{error}</p>}
      {!data && <p>{job ? `${job.status}${job.error ? "：" + job.error : ""}` : "选择左侧场景并运行，或查看已有实验。"}</p>}
      {data && <>
        <section className="panel controls"><strong>结果：{data.result.outcome} · {data.result.planner}</strong><span>回放协调阶段：{phase}</span>
          <button onClick={() => { if (time >= data.result.evaluated_until) setTime(0); setPlaying(!playing); }}>{playing ? "暂停" : "播放"}</button>
          <input aria-label="仿真时间" type="range" min="0" max={data.result.evaluated_until} value={time} onChange={e => { setPlaying(false); setTime(Number(e.target.value)); }} /><span>{time}s</span>
          <a href={`/api/runs/${selected}/result`} target="_blank" rel="noreferrer">结果 JSON</a><a href={`/api/runs/${selected}/manifest`} target="_blank" rel="noreferrer">复现清单</a>
        </section>
        <Windows scenario={data.spec} time={time} />
        <TaskTimeline result={data.result} time={time} />
        <section className="panel"><h2>终局任务结果</h2><table><thead><tr><th>任务</th><th>协调视图状态</th><th>实际开始</th><th>实际完成</th><th>结果收到</th></tr></thead><tbody>{Object.entries(data.result.tasks).map(([id, r]) => <tr key={id}><td>{id}</td><td>{r.status}</td><td>{r.actual_start_time ?? "—"}</td><td>{r.actual_end_time ?? "—"}</td><td>{r.result_received_at ?? "—"}</td></tr>)}</tbody></table></section>
        <ChatWindow messages={visible} teamName={data.spec.objective} />
      </>}
    </main>
  </div>;
}
