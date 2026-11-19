import React from "react";
import "./Message.css";
import getAvatar from "../../utils/avatar";
export default function Message({ message }) {
  return <article className="message"><div className="message-header">
    <img className="avatar" src={getAvatar(message.agent_id)} alt="" />
    <span className="username">{message.agent_id}</span><span className="event-meta">t={message.sim_time}s · {message.event_type}</span>
    </div><div className="message-bubble"><pre>{JSON.stringify(message, null, 2)}</pre></div></article>;
}
