"use client";

import Link from "next/link";
import { useRef, useState } from "react";
import { api, ChatResponse } from "@/lib/api";
import GroundedAnswer from "@/components/GroundedAnswer";

type Enquiry = {
  question: string;
  response: ChatResponse | null;
  error: string | null;
};

export default function ArchivePage() {
  const [enquiries, setEnquiries] = useState<Enquiry[]>([]);
  const [question, setQuestion] = useState("");
  const [asking, setAsking] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  async function submit() {
    const q = question.trim();
    if (!q || asking) return;

    setQuestion("");
    setAsking(true);
    const index = enquiries.length;
    setEnquiries((prev) => [...prev, { question: q, response: null, error: null }]);

    try {
      const response = await api.ask(q);
      setEnquiries((prev) =>
        prev.map((e, i) => (i === index ? { ...e, response } : e)),
      );
    } catch (e) {
      setEnquiries((prev) =>
        prev.map((item, i) =>
          i === index
            ? {
                ...item,
                error:
                  e instanceof Error
                    ? e.message
                    : "Could not reach the knowledge service.",
              }
            : item,
        ),
      );
    } finally {
      setAsking(false);
      requestAnimationFrame(() =>
        bottomRef.current?.scrollIntoView({ behavior: "smooth" }),
      );
    }
  }

  return (
    <main className="shell">
      <div className="masthead">
        <div>
          <div className="masthead-title">Archive</div>
          <div className="masthead-sub">ask about documented workflows</div>
        </div>
      </div>

      <div className="nav-tabs">
        <Link href="/workbench" className="nav-tab">
          Workbench
        </Link>
        <Link href="/approvals" className="nav-tab">
          Docket
        </Link>
        <Link href="/archive" className="nav-tab active">
          Archive
        </Link>
        <Link href="/agents/medical-report-extraction" className="nav-tab">
          Agents
        </Link>
      </div>

      {enquiries.length === 0 && (
        <div className="empty-state">
          Ask about a task, a policy, or how part of a workflow fits together.
          <br />
          Answers are drawn only from what&rsquo;s documented in the graph.
        </div>
      )}

      {enquiries.map((enquiry, i) => (
        <div key={i} className="enquiry">
          <div className="enquiry-question">{enquiry.question}</div>

          {enquiry.error && (
            <div className="enquiry-answer thin">{enquiry.error}</div>
          )}

          {!enquiry.error && !enquiry.response && (
            <div className="enquiry-answer" style={{ opacity: 0.6 }}>
              Searching the graph&hellip;
            </div>
          )}

          {enquiry.response && (
            <>
              <div
                className={`enquiry-answer${
                  enquiry.response.groundedness === 0 &&
                  !enquiry.response.context_empty
                    ? " thin"
                    : ""
                }`}
              >
                <GroundedAnswer
                  text={enquiry.response.answer}
                  citedNodeIds={enquiry.response.cited_node_ids}
                  unresolvedCitations={enquiry.response.unresolved_citations}
                />
              </div>

              <div className="enquiry-footer">
                <span>
                  {enquiry.response.retrieved_count} node
                  {enquiry.response.retrieved_count === 1 ? "" : "s"} consulted
                </span>
                <span>
                  grounding {enquiry.response.groundedness.toFixed(2)}
                </span>
                {enquiry.response.unresolved_citations.length > 0 && (
                  <span className="enquiry-warning">
                    {enquiry.response.unresolved_citations.length} citation
                    {enquiry.response.unresolved_citations.length === 1
                      ? ""
                      : "s"}{" "}
                    could not be verified
                  </span>
                )}
                {enquiry.response.groundedness === 0 &&
                  !enquiry.response.context_empty && (
                    <span className="enquiry-warning">
                      nothing in this answer is anchored to the graph
                    </span>
                  )}
              </div>
            </>
          )}
        </div>
      ))}

      <div ref={bottomRef} />

      <div className="ask-bar">
        <input
          className="ask-input"
          value={question}
          placeholder="e.g. what does the extraction step depend on?"
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") submit();
          }}
          disabled={asking}
        />
        <button
          className="ask-button"
          onClick={submit}
          disabled={asking || !question.trim()}
        >
          {asking ? "Asking…" : "Ask"}
        </button>
      </div>
    </main>
  );
}
