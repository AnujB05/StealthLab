"""
Medical report field extraction: PDF in, structured fields out.

Two real stages, deliberately kept separate:

  1. **Deterministic PDF text/table extraction** (pdfplumber). No model
     call, no ambiguity, fully testable without any API access. Tables
     are rendered as pipe-separated rows so a lab-value grid survives
     into the text an LLM sees, not just flowing prose.

  2. **LLM structuring** of that text into named fields. This is where a
     real model call happens, and it's the part that's never been run
     against real medical-report text -- same "first real run" caveat as
     everything else in this project, stated plainly rather than implied
     to be solved.

Deliberately does NOT attempt the scanned/no-text-layer case yet (the
pdf-reading skill's rasterize-and-read path). A PDF with no text layer
raises clearly rather than silently returning nothing, so a real failure
here is visible, not mistaken for "the report had no fields".

Data-handling note, worth stating plainly rather than glossing over: this
sends a medical report's contents to a third-party LLM API. Real
compliance implications (data processing agreements, HIPAA if the client
is a covered entity) are the client's to resolve before this runs against
real patient data, not something this code can decide for them.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Awaitable

from app.debate.panel import PanelAgent, _extract_json

log = logging.getLogger(__name__)

EXTRACT_SYSTEM = """\
You extract structured fields from the text of a medical report.

Extract every discrete field you can find: patient identifiers, dates, \
test names, measured values with their units, reference ranges, \
diagnoses, and provider notes. Do not summarize or interpret findings, \
extract exactly what is written.

For any numeric measurement, put the bare number in "value" (as a JSON \
number, not a string) and the unit separately in "unit". This matters: \
a value written as a string cannot be used numerically in a spreadsheet.

If the report states a reference or normal range for a test (e.g. \
"13.0-17.0", "< 200", "40-60"), capture it verbatim in \
"reference_range". This is clinically meaningful on its own, a value \
means little without knowing the range it's being judged against, and \
must not be dropped even though it isn't itself a single number. Leave \
it empty only when the report genuinely states no range for that field.

If a value is genuinely not numeric (a name, a diagnosis, free text), \
put it in "value" as a string and leave "unit" and "reference_range" \
empty.

If you cannot read some part of the document with confidence, note it in \
"warnings" rather than guessing at a field value.

Respond with a single JSON object and nothing else:

{
  "fields": {
    "<field name>": {
      "value": <number or string>,
      "unit": "<string, or empty>",
      "reference_range": "<string, or empty>"
    },
    ...
  },
  "warnings": ["<anything uncertain or unreadable>", ...]
}
"""


def extract_pdf_text(pdf_path: str) -> str:
    """
    Deterministic, no model call. Tables render as pipe-separated rows
    so a lab-value grid survives into the text as recognizable structure,
    not just space-mangled prose.
    """
    import pdfplumber

    parts: list[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if text.strip():
                parts.append(text)
            for table in page.extract_tables():
                for row in table:
                    parts.append(" | ".join(cell or "" for cell in row))

    result = "\n".join(parts)
    if not result.strip():
        # A PDF with fonts but empty extracted text is a real, distinct
        # failure from "the report had no fields" -- almost certainly a
        # scanned document with no text layer at all. Raising here means
        # that failure is visible as a real trace, not silently returned
        # as an empty, technically-successful extraction.
        raise ValueError(
            f"no extractable text found in {pdf_path!r}. This usually means "
            "a scanned PDF with no text layer -- rasterize-and-read via "
            "vision is not implemented in this skill yet."
        )
    return result


def make_pdf_field_extraction_skill(
    agent: PanelAgent, on_call=None,
) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
    """
    Closes over a real agent so the skill can be registered once and
    reused, while still respecting whichever provider is actually
    configured (local, General Compute, or the paid default roster) --
    the caller picks the agent, this function doesn't hardcode one.

    `on_call`, if given, fires after the real model call -- the same
    cost-recording hook every other real-model call site in this project
    already has (see app/debate/engine.py). Without this, spend from
    agent runs would silently never reach `llm_spend`, the exact gap
    already found and fixed once elsewhere in this project.
    """

    async def extract_pdf_fields(input_data: dict[str, Any]) -> dict[str, Any]:
        pdf_path = input_data["pdf_path"]
        text = extract_pdf_text(pdf_path)

        raw = await agent.respond(EXTRACT_SYSTEM, text)
        if on_call:
            await on_call(agent, EXTRACT_SYSTEM + text, raw)
        payload = _extract_json(raw)

        fields = payload.get("fields", {})
        if not fields:
            log.warning("extraction produced zero fields for %r -- likely a prompt or "
                        "model-output-format issue, not necessarily an empty report", pdf_path)

        return {"fields": fields, "warnings": payload.get("warnings", [])}

    return extract_pdf_fields
