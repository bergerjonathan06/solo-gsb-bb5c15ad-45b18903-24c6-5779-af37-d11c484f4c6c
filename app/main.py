"""FastAPI application: document import, validation tasks, fix preview."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Response

from . import db
from .analyzer import analyze
from .comparison import build_comparison
from .fixer import build_fix_preview
from .gate import GateRequestError, compile_gate_request, evaluate_gate
from .sarif import build_sarif
from .schemas import (
    GateRequest,
    ImportRequest,
    ValidationCompareRequest,
    ValidationRequest,
    gate_payload,
    normalize_doc_path,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()  # auto-initialize the database on startup
    yield


app = FastAPI(
    title="Markdown Reference Closure Validator",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# ---------------------------------------------------------------- imports ---

@app.post("/api/imports", status_code=201)
def create_import(req: ImportRequest) -> dict:
    documents: dict[str, str] = {}
    errors: list[str] = []
    for doc in req.documents:
        try:
            norm = normalize_doc_path(doc.path)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if norm in documents:
            errors.append(f"duplicate path: {norm!r}")
            continue
        documents[norm] = doc.content
    if errors:
        raise HTTPException(status_code=422, detail=errors)

    import_id = db.create_import(documents)
    return {
        "import_id": import_id,
        "doc_count": len(documents),
        "paths": sorted(documents),
    }


@app.get("/api/imports/{import_id}")
def get_import(import_id: str) -> dict:
    imp = db.get_import(import_id)
    if not imp:
        raise HTTPException(status_code=404, detail="import not found")
    return {
        "import_id": imp["id"],
        "created_at": imp["created_at"],
        "doc_count": imp["doc_count"],
        "paths": db.list_document_paths(import_id),
    }


@app.get("/api/imports/{import_id}/documents/{path:path}")
def get_document(import_id: str, path: str) -> dict:
    if not db.get_import(import_id):
        raise HTTPException(status_code=404, detail="import not found")
    content = db.get_document(import_id, path)
    if content is None:
        raise HTTPException(status_code=404, detail="document not found")
    return {"path": path, "content": content}


# ------------------------------------------------------------ validations ---

def _run_validation(validation_id: str) -> None:
    """Background task: build the reference graph and store diagnostics."""
    try:
        db.set_validation_running(validation_id)
        validation = db.get_validation(validation_id)
        documents = db.get_documents(validation["import_id"])
        analysis = analyze(documents)
        db.insert_diagnostics(validation_id, [d.to_dict() for d in analysis.diagnostics])
        db.finish_validation(validation_id, "done", summary=analysis.summary)
    except Exception as exc:  # pragma: no cover - defensive
        db.finish_validation(validation_id, "failed", error=str(exc))


@app.post("/api/validations", status_code=202)
def create_validation(req: ValidationRequest, background: BackgroundTasks) -> dict:
    if not db.get_import(req.import_id):
        raise HTTPException(status_code=404, detail="import not found")
    validation_id = db.create_validation(req.import_id)
    background.add_task(_run_validation, validation_id)
    return {"validation_id": validation_id, "status": "pending"}


@app.get("/api/validations")
def list_validations(limit: int = Query(default=50, ge=1, le=200)) -> dict:
    items = [
        {
            "validation_id": v["id"],
            "import_id": v["import_id"],
            "status": v["status"],
            "created_at": v["created_at"],
            "finished_at": v["finished_at"],
            "summary": v["summary"],
            "error": v["error"],
        }
        for v in db.list_validations(limit)
    ]
    return {"validations": items}


def _require_done_validation(validation_id: str, action: str) -> dict:
    """Load a validation that must exist and be finished; raise 404/409 else."""
    v = db.get_validation(validation_id)
    if not v:
        raise HTTPException(status_code=404, detail="validation not found")
    if v["status"] != "done":
        raise HTTPException(
            status_code=409,
            detail=f"validation is {v['status']}; {action} requires status 'done'",
        )
    return v


@app.post("/api/validations/compare")
def compare_validations(req: ValidationCompareRequest) -> dict:
    base = _require_done_validation(
        req.base_validation_id, "validation comparison"
    )
    target = _require_done_validation(
        req.target_validation_id, "validation comparison"
    )
    result = build_comparison(
        base_validation=base,
        target_validation=target,
        base_diagnostics=db.list_diagnostics(base["id"]),
        target_diagnostics=db.list_diagnostics(target["id"]),
        base_documents=db.get_documents(base["import_id"]),
        target_documents=db.get_documents(target["import_id"]),
    )
    return result


@app.get("/api/validations/{validation_id}")
def get_validation(
    validation_id: str,
    type: str | None = Query(default=None),
    path: str | None = Query(default=None),
) -> dict:
    v = db.get_validation(validation_id)
    if not v:
        raise HTTPException(status_code=404, detail="validation not found")
    diagnostics = db.list_diagnostics(validation_id, type, path)
    return {
        "validation_id": v["id"],
        "import_id": v["import_id"],
        "status": v["status"],
        "created_at": v["created_at"],
        "finished_at": v["finished_at"],
        "summary": v["summary"],
        "error": v["error"],
        "diagnostics": [
            {
                "type": d["type"],
                "severity": d["severity"],
                "path": d["path"],
                "line": d["line"],
                "message": d["message"],
                "details": d["details"],
            }
            for d in diagnostics
        ],
    }


# ------------------------------------------------------------------ sarif ---

SARIF_MEDIA_TYPE = "application/sarif+json"


@app.get("/api/validations/{validation_id}/sarif")
def export_validation_sarif(validation_id: str) -> Response:
    v = db.get_validation(validation_id)
    if not v:
        raise HTTPException(status_code=404, detail="validation not found")
    if v["status"] != "done":
        raise HTTPException(
            status_code=409,
            detail=(
                f"validation is {v['status']}; SARIF export requires status 'done'"
            ),
        )
    diagnostics = db.list_diagnostics(validation_id)
    report = build_sarif(v, diagnostics)
    return Response(
        content=json.dumps(report, ensure_ascii=False),
        media_type=SARIF_MEDIA_TYPE,
    )


# ------------------------------------------------------------ fix preview ---

@app.post("/api/validations/{validation_id}/fix-preview")
def fix_preview(validation_id: str) -> dict:
    v = db.get_validation(validation_id)
    if not v:
        raise HTTPException(status_code=404, detail="validation not found")
    if v["status"] != "done":
        raise HTTPException(
            status_code=409,
            detail=f"validation is {v['status']}; fix preview requires status 'done'",
        )
    documents = db.get_documents(v["import_id"])
    preview = build_fix_preview(documents)
    return {
        "validation_id": validation_id,
        "import_id": v["import_id"],
        **preview,
    }


# ------------------------------------------------------------------- gate ---

@app.post("/api/validations/{validation_id}/gate")
def quality_gate(validation_id: str, req: GateRequest) -> dict:
    """Evaluate error/warning thresholds with stable-priority ignore rules."""
    v = _require_done_validation(validation_id, "quality gate evaluation")
    try:
        thresholds, rules = compile_gate_request(gate_payload(req))
    except GateRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    diagnostics = db.list_diagnostics(validation_id)
    return evaluate_gate(validation_id, diagnostics, thresholds, rules)
