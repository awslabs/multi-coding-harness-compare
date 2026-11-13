# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import asyncio
import json
import time
import secrets
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, Response
from .artifact_storage import StorageError
from .artifact_readiness import ArtifactsNotReady, readiness
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import EFFORTS, load_config
from .service import Service
from .auth import access_token
from .models import ModelCatalogue
from .judge_models import JudgeModelCatalogue
from .test_review import DEFAULT_TEST_COVERAGE, TestCoverage


_CONFIG_CHOICES = Literal["default", "overrides", "none"]


class Submission(BaseModel):
    prompt: str = Field(min_length=1, max_length=100_000)
    repository: str | None = None
    harnesses: list[Literal["claude", "codex", "kiro"]] = Field(
        default_factory=lambda: ["claude", "codex", "kiro"], min_length=1, max_length=3)
    validation_plan: dict | None = None
    models: dict[Literal["claude", "codex", "kiro"], str | None] = Field(default_factory=dict)
    efforts: dict[Literal["claude", "codex", "kiro"], Literal[EFFORTS] | None] = Field(default_factory=dict)
    judge_model: str | None = Field(default=None, min_length=1, max_length=2048)
    planning_model: str | None = Field(default=None, min_length=1, max_length=2048)
    test_coverage: TestCoverage = DEFAULT_TEST_COVERAGE
    # Per-harness configuration choice: "default" | "overrides" | "none"
    configuration_choices: dict[Literal["claude", "codex", "kiro"], _CONFIG_CHOICES] = Field(
        default_factory=dict)


class JudgeSelection(BaseModel):
    judge_model: str | None = Field(default=None, min_length=1, max_length=2048)


class Answer(BaseModel):
    answer: str | dict


class Revision(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)
    validation_plan: dict | None = None
    harnesses: list[Literal["claude", "codex", "kiro"]] | None = Field(
        default=None, min_length=1, max_length=3)

    @field_validator("harnesses", mode="before")
    @classmethod
    def explicit_harnesses(cls, value):
        if value is None:
            raise ValueError("Select at least one coding harness.")
        return value


class TestFeedback(BaseModel):
    review_id: str
    feedback: str = Field(min_length=1, max_length=20000)
    test_coverage: TestCoverage | None = None


class TestApproval(BaseModel):
    review_id: str
    answers: dict[str, str] = Field(default_factory=dict)


def create_app() -> FastAPI:
    config = load_config()
    service = Service(config)
    catalogue = ModelCatalogue(config, service.store)
    judge_catalogue = JudgeModelCatalogue(config, service.store)
    token = access_token(config.root)

    def coverage_worker_ready(worker):
        return (not worker or time.time() - worker.get("heartbeat", 0) >= 5
                or worker.get("test_coverage_version") == 1)

    def require_coverage_worker():
        try:
            worker = service.store.get("system", "worker")
        except KeyError:
            worker = None
        if not coverage_worker_ready(worker):
            raise HTTPException(409, "Restart the idle worker to load the test coverage options.")

    def selection_worker_ready(worker):
        # A stale heartbeat may be a paused older worker that can still resume.
        return not worker or worker.get("harness_selection_version") == 1

    def require_selection_worker():
        try:
            worker = service.store.get("system", "worker")
        except KeyError:
            worker = None
        if not selection_worker_ready(worker):
            raise HTTPException(409, "Restart the idle worker to load coding harness selection.")

    @asynccontextmanager
    async def lifespan(app):
        yield
        await catalogue.close()
        await judge_catalogue.close()

    app = FastAPI(title="multi-coding-harness-compare", version="1.2.1", lifespan=lifespan)
    app.state.service = service
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])
    static = Path(__file__).parent / "static"

    def authenticated(request: Request) -> bool:
        authorization = request.headers.get("authorization", "")
        supplied = (authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ")
                    else request.cookies.get("arena_session", ""))
        return secrets.compare_digest(supplied, token)

    @app.middleware("http")
    async def same_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin:
            if origin != f"{request.url.scheme}://{request.headers.get('host')}":
                return JSONResponse({"detail": "Cross-origin mutations are not accepted"}, status_code=403)
        if request.url.path.startswith("/api/") and request.url.path != "/api/auth":
            if not authenticated(request):
                return JSONResponse({"detail": "Connect this browser using your multi-coding-harness-compare access link."},
                                    status_code=401, headers={"Cache-Control": "no-store"})
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        return response

    @app.get("/api/auth")
    def session_status(request: Request):
        # Reports only this browser's state, never the local access token.
        return {"authenticated": authenticated(request)}

    @app.post("/api/auth")
    def authenticate(data: Answer):
        if not isinstance(data.answer, str) or not secrets.compare_digest(data.answer, token):
            raise HTTPException(401, "Invalid local access token")
        response = JSONResponse({"authenticated": True})
        response.set_cookie("arena_session", token, httponly=True, samesite="strict", max_age=30 * 86400)
        return response

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail": "Requested object not found"}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(StorageError)
    async def storage_failure(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.exception_handler(ArtifactsNotReady)
    async def artifacts_not_ready(request, exc):
        return JSONResponse({"detail": str(exc), "artifact_readiness": exc.readiness}, status_code=409)

    @app.exception_handler(FileNotFoundError)
    async def missing_artifact(request, exc):
        return JSONResponse({"detail": "Saved artifact file not found"}, status_code=404)

    @app.get("/api/system")
    def system():
        try:
            worker = service.store.get("system", "worker")
        except KeyError:
            worker = None
        return {"worker": worker, "worker_online": bool(worker and time.time() - worker["heartbeat"] < 5),
                "test_coverage": {"supported": True, "worker_ready": coverage_worker_ready(worker),
                                  "default": DEFAULT_TEST_COVERAGE},
                "harness_selection": {"supported": True, "worker_ready": selection_worker_ready(worker)},
                "artifact_readiness": readiness(service.storage),
                "repository_roots": config.repository_roots,
                "limits": {**{k: getattr(config, k) for k in
                           ("active_seconds", "check_seconds", "max_turns", "memory")},
                           "enforcement": ("sampled RSS and process count; no CPU quota"
                               if config.runtime == "native"
                               else "AgentCore microVM session limits")},
                "runtime": config.runtime,
                "harnesses": {k: {"model": v.model,
                                  "effort": v.effort, "efforts": list(EFFORTS),
                                  "home_configured": bool(v.home_template),
                                  "setup": "existing macOS CLI" if v.inherit_host_setup else "dedicated setup",
                                  "cli_path": v.cli_path} for k, v in config.harnesses.items()},
                "judge_configured": bool(config.judge_model), "judge_model": config.judge_model,
                "judge_region": config.judge_region, "judge_profile": config.judge_profile,
                "evaluation_planner": {"model": config.effective_planning_model,
                                       "effort": config.planning_effort,
                                       "max_output_tokens": config.planning_max_tokens,
                                       "timeout_seconds": None},
                "planning_requires_claude": bool(config.planning_effort)}

    @app.post("/api/artifacts/recover")
    def recover_artifacts():
        return service.retry_artifact_recovery()

    @app.get("/api/comparisons")
    def history():
        return sorted(service.store.list("comparison"), key=lambda c: c["created_at"], reverse=True)

    @app.get("/api/models")
    async def models():
        return await catalogue.get()

    @app.post("/api/models/refresh")
    async def refresh_models():
        return await catalogue.get(refresh=True)

    @app.get("/api/judge/models")
    async def judge_models():
        return await judge_catalogue.get()

    @app.post("/api/judge/models/refresh")
    async def refresh_judge_models():
        return await judge_catalogue.get(refresh=True)

    @app.get("/api/projects/directories")
    def directories(path: str | None = None):
        return service.directories(path)

    @app.get("/api/harness-configuration")
    def harness_configuration(harness: str | None = None):
        """Return the configuration plan for each harness (or a specific one).

        Reports every source (defaults, S3, explicit overrides) with file names,
        skills, MCP server names, and which keys look like secrets — never values.
        """
        from .host_setup import configuration_plan
        harnesses = (
            {harness: config.harnesses[harness]} if harness and harness in config.harnesses
            else config.harnesses)
        return {h: configuration_plan(config, h) for h in harnesses}

    @app.post("/api/comparisons", status_code=201)
    async def submit(data: Submission):
        require_coverage_worker()
        # All three in any order is the comparison every worker can run.
        if set(data.harnesses) != {"claude", "codex", "kiro"}:
            require_selection_worker()
        # The configured defaults are checked against the judge catalogue as well.
        if data.judge_model or data.planning_model or config.effective_planning_model:
            await judge_catalogue.get()
        # Repository snapshots can be large; keep streaming/health endpoints responsive.
        return await asyncio.to_thread(service.create, data.prompt, data.repository,
                                       data.validation_plan, data.models, data.judge_model, data.test_coverage,
                                       data.efforts, data.planning_model,
                                       data.configuration_choices or {}, data.harnesses)

    @app.get("/api/comparisons/{id}")
    def detail(id: str):
        return service.report(id)

    @app.post("/api/comparisons/{id}/archive")
    def retry_archive(id: str):
        return service.retry_archive(id)

    @app.post("/api/comparisons/{id}/revisions")
    def revision(id: str, data: Revision):
        if data.harnesses is not None:
            require_selection_worker()
        return service.revise(id, data.text, data.validation_plan, data.harnesses)

    @app.post("/api/questions/{id}/answer")
    def answer(id: str, data: Answer):
        return service.answer(id, data.answer)

    @app.post("/api/comparisons/{id}/test-review/changes")
    def test_feedback(id: str, data: TestFeedback):
        if data.test_coverage is not None:
            require_coverage_worker()
        return service.review_tests(id, data.review_id, feedback=data.feedback, test_coverage=data.test_coverage)

    @app.post("/api/comparisons/{id}/test-review/start")
    def start_test_review(id: str):
        return service.start_test_review(id)

    @app.post("/api/comparisons/{id}/test-review/approve")
    def test_approval(id: str, data: TestApproval):
        return service.review_tests(id, data.review_id, answers=data.answers)

    @app.post("/api/comparisons/{id}/cancel")
    def cancel(id: str):
        service.cancel(id)
        return {"requested": True}

    @app.post("/api/comparisons/{id}/resume")
    def resume_comparison(id: str):
        return service.retry_comparison(id)

    @app.post("/api/comparisons/{id}/reevaluate")
    async def reevaluate(id: str, spec_version: int | None = None,
                         data: JudgeSelection | None = None, run_id: str | None = None):
        model = data.judge_model if data else None
        if model:
            await judge_catalogue.get()
        return service.reevaluate(id, spec_version, model, run_id)

    @app.post("/api/comparisons/{id}/reverify")
    async def reverify(id: str, data: JudgeSelection | None = None):
        model = data.judge_model if data else None
        if model:
            await judge_catalogue.get()
        return service.reverify(id, judge_model=model)

    @app.post("/api/runs/{id}/cancel")
    def cancel_run(id: str):
        run = service.store.get("run", id)
        service.cancel(run["comparison_id"], id)
        return {"requested": True}

    @app.post("/api/runs/{id}/resume")
    def resume_run(id: str):
        return service.retry(id)

    @app.get("/api/comparisons/{id}/test-review/attempts/{index}")
    def test_review_attempt(id: str, index: int):
        try:
            return service.read_test_review_attempt(id, index)
        except KeyError as exc:
            raise HTTPException(404, str(exc) or "attempt not found")
        except (StorageError, FileNotFoundError) as exc:
            raise HTTPException(404, f"archived record unavailable: {exc}")

    @app.get("/api/comparisons/{id}/events")
    def events(id: str, after: int = 0):
        service.store.get("comparison", id)
        return service.store.events(id, after)

    @app.get("/api/comparisons/{id}/stream")
    async def stream(id: str, request: Request, after: int = 0, last_event_id: str | None = Header(None)):
        service.store.get("comparison", id)
        cursor = max(after, int(last_event_id or 0))

        async def generate():
            nonlocal cursor
            while not await request.is_disconnected():
                events = service.store.events(id, cursor)
                for event in events:
                    cursor = event["seq"]
                    yield f"id: {cursor}\ndata: {json.dumps(event)}\n\n"
                if not events:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(.5)
        return StreamingResponse(generate(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/comparisons/{id}/report")
    def report(id: str):
        return JSONResponse(service.report(id), headers={
            "Content-Disposition": f'attachment; filename="comparison-{id[:8]}.json"'})

    @app.get("/api/runs/{id}/patch")
    def download_patch(id: str):
        run = service.store.get("run", id)
        path = run["artifacts"].get("patch")
        if not path:
            raise HTTPException(404, "No implementation patch has been collected")
        return Response(service.storage.read_file(run["comparison_id"], path), media_type="text/plain",
                        headers={"Content-Disposition": f'attachment; filename="{id[:8]}.patch"'})

    @app.get("/api/runs/{id}/diff")
    def diff(id: str):
        run = service.store.get("run", id)
        path = run["artifacts"].get("patch")
        return {"diff": service.storage.read_file(run["comparison_id"], path).decode(errors="replace") if path else None}

    @app.get("/api/runs/{id}/files")
    def files(id: str):
        run = service.store.get("run", id)
        path = run["artifacts"].get("snapshot")
        return {"files": service.storage.files(run["comparison_id"], path) if path else []}

    @app.get("/api/runs/{id}/file")
    def file(id: str, path: str):
        run = service.store.get("run", id)
        root = run["artifacts"].get("snapshot")
        if not root:
            raise HTTPException(404, "No implementation snapshot")
        candidate = Path(root) / path
        if Path(path).is_absolute() or ".." in Path(path).parts:
            raise HTTPException(404, "File not found")
        return {"path": path, "content": service.storage.read_file(
            run["comparison_id"], candidate).decode(errors="replace")}

    @app.get("/")
    def index():
        return FileResponse(static / "index.html")

    app.mount("/static", StaticFiles(directory=static), name="static")
    return app
