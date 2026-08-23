#!/usr/bin/env bash
# update.sh — Mise à jour transactionnelle de la gateway
#
# Usage (sur le serveur GPU, depuis n'importe quel répertoire) :
#   sudo bash /chemin/vers/repo/gateway/deploy/update.sh
#
# Ce script est idempotent et préserve :
#   - /etc/llm-gateway/env  (hors clés de mode explicitement demandées)
#   - /var/lib/llm-gateway/gateway.db  (base de données)
#   - /models/  (modèles GGUF)
#
# Il rafraîchit en revanche les artefacts d'exploitation versionnés : service
# systemd principal, timer de sauvegarde SQLite et son script. La rotation
# journald est installée si absente (jamais écrasée). Le timer de sauvegarde
# n'est (ré)activé automatiquement que s'il n'a jamais été installé — un timer
# volontairement désactivé par l'opérateur est laissé tel quel. Un timer activé
# mais resté INACTIF (défaut OPS-008 des versions antérieures) est en revanche
# armé, sans quoi aucune sauvegarde ne tourne jusqu'au prochain reboot.
#
# Il ne régénère jamais un secret existant et ne remplace jamais nodes.yaml.
# Pour mettre à jour aussi nginx : ajouter --nginx en argument.
#
# ── Gate de validation (COR-006) ─────────────────────────────────────────────
# Une version n'est conservée que si elle SERT réellement, pas seulement si elle
# répond. Trois contrôles se succèdent :
#
#   1. `evaruntime doctor` AVANT la bascule, sur le venv neuf et le code déjà
#      synchronisé : un hôte inapte est détecté sans jamais arrêter le service.
#   2. `/ready` après le redémarrage (readiness structurelle stricte, COR-005).
#   3. `deploy/smoke_test.sh` : recette du premier token de bout en bout sur le
#      vrai chemin public, puis `doctor` une seconde fois.
#
# L'ancienne version (code, venv, unité, mode) reste conservée jusqu'à la fin de
# la recette : tout échec fonctionnel la restaure. Une régression de TTFT est en
# revanche une ALERTE et ne provoque JAMAIS de rollback, sauf si l'opérateur
# l'exige explicitement avec --ttft-gate.

set -Eeuo pipefail
IFS=$'\n\t'

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
section() { echo -e "\n${CYAN}▶ $*${NC}"; }

# ── Démarrages systemd (COR-017) ──────────────────────────────────────────────
# Une unité qui a échoué plusieurs fois de suite atteint son start-limit :
# systemd refuse alors TOUT démarrage (« Start request repeated too quickly »)
# tant que le compteur n'est pas remis à zéro. C'est exactement ce qui a laissé
# la gateway à terre après un rollback de mode. `reset-failed` remet ce compteur
# à zéro; sur une unité saine c'est un no-op, donc sans risque avant chaque
# démarrage. Toute (re)mise en marche d'une unité passe par ces fonctions.
systemctl_start() {
    local unit="$1"
    systemctl reset-failed "$unit" 2>/dev/null || true
    systemctl start "$unit"
}

systemctl_restart() {
    local unit="$1"
    systemctl reset-failed "$unit" 2>/dev/null || true
    systemctl restart "$unit"
}

# `enable --now` = enable + start : il arme réellement le timer (OPS-008).
systemctl_enable_now() {
    local unit="$1"
    systemctl reset-failed "$unit" 2>/dev/null || true
    systemctl enable --now "$unit"
}

# Le service n'a pas pu être remis en marche alors que tout ce qui pouvait être
# restauré l'a déjà été : c'est une COUPURE de service, pas un avertissement.
# Message sans ambiguïté, commande de rétablissement exacte, sortie non nulle.
service_down() {
    echo "" >&2
    echo -e "${RED}[INDISPONIBILITÉ]${NC} $*" >&2
    echo -e "${RED}[INDISPONIBILITÉ]${NC} llm-gateway n'a PAS redémarré : la gateway est À TERRE." >&2
    echo "  Rétablissement manuel immédiat :" >&2
    echo "    sudo systemctl reset-failed llm-gateway" >&2
    echo "    sudo systemctl start llm-gateway" >&2
    echo "    sudo systemctl status llm-gateway --no-pager" >&2
    echo "    sudo journalctl -u llm-gateway -n 100 --no-pager" >&2
    exit 1
}

# SCRIPT_DIR = gateway/ (un niveau au-dessus de deploy/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=deploy-mode-lib.sh
source "$SCRIPT_DIR/deploy/deploy-mode-lib.sh"
# shellcheck source=nginx-lib.sh
source "$SCRIPT_DIR/deploy/nginx-lib.sh"
# shellcheck source=venv-retention-lib.sh
source "$SCRIPT_DIR/deploy/venv-retention-lib.sh"
# shellcheck source=env-template-lib.sh
source "$SCRIPT_DIR/deploy/env-template-lib.sh"
# shellcheck source=gpu-preflight-lib.sh
source "$SCRIPT_DIR/deploy/gpu-preflight-lib.sh"
# shellcheck source=code-layout-lib.sh
source "$SCRIPT_DIR/deploy/code-layout-lib.sh"

usage() {
    cat <<EOF
Usage: $0 [--mode local|cluster] [--cluster] [--allow-mode-change] [--nginx] [--dry-run]
          [--smoke-base-url URL] [--smoke-model ID] [--ttft-threshold-ms N] [--ttft-gate]
          [--skip-smoke-test] [--skip-doctor]

Sans --mode, le mode présent dans /etc/llm-gateway/env est conservé (local si
la clé est absente). --cluster reste un alias de --mode cluster.
Une migration exige --allow-mode-change. --dry-run ne modifie ni le dépôt ni l'hôte.

Validation de la version déployée (COR-006) :
  --smoke-base-url URL   Chemin public exercé par la recette du premier token.
                         Le viser sur nginx (https://…) couvre aussi TLS et le
                         non-buffering SSE du reverse-proxy. Défaut : la gateway
                         en direct. (env : EVA_SMOKE_BASE_URL)
  --smoke-model ID       Modèle exercé. Défaut : DEFAULT_MODEL_ID, sinon le plus
                         petit modèle activé.        (env : EVA_SMOKE_MODEL)
  --ttft-threshold-ms N  Seuil d'ALERTE sur le TTFT. 0 = désactivé (défaut).
                         (env : EVA_SMOKE_TTFT_THRESHOLD_MS)
  --ttft-gate            Transforme le dépassement du seuil en cause de rollback.
                         Sans cette option, un TTFT lent est signalé et la version
                         reste déployée.             (env : EVA_SMOKE_TTFT_GATE=1)
  --skip-smoke-test      DANGEREUX — désactive la recette du premier token : la
                         version est alors validée sur /ready seul, comme avant
                         COR-006. À réserver à un dépannage.
  --skip-doctor          Désactive les préflights doctor avant et après bascule.

Après une mise à jour validée, les venvs de release sont purgés en ne conservant
que les 2 plus récents (l'actif et le précédent, pour un retour arrière manuel).
EVA_GATEWAY_VENV_KEEP=N règle ce nombre; l'actif n'est jamais supprimé.
EOF
}

UPDATE_NGINX=false
REQUESTED_MODE=""
MODE_WAS_EXPLICIT=false
ALLOW_MODE_CHANGE=false
DRY_RUN=false
RUN_SMOKE_TEST=true
RUN_DOCTOR=true
SMOKE_BASE_URL="${EVA_SMOKE_BASE_URL:-}"
SMOKE_MODEL="${EVA_SMOKE_MODEL:-}"
TTFT_THRESHOLD_MS="${EVA_SMOKE_TTFT_THRESHOLD_MS:-0}"
TTFT_GATE=false
[[ "${EVA_SMOKE_TTFT_GATE:-0}" != "1" ]] || TTFT_GATE=true
while [[ $# -gt 0 ]]; do
    case "$1" in
        --smoke-base-url)
            [[ -n "${2:-}" ]] || { echo "--smoke-base-url requiert une URL" >&2; exit 2; }
            SMOKE_BASE_URL="$2"; shift 2 ;;
        --smoke-model)
            [[ -n "${2:-}" ]] || { echo "--smoke-model requiert un identifiant" >&2; exit 2; }
            SMOKE_MODEL="$2"; shift 2 ;;
        --ttft-threshold-ms)
            [[ "${2:-}" =~ ^[0-9]+$ ]] || { echo "--ttft-threshold-ms requiert un entier" >&2; exit 2; }
            TTFT_THRESHOLD_MS="$2"; shift 2 ;;
        --ttft-gate)       TTFT_GATE=true; shift ;;
        --skip-smoke-test) RUN_SMOKE_TEST=false; shift ;;
        --skip-doctor)     RUN_DOCTOR=false; shift ;;
        --mode)
            [[ $# -ge 2 ]] || { echo "--mode requiert local ou cluster" >&2; usage; exit 2; }
            deploy_validate_mode "$2" || { echo "Mode invalide : $2" >&2; usage; exit 2; }
            [[ "$MODE_WAS_EXPLICIT" != true || "$REQUESTED_MODE" == "$2" ]] || { echo "Options de mode contradictoires" >&2; exit 2; }
            REQUESTED_MODE="$2"; MODE_WAS_EXPLICIT=true; shift 2 ;;
        --mode=*)
            value="${1#*=}"
            deploy_validate_mode "$value" || { echo "Mode invalide : $value" >&2; usage; exit 2; }
            [[ "$MODE_WAS_EXPLICIT" != true || "$REQUESTED_MODE" == "$value" ]] || { echo "Options de mode contradictoires" >&2; exit 2; }
            REQUESTED_MODE="$value"; MODE_WAS_EXPLICIT=true; shift ;;
        --cluster)
            [[ "$MODE_WAS_EXPLICIT" != true || "$REQUESTED_MODE" == "cluster" ]] || { echo "--cluster contredit --mode $REQUESTED_MODE" >&2; exit 2; }
            REQUESTED_MODE="cluster"; MODE_WAS_EXPLICIT=true; shift ;;
        --allow-mode-change) ALLOW_MODE_CHANGE=true; shift ;;
        --nginx) UPDATE_NGINX=true; shift ;;
        --dry-run) DRY_RUN=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Option inconnue : $1" >&2; usage; exit 2 ;;
    esac
done

[[ "$TTFT_THRESHOLD_MS" =~ ^[0-9]+$ ]] || \
    { echo "Seuil TTFT invalide : $TTFT_THRESHOLD_MS (EVA_SMOKE_TTFT_THRESHOLD_MS)" >&2; exit 2; }

SMOKE_TEST_SCRIPT="$SCRIPT_DIR/deploy/smoke_test.sh"
NGINX_SITE="/etc/nginx/sites-available/llm-gateway"

# Répertoires
# ── Commandes exigées par le préflight (OPS-011) ──────────────────────────────
# Même convention déclarative que `install.sh` : source unique, dérivée par
# `gateway/tests/test_deploy_required_commands.py` et documentée en
# `docs/deployment.md` §1. Aucun `command -v <nom littéral>` ailleurs.
UPDATE_REQUIRED_COMMANDS=(awk chmod chown cp curl find git mkdir mktemp mv systemctl)
# Mode local uniquement : l'orchestrateur cluster n'a pas de GPU.
UPDATE_REQUIRED_COMMANDS_LOCAL=(nvidia-smi)
# Exigée seulement quand la recette du premier token est jouée (COR-006).
UPDATE_REQUIRED_COMMANDS_SMOKE=(python3)
# Absentes, ces commandes désactivent une fonction sans bloquer la mise à jour.
UPDATE_OPTIONAL_COMMANDS=(nginx sqlite3)

INSTALL_DIR="${LLM_GATEWAY_INSTALL_DIR:-/opt/llm-gateway}"
DATA_DIR="${LLM_GATEWAY_DATA_DIR:-/var/lib/llm-gateway}"
CONFIG_DIR="${LLM_GATEWAY_CONFIG_DIR:-/etc/llm-gateway}"
DB_PATH="$DATA_DIR/gateway.db"
BACKUP_DIR="$DATA_DIR/backups"
SERVICE_USER="llmservice"
CONFIG_FILE="$CONFIG_DIR/env"
# Rétention des venvs de release (OPS-010) : la release active + la précédente,
# pour qu'un retour arrière manuel reste possible. Les plus anciennes sont
# purgées une fois la version validée. Voir deploy/venv-retention-lib.sh.
VENV_KEEP_RELEASES="${EVA_GATEWAY_VENV_KEEP:-2}"
[[ "$VENV_KEEP_RELEASES" =~ ^[1-9][0-9]*$ ]] || \
    error "EVA_GATEWAY_VENV_KEEP doit être un entier >= 1 (reçu : '$VENV_KEEP_RELEASES')."
CURRENT_MODE="$(deploy_env_value "$CONFIG_FILE" CLUSTER_MODE)"
EFFECTIVE_MODE="$(deploy_select_mode "$CONFIG_FILE" "$REQUESTED_MODE")" || exit 1
PREVIOUS_MODE="${CURRENT_MODE:-local}"

if [[ -n "$CURRENT_MODE" && "$CURRENT_MODE" != "$EFFECTIVE_MODE" && "$ALLOW_MODE_CHANGE" != true && "$DRY_RUN" != true ]]; then
    error "Migration $CURRENT_MODE → $EFFECTIVE_MODE non confirmée. Vérifiez avec --dry-run puis ajoutez --allow-mode-change."
fi

echo ""
echo "EVARuntime — préflight mise à jour"
echo "  Mode demandé : ${REQUESTED_MODE:-<auto>}"
echo "  Mode existant  : ${CURRENT_MODE:-<absent; local par défaut>}"
echo "  Mode effectif  : $EFFECTIVE_MODE"
echo "  Conservation   : env, models.yaml, nodes.yaml, secrets, DB et GGUF"
if [[ "$RUN_SMOKE_TEST" == true ]]; then
    echo "  Validation     : doctor (avant/après) + /ready + recette du premier token"
    echo "  Chemin exercé  : ${SMOKE_BASE_URL:-<gateway en direct>}"
    if [[ "$TTFT_THRESHOLD_MS" -gt 0 ]]; then
        if [[ "$TTFT_GATE" == true ]]; then
            echo "  Seuil TTFT     : ${TTFT_THRESHOLD_MS} ms — GATE (dépassement = rollback)"
        else
            echo "  Seuil TTFT     : ${TTFT_THRESHOLD_MS} ms — alerte seulement (aucun rollback)"
        fi
    else
        echo "  Seuil TTFT     : désactivé (mesure rapportée sans gate)"
    fi
else
    echo "  Validation     : /ready SEUL — recette du premier token désactivée (--skip-smoke-test)"
fi

if [[ "$DRY_RUN" == true ]]; then
    echo "  Action         : aucune (--dry-run; pas de git pull, pip, systemd ou écriture)"
    echo "  Rétention venv : $VENV_KEEP_RELEASES releases conservées (actif + précédents)"
    # La purge n'a lieu qu'après une version VALIDÉE. On annonce ce qu'elle
    # emporterait dans l'état actuel du disque, sans rien supprimer : la release
    # neuve n'existant pas encore, la liste est majorante d'une place.
    while IFS= read -r prunable_venv; do
        [[ -n "$prunable_venv" ]] || continue
        echo "                   serait purgé : $prunable_venv"
    done < <(gateway_venv_prunable_releases "$INSTALL_DIR" "$INSTALL_DIR/venv" "$VENV_KEEP_RELEASES")
    if [[ -n "$CURRENT_MODE" && "$CURRENT_MODE" != "$EFFECTIVE_MODE" ]]; then
        echo "  Migration      : $CURRENT_MODE → $EFFECTIVE_MODE; l'exécution exigera --allow-mode-change"
    fi
    [[ "$EFFECTIVE_MODE" == "cluster" ]] && echo "  Agents         : à mettre à jour séparément sur chaque nœud"
    exit 0
fi

[[ $EUID -eq 0 ]] || error "Ce script doit être exécuté en root : sudo bash update.sh"

[[ -d "$INSTALL_DIR" ]] || error "$INSTALL_DIR n'existe pas — lancez d'abord install.sh"
[[ -f "$INSTALL_DIR/venv/bin/python" ]] || error "venv introuvable — lancez d'abord install.sh"
[[ -f "$CONFIG_FILE" ]] || error "Configuration introuvable : $CONFIG_FILE"
required=()
required+=("${UPDATE_REQUIRED_COMMANDS[@]}")
[[ "$RUN_SMOKE_TEST" != true ]] || required+=("${UPDATE_REQUIRED_COMMANDS_SMOKE[@]}")
for command_name in "${required[@]}"; do
    command -v "$command_name" &>/dev/null || \
        error "Préflight : commande requise introuvable : $command_name (cf. docs/deployment.md §1)"
done
UPDATE_ALLOW_NO_GPU="$(deploy_env_value "$CONFIG_FILE" "$GPU_WAIVER_ENV_KEY")"
UPDATE_GPU_VERDICT="$(
    deploy_gpu_verdict \
        "$EFFECTIVE_MODE" "$UPDATE_ALLOW_NO_GPU" "${UPDATE_REQUIRED_COMMANDS_LOCAL[0]}"
)" || error "Préflight local : hôte sans GPU et absence non assumée dans $CONFIG_FILE."
if [[ "$UPDATE_GPU_VERDICT" == "waived" ]]; then
    warn "Hôte SANS GPU : dérogation persistée respectée ($GPU_WAIVER_ENV_KEY=true)."
elif [[ "$UPDATE_GPU_VERDICT" == "detected" ]] && \
        deploy_gpu_waiver_declared "$UPDATE_ALLOW_NO_GPU"; then
    warn "GPU détecté mais $GPU_WAIVER_ENV_KEY=true subsiste : doctor signalera cette renonciation périmée."
fi
if [[ "$RUN_SMOKE_TEST" == true ]]; then
    [[ -f "$SMOKE_TEST_SCRIPT" ]] || \
        error "Préflight : recette du premier token introuvable ($SMOKE_TEST_SCRIPT). Utilisez --skip-smoke-test en connaissance de cause."
fi
if [[ "$EFFECTIVE_MODE" == "cluster" ]]; then
    [[ -f "$SCRIPT_DIR/deploy/llm-gateway-cluster.service" ]] || error "Préflight : unité orchestrateur introuvable"
else
    LLAMA_BIN="$(deploy_env_value "$CONFIG_FILE" LLAMA_SERVER_BIN)"
    [[ -x "${LLAMA_BIN:-/opt/llama.cpp/current/llama-server}" ]] || error "Préflight local : llama-server non exécutable (${LLAMA_BIN:-/opt/llama.cpp/current/llama-server})"
fi
# ── Durcissements absents d'un environnement antérieur à SEC-002 ──────────────
# `update.sh` ne régénère JAMAIS /etc/llm-gateway/env : un hôte installé avant
# SEC-002 n'aurait donc jamais ces clés. Elles ne sont PAS ajoutées d'autorité —
# écrire « CORS_ALLOW_ORIGINS= » sur une installation qui sert un client
# navigateur la casserait en silence, au milieu d'une mise à jour. On signale,
# l'opérateur tranche.
MISSING_HARDENING=()
for hardening_key in "${DEPLOY_HARDENING_KEYS[@]}"; do
    if ! grep -qE "^[[:space:]]*${hardening_key}=" "$CONFIG_FILE"; then
        MISSING_HARDENING+=("$hardening_key")
    fi
done
if (( ${#MISSING_HARDENING[@]} > 0 )); then
    warn "Durcissements SEC-002 absents de $CONFIG_FILE : ${MISSING_HARDENING[*]}"
    warn "→ Cette mise à jour ne les ajoute pas : les poser sans vous demander"
    warn "  pourrait couper un client navigateur ou refuser le démarrage."
    warn "→ Voir docs/deployment.md §5, puis « evaruntime doctor » avant de démarrer."
fi

info "Préflight validé; mise à jour en mode $EFFECTIVE_MODE."

prepare_model_registry() {
    local configured_models_file legacy_models_file
    configured_models_file="$(deploy_env_value "$CONFIG_FILE" MODELS_CONFIG_PATH)"
    legacy_models_file="$CONFIG_DIR/models.yaml"
if [[ -z "$configured_models_file" || "$configured_models_file" == "$legacy_models_file" ]]; then
    MODELS_FILE="$DATA_DIR/models.yaml"
    if [[ ! -f "$MODELS_FILE" ]]; then
        if [[ -f "$legacy_models_file" ]]; then
            cp "$legacy_models_file" "$MODELS_FILE"
        else
            cp "$SCRIPT_DIR/models.yaml" "$MODELS_FILE"
        fi
    fi
    chown "$SERVICE_USER:$SERVICE_USER" "$MODELS_FILE"
    chmod 640 "$MODELS_FILE"
    deploy_set_env_value "$CONFIG_FILE" MODELS_CONFIG_PATH "$MODELS_FILE"
    warn "Registre copié sans suppression vers $MODELS_FILE pour permettre les mutations admin atomiques."
else
    MODELS_FILE="$configured_models_file"
    if [[ "$MODELS_FILE" != "$DATA_DIR/"* ]]; then
        warn "Registre personnalisé conservé : vérifiez que llmservice peut écrire dans $(dirname "$MODELS_FILE")."
    fi
fi
}

install_gateway_service_unit() {
    local mode="$1"
    if [[ "$mode" == "cluster" ]]; then
        cp "$SCRIPT_DIR/deploy/llm-gateway-cluster.service" /etc/systemd/system/llm-gateway.service
    else
        cp "$SCRIPT_DIR/deploy/llm-gateway.service" /etc/systemd/system/llm-gateway.service
    fi
}

restore_previous_service_unit() {
    local fallback_mode="$1"
    if [[ -f "${UNIT_SNAPSHOT:-}" ]]; then
        cp "$UNIT_SNAPSHOT" /etc/systemd/system/llm-gateway.service
        chmod 644 /etc/systemd/system/llm-gateway.service
    else
        install_gateway_service_unit "$fallback_mode"
    fi
}

restore_code_snapshot() {
    local snapshot="$1"
    deploy_restore_gateway_code "$snapshot" "$INSTALL_DIR"
    rm -rf "$INSTALL_DIR/static"
    [[ ! -d "$snapshot/static" ]] || cp -a "$snapshot/static" "$INSTALL_DIR/static"
    chown root:"$SERVICE_USER" "$INSTALL_DIR"/*.py \
        "$INSTALL_DIR/requirements.txt" "$INSTALL_DIR/requirements.lock"
    [[ ! -d "$INSTALL_DIR/cluster" ]] || chown -R root:"$SERVICE_USER" "$INSTALL_DIR/cluster"
    [[ ! -d "$INSTALL_DIR/bootstrap" ]] || chown -R root:"$SERVICE_USER" "$INSTALL_DIR/bootstrap"
    [[ ! -d "$INSTALL_DIR/static" ]] || chown -R root:"$SERVICE_USER" "$INSTALL_DIR/static"
    chmod 640 "$INSTALL_DIR"/*.py
    [[ ! -d "$INSTALL_DIR/cluster" ]] || chmod 750 "$INSTALL_DIR/cluster"
    [[ ! -d "$INSTALL_DIR/cluster" ]] || chmod 640 "$INSTALL_DIR/cluster"/*.py
    [[ ! -d "$INSTALL_DIR/bootstrap" ]] || chmod 750 "$INSTALL_DIR/bootstrap"
    [[ ! -d "$INSTALL_DIR/bootstrap" ]] || \
        chmod 640 "$INSTALL_DIR/bootstrap"/*.py "$INSTALL_DIR/bootstrap/catalog.yaml"
    chmod 644 "$INSTALL_DIR/requirements.txt" "$INSTALL_DIR/requirements.lock"
}

VENV_SWITCHED=false
PREVIOUS_VENV_TARGET=""
TRANSACTION_ARMED=false
CODE_MUTATED=false
MODE_ACTIVATED=false
SERVICE_RESTART_STARTED=false

activate_staged_venv() {
    if [[ -L "$INSTALL_DIR/venv" ]]; then
        PREVIOUS_VENV_TARGET="$(readlink -f "$INSTALL_DIR/venv")"
        rm -f "$INSTALL_DIR/venv"
    else
        PREVIOUS_VENV_TARGET="$INSTALL_DIR/venv-pre-update-$(date +%Y%m%d-%H%M%S)"
        mv "$INSTALL_DIR/venv" "$PREVIOUS_VENV_TARGET"
    fi
    # Armer le rollback dès que l'ancien chemin a été retiré. Si la création du
    # nouveau symlink échoue, le trap peut ainsi restaurer l'ancien venv.
    VENV_SWITCHED=true
    ln -s "$STAGED_VENV" "$INSTALL_DIR/venv"
}

rollback_venv() {
    [[ "$VENV_SWITCHED" == true ]] || return 0
    rm -f "$INSTALL_DIR/venv"
    ln -s "$PREVIOUS_VENV_TARGET" "$INSTALL_DIR/venv"
    VENV_SWITCHED=false
}

rollback_failed_transaction() {
    local exit_code="$1" restart_failed=false
    [[ "$TRANSACTION_ARMED" == true ]] || return "$exit_code"

    trap - ERR
    set +e
    warn "Erreur avant validation finale : rollback transactionnel automatique."
    deploy_set_env_value "$CONFIG_FILE" CLUSTER_MODE "${PREVIOUS_MODE:-local}"
    rollback_venv
    if [[ "$CODE_MUTATED" == true ]]; then
        restore_code_snapshot "$CODE_SNAPSHOT"
    fi
    restore_previous_service_unit "${PREVIOUS_MODE:-local}"
    systemctl daemon-reload
    # `set +e` est actif : on RESTAURE d'abord tout ce qui peut l'être, on relève
    # l'échec de démarrage seulement ensuite (COR-017). L'inverse interromprait
    # le rollback à mi-chemin.
    if [[ "$SERVICE_RESTART_STARTED" == true ]]; then
        systemctl_start llm-gateway || restart_failed=true
    fi
    warn "Code, venv, mode et unité précédents restaurés. Snapshot : $CODE_SNAPSHOT"
    if [[ "$restart_failed" == true ]]; then
        service_down "Rollback transactionnel terminé, mais le redémarrage du service a ÉCHOUÉ."
    fi
    exit "$exit_code"
}

# ── Gate de validation (COR-006) ──────────────────────────────────────────────

# `evaruntime doctor` (AUT-012). Exit codes : 0 conforme, 1 échec bloquant,
# 2 erreur d'usage CLI, 3 avertissements seulement, 4 erreur interne de doctor.
# 0 ET 3 valent succès : le défaut nginx COR-009, par exemple, est signalé en
# avertissement et ne doit pas empêcher un déploiement par ailleurs sain.
# --verify-hashes n'est JAMAIS utilisé ici : il relit intégralement les GGUF,
# soit plusieurs centaines de Go à chaque mise à jour.
run_doctor() {
    local python_bin="$1" label="$2" rc=0
    [[ "$RUN_DOCTOR" == true ]] || { info "doctor ($label) ignoré (--skip-doctor)."; return 0; }
    [[ -x "$python_bin" ]] || { warn "doctor ($label) : interpréteur introuvable ($python_bin) — contrôle ignoré."; return 0; }
    [[ -f "$INSTALL_DIR/cli.py" ]] || { warn "doctor ($label) : cli.py introuvable — contrôle ignoré."; return 0; }

    set +e
    "$python_bin" "$INSTALL_DIR/cli.py" doctor \
        --env-file "$CONFIG_FILE" \
        --nginx-conf "$NGINX_SITE" \
        --systemd-unit /etc/systemd/system/llm-gateway.service
    rc=$?
    set -e

    case "$rc" in
        0) info "doctor ($label) : hôte conforme."; return 0 ;;
        3) warn "doctor ($label) : avertissements seulement (exit 3) — la mise à jour continue."; return 0 ;;
        *) warn "doctor ($label) : échec bloquant (exit $rc)."; return 1 ;;
    esac
}

# Recette du premier token. Rend l'exit code du script : 0 succès, 1 échec
# fonctionnel, 3 préflight, 4 seuil TTFT (uniquement si --ttft-gate), 5 identité
# éphémère résiduelle.
run_smoke_test() {
    local rc=0
    local args=(--env-file "$CONFIG_FILE")
    [[ -z "$SMOKE_BASE_URL" ]] || args+=(--base-url "$SMOKE_BASE_URL")
    [[ -z "$SMOKE_MODEL" ]]    || args+=(--model "$SMOKE_MODEL")
    [[ "$TTFT_THRESHOLD_MS" -le 0 ]] || args+=(--ttft-threshold-ms "$TTFT_THRESHOLD_MS")
    [[ "$TTFT_GATE" != true ]] || args+=(--fail-on-ttft)

    set +e
    bash "$SMOKE_TEST_SCRIPT" "${args[@]}"
    rc=$?
    set -e
    return "$rc"
}

# Restauration de la version précédente après un redémarrage. Extraite pour être
# partagée par les deux causes de rollback post-bascule : readiness jamais
# atteinte, et recette du premier token en échec. Ne rend jamais la main.
rollback_deployed_release() {
    local cause="$1" attempt

    warn "$cause"

    if [[ "$PREVIOUS_MODE" != "$EFFECTIVE_MODE" ]]; then
        section "ROLLBACK  Mode $EFFECTIVE_MODE → $PREVIOUS_MODE"
        deploy_set_env_value "$CONFIG_FILE" CLUSTER_MODE "$PREVIOUS_MODE"
        rollback_venv
        restore_code_snapshot "$CODE_SNAPSHOT"
        restore_previous_service_unit "$PREVIOUS_MODE"
        systemctl daemon-reload
        systemctl stop llm-gateway || true
        # Mode, code, venv et unité sont restaurés : plus rien n'est en attente.
        # Un échec de démarrage ici est une indisponibilité, pas un avertissement.
        systemctl_start llm-gateway || \
            service_down "Rollback du mode $EFFECTIVE_MODE → $PREVIOUS_MODE : démarrage refusé par systemd."
        for attempt in $(seq 1 20); do
            sleep 2
            if curl -sf http://127.0.0.1:8000/ready > /dev/null 2>&1; then
                error "Migration de mode échouée; le mode $PREVIOUS_MODE a été restauré et le service est sain."
            fi
        done
        error "Migration de mode et rollback ont échoué. Intervention requise : journalctl -u llm-gateway -n 100"
    fi

    section "ROLLBACK  Restauration du snapshot déployé"
    rollback_venv
    restore_code_snapshot "$CODE_SNAPSHOT"
    restore_previous_service_unit "$EFFECTIVE_MODE"
    systemctl daemon-reload
    systemctl stop llm-gateway || true
    # Snapshot, venv et unité sont restaurés : plus rien n'est en attente.
    # Un échec de démarrage ici est une indisponibilité, pas un avertissement.
    systemctl_start llm-gateway || \
        service_down "Rollback vers $CODE_SNAPSHOT : démarrage refusé par systemd."

    ROLLBACK_OK=false
    for attempt in $(seq 1 20); do
        sleep 2
        if curl -sf http://127.0.0.1:8000/ready > /dev/null 2>&1; then
            ROLLBACK_OK=true
            break
        fi
    done

    if [[ "$ROLLBACK_OK" == true ]]; then
        warn "Rollback réussi depuis $CODE_SNAPSHOT; le checkout Git est resté intact."
        warn "La version ${AFTER:0:8} n'est pas déployée; investiguez avant de réessayer."
        [[ -n "$BACKUP_FILE" ]] && warn "Sauvegarde DB pré-update : $BACKUP_FILE"
        exit 1
    else
        error "Rollback ÉCHOUÉ. Intervention requise : sudo journalctl -u llm-gateway -n 100 --no-pager"
    fi
}

echo ""
echo -e "${CYAN}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  LLM Gateway — Mise à jour du code${NC}"
echo -e "${CYAN}═══════════════════════════════════════════════════════════════${NC}"
echo "  Repo  : $REPO_DIR"
echo "  Cible : $INSTALL_DIR"
echo ""

# ── 1. git pull ───────────────────────────────────────────────────────────────

section "1/5  Mise à jour du dépôt git"
cd "$REPO_DIR"

BEFORE=$(git rev-parse HEAD)
if [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
    error "Checkout Git modifié : committez ou stash-ez les changements avant la mise à jour."
fi
git pull --ff-only
AFTER=$(git rev-parse HEAD)

if [[ "$BEFORE" == "$AFTER" ]]; then
    warn "Aucune nouvelle version disponible (HEAD = ${AFTER:0:8})."
    warn "Le déploiement continue quand même (dépendances ou static peut-être modifiés)."
else
    info "Mise à jour : ${BEFORE:0:8} → ${AFTER:0:8}"
    git log --oneline "$BEFORE".."$AFTER"
fi

# Snapshot du code réellement déployé. Le rollback ne modifie jamais le
# checkout Git de l'opérateur et ne le laisse pas en detached HEAD.
CODE_SNAPSHOT="$BACKUP_DIR/code-pre-update-$(date +%Y%m%d-%H%M%S)-${BEFORE:0:8}"
mkdir -p "$CODE_SNAPSHOT"
deploy_snapshot_gateway_code "$INSTALL_DIR" "$CODE_SNAPSHOT"
[[ ! -d "$INSTALL_DIR/static" ]] || cp -a "$INSTALL_DIR/static" "$CODE_SNAPSHOT/static"
UNIT_SNAPSHOT="$CODE_SNAPSHOT/llm-gateway.service"
[[ ! -f /etc/systemd/system/llm-gateway.service ]] || cp -a /etc/systemd/system/llm-gateway.service "$UNIT_SNAPSHOT"
chmod -R go-rwx "$CODE_SNAPSHOT"
info "Snapshot de rollback du code : $CODE_SNAPSHOT"
TRANSACTION_ARMED=true
trap 'rollback_failed_transaction $?' ERR

section "Préparation transactionnelle des dépendances Python"
STAGED_VENV="$INSTALL_DIR/venv-release-${AFTER:0:12}-$(date +%Y%m%d%H%M%S)"
"$INSTALL_DIR/venv/bin/python" -m venv "$STAGED_VENV"
"$STAGED_VENV/bin/pip" install --require-hashes \
    -r "$SCRIPT_DIR/requirements.lock" --quiet
"$STAGED_VENV/bin/pip" check
info "Venv neuf validé : $STAGED_VENV (l'ancien reste actif jusqu'au redémarrage)."
if [[ "$EFFECTIVE_MODE" == "cluster" ]]; then
    deploy_apply_mode \
        cluster "$CONFIG_FILE" "$CONFIG_DIR" \
        "$SCRIPT_DIR/deploy/nodes.yaml.example"
    NODES_FILE="$(deploy_env_value "$CONFIG_FILE" CLUSTER_NODES_PATH)"
    chown root:"$SERVICE_USER" "$NODES_FILE"
    if ! "$STAGED_VENV/bin/python" -c \
        'import sys; from pathlib import Path; sys.path.insert(0, sys.argv[2]); from cluster.nodes_config import load_nodes_config; cfg = load_nodes_config(Path(sys.argv[1])); print(f"Topologie valide: {len(cfg.nodes)} nœud(s)")' \
        "$NODES_FILE" "$SCRIPT_DIR"; then
        deploy_set_env_value "$CONFIG_FILE" CLUSTER_MODE "$PREVIOUS_MODE"
        error "Topologie cluster invalide. Le mode $PREVIOUS_MODE est conservé; corrigez $NODES_FILE puis relancez."
    fi
    if [[ "$PREVIOUS_MODE" != "cluster" ]]; then
        deploy_set_env_value "$CONFIG_FILE" CLUSTER_MODE "$PREVIOUS_MODE"
    fi
    info "Topologie cluster validée avant toute synchronisation du code."
fi
prepare_model_registry

# ── 2. Synchronisation du code Python ─────────────────────────────────────────

section "2/5  Synchronisation du code source"
CODE_MUTATED=true
deploy_sync_gateway_code "$SCRIPT_DIR" "$INSTALL_DIR"

chown root:"$SERVICE_USER" "$INSTALL_DIR"/*.py \
    "$INSTALL_DIR/requirements.txt" "$INSTALL_DIR/requirements.lock"
chown -R root:"$SERVICE_USER" "$INSTALL_DIR/cluster"
chown -R root:"$SERVICE_USER" "$INSTALL_DIR/bootstrap"
chmod 640 "$INSTALL_DIR"/*.py "$INSTALL_DIR/cluster"/*.py
chmod 640 "$INSTALL_DIR/bootstrap"/*.py "$INSTALL_DIR/bootstrap/catalog.yaml"
chmod 750 "$INSTALL_DIR/cluster" "$INSTALL_DIR/bootstrap"
chmod 644 "$INSTALL_DIR/requirements.txt" "$INSTALL_DIR/requirements.lock"

info "Fichiers Python copiés (gateway + cluster/ + bootstrap/)."

# ── 3. Synchronisation des fichiers statiques ─────────────────────────────────

section "3/5  Synchronisation des fichiers statiques"
if [[ -d "$SCRIPT_DIR/static" ]]; then
    mkdir -p "$INSTALL_DIR/static"
    cp -r "$SCRIPT_DIR/static/." "$INSTALL_DIR/static/"
    chown -R root:"$SERVICE_USER" "$INSTALL_DIR/static"
    find "$INSTALL_DIR/static" -type d -exec chmod 755 {} \;
    find "$INSTALL_DIR/static" -type f -exec chmod 644 {} \;
    info "Fichiers statiques copiés ($(find "$SCRIPT_DIR/static" -type f | wc -l) fichiers)."
else
    info "Aucun répertoire static/ dans le dépôt — rien à copier."
fi

# ── 4. Mise à jour des dépendances Python ────────────────────────────────────

section "4/5  Dépendances Python validées"
info "Le venv staged a passé pip check; permutation au redémarrage."

apply_selected_mode() {
section "Activation du mode $EFFECTIVE_MODE"
deploy_apply_mode \
    "$EFFECTIVE_MODE" "$CONFIG_FILE" "$CONFIG_DIR" \
    "$SCRIPT_DIR/deploy/nodes.yaml.example"
chmod 640 "$CONFIG_FILE"
chown root:"$SERVICE_USER" "$CONFIG_FILE"

info "Mode $EFFECTIVE_MODE activé."
}

# ── 4b. Mise à jour nginx (optionnel) ────────────────────────────────────────

if [[ "$UPDATE_NGINX" == true ]]; then
    section "4b. Mise à jour nginx"
    if command -v nginx &>/dev/null; then
        # Même rendu conditionnel qu'à l'installation (OPS-009).
        nginx_render_conf "$SCRIPT_DIR/deploy/nginx.conf" /etc/nginx/sites-available/llm-gateway
        info "nginx ${NGINX_DETECTED_VERSION:-?} — HTTP/2 : ${NGINX_HTTP2_FORM}"
        if nginx -t 2>/dev/null; then
            nginx -s reload
            info "nginx rechargé."
        else
            warn "Erreur de configuration nginx — rechargement annulé. Vérifiez manuellement."
        fi
    else
        warn "nginx non trouvé — ignoré."
    fi
fi

# ── 4c. Sauvegarde de la base de données AVANT redémarrage ────────────────────
# On sauvegarde AVANT de redémarrer : si le nouveau code migre/altère le schéma
# au démarrage, on garde une copie cohérente de l'état d'avant la mise à jour.
# `sqlite3 .backup` gère correctement une base WAL active (contrairement à un
# simple `cp` qui peut capturer un WAL incohérent).

section "4c. Sauvegarde de la base de données"
BACKUP_FILE=""
if [[ -f "$DB_PATH" ]]; then
    if command -v sqlite3 &>/dev/null; then
        mkdir -p "$BACKUP_DIR"
        chown "$SERVICE_USER:$SERVICE_USER" "$BACKUP_DIR" 2>/dev/null || true
        chmod 750 "$BACKUP_DIR"
        BACKUP_FILE="$BACKUP_DIR/gateway-pre-update-$(date +%Y%m%d-%H%M%S).db"
        # .backup est atomique et sûr sur une base WAL active
        if sqlite3 "$DB_PATH" ".backup '$BACKUP_FILE'"; then
            chown "$SERVICE_USER:$SERVICE_USER" "$BACKUP_FILE" 2>/dev/null || true
            chmod 600 "$BACKUP_FILE"
            info "Sauvegarde DB : $BACKUP_FILE ($(du -h "$BACKUP_FILE" | cut -f1))"
        else
            warn "Échec de la sauvegarde SQLite — la mise à jour continue quand même."
            warn "Vérifiez manuellement la base $DB_PATH avant de poursuivre."
            BACKUP_FILE=""
        fi
    else
        warn "sqlite3 introuvable — sauvegarde DB ignorée (installer : apt install sqlite3)."
    fi
else
    info "Pas de base de données à sauvegarder ($DB_PATH absent)."
fi

# ── 4d. Artefacts d'exploitation (timer de sauvegarde + rotation journald) ────
# Rafraîchis à chaque mise à jour, comme le service principal. On respecte le
# choix de l'opérateur : le timer n'est activé automatiquement que s'il n'a jamais
# été installé (première mise à jour depuis cette version) ; s'il a été désactivé
# délibérément on n'y retouche pas. La conf journald (globale, souvent ajustée au
# disque local) est créée si absente mais jamais écrasée.

section "4d. Timer de sauvegarde + rotation journald"

# État AVANT copie : distingue « jamais installé » de « désactivé volontairement ».
BACKUP_TIMER_STATE="$(systemctl is-enabled llm-gateway-backup.timer 2>/dev/null || true)"

# Même jeu que sur une installation neuve : recette du premier token,
# bibliothèques qu'elle source et modèle de matrice runtime compris.
deploy_sync_gateway_operational_files "$SCRIPT_DIR" "$INSTALL_DIR"
chown -R root:"$SERVICE_USER" "$INSTALL_DIR/deploy"
chmod 750 "$INSTALL_DIR/deploy" "$INSTALL_DIR/deploy/llm-gateway-backup.sh" \
          "$INSTALL_DIR/deploy/smoke_test.sh"
chmod 640 "$INSTALL_DIR/deploy/deploy-mode-lib.sh" \
          "$INSTALL_DIR/deploy/nginx-lib.sh" \
          "$INSTALL_DIR/deploy/runtime-variants.yaml.example"
cp "$SCRIPT_DIR/deploy/llm-gateway-backup.service" /etc/systemd/system/
cp "$SCRIPT_DIR/deploy/llm-gateway-backup.timer"   /etc/systemd/system/
systemctl daemon-reload

# Un timer de sauvegarde récalcitrant ne doit jamais faire échouer — ni pire,
# faire rollbacker — une mise à jour par ailleurs saine : les deux armements
# ci-dessous sont volontairement non fatals et se contentent d'un avertissement.
case "$BACKUP_TIMER_STATE" in
    enabled)
        # OPS-008 : les versions antérieures faisaient `enable` sans `--now`. Le
        # timer est alors `enabled` mais `inactive` : absent de `list-timers`, il
        # ne sauvegarde rien jusqu'au prochain reboot. On répare cet état ici.
        if systemctl is-active --quiet llm-gateway-backup.timer; then
            info "Timer de sauvegarde déjà armé — unités rafraîchies."
        elif systemctl_start llm-gateway-backup.timer; then
            info "Timer de sauvegarde activé mais inactif — armé (03:15, rétention 14 j)."
        else
            warn "Timer de sauvegarde activé mais INACTIF, et son démarrage a échoué."
            warn "  sudo systemctl start llm-gateway-backup.timer"
        fi
        ;;
    disabled|masked)
        warn "Timer de sauvegarde présent mais désactivé (choix opérateur) — laissé tel quel."
        warn "  Réactiver : sudo systemctl enable --now llm-gateway-backup.timer"
        ;;
    *)
        # Vide/introuvable = jamais installé (première mise à jour depuis cette version).
        # `--now` : sans lui le timer resterait inactive jusqu'au prochain reboot.
        # La base existe déjà à ce stade (elle vient même d'être sauvegardée en
        # 4c), donc un éventuel rattrapage `Persistent=true` est sans danger.
        if ! command -v sqlite3 &>/dev/null; then
            warn "sqlite3 introuvable — timer copié mais NON armé."
            warn "  apt install sqlite3 && sudo systemctl enable --now llm-gateway-backup.timer"
        elif systemctl_enable_now llm-gateway-backup.timer; then
            info "Timer de sauvegarde quotidienne armé (03:15, rétention 14 j)."
        else
            warn "Timer de sauvegarde NON armé — vérifiez puis relancez :"
            warn "  sudo systemctl enable --now llm-gateway-backup.timer"
        fi
        ;;
esac

# Rotation journald : créée si absente, jamais écrasée (peut être ajustée localement).
JOURNALD_DROPIN="/etc/systemd/journald.conf.d/llm-gateway.conf"
if [[ ! -f "$JOURNALD_DROPIN" ]]; then
    mkdir -p /etc/systemd/journald.conf.d
    cp "$SCRIPT_DIR/deploy/journald-llm-gateway.conf" "$JOURNALD_DROPIN"
    systemctl_restart systemd-journald
    info "Rotation journald installée (SystemMaxUse=500M, rétention 30 j)."
else
    info "Rotation journald déjà présente — conservée ($JOURNALD_DROPIN)."
fi

# ── 4e. doctor AVANT la bascule ──────────────────────────────────────────────
# Le service tourne encore l'ANCIEN code : un hôte inapte est détecté sans
# aucune coupure. On sonde avec le venv neuf et le code déjà synchronisé, donc
# exactement l'exécutable qui servira après la bascule. Un échec bloquant
# déclenche le rollback transactionnel : le service n'est jamais arrêté.

section "4e. Préflight doctor (avant bascule)"
if ! run_doctor "$STAGED_VENV/bin/python" "avant bascule"; then
    warn "L'hôte ne satisfait pas les préflights de la version ${AFTER:0:8}."
    warn "Le service n'a pas été arrêté; le code précédent est restauré."
    rollback_failed_transaction 1
fi

# ── 5. Mise à jour du service systemd + redémarrage ──────────────────────────

section "5/5  Redémarrage du service"
apply_selected_mode
MODE_ACTIVATED=true
install_gateway_service_unit "$EFFECTIVE_MODE"
systemctl daemon-reload

# Arrêt propre (laisse le temps à llama-server de se terminer)
info "Arrêt du service (max 30s)…"
SERVICE_RESTART_STARTED=true
systemctl stop llm-gateway || true
activate_staged_venv

info "Démarrage du service…"
# Chemin nominal : l'échec reste DÉLIBÉRÉMENT non fatal. La sonde de readiness
# ci-dessous enchaîne sur `rollback_deployed_release`, qui restaure la version
# précédente — un `error` ici court-circuiterait ce rollback.
systemctl_start llm-gateway || warn "Le démarrage a échoué; la readiness déclenchera le rollback."

# ── Attente du health check ───────────────────────────────────────────────────

echo -n "  Attente du démarrage"
HEALTHY=false
for i in $(seq 1 20); do
    sleep 2
    echo -n "."
    if curl -sf http://127.0.0.1:8000/ready > /dev/null 2>&1; then
        HEALTHY=true
        break
    fi
done
echo ""

TRANSACTION_ARMED=false
trap - ERR

if [[ "$HEALTHY" == true ]]; then
    HEALTH=$(curl -s http://127.0.0.1:8000/ready)
    info "Service prêt : $HEALTH"
else
    # ── Rollback automatique ──────────────────────────────────────────────────
    # Le service n'est pas devenu ready. Code, venv, unité et mode reviennent au
    # snapshot précédent; la DB n'est jamais restaurée sans arbitrage humain.
    rollback_deployed_release "Le service ne répond pas après $((20 * 2))s."
fi

# ── Validation fonctionnelle de la version (COR-006) ─────────────────────────
# `/ready` ne prouve QUE la readiness structurelle : registre lisible, binaire
# exécutable, GGUF présents, base inscriptible. Elle ne prouve pas qu'un token
# sort. C'est exactement le trou par lequel une version incapable de générer
# était acceptée avant COR-006. L'ancienne version reste conservée (snapshot de
# code, venv précédent, unité) jusqu'à la fin de cette recette.

section "Recette du premier token (smoke test)"
if [[ "$RUN_SMOKE_TEST" != true ]]; then
    warn "Recette du premier token DÉSACTIVÉE (--skip-smoke-test)."
    warn "La version est validée sur /ready seul : une version incapable de générer"
    warn "peut donc être conservée. Relancez la recette dès que possible :"
    warn "  sudo bash $INSTALL_DIR/deploy/smoke_test.sh"
else
    SMOKE_RC=0
    run_smoke_test || SMOKE_RC=$?
    case "$SMOKE_RC" in
        0)
            info "Premier token prouvé de bout en bout : la version ${AFTER:0:8} SERT."
            ;;
        4)
            # Ce code n'est atteignable que si l'opérateur a demandé --ttft-gate.
            rollback_deployed_release \
                "Gate TTFT activé et seuil dépassé (${TTFT_THRESHOLD_MS} ms) : rollback demandé par l'opérateur."
            ;;
        5)
            # La version SERT — le défaut porte sur l'identité de smoke test, pas
            # sur le code. Un rollback serait une réaction disproportionnée, mais
            # un compte résiduel doit rester bruyant et non nul.
            warn "La version ${AFTER:0:8} est fonctionnelle et RESTE déployée."
            error "Identité de smoke test résiduelle : retirez-la immédiatement (voir le rapport ci-dessus)."
            ;;
        *)
            rollback_deployed_release \
                "Recette du premier token en ÉCHEC (code $SMOKE_RC) : la version ${AFTER:0:8} répond mais ne sert pas."
            ;;
    esac
fi

# ── doctor APRÈS bascule ─────────────────────────────────────────────────────
# Deuxième passage, cette fois sur l'hôte réellement basculé : il peut relever
# une dérive apparue avec la nouvelle version (limites systemd, timeouts nginx,
# pool de ports). Non bloquant : la version a déjà PROUVÉ qu'elle sert, et un
# rollback sur un simple constat de configuration serait disproportionné.

section "Contrôle doctor (après bascule)"
if ! run_doctor "$INSTALL_DIR/venv/bin/python" "après bascule"; then
    warn "doctor signale un écart sur l'hôte basculé — à traiter, sans rollback :"
    warn "  la version déployée a passé la recette du premier token."
fi

# ── Rétention des venvs de release (OPS-010) ─────────────────────────────────
# Ici seulement : la version a PROUVÉ qu'elle sert, donc la release précédente
# n'est plus qu'un filet de sécurité — c'est à ce titre qu'on la garde, et qu'on
# ne garde qu'elle. Purger plus tôt supprimerait ce vers quoi un rollback
# rebascule. Un échec de purge n'est JAMAIS un échec de mise à jour : la gateway
# sert, il ne manque que de l'espace disque.

section "Rétention des venvs de release"
PRUNED=""
PRUNE_STATUS=0
PRUNED="$(gateway_venv_prune_releases "$INSTALL_DIR" "$INSTALL_DIR/venv" "$VENV_KEEP_RELEASES")" \
    || PRUNE_STATUS=$?
while IFS= read -r pruned_venv; do
    [[ -n "$pruned_venv" ]] || continue
    info "Venv de release purgé : $pruned_venv"
done <<< "$PRUNED"
if (( PRUNE_STATUS != 0 )); then
    warn "Purge des anciens venvs incomplète; la gateway est en service."
    warn "  Vérifiez l'espace disque puis : ls -d $INSTALL_DIR/venv-release-*"
elif [[ -z "$PRUNED" ]]; then
    info "$VENV_KEEP_RELEASES releases conservées — rien à purger."
else
    info "Venv actif conservé : $(readlink -f "$INSTALL_DIR/venv")"
    [[ -z "$PREVIOUS_VENV_TARGET" ]] || \
        info "Venv précédent conservé (retour arrière manuel) : $PREVIOUS_VENV_TARGET"
fi

# ── Vérification des secrets ──────────────────────────────────────────────────
# Les routes /admin répondent 503 tant qu'ADMIN_SECRET est vide ou CHANGE_ME_*.
if grep -qE '^(ADMIN_SECRET|INTERNAL_API_KEY|AGENT_SECRET)=(CHANGE_ME|[[:space:]]*$)' "$CONFIG_FILE" 2>/dev/null; then
    warn "Des secrets non configurés (CHANGE_ME_* ou vides) subsistent dans $CONFIG_FILE."
    warn "Les routes /admin restent DÉSACTIVÉES (503) tant qu'ADMIN_SECRET n'est pas défini."
    warn "Générer : python3 -c \"import secrets; print(secrets.token_urlsafe(32))\""
fi

# ── Résumé ────────────────────────────────────────────────────────────────────

echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Mise à jour terminée  ($(date '+%Y-%m-%d %H:%M:%S'))${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo ""
echo "  Version déployée : $(git -C "$REPO_DIR" log -1 --format='%h  %s  (%cr)')"
echo "  Mode déployé    : $EFFECTIVE_MODE"
echo ""
echo "  Commandes utiles :"
echo "    sudo journalctl -u llm-gateway -f          # logs en temps réel"
echo "    sudo systemctl status llm-gateway          # état du service"
echo "    curl http://127.0.0.1:8000/health          # santé de l'API"
echo "    curl http://127.0.0.1:8000/ready           # readiness structurelle (COR-005)"
echo "    sudo bash $INSTALL_DIR/deploy/smoke_test.sh"
echo "                                               # recette du premier token à la demande"
echo "    $INSTALL_DIR/venv/bin/python $INSTALL_DIR/cli.py doctor --env-file $CONFIG_FILE"
echo "                                               # préflight de l'hôte"
if [[ "$EFFECTIVE_MODE" == "cluster" ]]; then
    echo ""
    echo "  IMPORTANT : update.sh ne met pas les nœuds à jour à distance."
    echo "  Exécutez node_agent/deploy/update-agent.sh sur chaque agent."
fi
echo ""
