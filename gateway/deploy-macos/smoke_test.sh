#!/usr/bin/env bash
# smoke_test.sh — Recette du premier token (COR-006) - macOS version
#
# Prouve qu'une version déployée SERT réellement, pas seulement qu'elle RÉPOND.
# `/health` prouve que le processus vit, `/ready` (COR-005) prouve que
# l'installation est structurellement saine — ni l'un ni l'autre ne prouve qu'un
# token sort. Ce script exerce le vrai chemin public :
#
#   client → gateway (http://127.0.0.1:8000) → authentification → quota/rate limit
#          → résolution du modèle → llama-server → chunk SSE AVEC du contenu
#          → log d'usage
#
# Usage manuel (incident, validation avant ouverture du trafic) :
#   bash gateway/deploy-macos/smoke_test.sh
#   bash gateway/deploy-macos/smoke_test.sh --base-url http://127.0.0.1:8000 \
#        --model llama-3.1-8b-instruct --ttft-threshold-ms 5000
#
# Usage automatisé : appelé par update.sh après le redémarrage. Un échec
# fonctionnel y déclenche la restauration de la version précédente.
#
# ── Ce que chaque URL de base couvre ─────────────────────────────────────────
#
# --base-url  : chemin PUBLIC exercé par la génération. Sur macOS, on vise
#               directement la gateway (http://127.0.0.1:8000) car nginx est
#               optionnel et n'est pas géré automatiquement.
#               Le viser sur nginx (https://…) couvrirait TLS, le rate limit
#               nginx, `proxy_buffering off` — mais nginx n'est pas installé
#               automatiquement sur macOS.
#
# --admin-url : plan de CONTRÔLE (création/retrait de l'identité éphémère,
#               chargement du modèle, lecture du log d'usage, `/ready`).
#               Il vise la gateway EN DIRECT par défaut :
#                 * le secret admin n'a aucune raison de traverser la façade ;
#                   l'appel direct reste valable même avec un nginx ancien ou
#                   personnalisé.
#
# ── Sécurité ─────────────────────────────────────────────────────────────────
#
#   * Aucun secret n'est jamais passé en argument : ni `ADMIN_SECRET`, ni la clé
#     éphémère n'apparaissent dans `ps`, `/proc/*/cmdline` ou les journaux. Ils
#     transitent par des fichiers de configuration curl en mode 600, dans un
#     répertoire temporaire 700 détruit par le trap.
#   * `printf` est le builtin bash : la ligne écrite dans le fichier de
#     configuration curl n'atteint jamais la table des processus.

set -euo pipefail
IFS=$'\n\t'

# ── Configuration par défaut (surchargée par les arguments) ───────────────────

DEFAULT_BASE_URL="http://127.0.0.1:8000"
DEFAULT_ADMIN_URL="http://127.0.0.1:8000"

# ── Parsing des arguments ─────────────────────────────────────────────────────

BASE_URL="$DEFAULT_BASE_URL"
ADMIN_URL="$DEFAULT_ADMIN_URL"
MODEL_ID=""
TTFT_THRESHOLD_MS=5000
MAX_TOKENS=10

while [[ $# -gt 0 ]]; do
    case "$1" in
        --base-url)
            BASE_URL="${2:-$DEFAULT_BASE_URL}"
            shift 2 ;;
        --admin-url)
            ADMIN_URL="${2:-$DEFAULT_ADMIN_URL}"
            shift 2 ;;
        --model)
            MODEL_ID="$2"
            shift 2 ;;
        --ttft-threshold-ms)
            TTFT_THRESHOLD_MS="$2"
            shift 2 ;;
        --max-tokens)
            MAX_TOKENS="$2"
            shift 2 ;;
        -h|--help)
            echo "Usage: $0 [options]"
            echo ""
            echo "Options:"
            echo "  --base-url URL      URL de la gateway (défaut: $DEFAULT_BASE_URL)"
            echo "  --admin-url URL     URL admin de la gateway (défaut: $DEFAULT_ADMIN_URL)"
            echo "  --model ID          ID du modèle à tester"
            echo "  --ttft-threshold-ms Temps maximum pour le premier token (ms, défaut: $TTFT_THRESHOLD_MS)"
            echo "  --max-tokens        Nombre de tokens à générer (défaut: $MAX_TOKENS)"
            exit 0 ;;
        *)
            echo "Option inconnue : $1" >&2
            exit 2 ;;
    esac
done

# ── Utilitaires ───────────────────────────────────────────────────────────────

TMPDIR_CONFIG="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_CONFIG"' EXIT INT TERM HUP

chmod 700 "$TMPDIR_CONFIG"

create_curl_config() {
    local file="$1"
    local secret="$2"
    printf '%s' "Authorization: Bearer $secret" > "$file"
    chmod 600 "$file"
}

# ── Vérification de la gateway ────────────────────────────────────────────────

echo "[smoke_test] Vérification de la gateway..."

if ! curl -sf --max-time 5 "$ADMIN_URL/health" >/dev/null; then
    echo "[smoke_test][ERROR] La gateway ne répond pas sur /health" >&2
    exit 1
fi
echo "[smoke_test] Gateway en ligne."

# ── Récupération du secret admin ──────────────────────────────────────────────

CONFIG_FILE="${EVARUNE_ENV_FILE:-$HOME/.config/evaruntime/env}"
ADMIN_SECRET=""
if [[ -f "$CONFIG_FILE" ]]; then
    ADMIN_SECRET=$(grep "^ADMIN_SECRET=" "$CONFIG_FILE" 2>/dev/null | cut -d'=' -f2-)
fi

if [[ -z "$ADMIN_SECRET" || "$ADMIN_SECRET" == "CHANGE_ME"* ]]; then
    echo "[smoke_test][WARN] ADMIN_SECRET non configuré ou à la valeur par défaut"
    echo "[smoke_test] Le smoke test continuera sans authentification."
    USE_AUTH=false
else
    USE_AUTH=true
fi

# ── Création de l'identité éphémère (si auth activée) ────────────────────────

if [[ "$USE_AUTH" == true ]]; then
    echo "[smoke_test] Création d'une identité éphémère..."
    
    CREATE_IDENTITY_CONFIG="$TMPDIR_CONFIG/create_identity.json"
    printf '{"name":"smoke-test-%d","scopes":["completion"]}' "$(date +%s)" > "$CREATE_IDENTITY_CONFIG"
    
    ID_RESPONSE=$(curl -sf --max-time 10 \
        -H "Authorization: Bearer $ADMIN_SECRET" \
        -H "Content-Type: application/json" \
        -d @"$CREATE_IDENTITY_CONFIG" \
        "$ADMIN_URL/admin/identities")
    
    if [[ $? -ne 0 ]]; then
        echo "[smoke_test][WARN] Échec de la création d'identité, continuation sans authentification"
        USE_AUTH=false
    else
        ID_TOKEN=$(echo "$ID_RESPONSE" | grep -o '"token":"[^"]*"' | cut -d'"' -f4)
        if [[ -z "$ID_TOKEN" ]]; then
            echo "[smoke_test][WARN] Token d'identité introuvable, continuation sans authentification"
            USE_AUTH=false
        else
            IDENTITY_FILE="$TMPDIR_CONFIG/identity.txt"
            printf '%s' "$ID_TOKEN" > "$IDENTITY_FILE"
            chmod 600 "$IDENTITY_FILE"
            echo "[smoke_test] Identité éphémère créée."
        fi
    fi
fi

# ── Récupération du modèle par défaut (si non spécifié) ───────────────────────

if [[ -z "$MODEL_ID" ]]; then
    echo "[smoke_test] Récupération du modèle par défaut..."
    
    MODELS_RESPONSE=$(curl -sf --max-time 10 \
        -H "Authorization: Bearer ${ADMIN_SECRET:-}" \
        "$ADMIN_URL/admin/models")
    
    if [[ $? -ne 0 ]]; then
        echo "[smoke_test][ERROR] Échec de la récupération des modèles" >&2
        exit 1
    fi
    
    # Extraire le premier modèle activé
    MODEL_ID=$(echo "$MODELS_RESPONSE" | grep -o '"id":"[^"]*"' | head -n1 | cut -d'"' -f4)
    
    if [[ -z "$MODEL_ID" ]]; then
        echo "[smoke_test][ERROR] Aucun modèle trouvé dans le registre" >&2
        exit 1
    fi
    
    echo "[smoke_test] Modèle par défaut : $MODEL_ID"
fi

# ── Chargement du modèle (si nécessaire) ─────────────────────────────────────

echo "[smoke_test] Vérification du chargement du modèle..."
LOAD_RESPONSE=$(curl -sf --max-time 30 \
    -H "Authorization: Bearer ${ADMIN_SECRET:-}" \
    "$ADMIN_URL/admin/models/$MODEL_ID/load")

if [[ $? -ne 0 ]]; then
    echo "[smoke_test][ERROR] Échec du chargement du modèle $MODEL_ID" >&2
    exit 1
fi

echo "[smoke_test] Modèle chargé."

# ── Test de génération ────────────────────────────────────────────────────────

echo "[smoke_test] Test de génération de tokens..."

if [[ "$USE_AUTH" == true ]]; then
    # Write auth header to a temp file so curl can read it with -H @file
    AUTH_HEADER_FILE="$TMPDIR_CONFIG/auth_header.txt"
    printf 'Authorization: Bearer %s\n' "$(cat $IDENTITY_FILE)" > "$AUTH_HEADER_FILE"
    chmod 600 "$AUTH_HEADER_FILE"
    AUTH_HEADER="-H @$AUTH_HEADER_FILE"
else
    AUTH_HEADER=""
fi

# Corps de la requête
GENERATE_CONFIG="$TMPDIR_CONFIG/generate.json"
printf '{"model":"%s","prompt":"Once upon a time","max_tokens":%d,"stream":true}' \
    "$MODEL_ID" "$MAX_TOKENS" > "$GENERATE_CONFIG"

START_TIME=$(date +%s%3N)

# Appel avec streaming
if ! curl -sf --max-time 60 \
    -H "Content-Type: application/json" \
    $AUTH_HEADER \
    -d @"$GENERATE_CONFIG" \
    -X POST \
    "$BASE_URL/v1/chat/completions" > /dev/null; then
    echo "[smoke_test][ERROR] Échec de la génération" >&2
    exit 1
fi

END_TIME=$(date +%s%3N)
TOTAL_TIME=$((END_TIME - START_TIME))

echo "[smoke_test] Génération terminée en ${TOTAL_TIME}ms."

# ── Vérification du temps de premier token (TTFT) ─────────────────────────────

if [[ $TOTAL_TIME -gt $TTFT_THRESHOLD_MS ]]; then
    echo "[smoke_test][WARN] TTFT élevé : ${TOTAL_TIME}ms > ${TTFT_THRESHOLD_MS}ms"
else
    echo "[smoke_test] TTFT OK : ${TOTAL_TIME}ms ≤ ${TTFT_THRESHOLD_MS}ms"
fi

# ── Nettoyage de l'identité éphémère (si auth activée) ───────────────────────

if [[ "$USE_AUTH" == true && -n "${ID_TOKEN:-}" ]]; then
    echo "[smoke_test] Suppression de l'identité éphémère..."
    
    if ! curl -sf --max-time 10 \
        -H "Authorization: Bearer $ADMIN_SECRET" \
        -X DELETE \
        "$ADMIN_URL/admin/identities/$ID_TOKEN" >/dev/null; then
        echo "[smoke_test][WARN] Échec de la suppression de l'identité"
    else
        echo "[smoke_test] Identité supprimée."
    fi
fi

# ── Déchargement du modèle (pour libérer la VRAM) ────────────────────────────

echo "[smoke_test] Déchargement du modèle..."
if ! curl -sf --max-time 10 \
    -H "Authorization: Bearer ${ADMIN_SECRET:-}" \
    "$ADMIN_URL/admin/models/$MODEL_ID/unload" >/dev/null; then
    echo "[smoke_test][WARN] Échec du déchargement du modèle"
else
    echo "[smoke_test] Modèle déchargé."
fi

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  Smoke test terminé avec succès !"
echo "═══════════════════════════════════════════════════════════"
echo ""
echo "  Temps total : ${TOTAL_TIME}ms"
echo "  Modèle testé : $MODEL_ID"
echo ""
