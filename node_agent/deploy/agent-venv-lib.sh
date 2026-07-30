#!/usr/bin/env bash
# agent-venv-lib.sh — stratégie de venv du node-agent (COR-016).
#
# Sourcé par update-agent.sh. Testé pour de vrai par
# node_agent/tests/test_update_agent_venv.py, qui construit un vrai venv et
# vérifie qu'un exécutable de bin/ est encore lançable après la bascule.
#
# ── Pourquoi un venv ne se déplace pas ───────────────────────────────────────
# Les scripts console d'un venv (`bin/uvicorn`, `bin/pip`…) portent un shebang
# ABSOLU vers `<venv>/bin/python`. Déplacer le venv après coup laisse ce shebang
# pointer sur l'ancien chemin : l'exécutable devient `bad interpreter`, soit
# `203/EXEC Permission denied` côté systemd. `bin/python` est en revanche un
# lien vers l'interpréteur système et survit au déplacement — c'est ce qui a
# masqué le défaut lors du premier déploiement réel (ExecStartPre passait,
# ExecStart échouait).
#
# La stratégie est donc celle de gateway/deploy/update.sh :
#   1. le venv neuf est construit DIRECTEMENT à son emplacement définitif
#      (`<install_dir>/venv-agent-release-<horodatage>-<pid>`) ;
#   2. `<install_dir>/venv-agent` — le chemin figé dans l'unité systemd — est un
#      SYMLINK que l'on fait basculer d'une release à l'autre ;
#   3. le rollback rebascule ce symlink. Aucun venv n'est jamais déplacé.

# État de la bascule, partagé avec le script appelant (même convention que
# update.sh : VENV_SWITCHED / PREVIOUS_VENV_TARGET).
AGENT_VENV_SWITCHED=false
AGENT_VENV_PREVIOUS_TARGET=""

# agent_venv_new_release_path <install_dir>
# Chemin DÉFINITIF du venv neuf : il y est construit et n'en bougera jamais.
agent_venv_new_release_path() {
    local install_dir="$1"
    printf '%s/venv-agent-release-%s-%s\n' "$install_dir" "$(date +%Y%m%d-%H%M%S)" "$$"
}

# agent_venv_activate <venv_link> <staged_venv>
# Bascule <venv_link> (le chemin attendu par l'unité systemd) vers la release
# neuve, en mémorisant de quoi revenir en arrière.
agent_venv_activate() {
    local link="$1" staged="$2"

    if [[ -L "$link" ]]; then
        # Cas courant : agent déjà passé au schéma symlink.
        AGENT_VENV_PREVIOUS_TARGET="$(readlink -f "$link")"
        rm -f "$link"
    elif [[ -d "$link" ]]; then
        # Migration en place d'un agent installé par l'ancien install-agent.sh :
        # le venv réel est écarté une seule fois. Ses shebangs pointent sur
        # « …/venv-agent/bin/python » ; ils redeviennent valides dès que le
        # rollback réexpose ce répertoire SOUS SON CHEMIN D'ORIGINE via le
        # symlink. Aucune mise à jour ultérieure ne repassera par ce cas.
        AGENT_VENV_PREVIOUS_TARGET="${link}-pre-update-$(date +%Y%m%d-%H%M%S)"
        mv "$link" "$AGENT_VENV_PREVIOUS_TARGET"
    else
        AGENT_VENV_PREVIOUS_TARGET=""
    fi

    # Armé dès que l'ancien chemin a été retiré : si le ln -s échoue, le rollback
    # doit quand même pouvoir remettre l'agent en service.
    AGENT_VENV_SWITCHED=true
    ln -s "$staged" "$link"
}

# agent_venv_rollback <venv_link>
# Remet la release précédente en service. Aucun venv n'est déplacé ici non plus.
agent_venv_rollback() {
    local link="$1"
    [[ "$AGENT_VENV_SWITCHED" == true ]] || return 0
    [[ -n "$AGENT_VENV_PREVIOUS_TARGET" ]] || return 0
    rm -f "$link"
    ln -s "$AGENT_VENV_PREVIOUS_TARGET" "$link"
    AGENT_VENV_SWITCHED=false
}
