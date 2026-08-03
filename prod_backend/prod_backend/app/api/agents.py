"""
Run a registered agent against uploaded files (the medical-report ->
Excel client workflow, generalized to any skill-pair the graph defines).

Multiple uploads produce ONE combined output, not one file per input --
extraction still runs per-file (each PDF is its own document), but the
Excel-building step runs once at the end over everything that
successfully extracted, producing a single wide comparison table rather
than a pile of separate downloads.

Real, distinct security surface from anything else in this project so
far -- prompt injection assumes hostile *text*; this endpoint accepts
arbitrary *file bytes* from an anonymous client. Three checks matter
here specifically, each closing a different failure mode:

  1. Size is checked by actually counting bytes read, not trusted from
     the Content-Length header, which a client fully controls and can
     misstate.
  2. "Is this really a PDF" is checked by attempting to open it with the
     real PDF library, not by trusting the filename extension -- a file
     named `report.pdf` that isn't one fails here, not deep inside
     extraction with a confusing error.
  3. Downloads are served through an opaque, server-generated id
     (`generated_files.id`), never a client-supplied filename in a path.
     A `GET /files/{name}` built from user input is a textbook
     path-traversal vulnerability; this table exists specifically so
     that shape of endpoint is never necessary.
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.api.deps import enforce_limits, make_cost_recorder
from app.config import settings
from app.debate.panel import default_chat_agent
from app.services.execution import ExecutionHarness, SkillRegistry
from app.skills.excel_generation import build_combined_excel
from app.skills.pdf_extraction import make_pdf_field_extraction_skill

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/agents", tags=["agents"])


async def get_pool(request: Request):
    return request.app.state.pool


class ExtractionStatus(BaseModel):
    original_filename: str
    outcome: str  # 'success' | 'failure'
    field_count: Optional[int] = None
    error: Optional[str] = None


class RunResponse(BaseModel):
    extractions: list[ExtractionStatus]
    combined_file_id: Optional[str] = None
    combined_download_path: Optional[str] = None
    combined_error: Optional[str] = None


async def _save_upload_checked(upload: UploadFile, dest_dir: str) -> str:
    """
    Saves an upload to disk under a server-generated name, enforcing the
    real size cap by counting bytes actually read -- Content-Length is
    client-supplied and not trustworthy on its own.
    """
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, f"{uuid.uuid4()}.pdf")

    total = 0
    with open(dest_path, "wb") as f:
        while chunk := await upload.read(1024 * 1024):
            total += len(chunk)
            if total > settings.max_upload_bytes:
                f.close()
                os.remove(dest_path)
                raise HTTPException(
                    413, f"{upload.filename}: exceeds the "
                    f"{settings.max_upload_bytes // (1024*1024)}MB limit"
                )
            f.write(chunk)

    return dest_path


def _verify_is_real_pdf(path: str) -> None:
    """
    Opens the file with the real PDF library rather than trusting the
    extension or the client-declared content type. A disguised or
    corrupt file fails here, clearly, not deep inside extraction.
    """
    try:
        import pdfplumber
        with pdfplumber.open(path):
            pass
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"not a readable PDF: {exc}") from exc


async def _lookup_task(pool, skill_ref: str):
    row = await pool.fetchrow(
        "SELECT id, name FROM task_nodes WHERE skill_ref = $1 AND t_invalid IS NULL "
        "ORDER BY t_created DESC LIMIT 1",
        skill_ref,
    )
    if row is None:
        raise HTTPException(
            409, f"no task_node configured with skill_ref={skill_ref!r}. "
            "The graph needs this task seeded before the agent can run."
        )
    return row["id"]


@router.post("/medical-report-extraction/run", response_model=RunResponse)
async def run_medical_report_extraction(
    request: Request,
    files: list[UploadFile],
    pool=Depends(get_pool),
    scope_key: str = Depends(enforce_limits),
) -> RunResponse:
    if not files:
        raise HTTPException(400, "no files uploaded")

    extract_task_id = await _lookup_task(pool, "extract_medical_pdf")
    excel_task_id = await _lookup_task(pool, "build_excel")

    recorder = make_cost_recorder(pool, scope_key, operation="agent_run")
    registry = SkillRegistry()
    registry.register(
        "extract_medical_pdf",
        make_pdf_field_extraction_skill(default_chat_agent(), on_call=recorder),
    )
    # The registered skill_ref stays "build_excel" -- matching whatever
    # was already seeded in the graph -- even though the function behind
    # it now builds a combined multi-report table rather than one file
    # per input. Changing the underlying function without changing the
    # name avoids requiring a re-seed of task_nodes for anyone who
    # already set this up.
    registry.register("build_excel", build_combined_excel)
    harness = ExecutionHarness(pool, registry=registry)

    extractions: list[ExtractionStatus] = []
    # display name -> fields, only for files that actually extracted --
    # a failed extraction contributes nothing to the combined table
    # rather than poisoning it with an empty column.
    successful_fields: dict[str, dict] = {}

    for upload in files:
        original_name = upload.filename or "unknown"
        try:
            saved_path = await _save_upload_checked(upload, settings.agent_upload_dir)
        except HTTPException as exc:
            extractions.append(ExtractionStatus(
                original_filename=original_name, outcome="failure", error=exc.detail,
            ))
            continue

        _verify_is_real_pdf(saved_path)  # raises HTTPException directly on failure

        extract_result = await harness.execute(
            extract_task_id, "extract_medical_pdf", {"pdf_path": saved_path},
            actor_id=scope_key,
        )

        if extract_result.outcome != "success":
            extractions.append(ExtractionStatus(
                original_filename=original_name, outcome="failure",
                error=extract_result.error,
            ))
            continue

        fields = extract_result.output["fields"]
        # Two uploads sharing a base filename would otherwise collide as
        # dict keys and silently overwrite one report's column with the
        # other's -- disambiguate rather than let that happen quietly.
        display_name = os.path.splitext(original_name)[0]
        if display_name in successful_fields:
            display_name = f"{display_name} ({len(successful_fields) + 1})"
        successful_fields[display_name] = fields

        extractions.append(ExtractionStatus(
            original_filename=original_name, outcome="success",
            field_count=len(fields),
        ))

    response = RunResponse(extractions=extractions)

    if not successful_fields:
        # Nothing extracted at all -- no point calling the build step,
        # and no combined file to report.
        return response

    combined_result = await harness.execute(
        excel_task_id, "build_excel",
        {"reports": successful_fields, "output_dir": settings.agent_output_dir},
        actor_id=scope_key,
    )

    if combined_result.outcome != "success":
        response.combined_error = combined_result.error
        return response

    display_name = "combined-extracted-fields.xlsx"
    file_row = await pool.fetchrow(
        "INSERT INTO generated_files (disk_path, display_name, content_type, scope_key) "
        "VALUES ($1, $2, "
        "'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', $3) "
        "RETURNING id",
        combined_result.output["file_path"], display_name, scope_key,
    )

    response.combined_file_id = str(file_row["id"])
    response.combined_download_path = f"/v1/agents/files/{file_row['id']}"
    return response


@router.get("/files/{file_id}")
async def download_file(file_id: uuid.UUID, pool=Depends(get_pool)):
    row = await pool.fetchrow(
        "SELECT disk_path, display_name, content_type FROM generated_files WHERE id = $1",
        file_id,
    )
    if row is None:
        raise HTTPException(404, "file not found or already expired")
    if not os.path.exists(row["disk_path"]):
        # The DB row and the actual file can drift apart (disk cleanup,
        # container restart on ephemeral storage) -- a clear 410 beats a
        # generic 500 from FileResponse failing to open a missing path.
        raise HTTPException(410, "file is no longer available")

    await pool.execute(
        "UPDATE generated_files SET downloaded_at = now() WHERE id = $1", file_id
    )
    return FileResponse(
        row["disk_path"], media_type=row["content_type"], filename=row["display_name"]
    )
