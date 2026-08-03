#!/usr/bin/env bash
# Layout du code Python déployé sous /opt/llm-gateway.
#
# Les scripts d'installation et de mise à jour doivent copier le même ensemble :
# modules racine, package cluster et package bootstrap avec son catalogue. Garder
# cette règle dans une fonction sourçable permet de l'exercer sans root ni hôte
# systemd ; une copie oubliée ne doit plus être découverte après installation.

_deploy_code_safe_roots() {
    local source_root="$1" target_root="$2" operation="$3"
    if [[ -z "$source_root" || -z "$target_root" || "$target_root" == "/" || \
          "$source_root" == "$target_root" ]]; then
        echo "$operation refusée : racines source/cible dangereuses ou identiques" >&2
        return 1
    fi
}

deploy_sync_gateway_code() {
    local source_root="$1" target_root="$2"

    _deploy_code_safe_roots "$source_root" "$target_root" "Synchronisation du code" || return 1

    [[ -f "$source_root/requirements.txt" ]] || {
        echo "Source gateway incomplète : requirements.txt introuvable dans $source_root" >&2
        return 1
    }
    [[ -d "$source_root/cluster" ]] || {
        echo "Source gateway incomplète : package cluster/ introuvable dans $source_root" >&2
        return 1
    }
    [[ -d "$source_root/bootstrap" && -f "$source_root/bootstrap/catalog.yaml" ]] || {
        echo "Source gateway incomplète : package bootstrap/ ou catalog.yaml introuvable" >&2
        return 1
    }

    mkdir -p "$target_root"
    cp "$source_root"/*.py "$target_root/"
    cp "$source_root/requirements.txt" "$target_root/"

    # Remplacer les packages plutôt que les superposer : un module supprimé dans
    # une release ne doit pas rester importable dans la suivante.
    rm -rf "$target_root/cluster" "$target_root/bootstrap"
    mkdir -p "$target_root/cluster" "$target_root/bootstrap"
    cp "$source_root/cluster"/*.py "$target_root/cluster/"
    cp "$source_root/bootstrap"/*.py "$target_root/bootstrap/"
    cp "$source_root/bootstrap/catalog.yaml" "$target_root/bootstrap/"

    rm -rf "$target_root/__pycache__" \
           "$target_root/cluster/__pycache__" \
           "$target_root/bootstrap/__pycache__"
}


deploy_snapshot_gateway_code() {
    local installed_root="$1" snapshot_root="$2"

    _deploy_code_safe_roots "$installed_root" "$snapshot_root" "Snapshot du code" || return 1

    mkdir -p "$snapshot_root"
    cp "$installed_root"/*.py "$snapshot_root/"
    cp "$installed_root/requirements.txt" "$snapshot_root/"
    [[ ! -d "$installed_root/cluster" ]] || \
        cp -a "$installed_root/cluster" "$snapshot_root/cluster"
    [[ ! -d "$installed_root/bootstrap" ]] || \
        cp -a "$installed_root/bootstrap" "$snapshot_root/bootstrap"
}


deploy_restore_gateway_code() {
    local snapshot_root="$1" installed_root="$2"

    _deploy_code_safe_roots "$snapshot_root" "$installed_root" "Restauration du code" || return 1

    [[ -f "$snapshot_root/requirements.txt" ]] || {
        echo "Snapshot gateway incomplet : requirements.txt introuvable dans $snapshot_root" >&2
        return 1
    }

    cp "$snapshot_root"/*.py "$installed_root/"
    cp "$snapshot_root/requirements.txt" "$installed_root/"
    rm -rf "$installed_root/cluster" "$installed_root/bootstrap"
    [[ ! -d "$snapshot_root/cluster" ]] || \
        cp -a "$snapshot_root/cluster" "$installed_root/cluster"
    [[ ! -d "$snapshot_root/bootstrap" ]] || \
        cp -a "$snapshot_root/bootstrap" "$installed_root/bootstrap"
    rm -rf "$installed_root/__pycache__" \
           "$installed_root/cluster/__pycache__" \
           "$installed_root/bootstrap/__pycache__"
}
