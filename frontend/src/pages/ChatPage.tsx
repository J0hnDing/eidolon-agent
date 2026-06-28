import { FormEvent, useState } from "react";

type ChatMessage = {
  id: number;
  role: "user" | "assistant";
  content: string;
};

const initialMessages: ChatMessage[] = [
  {
    id: 1,
    role: "assistant",
    content: "Chat is local UI state for now. Backend and Codex integration come later.",
  },
];

export default function ChatPage() {
  const [messages, setMessages] = useState<ChatMessage[]>(initialMessages);
  const [draft, setDraft] = useState("");

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const content = draft.trim();
    if (!content) return;

    const nextId = Date.now();
    setMessages((current) => [
      ...current,
      { id: nextId, role: "user", content },
      {
        id: nextId + 1,
        role: "assistant",
        content: "Not wired to an assistant yet. This message stays in React state only.",
      },
    ]);
    setDraft("");
  }

  return (
    <section className="page stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">Conversation</p>
          <h1>Chat</h1>
        </div>
      </header>

      <div className="chat-panel">
        <div className="message-list" aria-live="polite">
          {messages.map((message) => (
            <article key={message.id} className={`message ${message.role}`}>
              <span>{message.role}</span>
              <p>{message.content}</p>
            </article>
          ))}
        </div>
        <form className="composer" onSubmit={handleSubmit}>
          <input
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="Type a message"
            aria-label="Chat message"
          />
          <button type="submit">Send</button>
        </form>
      </div>
    </section>
  );
}
