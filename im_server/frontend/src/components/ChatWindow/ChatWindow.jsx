import React from "react";
import Message from "../Message/Message";
import "./ChatWindow.css";
export default function ChatWindow({ messages, teamName }) {
  return <section className="panel"><h2>事件与消息记录 · {messages.length} 条</h2><p>{teamName}</p>
    <div className="chat-window"><div className="chat-messages">{messages.map(message => <Message key={message.event_id} message={message} />)}</div></div>
  </section>;
}
