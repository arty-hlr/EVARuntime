"""
Point d'entrée FastAPI — Inference Gateway UPPA L40S.

Lancement (développement) :
    uvicorn main:app --host 127.0.0.1 --port 8000 --reload

Lancement (production via systemd) :
    uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1 --loop uvloop
"""
from __future__ import annotations

import logging
import logging.config
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

import database as db
from admin import router as admin_router
from auth import get_current_user
from config import settings
from llama_version import enforce_llama_min_build
from metrics import router as metrics_router
from model_manager import model_manager
from model_registry import IntegrityError
from proxy import aclose_http_client, init_http_client, models_response, proxy_request
from rate_limiter import check_rate_limit
from readiness import caller_is_privileged, evaluate_readiness

# ── Logging ───────────────────────────────────────────────────────────────────

logging.config.dictConfig({
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "default",
        },
    },
    "root": {
        "level": "INFO",
        "handlers": ["console"],
    },
    "loggers": {
        "llama-server": {"level": "WARNING"},
        "httpx": {"level": "WARNING"},
        "uvicorn.access": {"level": "INFO"},
    },
})

log = logging.getLogger(__name__)


# ── Validation du runtime d'inférence ─────────────────────────────────────────

async def _validate_inference_runtime(enabled_models) -> None:
    """
    Valide les artefacts qui exécutent réellement l'inférence sur CET hôte.

    En mode local, la gateway possède le binaire llama-server et les GGUF : elle
    applique donc les garde-fous de version et d'intégrité avant d'accepter du
    trafic. En mode cluster, l'orchestrateur ne doit pas exiger que ces fichiers
    existent localement : chaque node-agent applique les mêmes contrôles au
    chargement, sur le nœud qui possède effectivement le binaire et les modèles.
    """
    if settings.cluster_mode == "cluster":
        log.info(
            "Mode cluster : validation llama-server/GGUF déléguée aux node-agents."
        )
        return

    ok = await enforce_llama_min_build(
        settings.llama_server_bin, settings.llama_server_min_build
    )
    if not ok:
        raise RuntimeError(
            "llama-server ne satisfait pas LLAMA_SERVER_MIN_BUILD — "
            "démarrage refusé (binaire potentiellement vulnérable)."
        )

    for model in enabled_models:
        if model.sha256 is None:
            continue
        try:
            model.verify_integrity()
            log.info("Intégrité SHA-256 vérifiée : %s", model.id)
        except IntegrityError as exc:
            log.critical("Intégrité GGUF compromise : %s", exc)
            raise RuntimeError(
                f"Vérification d'intégrité échouée pour '{model.id}' — "
                "démarrage refusé."
            ) from exc


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialisation au démarrage, nettoyage à l'arrêt."""
    log.info("=== LLM Gateway UPPA démarrage ===")

    # Vérification des secrets — fail-closed côté routes, alerte côté logs.
    if settings.admin_secret_is_placeholder():
        log.critical(
            "ADMIN_SECRET non configuré (vide ou CHANGE_ME_*) — les routes /admin "
            "sont DÉSACTIVÉES tant qu'un secret fort n'est pas défini."
        )
    if settings.cluster_mode == "local" and settings.internal_api_key_is_placeholder():
        log.critical(
            "INTERNAL_API_KEY non configurée (vide ou CHANGE_ME_*) — la clé "
            "gateway ↔ llama-server est prévisible. Définissez un secret fort."
        )

    # Afficher le registre des modèles et le budget VRAM
    registry = model_manager.registry
    all_models = registry.list_all()
    enabled_models = registry.list_enabled()
    log.info(
        "Registre : %d modèle(s) total, %d activé(s) — config : %s",
        len(all_models), len(enabled_models), settings.models_config_path,
    )
    for model in all_models:
        status = "ACTIVÉ " if model.enabled else "désactivé"
        log.info(
            "  [%s] %s — %.1f GB VRAM — %s",
            status, model.id, model.vram_gb, model.path,
        )

    if settings.cluster_mode == "local":
        budget = settings.effective_vram_budget_gb()
        log.info(
            "Budget VRAM : %.1f GB total — %.1f GB overhead — %.0f%% marge "
            "→ %.1f GB net disponible",
            settings.total_vram_gb,
            settings.vram_overhead_gb,
            settings.vram_safety_margin * 100,
            budget,
        )
        log.info(
            "Pool de ports : %d-%d (%d modèles max simultanés)",
            settings.base_llama_port,
            settings.base_llama_port + settings.max_loaded_models - 1,
            settings.max_loaded_models,
        )
        log.info("Idle timeout  : %ds", settings.idle_timeout_seconds)
    else:
        log.info(
            "Capacité cluster : budgets VRAM et ports lus dynamiquement depuis "
            "les node-agents; les paramètres GPU locaux sont ignorés."
        )

    # ── Garde-fou supply-chain : version du binaire llama-server ──────────────
    # NON FATAL par défaut : en test/CI il n'y a aucun binaire llama-server réel,
    # donc la sonde se contente d'un avertissement et le démarrage continue. Le
    # seul cas de refus est un enforcement EXPLICITE (LLAMA_SERVER_MIN_BUILD > 0)
    # avec un build lu strictement inférieur au minimum patché.
    # En local, ces artefacts vivent sur la gateway. En cluster, ils vivent sur
    # les nœuds et sont validés par le node-agent au moment du chargement.
    await _validate_inference_runtime(enabled_models)

    await db.init_db()
    log.info("Base de données initialisée : %s", settings.db_path)

    log.info("Mode déploiement : CLUSTER_MODE=%s", settings.cluster_mode)
    await model_manager.start_health_monitor()

    # ── Robustesse cycle de vie (mode local uniquement) ───────────────────────
    # Détection best-effort des llama-server orphelins tenant un port du pool
    # (survivants d'un crash gateway). LOG seulement par défaut — ne tue rien.
    # En test (ports libres) : aucune détection, retour immédiat.
    if hasattr(model_manager, "detect_orphan_ports"):
        try:
            await model_manager.detect_orphan_ports()
        except Exception as exc:  # best-effort — jamais fatal au démarrage
            log.warning("Détection des orphelins au démarrage ignorée : %s", exc)

    # Réconciliation VRAM périodique (nvidia-smi) — inerte sans GPU/nvidia-smi.
    if hasattr(model_manager, "start_vram_reconcile"):
        try:
            await model_manager.start_vram_reconcile()
        except Exception as exc:
            log.warning("Réconciliation VRAM non démarrée (non fatal) : %s", exc)

    # Client HTTP partagé vers les llama-server (chemin chaud d'inférence).
    # Créé une fois ici, réutilisé par toutes les requêtes proxy (keep-alive),
    # fermé au shutdown. Jamais recréé par requête.
    init_http_client()

    yield

    if settings.cluster_mode == "cluster":
        log.info(
            "Arrêt de l'orchestrateur — modèles distants préservés pour le "
            "redémarrage et la réconciliation."
        )
    else:
        log.info("Arrêt de la gateway — déchargement de tous les modèles locaux…")
    await model_manager.shutdown()
    await aclose_http_client()
    log.info("=== LLM Gateway UPPA arrêt propre ===")


# ── Application ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="LLM Inference Gateway UPPA",
    description=(
        "Inference gateway souverain du cluster EVA (hébergé à l'UPPA). "
        "Compatible API OpenAI. Multi-modèles avec gestion VRAM automatique. "
        "Accès réservé aux membres authentifiés."
    ),
    version="2.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(
    CORSMiddleware,
    # Configurable via CORS_ALLOW_ORIGINS (liste séparée par des virgules).
    # En production, restreindre aux domaines clients connus.
    allow_origins=settings.cors_allow_origins,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
    max_age=600,
)

app.include_router(admin_router)
app.include_router(metrics_router)


# ── Middleware de logging des requêtes ────────────────────────────────────────

@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.monotonic()
    response = await call_next(request)
    duration_ms = int((time.monotonic() - start) * 1000)

    _silent = ("/health", "/v1/models")
    if not request.url.path.startswith("/admin/metrics") and request.url.path not in _silent:
        log.info(
            "%s %s %d %dms",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
    return response


# ── Routes publiques ──────────────────────────────────────────────────────────

@app.get("/admin/dashboard", include_in_schema=False)
async def dashboard_ui():
    """Sert le dashboard d'administration (SPA HTML)."""
    html_path = Path(__file__).parent / "static" / "dashboard.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/health", include_in_schema=False)
async def health():
    """Health check utilisé par nginx et le monitoring."""
    status = model_manager.status()
    loaded = [m["id"] for m in status["models"] if m["state"] == "ready"]
    return {
        "status": "ok",
        "models_loaded": loaded,
        "vram_used_gb": status["vram_budget"]["used_gb"],
        "vram_available_gb": status["vram_budget"]["available_gb"],
    }


@app.get("/ready", include_in_schema=False)
async def ready(request: Request):
    """
    Readiness STRUCTURELLE stricte (distincte de la liveness de /health).

    Renvoie 200 seulement si tous les contrôles structurels critiques passent :
    registre lisible, au moins un modèle activé, binaire llama-server exécutable,
    GGUF des modèles activés présents et lisibles, base inscriptible, au moins un
    modèle qui tient dans le budget VRAM, et capacité de service disponible. En
    mode cluster, binaire et GGUF sont délégués aux node-agents : l'équivalent
    structurel est « inventaire de nœuds lisible ET au moins un nœud en ligne ».
    Sinon 503, avec le code du premier contrôle critique en échec dans `reason`.

    Ce que /ready NE garantit PAS : qu'un modèle génère effectivement des tokens.
    C'est la serving readiness, exposée en information (`levels.serving`) mais
    prouvée seulement par le smoke test de mise à jour (COR-006).

    Le corps public ne divulgue aucun chemin de fichier, URL de nœud ni secret :
    seulement des identifiants de contrôle et des codes stables. Un appelant
    présentant `ADMIN_SECRET` reçoit en plus les messages actionnables détaillés
    (qui, eux, contiennent des chemins) — même niveau de confiance que /admin/*.
    """
    report = await evaluate_readiness(model_manager, config=settings)

    if caller_is_privileged(request.headers.get("authorization"), settings):
        body = report.detailed_body()
    else:
        body = report.public_body()

    if report.structural_ok:
        return body
    return JSONResponse(status_code=503, content=body)


@app.get("/v1/models")
async def list_models(user: dict = Depends(get_current_user)):
    """Liste les modèles disponibles — compatible openai.models.list()."""
    return models_response(model_manager)


@app.get("/v1/capacity")
async def capacity_status(user: dict = Depends(get_current_user)):
    """
    État minimal de la queue d'admission VRAM.

    Route authentifiée par clé API utilisateur. Ne révèle ni VRAM détaillée,
    ni modèles chargés, ni chemins fichiers : seulement l'état exploitable par
    une application cliente pour afficher attente/saturation et gérer Retry-After.
    """
    status = model_manager.status()
    queue = status.get("capacity_queue")
    if not queue:
        return {
            "object": "capacity_queue",
            "mode": settings.cluster_mode,
            "available": False,
            "enabled": False,
            "status": "unavailable",
            "waiters": 0,
            "max_waiters": None,
            "timeout_seconds": None,
            "retry_after_seconds": settings.capacity_queue_retry_after_seconds,
        }

    waiters = int(queue.get("waiters", 0))
    max_waiters = int(queue.get("max_waiters", 0))
    enabled = bool(queue.get("enabled", False))

    queue_status = "disabled"
    if enabled:
        if max_waiters > 0 and waiters >= max_waiters:
            queue_status = "full"
        elif waiters > 0:
            queue_status = "waiting"
        else:
            queue_status = "idle"

    return {
        "object": "capacity_queue",
        "mode": settings.cluster_mode,
        "available": True,
        "enabled": enabled,
        "status": queue_status,
        "waiters": waiters,
        "max_waiters": max_waiters,
        "timeout_seconds": queue.get("timeout_seconds"),
        "retry_after_seconds": settings.capacity_queue_retry_after_seconds,
    }


# ── Routes d'inférence (protégées + rate limitées) ────────────────────────────

@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    user: dict = Depends(check_rate_limit),
):
    """
    Chat completions — compatible OpenAI.
    Supporte le streaming SSE (stream: true) et le mode classique.
    Le modèle est sélectionné via le champ "model" du body JSON.
    Chargé automatiquement si nécessaire, avec éviction LRU si besoin de VRAM.
    """
    return await proxy_request(request, "/v1/chat/completions", user, model_manager)


@app.post("/v1/completions")
async def completions(
    request: Request,
    user: dict = Depends(check_rate_limit),
):
    """Legacy text completions — compatible OpenAI."""
    return await proxy_request(request, "/v1/completions", user, model_manager)


@app.post("/v1/completion")
@app.post("/completion")
async def raw_completion(
    request: Request,
    user: dict = Depends(check_rate_limit),
):
    """
    Endpoint natif llama.cpp. Prend un champ 'prompt' (string) au lieu de 'messages'.
    Tous les paramètres de sampling avancés sont supportés sans configuration particulière :
    mirostat, dry_multiplier, dry_base, xtc_*, repeat_last_n, repeat_penalty, ignore_eos, etc.
    Utile pour les scripts llama.cpp existants ou les cas sans chat template.
    """
    return await proxy_request(request, "/completion", user, model_manager)


@app.post("/v1/tokenize")
async def tokenize(
    request: Request,
    user: dict = Depends(check_rate_limit),
):
    """Tokenise un texte — retourne les token IDs. Body: {"model": "...", "content": "..."}"""
    return await proxy_request(request, "/tokenize", user, model_manager)


@app.post("/v1/detokenize")
async def detokenize(
    request: Request,
    user: dict = Depends(check_rate_limit),
):
    """Reconstruit du texte depuis des token IDs. Body: {"model": "...", "tokens": [...]}"""
    return await proxy_request(request, "/detokenize", user, model_manager)


# ── Gestionnaire d'erreurs global ────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    log.exception("Erreur non gérée sur %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "message": "Erreur interne du serveur.",
                "type": "server_error",
                "code": "500",
            }
        },
    )
