#!/usr/bin/env bash
# Mise à jour transactionnelle d'un node-agent déjà installé.
# Préserve /etc/llm-gateway-agent, les certificats, secrets, logs et /models.
#
# Stratégie de venv (COR-016) : identique à gateway/deploy/update.sh. Le venv
# neuf est construit à son EMPLACEMENT DÉFINITIF et `venv-agent` est un symlink
# que l'on bascule. Voir deploy/agent-venv-lib.sh pour le détail.
#
# Codes de sortie :
#   0  mise à jour déployée
#   1  échec : la version précédente a été restaurée et l'agent tourne à nouveau
#   9  INDISPONIBILITÉ : rollback effectué mais l'agent ne redémarre pas
set -Eeuo pipefail
IFS=$'\n\t'

EXIT_SERVICE_DOWN=9

DRY_RUN=false
PULL_REPOSITORY=true
INSTALL_DIR="/opt/llm-gateway"
# Chemin figé dans l'unité systemd. Après COR-016 c'est un SYMLINK vers la
# release en service; il n'est jamais déplacé, seulement rebasculé.
VENV_DIR="$INSTALL_DIR/venv-agent"
ENV_FILE="/etc/llm-gateway-agent/env"
SERVICE="llm-gateway-agent"
SERVICE_FILE="/etc/systemd/system/$SERVICE.service"
ROLLBACK_DIR="$INSTALL_DIR/.agent-rollback"
WORK_DIR=""
STAGED_VENV=""
ROLLBACK_READY=false
WAS_ACTIVE=false

info() { printf '[INFO] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*" >&2; }
die()  { printf '[ERREUR] %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
Usage: sudo bash update-agent.sh [--dry-run] [--no-pull]

  --dry-run   Exécute les préflights et décrit les actions, sans aucune écriture
  --no-pull   Déploie le checkout courant sans `git pull --ff-only`

La mise à jour construit un venv neuf, sauvegarde la version installée, redémarre
et sonde l'agent. En cas d'échec, code + venv + unité systemd sont restaurés.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=true; shift ;;
        --no-pull) PULL_REPOSITORY=false; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "Option inconnue : $1" ;;
    esac
done

[[ $EUID -eq 0 ]] || die "Exécutez ce script avec sudo/root."
for command in python3 rsync systemctl; do
    command -v "$command" >/dev/null || die "Commande requise absente : $command"
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SOURCE_AGENT="$REPO_ROOT/node_agent"
SOURCE_GATEWAY="$REPO_ROOT/gateway"

# shellcheck source=agent-venv-lib.sh
source "$SCRIPT_DIR/agent-venv-lib.sh"

[[ -f "$SOURCE_AGENT/main.py" && -f "$SOURCE_GATEWAY/server_manager.py" ]] || \
    die "Checkout EVARuntime incomplet : $REPO_ROOT"
[[ -x "$VENV_DIR/bin/python" ]] || die "Installation absente : $VENV_DIR"
[[ -f "$ENV_FILE" ]] || die "Configuration absente : $ENV_FILE"
[[ -f "$SERVICE_FILE" ]] || die "Unité systemd absente : $SERVICE_FILE"

validate_source() {
    bash -n "$SOURCE_AGENT/deploy/install-agent.sh"
    bash -n "$SOURCE_AGENT/deploy/update-agent.sh"
    bash -n "$SOURCE_AGENT/deploy/agent-venv-lib.sh"
    python3 -c '
import ast, pathlib, sys
for root in map(pathlib.Path, sys.argv[1:]):
    for path in root.rglob("*.py"):
        if ".venv" not in path.parts:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
' "$SOURCE_AGENT" "$SOURCE_GATEWAY"
}

info "Préflight du checkout $REPO_ROOT"
validate_source
"$VENV_DIR/bin/python" "$SOURCE_AGENT/preflight.py" --env "$ENV_FILE"

if [[ "$DRY_RUN" == true ]]; then
    info "DRY-RUN : aucune modification effectuée."
    if [[ "$PULL_REPOSITORY" == true ]]; then
        info "Action prévue : git pull --ff-only dans $REPO_ROOT"
    fi
    info "Actions prévues : venv neuf, sync node_agent+gateway, unité systemd, restart+health."
    info "Venv neuf : $INSTALL_DIR/venv-agent-release-* (construit sur place), $VENV_DIR rebasculé."
    info "Rollback prévu : $ROLLBACK_DIR (code + unité) et rebascule du symlink $VENV_DIR."
    if [[ -d "$VENV_DIR" && ! -L "$VENV_DIR" ]]; then
        info "Agent installé avant COR-016 : le venv réel sera migré en place lors de cette mise à jour."
    fi
    exit 0
fi

if [[ "$PULL_REPOSITORY" == true ]]; then
    command -v git >/dev/null || die "git est requis sans --no-pull."
    git -C "$REPO_ROOT" diff --quiet --ignore-submodules -- || \
        die "Checkout modifié localement; committez/stashez ou utilisez --no-pull."
    git -C "$REPO_ROOT" diff --cached --quiet --ignore-submodules -- || \
        die "Checkout contient des changements indexés; committez/stashez ou utilisez --no-pull."
    BEFORE="$(git -C "$REPO_ROOT" rev-parse --short HEAD)"
    git -C "$REPO_ROOT" pull --ff-only
    AFTER="$(git -C "$REPO_ROOT" rev-parse --short HEAD)"
    info "Checkout mis à jour : $BEFORE -> $AFTER"
    # Le pull peut avoir changé les scripts : revalider ce qui va être déployé.
    validate_source
fi

# Le staging ne contient QUE du code (relogeable). Le venv, lui, n'y passe
# jamais : il est construit directement à son emplacement définitif (COR-016).
WORK_DIR="$(mktemp -d "$INSTALL_DIR/.agent-update.XXXXXX")"
cleanup() {
    [[ -n "$WORK_DIR" && -d "$WORK_DIR" ]] && rm -rf "$WORK_DIR"
}

# Purge une release de venv qui n'est plus référencée par aucun symlink.
discard_staged_venv() {
    [[ -n "$STAGED_VENV" && -d "$STAGED_VENV" ]] || return 0
    [[ "$STAGED_VENV" == "$INSTALL_DIR/venv-agent-release-"* ]] || return 0
    [[ "$(readlink -f "$VENV_DIR" 2>/dev/null || true)" != "$STAGED_VENV" ]] || return 0
    rm -rf "$STAGED_VENV"
}

rollback() {
    local original_status="$1" restart_status=0
    warn "Échec de mise à jour; restauration de la version précédente."
    set +e
    systemctl stop "$SERVICE"
    if [[ -d "$ROLLBACK_DIR/node_agent" ]]; then
        rsync -a --delete "$ROLLBACK_DIR/node_agent/" "$INSTALL_DIR/node_agent/"
    fi
    if [[ -d "$ROLLBACK_DIR/gateway" ]]; then
        rsync -a --delete "$ROLLBACK_DIR/gateway/" "$INSTALL_DIR/gateway/"
    fi
    # Rebascule du symlink : la release précédente est restée en place, intacte.
    agent_venv_rollback "$VENV_DIR"
    if [[ -f "$ROLLBACK_DIR/llm-gateway-agent.service" ]]; then
        cp "$ROLLBACK_DIR/llm-gateway-agent.service" "$SERVICE_FILE"
    fi
    systemctl daemon-reload
    if [[ "$WAS_ACTIVE" == true ]]; then
        # COR-017 : les tentatives de démarrage qui viennent d'échouer ont pu
        # atteindre le start-limit systemd. Sans reset-failed, le start du
        # rollback échoue sur « Start request repeated too quickly » et laisse
        # le nœud à terre. Sur une unité saine, c'est un no-op.
        systemctl reset-failed "$SERVICE" 2>/dev/null
        systemctl start "$SERVICE"
        restart_status=$?
    fi
    set -e
    if (( restart_status != 0 )); then
        # Jamais un simple avertissement : le nœud ne sert plus (COR-017).
        warn "INDISPONIBILITÉ : la version précédente a été restaurée mais $SERVICE"
        warn "  ne redémarre pas. Le nœud ne sert plus aucune requête."
        warn "  systemctl reset-failed $SERVICE && systemctl start $SERVICE"
        warn "  journalctl -u $SERVICE -n 100 --no-pager"
        return "$EXIT_SERVICE_DOWN"
    fi
    warn "Rollback terminé. Inspectez : journalctl -u $SERVICE -n 100"
    return "$original_status"
}

on_error() {
    local status=$? rollback_status=0
    trap - ERR
    if [[ "$ROLLBACK_READY" == true ]]; then
        rollback "$status" || rollback_status=$?
        if (( rollback_status == EXIT_SERVICE_DOWN )); then
            status="$EXIT_SERVICE_DOWN"
        fi
    fi
    discard_staged_venv
    cleanup
    exit "$status"
}
trap on_error ERR
trap cleanup EXIT

info "Construction du staging isolé : $WORK_DIR"
rsync -a --delete --delete-excluded \
    --exclude '.env' --exclude '.venv' --exclude '__pycache__' \
    --exclude '.pytest_cache' --exclude '.ruff_cache' --exclude '.DS_Store' \
    "$SOURCE_AGENT/" "$WORK_DIR/node_agent/"
rsync -a --delete --delete-excluded \
    --exclude '.env' --exclude '.venv' --exclude '__pycache__' \
    --exclude '.pytest_cache' --exclude '.ruff_cache' --exclude '.DS_Store' \
    "$SOURCE_GATEWAY/" "$WORK_DIR/gateway/"
# Venv neuf construit à son EMPLACEMENT DÉFINITIF (COR-016) : les shebangs de
# bin/ y pointeront pour de bon. L'ancien reste en service jusqu'à la bascule.
STAGED_VENV="$(agent_venv_new_release_path "$INSTALL_DIR")"
info "Construction du venv neuf sur place : $STAGED_VENV"
python3 -m venv "$STAGED_VENV"
chmod 0755 "$STAGED_VENV"
"$STAGED_VENV/bin/python" -m pip install --quiet --upgrade pip
"$STAGED_VENV/bin/python" -m pip install --quiet \
    -r "$WORK_DIR/node_agent/requirements.txt"
"$STAGED_VENV/bin/python" -m pip check
"$STAGED_VENV/bin/python" "$WORK_DIR/node_agent/preflight.py" --env "$ENV_FILE"

if systemctl is-active --quiet "$SERVICE"; then
    WAS_ACTIVE=true
fi

# Une seule sauvegarde complète et déterministe : la dernière version connue
# bonne. Le venv n'y figure PAS : la release précédente reste où elle a été
# construite et le rollback se contente de rebasculer le symlink (COR-016).
rm -rf "$ROLLBACK_DIR"
install -d -m 0700 -o root -g root "$ROLLBACK_DIR"
rsync -a "$INSTALL_DIR/node_agent/" "$ROLLBACK_DIR/node_agent/"
rsync -a "$INSTALL_DIR/gateway/" "$ROLLBACK_DIR/gateway/"
cp "$SERVICE_FILE" "$ROLLBACK_DIR/llm-gateway-agent.service"

ROLLBACK_READY=true
if [[ "$WAS_ACTIVE" == true ]]; then
    systemctl stop "$SERVICE"
fi
agent_venv_activate "$VENV_DIR" "$STAGED_VENV"

rsync -a --delete "$WORK_DIR/node_agent/" "$INSTALL_DIR/node_agent/"
rsync -a --delete "$WORK_DIR/gateway/" "$INSTALL_DIR/gateway/"
chown -h root:root "$VENV_DIR"
chown -R root:root "$STAGED_VENV" "$INSTALL_DIR/node_agent" "$INSTALL_DIR/gateway"
find "$INSTALL_DIR/node_agent" "$INSTALL_DIR/gateway" -type d -exec chmod 0755 {} +
find "$INSTALL_DIR/node_agent" "$INSTALL_DIR/gateway" -type f -exec chmod 0644 {} +
chmod 0755 "$INSTALL_DIR/node_agent/deploy/"*.sh
install -m 0644 -o root -g root \
    "$INSTALL_DIR/node_agent/deploy/llm-gateway-agent.service" "$SERVICE_FILE"

"$VENV_DIR/bin/python" "$INSTALL_DIR/node_agent/preflight.py" --env "$ENV_FILE"
systemctl daemon-reload

if [[ "$WAS_ACTIVE" == true ]]; then
    # COR-017 : purger un éventuel état `failed` hérité d'un incident précédent.
    # Sans cela le start-limit systemd peut refuser ce démarrage alors que la
    # version déployée est saine. No-op sur une unité en bon état.
    systemctl reset-failed "$SERVICE" 2>/dev/null || true
    systemctl start "$SERVICE"
    HEALTHY=false
    for attempt in 1 2 3 4 5; do
        if "$VENV_DIR/bin/python" "$INSTALL_DIR/node_agent/preflight.py" \
            --env "$ENV_FILE" --check-health --timeout 5; then
            HEALTHY=true
            break
        fi
        warn "Health-check $attempt/5 échoué; nouvelle tentative..."
        sleep 2
    done
    [[ "$HEALTHY" == true ]] || false
else
    info "Service précédemment inactif : état préservé, pas de démarrage automatique."
fi

ROLLBACK_READY=false
info "Mise à jour node-agent réussie. Rollback conservé dans $ROLLBACK_DIR."
info "Venv en service : $VENV_DIR -> $STAGED_VENV"
if [[ -n "$AGENT_VENV_PREVIOUS_TARGET" ]]; then
    info "Venv précédent conservé (rollback manuel possible) : $AGENT_VENV_PREVIOUS_TARGET"
fi
info "Secrets, TLS, modèles et configuration n'ont pas été modifiés."
