"""
Endpoints d'administration — protégés par :
  1. Secret admin (Bearer <ADMIN_SECRET> dans le header Authorization)
  2. Filtrage IP nginx (réseau campus uniquement — configuré dans nginx.conf)

Ces routes ne sont PAS dans le préfixe /v1/ pour éviter toute confusion
avec l'API OpenAI.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

import database as db
from auth import require_admin
from config import settings
from model_manager import (
    CapacityQueueFull,
    CapacityQueueTimeout,
    ModelBusyError,
    model_manager,
)
from schemas import (
    GatewayStatus,
    KeyCreate,
    KeyCreateResponse,
    KeyResponse,
    LlamaParamsSchema,
    ModelEntryCreate,
    ModelEntryUpdate,
    ModelStatusResponse,
    UsageEntry,
    UsageSummaryEntry,
    UserCreate,
    UserResponse,
    UserUpdate,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

# ── Helpers ───────────────────────────────────────────────────────────────────

_BYTES_PER_KV_TOKEN: dict[str, float] = {
    "f16": 2.0,
    "bf16": 2.0,
    "q8_0": 1.0,
    "q5_0": 0.625,
    "q4_0": 0.5,
}


def _warn_kv_cache(model_id: str, vram_gb: float, lp: LlamaParamsSchema) -> None:
    """
    Avertit si le KV cache estimé représente plus de 50 % du vram_gb déclaré.
    Le calcul est un minorant : il suppose une architecture 7B (128 B/token de KV par couche).
    Pour les modèles plus grands le vrai cache sera encore plus gros.
    """
    bytes_k = _BYTES_PER_KV_TOKEN.get(lp.cache_type_k, 2.0)
    bytes_v = _BYTES_PER_KV_TOKEN.get(lp.cache_type_v, 2.0)
    kv_gb = lp.ctx_size * lp.parallel * (bytes_k + bytes_v) * 128 / 1e9
    if kv_gb > vram_gb * 0.5:
        log.warning(
            "[%s] KV cache estimé à %.2f GB (ctx_size=%d × parallel=%d × cache quant) "
            "dépasse 50%% du vram_gb déclaré (%.1f GB). "
            "Vérifiez que vram_gb inclut bien les poids ET le KV cache.",
            model_id, kv_gb, lp.ctx_size, lp.parallel, vram_gb,
        )


# ── Déchargement protégé (COR-004) ────────────────────────────────────────────
#
# INVARIANT (AGENTS.md) : un modèle qui traite une requête active ne doit pas
# être évincé. Toutes les opérations admin qui déchargent un modèle passent par
# `_unload_for_admin` : drain borné, puis 409 explicite si des requêtes sont
# encore actives. Le forçage est possible mais jamais implicite.

_FORCE_DESCRIPTION = (
    "Interrompre les requêtes actives au lieu de refuser en 409. "
    "À n'utiliser que sur décision explicite de l'opérateur."
)

# Marqueurs de conflit « requêtes actives » du mode cluster. ClusterManager
# signale ce refus par un RuntimeError (pas de ModelBusyError) ; on le mappe sur
# le même code HTTP pour que le contrat d'erreur soit identique dans les deux
# modes de déploiement.
_BUSY_CONFLICT_MARKERS = ("requêtes actives", "ne peut pas être déchargé")


def _unload_conflict_detail(exc: Exception) -> str | None:
    """Détail du conflit si `exc` signale des requêtes actives, sinon None."""
    if isinstance(exc, ModelBusyError):
        return str(exc)
    text = str(exc)
    if any(marker in text for marker in _BUSY_CONFLICT_MARKERS):
        return text
    return None


async def _unload_for_admin(model_id: str, *, force: bool = False) -> None:
    """
    Décharge un modèle pour le compte d'une route admin.

    Lève HTTPException 409 si des requêtes sont encore actives après le drain
    (`ADMIN_UNLOAD_DRAIN_TIMEOUT_SECONDS`), 503 si le backend n'a pas confirmé le
    déchargement. Le corps d'erreur reste au format admin habituel
    (`{"detail": "<message actionnable>"}`), pas au format OpenAI des routes /v1.
    """
    supports_force = getattr(model_manager, "supports_unload_force", False)

    try:
        if supports_force:
            await model_manager.unload_model(model_id, force=force)
        else:
            # Mode cluster : aucun chemin de forçage n'existe (les node-agents
            # refusent tout modèle avec des requêtes actives). On tente le
            # déchargement normal — force=true n'a de conséquence que sur un
            # modèle occupé, cas traité ci-dessous.
            await model_manager.unload_model(model_id)
    except RuntimeError as exc:
        detail = _unload_conflict_detail(exc)
        if detail is None:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if force and not supports_force:
            # Ne jamais ignorer un force=true en silence : dire qu'il est
            # indisponible dans ce mode de déploiement.
            detail += (
                " Note : force=true n'est pas supporté par ce mode de déploiement — "
                "un modèle traitant des requêtes actives ne peut pas être déchargé "
                "de force en mode cluster."
            )
        raise HTTPException(status_code=409, detail=detail) from exc


def _max_model_capacity_gb() -> float | None:
    """Capacité effective maximale d'un seul hôte d'inférence connu."""
    if settings.cluster_mode != "cluster":
        return settings.effective_vram_budget_gb()

    cluster_status = getattr(model_manager, "cluster_status", None)
    if cluster_status is None:
        return None
    capacities = [
        float(node["used_vram_gb"]) + float(node["available_vram_gb"])
        for node in cluster_status()
        if node.get("online")
        and node.get("used_vram_gb") is not None
        and node.get("available_vram_gb") is not None
    ]
    return max(capacities, default=None)


# ── Statut système multi-modèles ──────────────────────────────────────────────

@router.get("/status", response_model=GatewayStatus)
async def get_status(_: None = Depends(require_admin)) -> dict:
    """
    État complet de la gateway : budget VRAM + état de chaque modèle.
    """
    return {"status": "ok", **model_manager.status()}


# ── Registre des modèles ──────────────────────────────────────────────────────

@router.get("/models", response_model=list[ModelStatusResponse])
async def list_models(_: None = Depends(require_admin)) -> list[dict]:
    """
    Liste tous les modèles du registre avec leur état live (chargé / déchargé).
    """
    return model_manager.status()["models"]


@router.post("/models", response_model=ModelStatusResponse, status_code=201)
async def register_model(
    body: ModelEntryCreate,
    _: None = Depends(require_admin),
) -> dict:
    """
    Enregistre un nouveau modèle dans le registre.

    Validations de sécurité :
    - path doit être absolu et pointer vers un fichier .gguf
    - path doit être sous un répertoire autorisé (si ALLOWED_MODEL_DIRS est configuré)
    - En local, le fichier .gguf doit exister sur la gateway
    - En cluster, son existence est validée par chaque node-agent au chargement
    - vram_gb doit tenir sur au moins un hôte d'inférence connu
    - Le modèle n'est PAS chargé automatiquement après enregistrement
    """
    # En cluster les GGUF vivent sur les nœuds, pas nécessairement sur
    # l'orchestrateur. Le node-agent valide existence, lisibilité et intégrité
    # avant de réserver un port ou de lancer llama-server.
    model_path = Path(body.path)
    if settings.cluster_mode == "local" and not model_path.exists():
        raise HTTPException(
            status_code=422,
            detail=f"Fichier introuvable sur le serveur : {body.path}",
        )

    # En cluster, comparer au plus grand budget EFFECTIF d'un seul nœud (pas à
    # la somme du cluster : un modèle ne peut pas être fractionné). Si aucun
    # heartbeat n'est encore disponible, autoriser l'enregistrement de la
    # métadonnée; le scheduler refusera explicitement un chargement impossible.
    budget = _max_model_capacity_gb()
    if budget is not None and body.vram_gb > budget:
        raise HTTPException(
            status_code=422,
            detail=(
                f"vram_gb ({body.vram_gb:.1f} GB) dépasse le budget VRAM net disponible "
                f"({budget:.1f} GB). Ce modèle ne pourra jamais être chargé seul."
            ),
        )

    _warn_kv_cache(body.id, body.vram_gb, body.llama_params)

    try:
        entry_dict = {
            "id": body.id,
            "path": body.path,
            "description": body.description,
            "vram_gb": body.vram_gb,
            "enabled": body.enabled,
            "capabilities": body.capabilities,
            "llama_params": body.llama_params.model_dump(),
        }
        model = model_manager.registry.add(entry_dict)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    log.info("Admin : nouveau modèle enregistré '%s'", body.id)
    return {
        "id": model.id,
        "description": model.description,
        "enabled": model.enabled,
        "vram_gb": model.vram_gb,
        "capabilities": model.capabilities,
        "state": "unloaded",
        "path": str(model.path),
        "pid": None,
        "port": None,
        "uptime_seconds": None,
        "idle_seconds": None,
        "llama_params": None,
    }


@router.patch("/models/{model_id}", response_model=ModelStatusResponse)
async def update_model(
    model_id: str,
    body: ModelEntryUpdate,
    force: bool = Query(False, description=_FORCE_DESCRIPTION),
    _: None = Depends(require_admin),
) -> dict:
    """
    Met à jour les métadonnées d'un modèle (enabled, vram_gb, description, llama_params).

    llama_params — remplacement complet. Si fourni, le modèle chargé est déchargé
    pour que la prochaine requête le relance avec les nouveaux paramètres.
    Cela permet de corriger cpu_moe, ctx_size, parallel, etc. sans redémarrer la gateway.

    enabled=false — décharge le modèle.

    Ces deux cas déchargent le modèle : ils sont donc refusés en 409 si des
    requêtes sont encore actives après le drain, et le registre n'est alors PAS
    modifié (jamais de `enabled: false` persisté sur un modèle qui sert encore).
    """
    if not model_manager.registry.get(model_id):
        raise HTTPException(status_code=404, detail=f"Modèle '{model_id}' introuvable dans le registre.")

    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=422, detail="Aucun champ à mettre à jour.")

    if "vram_gb" in updates:
        budget = _max_model_capacity_gb()
        if budget is not None and float(updates["vram_gb"]) > budget:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"vram_gb ({float(updates['vram_gb']):.1f} GB) dépasse le "
                    f"budget maximal d'un hôte d'inférence ({budget:.1f} GB)."
                ),
            )

    # Hot-reload : si llama_params changent, le processus doit être relancé.
    # enabled=false : le modèle doit cesser de servir.
    #
    # Le déchargement a lieu AVANT la mise à jour du registre : un refus (409)
    # doit laisser le registre intact, sinon on persisterait un état incohérent
    # (« désactivé » alors que llama-server continue de répondre). Aucun `await`
    # ne sépare le retour de `_unload_for_admin` de `registry.update` : en
    # asyncio coopératif, aucune requête ne peut s'intercaler pour recharger le
    # modèle avec les anciens paramètres.
    unload_reason = None
    if "llama_params" in updates:
        unload_reason = "llama_params modifiés"
    elif updates.get("enabled") is False:
        unload_reason = "modèle désactivé"

    if unload_reason:
        await _unload_for_admin(model_id, force=force)

    try:
        model = model_manager.registry.update(model_id, **updates)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    if unload_reason:
        log.info(
            "Admin : %s pour '%s' — modèle déchargé, rechargement automatique "
            "à la prochaine requête.",
            unload_reason, model_id,
        )

    # Avertissement KV cache si vram_gb ou llama_params ont changé
    if "vram_gb" in updates or "llama_params" in updates:
        lp_schema = LlamaParamsSchema(**model.llama_params.__dict__)
        _warn_kv_cache(model_id, model.vram_gb, lp_schema)

    # Récupérer l'état live
    status_list = model_manager.status()["models"]
    entry = next((m for m in status_list if m["id"] == model_id), None)
    return entry or {"id": model_id, "state": "unloaded", **model.__dict__}


@router.delete("/models/{model_id}", status_code=200)
async def delete_model(
    model_id: str,
    force: bool = Query(False, description=_FORCE_DESCRIPTION),
    _: None = Depends(require_admin),
) -> dict:
    """
    Supprime un modèle du registre.
    Le modèle doit être déchargé au préalable (ou sera déchargé automatiquement).

    Refusé en 409 si le modèle traite encore des requêtes après le drain : dans
    ce cas le modèle reste dans le registre et continue de servir.
    """
    if not model_manager.registry.get(model_id):
        raise HTTPException(status_code=404, detail=f"Modèle '{model_id}' introuvable dans le registre.")

    # Décharger d'abord si chargé (protégé contre les requêtes actives).
    # Un refus lève une 409 ici même : le registre n'est pas touché.
    await _unload_for_admin(model_id, force=force)

    try:
        model_manager.registry.remove(model_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    log.info("Admin : modèle '%s' supprimé du registre", model_id)
    return {"message": f"Modèle '{model_id}' supprimé du registre."}


@router.post("/models/{model_id}/load")
async def load_model(
    model_id: str,
    _: None = Depends(require_admin),
) -> dict:
    """
    Pré-charge un modèle en mémoire (warm-up).
    Utile pour éviter la latence de cold-start sur la première requête.
    Évinçe un modèle LRU si le budget VRAM est dépassé.
    """
    try:
        await model_manager.ensure_model_loaded(model_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except (CapacityQueueFull, CapacityQueueTimeout) as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc),
            headers={"Retry-After": str(settings.capacity_queue_retry_after_seconds)},
        )
    except (RuntimeError, TimeoutError) as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    log.info("Admin : modèle '%s' pré-chargé", model_id)
    return {"message": f"Modèle '{model_id}' chargé et prêt."}


@router.post("/models/{model_id}/unload")
async def unload_model(
    model_id: str,
    force: bool = Query(False, description=_FORCE_DESCRIPTION),
    _: None = Depends(require_admin),
) -> dict:
    """
    Décharge un modèle spécifique et libère sa VRAM.
    Sans effet si le modèle n'est pas chargé.

    Le modèle est mis en quarantaine (plus aucune nouvelle requête admise) puis
    drainé pendant `ADMIN_UNLOAD_DRAIN_TIMEOUT_SECONDS`. S'il traite encore des
    requêtes à l'expiration, la route répond 409 et le modèle reste chargé et
    utilisable — aucune génération n'est interrompue en silence.
    """
    if not model_manager.registry.get(model_id):
        raise HTTPException(status_code=404, detail=f"Modèle '{model_id}' introuvable dans le registre.")

    await _unload_for_admin(model_id, force=force)
    log.info("Admin : modèle '%s' déchargé", model_id)
    return {"message": f"Modèle '{model_id}' déchargé. VRAM libérée."}


@router.post("/unload")
async def unload_all(_: None = Depends(require_admin)) -> dict:
    """
    Décharge tous les modèles sans arrêter le gestionnaire d'inférence.

    Refusé en 409 si une génération est encore active après le drain — aucun
    modèle n'est déchargé dans ce cas. Il n'y a pas de forçage global : utiliser
    POST /admin/models/{id}/unload?force=true modèle par modèle.
    """
    try:
        await model_manager.unload_all_models()
    except RuntimeError as exc:
        # 409 si l'opération entre en conflit avec un stream actif; 503 si un
        # agent n'a pas confirmé le déchargement. Dans les deux cas, la gateway
        # conserve son inventaire au lieu d'annoncer à tort de la VRAM libérée.
        detail = _unload_conflict_detail(exc)
        if detail is not None:
            raise HTTPException(status_code=409, detail=detail) from exc
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"message": "Tous les modèles déchargés. VRAM entièrement libérée."}


# ── Cluster multi-nœuds ───────────────────────────────────────────────────────

@router.get("/cluster")
async def cluster_status(_: None = Depends(require_admin)) -> dict:
    """
    État de chaque nœud du cluster (uniquement en CLUSTER_MODE=cluster).
    Retourne 200 avec cluster_mode=local et une liste vide en mode mono-nœud.
    """
    from config import settings as cfg
    if cfg.cluster_mode != "cluster" or not hasattr(model_manager, "cluster_status"):
        return {
            "cluster_mode": cfg.cluster_mode,
            "nodes": [],
            "info": "Mode local — aucun nœud distant à afficher.",
        }
    return {
        "cluster_mode": "cluster",
        "nodes": model_manager.cluster_status(),
    }


# ── Gestion utilisateurs ──────────────────────────────────────────────────────

@router.post("/users", response_model=UserResponse, status_code=201)
async def create_user(
    body: UserCreate,
    _: None = Depends(require_admin),
) -> dict:
    """Crée un nouvel utilisateur."""
    try:
        user = await db.create_user(
            username=body.username,
            email=body.email,
            rpm_limit=body.rpm_limit,
            monthly_token_limit=body.monthly_token_limit,
            notes=body.notes,
        )
    except Exception as exc:
        if "UNIQUE constraint" in str(exc):
            raise HTTPException(
                status_code=409,
                detail="Un utilisateur avec ce nom ou cet email existe déjà."
            )
        raise
    return user


@router.get("/users", response_model=list[UserResponse])
async def list_users(_: None = Depends(require_admin)) -> list[dict]:
    """Liste tous les utilisateurs."""
    return await db.list_users()


@router.get("/users/{username}", response_model=UserResponse)
async def get_user(
    username: str,
    _: None = Depends(require_admin),
) -> dict:
    user = await db.get_user_by_username(username)
    if not user:
        raise HTTPException(status_code=404, detail=f"Utilisateur '{username}' introuvable.")
    return user


@router.patch("/users/{username}", response_model=UserResponse)
async def update_user(
    username: str,
    body: UserUpdate,
    _: None = Depends(require_admin),
) -> dict:
    """Modifie un utilisateur (activation/désactivation, RPM, quota, etc.)."""
    user = await db.get_user_by_username(username)
    if not user:
        raise HTTPException(status_code=404, detail=f"Utilisateur '{username}' introuvable.")

    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=422, detail="Aucun champ à mettre à jour.")

    updated = await db.update_user(user["id"], **updates)
    return updated


# ── Gestion des clés API ──────────────────────────────────────────────────────

@router.post("/users/{username}/keys", response_model=KeyCreateResponse, status_code=201)
async def create_key(
    username: str,
    body: KeyCreate,
    _: None = Depends(require_admin),
) -> dict:
    """
    Génère une nouvelle clé API pour l'utilisateur.
    La clé brute est retournée UNE SEULE FOIS — impossible de la récupérer ensuite.
    """
    user = await db.get_user_by_username(username)
    if not user:
        raise HTTPException(status_code=404, detail=f"Utilisateur '{username}' introuvable.")

    raw_key, key_row = await db.create_api_key(
        user_id=user["id"],
        name=body.name,
        expires_at=body.expires_at,
    )

    log.info(
        "Nouvelle clé API créée pour '%s' (préfixe: %s)",
        username, key_row["key_prefix"],
    )

    return {
        "api_key": raw_key,
        "key_prefix": key_row["key_prefix"],
        "name": key_row["name"],
        "created_at": key_row["created_at"],
        "expires_at": key_row["expires_at"],
    }


@router.get("/users/{username}/keys", response_model=list[KeyResponse])
async def list_keys(
    username: str,
    _: None = Depends(require_admin),
) -> list[dict]:
    """Liste les clés d'un utilisateur (sans la valeur brute)."""
    user = await db.get_user_by_username(username)
    if not user:
        raise HTTPException(status_code=404, detail=f"Utilisateur '{username}' introuvable.")

    return await db.list_keys_for_user(user["id"])


@router.delete("/users/{username}", status_code=200)
async def delete_user(
    username: str,
    _: None = Depends(require_admin),
) -> dict:
    """Supprime un utilisateur et toutes ses clés API (action irréversible)."""
    user = await db.get_user_by_username(username)
    if not user:
        raise HTTPException(status_code=404, detail=f"Utilisateur '{username}' introuvable.")

    deleted = await db.delete_user(user["id"])
    if not deleted:
        raise HTTPException(status_code=500, detail="Échec de la suppression.")

    log.info("Admin : utilisateur '%s' supprimé", username)
    return {"message": f"Utilisateur '{username}' supprimé avec succès."}


@router.delete("/keys/{key_prefix}", status_code=200)
async def revoke_key(
    key_prefix: str,
    _: None = Depends(require_admin),
) -> dict:
    """
    Révoque une clé API par son préfixe (ex: 'llmgw-abc12345').
    La révocation est immédiate — la prochaine requête avec cette clé recevra un 401.
    """
    revoked = await db.revoke_key(key_prefix)
    if not revoked:
        raise HTTPException(
            status_code=404,
            detail=f"Aucune clé active avec le préfixe '{key_prefix}'."
        )

    log.info("Clé révoquée : préfixe '%s'", key_prefix)
    return {"message": f"Clé '{key_prefix}' révoquée avec succès."}


# ── Rapports d'usage ──────────────────────────────────────────────────────────

@router.get("/usage", response_model=list[UsageEntry])
async def get_usage(
    username: Optional[str] = Query(None, description="Filtrer par utilisateur"),
    from_date: Optional[str] = Query(None, description="Date de début ISO 8601 (ex: 2025-01-01)"),
    to_date: Optional[str] = Query(None, description="Date de fin ISO 8601 (ex: 2025-01-31)"),
    limit: int = Query(1000, ge=1, le=10000),
    _: None = Depends(require_admin),
) -> list[dict]:
    """Journal d'usage détaillé (une ligne par requête)."""
    user_id: int | None = None
    if username:
        user = await db.get_user_by_username(username)
        if not user:
            raise HTTPException(status_code=404, detail=f"Utilisateur '{username}' introuvable.")
        user_id = user["id"]

    return await db.get_usage_report(
        user_id=user_id,
        from_date=from_date,
        to_date=to_date,
        limit=limit,
    )


@router.get("/usage/summary", response_model=list[UsageSummaryEntry])
async def get_usage_summary(
    from_date: Optional[str] = Query(None, description="Date de début ISO 8601"),
    to_date: Optional[str] = Query(None, description="Date de fin ISO 8601"),
    _: None = Depends(require_admin),
) -> list[dict]:
    """Résumé agrégé par utilisateur — idéal pour le reporting mensuel."""
    return await db.get_usage_summary(from_date=from_date, to_date=to_date)
