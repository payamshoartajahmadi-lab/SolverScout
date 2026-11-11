"""Authenticated FastAPI surface for the local Streamlit UI and Workflow Worker."""
from __future__ import annotations

import hmac
import logging
import os
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from p_mc_optimizer.application.errors import ApiError
from p_mc_optimizer.application.jobs import ApplicationService, AnswersInput, JobVersion, public_job
from p_mc_optimizer.domain.models import CreateJob, DomainError, strict_json_loads
from p_mc_optimizer.persistence import Repository
from p_mc_optimizer.research.contracts import CreateJobV2, ResearchOutcome
from p_mc_optimizer.research.contracts_v21 import CreateJobV21

LOGGER = logging.getLogger(__name__)
TOKEN_HEADER = "X-PMC-Internal-Token"
MAX_REQUEST_BYTES = 1024 * 1024
ARTIFACT_DOWNLOADS = {
    "report_json": ("report.json", "application/json"),
    "report_html": ("report.html", "text/html; charset=utf-8"),
    "trials_csv": ("trials.csv", "text/csv; charset=utf-8"),
    "sources_csv": ("sources.csv", "text/csv; charset=utf-8"),
    "research_json": ("research.json", "application/json"),
    "evaluation_history_csv": ("evaluation_history.csv", "text/csv; charset=utf-8"),
}
DOMAIN_CODES = {
    "UNSUPPORTED_PROBLEM", "INVALID_EXPRESSION", "UNKNOWN_VARIABLE",
    "AMBIGUOUS_PROBLEM", "BOUNDS_RANGE_UNSUPPORTED", "PROVIDER_CONFIGURATION_REQUIRED",
}
NUMERIC_POLICY_CODES = {"NUMERIC_POLICY_INVALID", "NUMERIC_POLICY_BUDGET_TOO_SMALL",
                        "NUMERIC_POLICY_ENVIRONMENT_MISMATCH", "NUMERIC_POLICY_QUALIFICATION_REQUIRED",
                        "NUMERIC_POLICY_AUTHORIZATION_INVALID"}
DOMAIN_MESSAGES = {
    "UNSUPPORTED_PROBLEM": "مدل مسئله با دامنهٔ نسخهٔ اول سازگار نیست.",
    "INVALID_EXPRESSION": "عبارت تابع هدف با دستور زبان مجاز سازگار نیست.",
    "UNKNOWN_VARIABLE": "عبارت تابع هدف از نام تعریف‌نشده استفاده می‌کند.",
    "PROVIDER_CONFIGURATION_REQUIRED": "تنظیمات مدل زبانی و اتصال آن را بررسی کنید.",
    "AMBIGUOUS_PROBLEM": "اطلاعات مسئله برای ساخت مدل کامل کافی نیست.",
    "BOUNDS_RANGE_UNSUPPORTED": "فاصلهٔ میان کران‌های یک متغیر در بازهٔ عددی مجاز نیست.",
}


def error_response(request: Request, error: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content={
            "error_code": error.error_code,
            "message_fa": error.message_fa,
            "field_errors": error.field_errors,
            "retryable": error.retryable,
            "request_id": getattr(request.state, "request_id", str(uuid4())),
        },
    )


def canonical_id(value: str, code="JOB_NOT_FOUND") -> str:
    try:
        identifier = UUID(value)
    except ValueError:
        identifier = None
    if identifier is None or identifier.version != 4 or str(identifier) != value:
        raise ApiError(404, code, "شناسهٔ درخواست‌شده پیدا نشد.")
    return value


def problem_summary(job: dict) -> str | None:
    """Return a bounded display summary for the authenticated history view."""
    source = job.get("input") or {}
    if source.get("kind") == "natural_language":
        title = " ".join((source.get("text") or "").split())
    elif source.get("kind") == "structured_problem":
        model = source["problem"]
        objective = model["objective"]
        sense = "بیشینه‌سازی" if objective["sense"] == "maximize" else "کمینه‌سازی"
        function = objective["source"]
        formula = function.get("expression") or function.get("function_id")
        title = f"{sense} با {len(model['variables'])} متغیر"
        if formula:
            formula = " ".join(formula.split())
            available = 115 - len(title)
            if len(formula) > available:
                formula = formula[:available - 1] + "…"
            return f"{title} · \u2066{formula}\u2069"
    else:
        return None
    return title[:117] + "…" if len(title) > 120 else title or None


def job_routes(repo: Repository, service: ApplicationService, research_store=None) -> APIRouter:
    router = APIRouter()

    @router.post("/jobs", status_code=202)
    def create_job(payload: CreateJob | CreateJobV2 | CreateJobV21,
                   idempotency_key: Annotated[str, Header(alias="Idempotency-Key")]):
        return public_job(service.create_job(payload, idempotency_key))

    @router.get("/jobs")
    def list_jobs(
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
        include_problem_summary: bool = False,
    ):
        jobs = repo.list_jobs(limit=limit, offset=offset)
        if include_problem_summary:
            return [public_job(job) | {"problem_summary": problem_summary(job)} for job in jobs]
        return [public_job(job) for job in jobs]

    @router.get("/jobs/{job_id}")
    def get_job(job_id: str):
        return public_job(repo.get_job(canonical_id(job_id)))

    @router.get("/jobs/{job_id}/problem")
    def get_problem(job_id: str):
        return repo.get_problem(canonical_id(job_id))

    @router.post("/jobs/{job_id}/answers", status_code=202)
    def answers(job_id: str, payload: AnswersInput):
        return public_job(service.answer_job(canonical_id(job_id), payload))

    @router.post("/jobs/{job_id}/resume", status_code=202)
    def resume(job_id: str, payload: JobVersion):
        return public_job(service.resume_job(canonical_id(job_id), payload.expected_job_version))

    @router.post("/jobs/{job_id}/cancel", status_code=202)
    def cancel(job_id: str, payload: JobVersion, response: Response):
        status, job = service.cancel_job(canonical_id(job_id), payload.expected_job_version)
        response.status_code = status
        return public_job(job)

    @router.get("/jobs/{job_id}/events")
    def events(
        job_id: str,
        after_sequence: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ):
        return repo.list_events(canonical_id(job_id), after_sequence, limit)

    @router.get("/jobs/{job_id}/trials")
    def trials(job_id: str):
        return repo.list_trials(canonical_id(job_id))

    @router.get("/jobs/{job_id}/report")
    def report(job_id: str):
        return repo.get_report(canonical_id(job_id))

    @router.get("/jobs/{job_id}/research")
    def job_research(job_id: str):
        job = repo.get_job(canonical_id(job_id))
        if job["policy"]["policy_version"] == "2.1":
            return job["workflow"].get("research_outcome") or {
                "schema_version": "2.1", "retrieval_mode": job["policy"]["retrieval_mode"],
                "status": {
                    "failed": "failed", "cancelled": "cancelled",
                    "completed": "not_requested", "running": "running",
                }.get(job["job_status"], "pending"),
                "sync_run_id": job["workflow"].get("source_sync_run_id"),
            }
        if job["policy"]["policy_version"] != "2.0":
            return {"status": "not_requested"}
        outcome = research_store.outcome(job_id)
        if outcome is not None:
            from p_mc_optimizer.api.research_v2 import public_research

            return public_research(ResearchOutcome.model_validate(outcome))
        status = (
            "cancelled" if job["job_status"] == "cancelled" else
            "running" if job["phase"] in {"research_planning", "searching", "fetching_evidence"} else
            "pending" if job["policy"]["selection_policy"] == "web_research_then_pilot" else "not_requested"
        )
        usage = research_store.ledger(job_id)
        return {"status": status, "query_plan": job["workflow"].get("query_plan"), **usage}

    @router.get("/artifacts/{artifact_id}")
    def artifact(artifact_id: str):
        artifact_id = canonical_id(artifact_id, "ARTIFACT_NOT_FOUND")
        metadata = repo.get_artifact(artifact_id)
        path = repo.artifact_path(artifact_id)
        fallback_name = path.name
        if path.suffix in {".json", ".html", ".csv", ".jsonl"}:
            fallback_name = metadata["kind"] + path.suffix
        filename, media_type = ARTIFACT_DOWNLOADS.get(
            metadata["kind"], (fallback_name, "application/octet-stream"),
        )
        return FileResponse(
            path, filename=filename, media_type=media_type,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    return router


def create_app(
    data_dir: Path | str,
    internal_token: str,
    *,
    transport=None,
    research_search=None,
    research_fetch=None,
    library_transport=None,
    instance_id: str | None = None,
) -> FastAPI:
    from p_mc_optimizer.runtime_provenance import startup, runtime_status

    startup("api", instance_id=instance_id, data_dir=data_dir)
    from p_mc_optimizer.api.library import LibrarySettings, library_router, sync_lifespan
    from p_mc_optimizer.api.library_jobs import LibraryJobs
    from p_mc_optimizer.catalog import CatalogStore
    from p_mc_optimizer.catalog.routes import catalog_router
    from p_mc_optimizer.research.gateway import ResearchGateway
    from p_mc_optimizer.research.library_retrieval import LibraryRetriever
    from p_mc_optimizer.research.library_store import LibraryStore
    from p_mc_optimizer.research.library_sync import SourceSynchronizer
    from p_mc_optimizer.research.routes import create_research_router
    from p_mc_optimizer.research.settings import SearchSettings
    from p_mc_optimizer.research.storage import ResearchStore
    from p_mc_optimizer.llm.gateway import LLMGateway
    from p_mc_optimizer.llm.routes import create_llm_router

    if not internal_token:
        raise ValueError("An instance token is required")
    repo = Repository(data_dir)
    gateway = LLMGateway(repo, transport=transport, internal_token=internal_token)
    research_store = ResearchStore(repo.path, repo.data_dir / "artifacts" / "research")
    research_gateway = ResearchGateway(research_store, search=research_search, fetch=research_fetch)
    search_settings = SearchSettings(research_store, gateway.broker, research_gateway)
    catalog = CatalogStore(repo.path)
    library = LibraryStore(research_store, sanitize=search_settings.sanitize)
    retriever = LibraryRetriever(library)
    synchronizer = SourceSynchronizer(library, transport=library_transport, sanitize=search_settings.sanitize)
    retrieval = LibrarySettings(library)
    research_jobs = LibraryJobs(repo, gateway, search_settings, catalog, library, retriever, retrieval, synchronizer)
    service = ApplicationService(repo, profile_snapshot=gateway.profile_snapshot,
                                 resume_check=research_jobs.resume, freeze_v2=research_jobs.freeze)
    app = FastAPI(title="SolverScout", version="2.1", redirect_slashes=False, lifespan=sync_lifespan(synchronizer))
    app.state.repo = repo
    app.state.gateway = gateway
    app.state.service = service
    app.state.search_settings = search_settings
    app.state.catalog = catalog
    app.state.library = library
    app.state.synchronizer = synchronizer

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        request.state.request_id = str(uuid4())
        if request.url.path != "/healthz":
            tokens = request.headers.getlist(TOKEN_HEADER)
            if len(tokens) != 1 or not hmac.compare_digest(tokens[0].encode(), internal_token.encode()):
                return error_response(request, ApiError(401, "LOCAL_AUTH_REQUIRED", "درخواست محلی احراز نشد."))
        try:
            if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                length = request.headers.get("Content-Length")
                if length is not None and not 0 <= int(length) <= MAX_REQUEST_BYTES:
                    raise ValueError("Invalid request body length")
                body = await request.body()
                if body:
                    payload = strict_json_loads(body, limit=MAX_REQUEST_BYTES)
                    if request.url.path not in {
                        "/settings/llm/credential", "/settings/search/credential",
                        "/settings/search-providers/tavily/credential",
                    } and (
                        gateway.payload_contains_secret(payload) or search_settings.sanitize(payload) != payload
                    ):
                        return error_response(
                            request, ApiError(422, "INVALID_SCHEMA", "کلید و توکن را فقط از مسیر تنظیمات ثبت کنید."),
                        )
        except (ValueError, UnicodeError, RecursionError):
            return error_response(request, ApiError(422, "INVALID_SCHEMA", "بدنهٔ درخواست باید JSON معتبر UTF-8 باشد."))
        try:
            response = await call_next(request)
        except Exception as error:
            LOGGER.error("Local API failure request_id=%s type=%s", request.state.request_id, type(error).__name__)
            return error_response(request, ApiError(500, "INTERNAL_ERROR", "خطای داخلی رخ داده است."))
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, error: ApiError):
        return error_response(request, error)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, error: StarletteHTTPException):
        code = "NOT_FOUND" if error.status_code == 404 else "HTTP_ERROR"
        return error_response(request, ApiError(error.status_code, code, "مسیر یا روش درخواست در دسترس نیست."))

    @app.exception_handler(DomainError)
    async def handle_domain_error(request: Request, error: DomainError):
        code = error.code if error.code in DOMAIN_CODES | NUMERIC_POLICY_CODES else "INVALID_SCHEMA"
        return error_response(
            request, ApiError(409 if code == "NUMERIC_POLICY_ENVIRONMENT_MISMATCH" else 422, code,
                             str(error) if code in NUMERIC_POLICY_CODES else DOMAIN_MESSAGES.get(code, "ساختار مسئله معتبر نیست.")),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, error: RequestValidationError):
        failures = error.errors()
        code = next((failure["type"] for failure in failures if failure["type"] in DOMAIN_CODES), "INVALID_SCHEMA")
        field_errors = [
            {
                "path": [
                    item for item in (failure["loc"][:-1] if failure["type"] == "extra_forbidden" else failure["loc"])
                    if isinstance(item, int) or (isinstance(item, str) and len(item) <= 48 and item.isidentifier())
                ],
                "code": failure["type"],
            }
            for failure in failures
        ]
        return error_response(
            request,
            ApiError(422, code, DOMAIN_MESSAGES.get(code, "فیلدهای درخواست با ساختار مجاز سازگار نیستند."),
                     field_errors=field_errors),
        )

    @app.get("/healthz")
    def health():
        return {"status": "ok", "instance_id": instance_id}

    @app.get("/runtime")
    def runtime():
        return runtime_status(data_dir)

    app.include_router(job_routes(repo, service, research_store))
    app.include_router(create_research_router(search_settings))
    app.include_router(library_router(library, retriever, search_settings, retrieval, synchronizer))
    app.include_router(research_jobs.router())
    app.include_router(catalog_router(catalog))
    app.include_router(create_llm_router(gateway))
    return app


def create_from_env() -> FastAPI:
    """Uvicorn factory used by the launcher."""
    return create_app(
        os.environ["PMC_DATA_DIR"], os.environ["PMC_INTERNAL_TOKEN"],
        instance_id=os.environ.get("PMC_INSTANCE_ID"),
    )
