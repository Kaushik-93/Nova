import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/** Formats text a model wrote from source data, so nothing in it is trusted. Raw HTML stays off,
 *  which is the library's default, and images are left out, because loading one would make the
 *  reader's browser request whatever address the text names. */
export function Markdown({ children }: { children: string }) {
  return (
    <div className="markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        disallowedElements={["img"]}
        unwrapDisallowed
        components={{ a: ({ href, children: label }) => <a href={href} target="_blank" rel="noreferrer noopener">{label}</a> }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
