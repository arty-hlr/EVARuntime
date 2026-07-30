#!/usr/bin/env bash
# venv-retention-lib.sh — rétention des venvs de release de la gateway (OPS-010).
#
# Sourcé par update.sh. Testé pour de vrai par
# gateway/tests/test_update_venv_retention.py, qui construit une arborescence de
# releases en répertoire temporaire et vérifie ce qui survit à la purge.
#
# ── Pourquoi ce fichier existe ───────────────────────────────────────────────
# `update.sh` construit chaque venv à son emplacement DÉFINITIF
# (`<install_dir>/venv-release-<commit>-<horodatage>`) et fait basculer le
# symlink `<install_dir>/venv` — le chemin figé dans l'unité systemd. C'est ce
# qui rend le rollback instantané et sans déplacement de venv, mais le corollaire
# est qu'aucune release n'est jamais écrasée : chaque mise à jour LAISSE une
# arborescence complète sur le disque, plus un éventuel `venv-pre-update-*` issu
# de la migration depuis l'ancien schéma. Sans purge, /opt sature en silence.
#
# La rétention est donc explicite et BORNÉE : on conserve la release ACTIVE et
# les `keep - 1` plus récentes des autres — par défaut la précédente, ce qui
# laisse réalisable le retour arrière manuel documenté dans docs/deployment.md.
# Tout le reste est purgé.
#
# La cible courante du symlink n'est JAMAIS supprimée, quel que soit son âge :
# c'est le venv sur lequel la gateway tourne.
#
# L'ordre est celui des dates de modification, jamais celui des noms : le nom
# d'une release commence par un hash de commit et ne se trie pas
# chronologiquement. Le nom ne sert qu'à départager deux dates égales, pour que
# la décision reste déterministe.
#
# Le node-agent applique la même politique sur ses propres releases, dans
# node_agent/deploy/agent-venv-lib.sh (composants volontairement indépendants,
# même motif que deploy-mode-lib.sh).

GATEWAY_VENV_KEEP_DEFAULT=2

# gateway_venv_mtime <chemin>
# Date de modification en secondes. `stat` n'a pas la même syntaxe selon les
# systèmes : GNU d'abord (le serveur), BSD ensuite (poste de développement).
gateway_venv_mtime() {
    stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || printf '0\n'
}

# gateway_venv_release_dirs <install_dir>
# Toutes les releases présentes, de la plus récente à la plus ancienne. Un lien
# symbolique n'en est pas une : `venv` lui-même ne peut donc jamais ressortir
# d'ici, quelle que soit sa cible.
gateway_venv_release_dirs() {
    local install_dir="$1" path
    for path in "$install_dir"/venv-release-* "$install_dir"/venv-pre-update-*; do
        # Un glob sans correspondance se rend lui-même : le test -d l'écarte.
        [[ -d "$path" && ! -L "$path" ]] || continue
        printf '%s\t%s\n' "$(gateway_venv_mtime "$path")" "$path"
    done | LC_ALL=C sort -t"$(printf '\t')" -k1,1nr -k2,2r | cut -f2-
}

# gateway_venv_prunable_releases <install_dir> <venv_link> [keep]
# Les releases que la rétention désigne, SANS rien supprimer. Sépare la décision
# de l'effacement : c'est ce que les tests vérifient sans dépendre d'un `rm`.
gateway_venv_prunable_releases() {
    local install_dir="$1" link="$2" keep="${3:-$GATEWAY_VENV_KEEP_DEFAULT}"
    local current="" kept=0 path

    [[ "$keep" =~ ^[1-9][0-9]*$ ]] || keep="$GATEWAY_VENV_KEEP_DEFAULT"

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
    done < <(gateway_venv_release_dirs "$install_dir")
}

# gateway_venv_prune_releases <install_dir> <venv_link> [keep]
# Applique la rétention et rend sur stdout les répertoires réellement supprimés,
# un par ligne. Rend 1 si au moins une suppression a échoué : l'appelant en fait
# un avertissement, jamais un échec de mise à jour.
gateway_venv_prune_releases() {
    local install_dir="$1" link="$2" keep="${3:-$GATEWAY_VENV_KEEP_DEFAULT}"
    local current="" path status=0

    current="$(readlink -f "$link" 2>/dev/null || true)"

    while IFS= read -r path; do
        # Revalidation avant un `rm -rf`. La liste vient d'être calculée, mais
        # rien ne coûte moins cher que de reposer les trois questions qui
        # comptent : est-ce un vrai répertoire, porte-t-il un nom de release, et
        # n'est-ce pas le venv en service ?
        [[ -n "$path" && -d "$path" && ! -L "$path" ]] || continue
        [[ "$path" == "$install_dir/venv-release-"* || \
           "$path" == "$install_dir/venv-pre-update-"* ]] || continue
        [[ "$path" != "$current" ]] || continue
        if rm -rf -- "$path"; then
            printf '%s\n' "$path"
        else
            status=1
        fi
    done < <(gateway_venv_prunable_releases "$install_dir" "$link" "$keep")

    return "$status"
}
