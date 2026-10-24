import { memo } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

// Chat content is text, never trusted HTML. Images remain explicit links so a
// generated reply cannot cause the browser to contact a remote image host.
export const MessageMarkdown = memo(function MessageMarkdown({ content }: { content: string }) {
  return <div className="message-markdown"><Markdown skipHtml remarkPlugins={[remarkGfm]} components={{
    a: ({ href, children }) => href
      ? <a href={href} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span>,
    img: ({ src, alt }) => src
      ? <a href={src} target="_blank" rel="noopener noreferrer">{alt || "View image"}</a> : <span>{alt}</span>,
    h1: ({ children }) => <h3>{children}</h3>,
    h2: ({ children }) => <h3>{children}</h3>,
  }}>{content}</Markdown></div>;
});
