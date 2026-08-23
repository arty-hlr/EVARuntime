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

# ── Rétention des releases (OPS-010) ─────────────────────────────────────────
# Corollaire de la stratégie ci-dessus : plus aucun venv n'est écrasé, donc
# chaque mise à jour LAISSE une release complète (~200 Mo) sur le disque du
# nœud, plus un éventuel `venv-agent-pre-update-*` issu de la migration. Sans
# purge, un nœud mis à jour régulièrement finit par saturer /opt sans le moindre
# message. La rétention est donc explicite et BORNÉE : on conserve la release
# ACTIVE et les `keep - 1` plus récentes des autres — par défaut la précédente,
# ce qui laisse le retour arrière manuel documenté dans docs/deployment.md
# (« Stratégie de venv du node-agent ») réalisable. Le reste est purgé.
#
# La cible courante du symlink n'est JAMAIS supprimée, quel que soit son âge :
# c'est le venv sur lequel l'agent tourne.
#
# L'ordre est celui des dates de modification, jamais celui des noms : le nom
# n'est trié chronologiquement ni côté gateway (il commence par un hash de
# commit) ni entre les deux préfixes. Il ne sert qu'à départager deux dates
# égales, pour que la décision reste déterministe.

AGENT_VENV_KEEP_DEFAULT=2

# agent_venv_mtime <chemin>
# Date de modification en secondes. `stat` n'a pas la même syntaxe selon les
# systèmes : GNU d'abord (les nœuds), BSD ensuite (poste de développement).
agent_venv_mtime() {
    stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || printf '0\n'
}

# agent_venv_release_dirs <install_dir>
# Toutes les releases présentes, de la plus récente à la plus ancienne. Un lien
# symbolique n'en est pas une : `venv-agent` lui-même ne peut donc jamais
# ressortir d'ici, quelle que soit sa cible.
agent_venv_release_dirs() {
    local install_dir="$1" path
    for path in "$install_dir"/venv-agent-release-* "$install_dir"/venv-agent-pre-update-*; do
        # Un glob sans correspondance se rend lui-même : le test -d l'écarte.
        [[ -d "$path" && ! -L "$path" ]] || continue
        printf '%s\t%s\n' "$(agent_venv_mtime "$path")" "$path"
    done | LC_ALL=C sort -t"$(printf '\t')" -k1,1nr -k2,2r | cut -f2-
}

# agent_venv_prunable_releases <install_dir> <venv_link> [keep]
# Les releases que la rétention désigne, SANS rien supprimer. Sépare la décision
# de l'effacement : c'est ce que `--dry-run` annonce, et ce que les tests
# vérifient sans dépendre d'un `rm`.
agent_venv_prunable_releases() {
    local install_dir="$1" link="$2" keep="${3:-$AGENT_VENV_KEEP_DEFAULT}"
    local current="" kept=0 path

    [[ "$keep" =~ ^[1-9][0-9]*$ ]] || keep="$AGENT_VENV_KEEP_DEFAULT"

    current="$(readlink -f "$link" 2>/dev/null || true)"
    # La release active occupe une place du quota : keep=2 = active + précédente.
    [[ -z "$current" || ! -d "$current" ]] || kept=1

    while IFS= read -r path; do
        [[ -n "$path" ]] || continue
        [[ "$path" != "$current" ]] || continue
        if (( kept < keep )); then
            kept=$(( kept + 1 ))
            continue
        fi
        printf '%s\n' "$path"
    done < <(agent_venv_release_dirs "$install_dir")
}

# agent_venv_prune_releases <install_dir> <venv_link> [keep]
# Applique la rétention et rend sur stdout les répertoires réellement supprimés,
# un par ligne. Rend 1 si au moins une suppression a échoué : l'appelant en fait
# un avertissement, jamais un échec de mise à jour.
agent_venv_prune_releases() {
    local install_dir="$1" link="$2" keep="${3:-$AGENT_VENV_KEEP_DEFAULT}"
    local current="" path status=0

    current="$(readlink -f "$link" 2>/dev/null || true)"

    while IFS= read -r path; do
        # Revalidation avant un `rm -rf`. La liste vient d'être calculée, mais
        # rien ne coûte moins cher que de reposer les trois questions qui
        # comptent : est-ce un vrai répertoire, porte-t-il un nom de release, et
        # n'est-ce pas le venv en service ?
        [[ -n "$path" && -d "$path" && ! -L "$path" ]] || continue
        [[ "$path" == "$install_dir/venv-agent-release-"* || \
           "$path" == "$install_dir/venv-agent-pre-update-"* ]] || continue
        [[ "$path" != "$current" ]] || continue
        if rm -rf -- "$path"; then
            printf '%s\n' "$path"
        else
            status=1
        fi
    done < <(agent_venv_prunable_releases "$install_dir" "$link" "$keep")

    return "$status"
}
