import { Fragment, ReactNode } from "react";

/**
 * Minimal markdown renderer for streamed assistant answers.
 *
 * The answer bubble used `whitespace-pre-wrap` and nothing else, so the model's
 * markdown arrived on screen as literal syntax — `**Trip Plan:**` and
 * `* **Nights 1-2:**` shown verbatim. That reads as a bug to anyone looking at
 * it, because it is one.
 *
 * Deliberately NOT `react-markdown`. This component re-renders on every
 * streamed token, so a full CommonMark parse per keystroke is the wrong shape
 * of cost, and the dependency is ~40 kB for four constructs. What the model
 * actually emits here is bold, bullets, headings and the occasional numbered
 * list — so that is what this handles.
 *
 * It renders text nodes only: no raw HTML is ever interpreted, so a model that
 * emits `<script>` gets an escaped string, not an injection.
 */

/** Split on **bold** / *italic* / `code`, keeping everything else verbatim. */
function inline(text: string, keyPrefix: string): ReactNode[] {
  const out: ReactNode[] = [];
  // Bold first: `**x**` would otherwise be eaten by the italic rule.
  const pattern = /(\*\*[^*]+\*\*|`[^`]+`|\*[^*\n]+\*)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;

  while ((m = pattern.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const tok = m[0];
    const key = `${keyPrefix}-i${i++}`;
    if (tok.startsWith("**")) {
      out.push(
        <strong key={key} className="font-semibold text-gray-900">
          {tok.slice(2, -2)}
        </strong>,
      );
    } else if (tok.startsWith("`")) {
      out.push(
        <code key={key} className="px-1 py-0.5 rounded bg-gray-200/70 text-[0.85em] font-mono">
          {tok.slice(1, -1)}
        </code>,
      );
    } else {
      out.push(
        <em key={key} className="italic">
          {tok.slice(1, -1)}
        </em>,
      );
    }
    last = m.index + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export function Markdown({ text }: { text: string }) {
  const lines = text.split("\n");
  const blocks: ReactNode[] = [];
  let bullets: string[] = [];
  let ordered: string[] = [];

  const flush = (key: string) => {
    if (bullets.length) {
      blocks.push(
        <ul key={`ul-${key}`} className="my-1.5 space-y-1 pl-1">
          {bullets.map((b, i) => (
            <li key={i} className="flex gap-2">
              <span className="mt-[7px] h-1 w-1 flex-shrink-0 rounded-full bg-[#e61e4d]" />
              <span className="flex-1">{inline(b, `b${key}-${i}`)}</span>
            </li>
          ))}
        </ul>,
      );
      bullets = [];
    }
    if (ordered.length) {
      blocks.push(
        <ol key={`ol-${key}`} className="my-1.5 space-y-1 pl-1">
          {ordered.map((b, i) => (
            <li key={i} className="flex gap-2">
              <span className="text-[11px] font-semibold text-[#e61e4d] mt-[1px] w-3.5 flex-shrink-0">
                {i + 1}.
              </span>
              <span className="flex-1">{inline(b, `o${key}-${i}`)}</span>
            </li>
          ))}
        </ol>,
      );
      ordered = [];
    }
  };

  lines.forEach((raw, idx) => {
    const line = raw.trimEnd();
    const bullet = line.match(/^\s*[-*+]\s+(.*)$/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    const heading = line.match(/^(#{1,4})\s+(.*)$/);

    if (bullet) {
      if (ordered.length) flush(String(idx));
      bullets.push(bullet[1]);
      return;
    }
    if (numbered) {
      if (bullets.length) flush(String(idx));
      ordered.push(numbered[1]);
      return;
    }
    flush(String(idx));

    if (heading) {
      blocks.push(
        <p key={`h-${idx}`} className="mt-2 mb-1 font-semibold text-gray-900">
          {inline(heading[2], `h${idx}`)}
        </p>,
      );
      return;
    }
    if (!line.trim()) {
      blocks.push(<div key={`sp-${idx}`} className="h-1.5" />);
      return;
    }
    blocks.push(
      <p key={`p-${idx}`} className="leading-relaxed">
        {inline(line, `p${idx}`)}
      </p>,
    );
  });

  flush("end");
  return <Fragment>{blocks}</Fragment>;
}
