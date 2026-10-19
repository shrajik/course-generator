"""Application settings. Everything is environment driven - no secrets in code."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ResearchMode = Literal["fast", "deep"]
WritingMode = Literal["parallel", "sequential"]
AppEnvironment = Literal["development", "production", "test"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        protected_namespaces=(),
    )

    # --- OpenAI ---------------------------------------------------------------
    #  As of the Azure text-model migration, this key is used for ONE thing
    #  only: `embed()` (app.services.openai_service) - text embeddings for
    #  semantic memory. None of the three Azure deployments below are an
    #  embedding model, so this subsystem was explicitly left out of the
    #  migration's scope rather than silently routed anywhere - see
    #  `enable_embeddings`'s own docstring; it already fails soft to
    #  keyword-only search with no key configured at all. Every OTHER text
    #  workflow (writer/editor/planner/reviewer/research/deep research) now
    #  goes through the azure_gpt5*/azure_gpt55* settings below instead, and
    #  never reads this key - see OpenAIClient._client_for.
    openai_api_key: str = ""
    openai_base_url: str | None = None

    # --- Azure text models ----------------------------------------------------
    #  Three separate Azure resources/deployments, one per model, each with
    #  its own endpoint/key/deployment-name (confirmed from the official
    #  "Web search with the Responses API" doc: Azure's /openai/v1/
    #  compatibility endpoint works with the plain `AsyncOpenAI` SDK client
    #  unmodified - just `base_url` + `api_key` pointed at Azure, no
    #  `AsyncAzureOpenAI`, no `api-version` query param - so OpenAIClient
    #  keeps using the same SDK class, just one instance per resource. The
    #  `model` field on every actual request must be the DEPLOYMENT NAME
    #  (`azure_gpt5_deployment` etc.), never the literal "gpt-5" unless that
    #  happens to also be what you named the deployment - see
    #  `_client_for`'s own docstring. `azure_*_endpoint` is the resource's
    #  base `https://<resource>.openai.azure.com/openai/v1/` URL (NOT a
    #  model-specific path - the deployment name goes in the request body,
    #  not the URL, same convention as azure_mai_image_endpoint). All three
    #  fields for a given model are required together - validated at call
    #  time (OpenAIClient._client_for), not here, so constructing Settings
    #  never fails just because a deployment isn't configured yet. Never
    #  logged, never returned to a caller, never exposed to the frontend -
    #  read server-side only, exactly like openai_api_key above.
    azure_gpt5_endpoint: str = ""
    azure_gpt5_api_key: str = ""
    azure_gpt5_deployment: str = ""
    azure_gpt5_mini_endpoint: str = ""
    azure_gpt5_mini_api_key: str = ""
    azure_gpt5_mini_deployment: str = ""
    azure_gpt55_endpoint: str = ""
    azure_gpt55_api_key: str = ""
    azure_gpt55_deployment: str = ""
    #  Genuine multi-step "deep research" behaviour on gpt-5.5 needs this
    #  set explicitly - per Microsoft's own docs, the model alone doesn't
    #  imply deep/agentic search; "Use this mode with o3-deep-research, or
    #  with gpt-5.5 and reasoning.effort set to high or xhigh" (the old
    #  `o4-mini-deep-research` model baked this behaviour in by itself,
    #  which is exactly what's being replaced). Only ever sent on the `deep`
    #  research call, never on a plain chat/structured call, and only when
    #  the resolved model is reasoning-capable - see `research()`.
    deep_research_reasoning_effort: str = "high"

    # Authoring quality matters most for the writer; planning, reviewing and
    # note-shaping are judgement/formatting tasks that a faster tier handles
    # well. If a configured model is unavailable the client falls back to
    # `fallback_model` once, so an aggressive default cannot break a deployment.
    writer_model: str = "gpt-5"
    editor_model: str = "gpt-5"
    planner_model: str = "gpt-5-mini"
    reviewer_model: str = "gpt-5-mini"
    research_model: str = "gpt-5-mini"
    #  Migrated from "o4-mini-deep-research" (an OpenAI-specific specialised
    #  deep-research model) to "gpt-5.5" - see deep_research_reasoning_effort
    #  above for what actually makes this genuinely "deep" rather than a
    #  single shallow search on a bigger model.
    deep_research_model: str = "gpt-5.5"
    fallback_model: str = "gpt-5"
    image_model: str = "gpt-image-1"
    image_size: str = "1024x1024"
    #  Diagrams ask for *structure* (nodes/edges), not pixels, so a cheaper
    #  reasoning-tier model is enough - the same tier as the planner/reviewer.
    diagram_model: str = "gpt-5-mini"

    # --- image provider ------------------------------------------------------
    #  Migrated from "openai" to "azure" (default, as of this setting):
    #  gpt-image-1 is no longer used anywhere by default - the OpenAI account
    #  this project was using ran out of image-generation credit, so every
    #  raster illustration category now defaults to an Azure provider
    #  instead. "azure" routes through Microsoft Azure AI Foundry's
    #  FLUX.2-flex (see app.services.azure_image_provider) - this is what
    #  section_intro icons and every other non-textbook illustration use.
    #  "azure_mai" routes through Microsoft Foundry's MAI-Image-2.6 instead
    #  (see app.services.azure_mai_image_provider, and
    #  textbook_image_provider below, which overrides this default
    #  specifically for `illustration_style: "textbook"`) - a separate value
    #  from "azure" because its request/auth contract is genuinely different
    #  from FLUX.2-flex (api-key header vs Bearer, deployment name in the
    #  body), not just a different endpoint. "openai" still exists and still
    #  works (e.g. for local testing against a different account) but is no
    #  longer selected by default anywhere. Selected explicitly via
    #  configuration; OpenAIClient.image() never silently falls back between
    #  providers, since that could mask a real production misconfiguration
    #  rather than surface it - so a genuinely unconfigured Azure deployment
    #  fails clearly (a missing-endpoint/key error) rather than quietly
    #  spending OpenAI credit again.
    image_provider: str = "azure"
    #  Migrated from "openai" to "azure_mai": textbook-style images now use
    #  Microsoft Foundry's MAI-Image-2.6 (see app.services.
    #  azure_mai_image_provider) instead of OpenAI's gpt-image-1, to use our
    #  Azure deployment rather than OpenAI credits for this category.
    #
    #  History this override exists at all: an earlier real, confirmed
    #  side-by-side comparison (3 topics, both providers, identical prompts)
    #  found Azure's FLUX.2-flex meaningfully less reliable at precise text -
    #  one real run produced a genuinely garbled label ("Irrlstior" instead
    #  of "Indicator") and missed requested labels outright on 2 of 3 topics
    #  (text_verified=False), while gpt-image-1 rendered every requested
    #  label correctly on all 3. MAI-Image-2.6 is a different model with
    #  Microsoft's own documented "stronger text rendering" claim versus
    #  earlier MAI generations - not the same FLUX.2-flex tradeoff - but
    #  `_verify_generated_text`'s bounded verify-and-retry in ImageService
    #  stays in place regardless, so a real label failure is still caught
    #  and flagged (text_verified=False) rather than silently shipped.
    #  A `textbook`-style illustration's whole job is accurately depicting
    #  and labelling a real apparatus/structure (see ImageContent.
    #  illustration_style / TEXTBOOK_ILLUSTRATION_GUIDANCE), which is why
    #  this category gets its own override instead of inheriting
    #  `image_provider` (see ImageService._generate_illustration).
    textbook_image_provider: str = "azure_mai"
    #  Azure AI Foundry FLUX.2-flex. Both are required only when
    #  image_provider == "azure" - validated at call time (in
    #  azure_image_provider.generate_image), not here, so constructing
    #  Settings never fails just because Azure isn't configured. Never
    #  logged, never returned to a caller, never exposed to the frontend -
    #  read server-side only, exactly like openai_api_key above.
    azure_flux_endpoint: str = ""
    azure_flux_api_key: str = ""
    #  Microsoft Foundry MAI-Image-2.6. All three are required only when
    #  image_provider (or the per-call override, e.g. textbook_image_
    #  provider) == "azure_mai" - validated at call time (in
    #  azure_mai_image_provider.generate_image), not here, so constructing
    #  Settings never fails just because Azure isn't configured. Never
    #  logged, never returned to a caller, never exposed to the frontend -
    #  read server-side only, exactly like openai_api_key above.
    #  `azure_mai_image_endpoint` is the FULL generations URL (same
    #  convention as azure_flux_endpoint above), e.g.
    #  "https://<resource-name>.services.ai.azure.com/mai/v1/images/generations"
    #  - not just the resource's base URL. `azure_mai_image_deployment` is
    #  the deployment name assigned when the model was deployed in Foundry;
    #  the API's own `model` request field must carry this deployment name,
    #  never the literal string "MAI-Image-2.6" (see the official docs this
    #  provider was built against: https://learn.microsoft.com/en-us/azure/
    #  foundry/foundry-models/how-to/use-foundry-models-mai-image).
    azure_mai_image_endpoint: str = ""
    azure_mai_image_api_key: str = ""
    azure_mai_image_deployment: str = ""

    # --- pipeline behaviour -------------------------------------------------
    research_mode: ResearchMode = "fast"
    #  parallel  - chapters are written concurrently against the blueprint's
    #              planned summaries (fast; the default)
    #  sequential- each chapter waits for the previous chapter's real summary
    #              (slow; kept for A/B comparison of continuity quality)
    writing_mode: WritingMode = "parallel"
    #  One extra cheap call after a parallel run that checks for repetition and
    #  broken transitions across chapters.
    continuity_pass: bool = True
    #  Ask the search-enabled call for structured output directly instead of
    #  shaping the notes in a second call.
    single_call_research: bool = True
    #  On review failure, rewrite only the offending blocks rather than the
    #  whole chapter.
    surgical_revision: bool = True
    max_review_revisions: int = Field(default=1, ge=0, le=3)

    # --- code execution --------------------------------------------------
    # The sandboxed executor is a separate service on an internal-only network
    # (see docker-compose.yml). The backend picks the limits for a run; the
    # browser never does, and network access is not a setting a request can
    # change.
    code_executor_url: str = "http://code-executor:9000"
    code_execution_timeout_seconds: int = Field(default=10, ge=1, le=30)
    code_execution_memory_mb: int = Field(default=256, ge=32, le=512)
    code_execution_max_bytes: int = Field(default=100_000, ge=1_000, le=200_000)
    # Extra time over the run budget for compiling and for the HTTP hop, so a
    # slow-but-legitimate compile is not mistaken for an unreachable executor.
    code_executor_http_timeout_seconds: int = Field(default=60, ge=5, le=120)
    # Languages the writer is allowed to emit as runnable cells. A hint for
    # generation only - what the editor offers always comes from the live
    # executor, so this can lag a deployment without ever showing a language
    # that cannot run.
    code_cell_languages: list[str] = ["python", "javascript", "cpp", "java"]
    # Code-execution verification follow-up (quality governance) - whether
    # the reviewer actually runs each `code_cell` in the sandboxed executor
    # during review, vs. only the editor's own Run button doing so. Default
    # on; a deployment without the executor reachable degrades to
    # `verification_failed` findings per cell (fail-closed, never silently
    # skipped) rather than needing this flipped off - but it exists as an
    # explicit kill switch, same pattern as `enable_image_generation`.
    enable_code_cell_execution_verification: bool = True
    # Conservative per-chapter cap: a chapter with more runnable code cells
    # than this has the excess ones explicitly marked "skipped" (never
    # silently treated as verified) rather than executed - bounds how much
    # latency one chapter's review can add regardless of how many cells a
    # writer emits.
    code_execution_max_cells_per_chapter: int = Field(default=5, ge=1, le=50)
    # Its own concurrency phase (see concurrency_for below) - chapters review
    # in parallel, and the executor's own global semaphore
    # (code-executor/app/config.py MAX_CONCURRENT) is shared across every
    # course currently generating, not just one. A conservative default here
    # avoids this backend routinely hitting the executor's own 429 "busy"
    # response under normal parallel-chapter load.
    code_execution_concurrency: int = Field(default=2, ge=0, le=32)
    #  Trim the context handed to the writer / reviewer: input size is latency.
    research_context_chars: int = Field(default=6000, ge=1000, le=40000)
    reviewer_context_chars: int = Field(default=20000, ge=2000, le=120000)

    mock_openai: bool = False
    #  Test-only: makes the offline mock client wait, so concurrency behaviour
    #  can be measured without an API key. Never set this in production.
    mock_latency_ms: int = Field(default=0, ge=0, le=10000)
    enable_image_generation: bool = True
    #  When true, `image` blocks marked kind="diagram" render as a structured
    #  SVG instead of a raster illustration. Falls back to the raster path on
    #  any failure either way, so this only controls the preferred path.
    enable_diagram_generation: bool = True
    #  When true, `image` blocks marked kind="concept_experience" render as a
    #  deterministic, static HTML+CSS visual (see
    #  app.services.concept_visual_service) instead of a raster illustration.
    #  Was a POC feature flag defaulting to False; validated end-to-end
    #  (offline tests, real-API dry runs, a real writer→PDF→frontend pass)
    #  and turned on for real. Every existing diagram/illustration block is
    #  unaffected either way, and `ImageService` still falls back to a raster
    #  illustration on any failure - flipping this off reproduces the exact
    #  pre-Phase-4 behaviour with no other change.
    enable_concept_experience_visuals: bool = True
    #  Same reasoning-tier choice as `diagram_model` - concept_experience
    #  planning asks for structure (concept/entities/interactions), not
    #  pixels.
    concept_visual_model: str = "gpt-5-mini"
    #  AI memory layer (templates/visual knowledge/course samples/generation
    #  history retrieval - see app.services.memory_service). Retrieval always
    #  fails soft regardless of this flag; the flag exists to disable the
    #  extra DB queries entirely, e.g. for a slow/degraded database.
    enable_memory_retrieval: bool = True

    # --- embeddings / semantic memory ---------------------------------------
    #  Hybrid retrieval = keyword (ILIKE, always available) + semantic
    #  (embeddings + pgvector, best-effort). This flag controls only the
    #  semantic half - turning it off (or any failure: no API key, provider
    #  down, pgvector missing) always leaves plain keyword retrieval intact.
    #  See app.services.embedding_service / hybrid_retriever.
    enable_embeddings: bool = True
    embedding_model: str = "text-embedding-3-small"
    #  Must match the Vector(...) column width created by the migration - see
    #  alembic/versions/20260912_0008_memory_embeddings.py. Changing this
    #  requires a new migration, not just a config change.
    embedding_dimensions: int = Field(default=1536, ge=1, le=4096)
    embedding_batch_size: int = Field(default=64, ge=1, le=512)
    #  Optional single Azure embedding deployment (e.g. text-embedding-3-large)
    #  - same /openai/v1/ compatibility pattern as the Azure text models above
    #  (plain AsyncOpenAI client, base_url+api_key, deployment name in the
    #  request body - see OpenAIClient._embedding_client). Purely additive:
    #  when any of these three is blank, embed() uses the existing direct
    #  OpenAI client (openai_api_key) and `embedding_model` exactly as
    #  before this setting existed - an existing deployment with only
    #  OPENAI_API_KEY configured is completely unaffected. When all three
    #  are set, every embedding call requests `embedding_dimensions` back
    #  from the Azure deployment (text-embedding-3-* models support
    #  truncating their native output to a smaller width) rather than the
    #  model's full native size, so the EXISTING pgvector column width
    #  (see embedding_dimensions' own docstring) never needs a new
    #  migration just to switch providers - widening it for the model's
    #  full native dimensionality is a deliberate separate decision, not
    #  something this flag makes silently.
    azure_embedding_endpoint: str = ""
    azure_embedding_api_key: str = ""
    azure_embedding_deployment: str = ""
    #  Below this cosine similarity (0..1), a semantic match is discarded
    #  rather than merged into results - keeps irrelevant nearest-neighbours
    #  out of the prompt. Tune per embedding model if this one changes.
    semantic_similarity_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    #  Hybrid score = keyword_weight * (1 if an ILIKE hit) +
    #  semantic_weight * cosine_similarity. Keyword gets the larger default
    #  weight because an exact substring match is a stronger, zero-ambiguity
    #  signal than a continuous similarity score; semantic still meaningfully
    #  moves the ranking and is what surfaces differently-worded matches.
    keyword_score_weight: float = Field(default=0.55, ge=0.0, le=1.0)
    semantic_score_weight: float = Field(default=0.45, ge=0.0, le=1.0)

    # --- concurrency --------------------------------------------------------
    #  Per-phase limits; 0 means "use max_concurrency". Each phase gets its own
    #  adaptive limiter that halves itself when the provider returns 429.
    max_concurrency: int = Field(default=6, ge=1, le=32)
    research_concurrency: int = Field(default=0, ge=0, le=32)
    writer_concurrency: int = Field(default=0, ge=0, le=32)
    image_concurrency: int = Field(default=2, ge=0, le=32)
    request_timeout: float = 900.0
    max_call_retries: int = Field(default=4, ge=1, le=8)

    # --- Storage / misc -----------------------------------------------------
    data_dir: Path = Path("data")
    database_url: str = "postgresql+asyncpg://course_app:course_app_dev@localhost:5432/course_generator"
    #  PostgreSQL is the primary store for course/blueprint/document records.
    #  Tests that don't need a live database turn this off (see conftest.py);
    #  research/chapters/assets/exports always stay on the filesystem regardless.
    use_database: bool = True
    # Gate 1 (client blueprint section 4): block chapter generation until the
    # outline has been approved. Turn off only for scripts that build courses
    # without a person in the loop.
    require_outline_approval: bool = True

    # --- uploaded source material (app/services/source_service.py) -----------
    max_source_bytes: int = Field(default=20 * 1024 * 1024, ge=1024)
    max_sources_per_course: int = Field(default=10, ge=1, le=50)
    # How many passages, and how many characters of them, a chapter's writer
    # is shown from the uploaded sources.
    source_excerpts_per_chapter: int = Field(default=6, ge=1, le=20)
    source_excerpt_chars: int = Field(default=6000, ge=500, le=30000)

    # --- global knowledge library (app/services/knowledge_service.py) --------
    # Admin-uploaded documents that every course searches while it is written.
    enable_global_knowledge: bool = True
    knowledge_excerpts_per_chapter: int = Field(default=4, ge=1, le=20)
    knowledge_excerpt_chars: int = Field(default=4000, ge=500, le=30000)
    # How close in meaning, and how much of the query's wording, a passage must
    # have to be offered to a chapter at all. The library covers many subjects;
    # these keep a chapter from being handed another subject's material.
    knowledge_min_semantic: float = Field(default=0.3, ge=0.0, le=1.0)
    knowledge_min_coverage: float = Field(default=0.25, ge=0.0, le=1.0)

    # --- fact-check audit (app/services/audit_service.py) ---------------------
    # Check a finished course against its source documents automatically.
    enable_fact_check: bool = True
    # Let a model read the doubtful sentences against their evidence (one call
    # per chapter). Off, the rule-based findings stand on their own.
    fact_check_ai: bool = True
    fact_check_max_claims: int = Field(default=15, ge=1, le=60)

    # --- review and notifications ---------------------------------------------
    # Final approval of a course written from sources waits for the fact check
    # to be run and its high-severity findings to be decided.
    require_findings_resolved: bool = True
    notifications_enabled: bool = True
    # Email is sent only when a host is set; the in-app inbox is always written.
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_use_tls: bool = True
    # Where the app is served, so an email can link to the review screen.
    public_app_url: str = ""
    log_level: str = "INFO"
    app_name: str = "AI Course Creation Platform - Backend POC"
    app_version: str = "0.2.0"
    app_env: AppEnvironment = "development"
    frontend_origin: str = "http://localhost:3000"

    # --- Authentication -----------------------------------------------------
    jwt_secret: str = ""
    jwt_refresh_secret: str = ""
    access_token_expire_minutes: int = Field(default=30, ge=1)
    refresh_token_expire_days: int = Field(default=1, ge=1)
    # Server-side bootstrap only: if set, registering this email creates the
    # initial admin account. Public clients cannot choose their own role.
    initial_admin_email: str = ""

    # --- derived ------------------------------------------------------------
    @property
    def use_mock_ai(self) -> bool:
        """Mock client is used explicitly, or implicitly when NOTHING at all is
        configured for real text generation. Updated for the Azure migration:
        `openai_api_key` alone no longer gates this - it's scoped to
        embeddings only now (see its own docstring) and configuring it
        alone must not make the app think real text generation is ready.
        Real generation needs at least one of the three Azure text
        deployments; `openai_api_key` is included in the OR only so a
        pre-migration .env (key set, no Azure vars) still falls back to
        mock cleanly instead of constructing a client that can't actually
        serve writer/editor/etc - it never substitutes for a configured
        Azure deployment once any text request is actually attempted (see
        OpenAIClient._client_for, which never reads this key)."""
        any_azure_text_model = any(
            key.strip()
            for key in (
                self.azure_gpt5_api_key,
                self.azure_gpt5_mini_api_key,
                self.azure_gpt55_api_key,
            )
        )
        return self.mock_openai or not (any_azure_text_model or self.openai_api_key.strip())

    @property
    def courses_dir(self) -> Path:
        return self.data_dir / "courses"

    @property
    def index_dir(self) -> Path:
        return self.data_dir / "index"

    @property
    def secure_cookies(self) -> bool:
        return self.app_env == "production"

    def concurrency_for(self, phase: str) -> int:
        override = {
            "research": self.research_concurrency,
            "writer": self.writer_concurrency,
            "image": self.image_concurrency,
            "code_execution": self.code_execution_concurrency,
        }.get(phase, 0)
        return override or self.max_concurrency


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Used by tests after mutating the environment."""
    get_settings.cache_clear()
