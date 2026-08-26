#!/usr/bin/env bash
# doctor-macos.sh — Diagnostic préflight pour EVARuntime sur macOS
#
# Usage :
#   bash doctor-macos.sh [--json]
#
# Vérifie l'état complet de l'installation et signale les problèmes potentiels.
#
# IMPORTANT : Le mode cluster n'est PAS supporté sur macOS. Seul le mode local
#             (mono-nœud) est fonctionnel.

set -euo pipefail
IFS=$'\n\t'

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PASS() { echo -e "  ${GREEN}✓${NC} $*"; }
FAIL() { echo -e "  ${RED}✗${NC} $*"; errors=$((errors + 1)); }
WARN() { echo -e "  ${YELLOW}⚠${NC} $*"; warnings=$((warnings + 1)); }

JSON_OUTPUT=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --json) JSON_OUTPUT=true; shift ;;
        *) echo "Option inconnue : $1" >&2; exit 2 ;;
    esac
done

errors=0
warnings=0

# ── Configuration macOS ───────────────────────────────────────────────────────

INSTALL_DIR="${EVARUNE_INSTALL_DIR:-$HOME/Library/Application Support/evaruntime/gateway}"
DATA_DIR="${EVARUNE_DATA_DIR:-$HOME/Library/Application Support/evaruntime/data}"
LOG_DIR="${EVARUNE_LOG_DIR:-$HOME/Library/Application Support/evaruntime/logs}"
CONFIG_FILE="$HOME/.config/evaruntime/env"
MODELS_DIR="${EVARUNE_MODELS_DIR:-$HOME/Library/Application Support/evaruntime/models}"
PLIST_PATH="/Library/LaunchDaemons/com.evaruntime.gateway.plist"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck source=deploy-mode-lib.sh
source "$SCRIPT_DIR/deploy-macos/deploy-mode-lib.sh"

# ── Prérequis système ────────────────────────────────────────────────────────

echo "EVARuntime Doctor — macOS Diagnostic"
echo "====================================="
echo ""

echo "1. Système"
echo "---------"

if [[ "$(uname)" == "Darwin" ]]; then
    PASS "macOS détecté ($(sw_vers -productVersion))"
else
    FAIL "Ce script est conçu pour macOS uniquement."
fi

# ── Homebrew ──────────────────────────────────────────────────────────────────

echo ""
echo "2. Prérequis"
echo "-----------"

if command -v brew &>/dev/null; then
    PASS "Homebrew installé ($(brew --version | head -n1))"
    
    # Vérifier l'architecture Homebrew
    BREW_ARCH=$(uname -m)
    if [[ "$BREW_ARCH" == "arm64" ]]; then
        PASS "Homebrew ARM64 (natif Apple Silicon)"
    else
        WARN "Homebrew x86_64 (Rosetta) — les performances d'inférence seront limitées"
    fi
else
    FAIL "Homebrew requis mais introuvable. Installez-le depuis https://brew.sh"
fi

# ── Python ────────────────────────────────────────────────────────────────────

PYTHON="${PYTHON:-python3}"
if command -v "$PYTHON" &>/dev/null; then
    PYTHON_VERSION=$("$PYTHON" --version 2>&1 | cut -d' ' -f2)
    PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d'.' -f1)
    PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d'.' -f2)
    
    if [[ "$PYTHON_MAJOR" -ge 3 ]] && [[ "$PYTHON_MINOR" -ge 11 ]]; then
        PASS "Python $PYTHON_VERSION OK (≥ 3.11 requis)"
    else
        FAIL "Python 3.11+ requis, version trouvée : $PYTHON_VERSION"
    fi
else
    FAIL "Python introuvable. Installez-le via Homebrew : brew install python@3.12"
fi

# ── llama.cpp / llama-server ─────────────────────────────────────────────────

LLAMA_BIN="$(deploy_env_value "$CONFIG_FILE" LLAMA_SERVER_BIN 2>/dev/null || echo "")"
LLAMA_BIN="${LLAMA_BIN:-$(brew --prefix 2>/dev/null)/bin/llama-server}"

if [[ -x "$LLAMA_BIN" ]]; then
    PASS "llama-server trouvé : $LLAMA_BIN"
    
    # Vérifier la version
    LLAMA_VERSION=$("$LLAMA_BIN" --version 2>&1 | head -n1 || echo "inconnue")
    PASS "Version : $LLAMA_VERSION"
else
    WARN "llama-server introuvable à $LLAMA_BIN"
    WARN "→ Installez avec : brew install llama.cpp"
fi

# ── GPU Apple Silicon (Metal) ────────────────────────────────────────────────

echo ""
echo "3. GPU / Accélération"
echo "--------------------"

if [[ "$(uname -m)" == "arm64" ]]; then
    PASS "Apple Silicon détecté — Metal disponible pour l'accélération GPU"
    
    # Estimer la RAM unifiée
    TOTAL_RAM_BYTES=$(sysctl -n hw.memsize 2>/dev/null || echo "0")
    TOTAL_RAM_GB=$((TOTAL_RAM_BYTES / 1073741824))
    PASS "RAM unifiée : ${TOTAL_RAM_GB} GB"
    
    # Vérifier le nombre de coeurs GPU (informationnel)
    GPU_CORES=$(system_profiler SPDisplaysDataType 2>/dev/null | grep -i "GPU cores" | awk '{print $NF}' || echo "?")
    if [[ "$GPU_CORES" != "?" ]]; then
        PASS "GPU : ${GPU_CORES} coeurs"
    fi
else
    WARN "Architecture non ARM64 — l'accélération Metal pourrait être limitée"
fi

# ── Service launchd ───────────────────────────────────────────────────────────

echo ""
echo "4. Service"
echo "---------"

if [[ -f "$PLIST_PATH" ]]; then
    PASS "Plist launchd présent : $PLIST_PATH"
    
    if sudo launchctl list com.evaruntime.gateway &>/dev/null; then
        PASS "Service chargé dans launchd"
        
        # Vérifier si le service est en cours d'exécution
        SERVICE_PID=$(sudo launchctl list com.evaruntime.gateway | grep PID | grep -oE '\d+' || echo "")
        if [[ -n "$SERVICE_PID" && "$SERVICE_PID" != "0" ]]; then
            PASS "Service en cours d'exécution (PID: $SERVICE_PID)"
            
            # Tester la réponse HTTP
            if curl -s --max-time 5 http://127.0.0.1:8000/health &>/dev/null; then
                PASS "Gateway répond sur /health"
                
                if curl -s --max-time 5 http://127.0.0.1:8000/ready &>/dev/null; then
                    PASS "Gateway prête (ready state OK)"
                else
                    WARN "Gateway non prête — consultez les logs pour plus de détails"
                fi
            else
                WARN "Gateway ne répond pas sur http://127.0.0.1:8000/health"
            fi
        else
            WARN "Service chargé mais pas en cours d'exécution"
            WARN "→ Démarrer avec : launchctl load $PLIST_PATH"
        fi
    else
        WARN "Plist présent mais service non chargé"
        WARN "→ Charger avec : launchctl load $PLIST_PATH"
    fi
else
    WARN "Plist launchd introuvable — le service n'est pas installé"
    WARN "→ Installer avec : bash gateway/deploy-macos/install.sh"
fi

# ── Installation ──────────────────────────────────────────────────────────────

echo ""
echo "5. Installation"
echo "--------------"

if [[ -d "$INSTALL_DIR" ]]; then
    PASS "Répertoire d'installation présent : $INSTALL_DIR"
    
    if [[ -f "$INSTALL_DIR/requirements.txt" && -f "$INSTALL_DIR/requirements.lock" ]]; then
        PASS "Fichiers de dépendances présents"
    else
        FAIL "requirements.txt ou requirements.lock introuvable dans l'installation"
    fi
    
    if [[ -d "$INSTALL_DIR/venv" ]]; then
        VENV_PYTHON="$INSTALL_DIR/venv/bin/python"
        if "$VENV_PYTHON" -c "import fastapi" 2>/dev/null; then
            PASS "Environnement virtuel fonctionnel (FastAPI importable)"
        else
            FAIL "Environnement virtuel présent mais FastAPI introuvable"
            WARN "→ Réinstaller : bash gateway/deploy-macos/install.sh"
        fi
    else
        FAIL "Environnement virtuel introuvable dans $INSTALL_DIR/venv"
    fi
else
    FAIL "Répertoire d'installation introuvable : $INSTALL_DIR"
fi

# ── Configuration ─────────────────────────────────────────────────────────────

echo ""
echo "6. Configuration"
echo "---------------"

if [[ -f "$CONFIG_FILE" ]]; then
    PASS "Fichier de configuration présent : $CONFIG_FILE"
    
    # Vérifier les clés essentielles
    for key in MODELS_CONFIG_PATH LLAMA_SERVER_BIN DB_PATH; do
        if grep -q "^${key}=" "$CONFIG_FILE" 2>/dev/null; then
            PASS "Clé $key présente dans la configuration"
        else
            FAIL "Clé $key absente de la configuration"
        fi
    done
    
    # Vérifier les secrets
    ADMIN_SECRET=$(grep "^ADMIN_SECRET=" "$CONFIG_FILE" 2>/dev/null | cut -d'=' -f2-)
    if [[ -n "$ADMIN_SECRET" && ! "$ADMIN_SECRET" == "CHANGE_ME"* ]]; then
        PASS "ADMIN_SECRET configuré (non par défaut)"
    else
        FAIL "ADMIN_SECRET non configuré ou à la valeur par défaut"
    fi
    
    INTERNAL_KEY=$(grep "^INTERNAL_API_KEY=" "$CONFIG_FILE" 2>/dev/null | cut -d'=' -f2-)
    if [[ -n "$INTERNAL_KEY" && ! "$INTERNAL_KEY" == "CHANGE_ME"* ]]; then
        PASS "INTERNAL_API_KEY configuré (non par défaut)"
    else
        FAIL "INTERNAL_API_KEY non configuré ou à la valeur par défaut"
    fi
    
    # Mode actuel
    CLUSTER_MODE=$(grep "^CLUSTER_MODE=" "$CONFIG_FILE" 2>/dev/null | cut -d'=' -f2-)
    if [[ -n "$CLUSTER_MODE" ]]; then
        PASS "Mode : $CLUSTER_MODE"
    fi
else
    FAIL "Fichier de configuration introuvable : $CONFIG_FILE"
fi

# ── Registre des modèles ──────────────────────────────────────────────────────

echo ""
echo "7. Modèles"
echo "---------"

MODELS_CONFIG_PATH=$(grep "^MODELS_CONFIG_PATH=" "$CONFIG_FILE" 2>/dev/null | cut -d'=' -f2- || echo "")
if [[ -n "$MODELS_CONFIG_PATH" && -f "$MODELS_CONFIG_PATH" ]]; then
    PASS "Registre des modèles présent : $MODELS_CONFIG_PATH"
    
    # Compter les modèles activés
    ENABLED_COUNT=$(grep -Ec "^\s*enabled: true" "$MODELS_CONFIG_PATH" 2>/dev/null || echo "0")
    TOTAL_COUNT=$(grep -Ec "^\s*- id:" "$MODELS_CONFIG_PATH" 2>/dev/null || echo "0")
    PASS "Modèles dans le registre : $TOTAL_COUNT total, $ENABLED_COUNT activés"
    
    # Vérifier les chemins GGUF
    if [[ -d "$MODELS_DIR" ]]; then
        GGUF_COUNT=$(find "$MODELS_DIR" -name "*.gguf" 2>/dev/null | wc -l)
        if [[ "$GGUF_COUNT" -gt 0 ]]; then
            PASS "Fichiers GGUF trouvés : $GGUF_COUNT dans $MODELS_DIR"
        else
            WARN "Aucun fichier .gguf trouvé dans $MODELS_DIR"
        fi
    fi
elif [[ -n "$MODELS_CONFIG_PATH" ]]; then
    FAIL "Registre des modèles spécifié mais introuvable : $MODELS_CONFIG_PATH"
else
    WARN "MODELS_CONFIG_PATH non défini dans la configuration"
fi

# ── Logs ──────────────────────────────────────────────────────────────────────

echo ""
echo "8. Logs"
echo "------"

if [[ -d "$LOG_DIR" ]]; then
    PASS "Répertoire logs présent : $LOG_DIR"
    
    if [[ -f "$LOG_DIR/gateway.log" ]]; then
        LOG_SIZE=$(du -h "$LOG_DIR/gateway.log" 2>/dev/null | cut -f1)
        LAST_LINE=$(tail -n5 "$LOG_DIR/gateway.log" 2>/dev/null || echo "(vide)")
        PASS "gateway.log présent (${LOG_SIZE})"
        
        # Vérifier les dernières erreurs
        ERROR_COUNT=$(grep -c "\[ERROR\]" "$LOG_DIR/gateway-error.log" 2>/dev/null || echo "0")
        if [[ "$ERROR_COUNT" -gt 0 ]]; then
            WARN "$ERROR_COUNT erreur(s) trouvée(s) dans les logs récents"
        fi
    else
        WARN "gateway.log introuvable (service peut-être jamais démarré)"
    fi
    
    if [[ -f "$LOG_DIR/gateway-error.log" ]]; then
        ERROR_LOG_SIZE=$(du -h "$LOG_DIR/gateway-error.log" 2>/dev/null | cut -f1)
        PASS "gateway-error.log présent (${ERROR_LOG_SIZE})"
    else
        WARN "gateway-error.log introuvable (service peut-être jamais démarré)"
    fi
else
    FAIL "Répertoire logs introuvable : $LOG_DIR"
fi

# ── Nginx (optionnel) ────────────────────────────────────────────────────────

echo ""
echo "9. Nginx (optionnel)"
echo "-------------------"

if command -v nginx &>/dev/null; then
    NGINX_VERSION=$(nginx -v 2>&1 | cut -d'/' -f2)
    PASS "nginx installé (version $NGINX_VERSION)"
    
    # Vérifier la configuration EVARuntime
    NGINX_CONF_DIR="$(brew --prefix 2>/dev/null)/etc/nginx/servers"
    if [[ -f "$NGINX_CONF_DIR/llm-gateway" ]]; then
        PASS "Configuration llm-gateway trouvée dans nginx"
        
        if nginx -t 2>/dev/null; then
            PASS "Configuration nginx valide"
        else
            FAIL "Erreur de syntaxe dans la configuration nginx"
        fi
    else
        WARN "Configuration llm-gateway introuvable dans $NGINX_CONF_DIR/"
        WARN "→ Copier gateway/deploy-macos/nginx.conf.macOS et recharger nginx"
    fi
else
    PASS "nginx non installé — optionnel, la gateway fonctionne sans"
fi

# ── Résumé ────────────────────────────────────────────────────────────────────

echo ""
echo "====================================="
if [[ "$errors" -eq 0 && "$warnings" -eq 0 ]]; then
    echo -e "${GREEN}✓ Tout est en ordre !${NC}"
elif [[ "$errors" -eq 0 ]]; then
    echo -e "${YELLOW}⚠ $warnings avertissement(s) détecté(s), aucun blocage critique.${NC}"
else
    echo -e "${RED}✗ $errors erreur(s) critique(s) détectée(s).${NC}"
fi

if [[ "$warnings" -gt 0 ]]; then
    echo "  $warnings avertissement(s)"
fi

echo ""
echo "Pour plus de détails, consultez :"
echo "  - Logs : tail -f $LOG_DIR/gateway.log"
echo "  - Documentation : docs/deployment.md"
echo ""
