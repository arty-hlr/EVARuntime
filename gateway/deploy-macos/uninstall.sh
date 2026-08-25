#!/usr/bin/env bash
# uninstall.sh — Désinstallation propre d'EVARuntime sur macOS
#
# Usage :
#   bash uninstall.sh [--keep-data] [--keep-config] [--force]
#
# Options :
#   --keep-data     Conserve les modèles GGUF et la base de données
#   --keep-config   Conserve le fichier de configuration (secrets inclus)
#   --force         Supprime tout sans confirmation interactive
#
# IMPORTANT : Le mode cluster n'est PAS supporté sur macOS. Seul le mode local
#             (mono-nœud) est fonctionnel.

set -euo pipefail
IFS=$'\n\t'

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()    { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

KEEP_DATA=false
KEEP_CONFIG=false
FORCE=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --keep-data) KEEP_DATA=true; shift ;;
        --keep-config) KEEP_CONFIG=true; shift ;;
        --force) FORCE=true; shift ;;
        *) echo "Option inconnue : $1" >&2; exit 2 ;;
    esac
done

# ── Configuration macOS ───────────────────────────────────────────────────────

INSTALL_DIR="${EVARUNE_INSTALL_DIR:-$HOME/Library/Application Support/evaruntime/gateway}"
DATA_DIR="${EVARUNE_DATA_DIR:-$HOME/Library/Application Support/evaruntime/data}"
LOG_DIR="${EVARUNE_LOG_DIR:-$HOME/Library/Application Support/evaruntime/logs}"
CONFIG_FILE="$HOME/.config/evaruntime/env"
MODELS_DIR="${EVARUNE_MODELS_DIR:-$HOME/Library/Application Support/evaruntime/models}"
PLIST_PATH="/Library/LaunchDaemons/com.evaruntime.gateway.plist"

# ── Arrêt du service ──────────────────────────────────────────────────────────

info "Arrêt du service…"
if sudo launchctl list com.evaruntime.gateway &>/dev/null; then
    if sudo launchctl bootout system/com.evaruntime.gateway 2>&1 | grep -q failed && sudo launchctl unload "$PLIST_PATH" 2>&1 | grep -q "failed"; then
        error "Échec de l'arrêt du service launchd. Vérifiez les permissions et les logs."
    else
        info "Service arrêté."
    fi
else
    info "Service déjà arrêté (ou jamais installé)."
fi

# ── Suppression du plist launchd ──────────────────────────────────────────────

if [[ -f "$PLIST_PATH" ]]; then
    rm -f "$PLIST_PATH"
    info "Plist launchd supprimé."
else
    info "Plist launchd introuvable (déjà supprimé)."
fi

# ── Suppression des fichiers ──────────────────────────────────────────────────

echo ""
info "Répertoire d'installation : $INSTALL_DIR"
info "Données : $DATA_DIR"
info "Logs : $LOG_DIR"
info "Configuration : $CONFIG_FILE"
info "Modèles : $MODELS_DIR"
echo ""

if [[ "$FORCE" != true ]]; then
    read -r -p "Continuer la désinstallation ? (o/N) " answer
    case "$answer" in
        [oO][kK]|[oO]) ;;
        *) echo "Annulation."; exit 0 ;;
    esac
fi

# Supprimer le venv et le code
if [[ -d "$INSTALL_DIR" ]]; then
    rm -rf "$INSTALL_DIR"
    info "Répertoire d'installation supprimé."
fi

# Supprimer les logs
if [[ -d "$LOG_DIR" && "$KEEP_DATA" != true ]]; then
    rm -rf "$LOG_DIR"
    info "Logs supprimés."
fi

# Supprimer la base de données (sauf --keep-data)
if [[ -d "$DATA_DIR" && "$KEEP_DATA" != true ]]; then
    rm -rf "$DATA_DIR"
    info "Base de données supprimée."
fi

# Supprimer les modèles (sauf --keep-data)
if [[ -d "$MODELS_DIR" && "$KEEP_DATA" != true ]]; then
    rm -rf "$MODELS_DIR"
    info "Modèles GGUF supprimés."
fi

# Supprimer la configuration (sauf --keep-config)
if [[ -f "$CONFIG_FILE" && "$KEEP_CONFIG" != true ]]; then
    rm -f "$CONFIG_FILE"
    rmdir "$(dirname "$CONFIG_FILE")" 2>/dev/null || true
    info "Configuration supprimée."
fi

# Supprimer le répertoire racine s'il est vide
ROOT_DIR="${EVARUNE_DATA_DIR:-$HOME/Library/Application Support/evaruntime}"
if [[ -d "$ROOT_DIR" ]]; then
    remaining=$(ls -A "$ROOT_DIR" 2>/dev/null)
    if [[ -z "$remaining" ]]; then
        rm -rf "$ROOT_DIR"
        info "Répertoire racine evaruntime supprimé."
    fi
fi

# ── Résumé ────────────────────────────────────────────────────────────────────

echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Désinstallation terminée !${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo ""

if [[ "$KEEP_DATA" == true ]]; then
    warn "Les données ont été conservées dans $DATA_DIR"
fi
if [[ "$KEEP_CONFIG" == true ]]; then
    warn "La configuration a été conservée dans $CONFIG_FILE"
fi
echo ""
