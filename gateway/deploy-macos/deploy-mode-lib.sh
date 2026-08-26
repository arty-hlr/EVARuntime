#!/usr/bin/env bash
# deploy-mode-lib.sh — Validation du mode de déploiement (macOS)
#
# IMPORTANT : Le mode cluster n'est PAS supporté sur macOS.
# Cette bibliothèque fournit uniquement la fonction de validation.

deploy_validate_mode() {
    case "$1" in
        local) return 0 ;;
        cluster)
            echo "Mode cluster non supporté sur macOS. Utilisez 'local'." >&2
            return 1
            ;;
        *) echo "Mode invalide : $1 (attendu: local)" >&2; return 1 ;;
    esac
}

# deploy_env_value <fichier> <clé> [défaut]
# Lit une valeur depuis le fichier d'environnement. Ignore les commentaires et lignes vides.
deploy_env_value() {
    local file="$1" key="$2" default="${3:-}"
    if [[ -f "$file" ]]; then
        local value
        value=$(grep -E "^${key}=" "$file" 2>/dev/null | head -n1 | cut -d'=' -f2- | sed 's/^"\(.*\)"$/\1/' | sed "s/^'\(.*\)'$/\1/")
        if [[ -n "$value" ]]; then
            printf '%s\n' "$value"
            return 0
        fi
    fi
    printf '%s\n' "$default"
}

# deploy_set_env_value <fichier> <clé> <valeur>
# Modifie ou ajoute une clé dans le fichier d'environnement.
deploy_set_env_value() {
    local file="$1" key="$2" value="$3"
    
    if [[ -f "$file" ]]; then
        # Vérifier si la clé existe déjà
        if grep -qE "^${key}=" "$file" 2>/dev/null; then
            # Utiliser sed pour remplacer (macOS compatible)
            local escaped_value
            escaped_value=$(printf '%s\n' "$value" | sed 's/[&/\]/\\&/g')
            sed -i '' "s|^${key}=.*|${key}=${escaped_value}|" "$file" 2>/dev/null || {
                # Fallback si sed -i échoue
                local tmpfile
                tmpfile=$(mktemp)
                while IFS= read -r line; do
                    if [[ "$line" =~ ^"${key}"= ]]; then
                        echo "${key}=${value}"
                    else
                        echo "$line"
                    fi
                done < "$file" > "$tmpfile"
                mv "$tmpfile" "$file"
            }
        else
            # Ajouter la clé à la fin du fichier
            printf '%s=%s\n' "$key" "$value" >> "$file"
        fi
    fi
}

# deploy_apply_mode <mode> <config_file> <config_dir> [nodes_yaml_example]
# Applique les réglages spécifiques au mode dans le fichier d'environnement.
deploy_apply_mode() {
    local mode="$1" config_file="$2" config_dir="$3" nodes_yaml="${4:-}"
    
    deploy_set_env_value "$config_file" CLUSTER_MODE "$mode"
    
    if [[ "$mode" == "cluster" && -n "$nodes_yaml" ]]; then
        local cluster_nodes_path="${config_dir}/nodes.yaml"
        deploy_set_env_value "$config_file" CLUSTER_NODES_PATH "$cluster_nodes_path"
        
        # Générer un secret agent si absent
        local current_secret
        current_secret=$(deploy_env_value "$config_file" AGENT_SECRET)
        if [[ -z "$current_secret" || "$current_secret" == "CHANGE_ME"* ]]; then
            local new_secret
            new_secret=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
            deploy_set_env_value "$config_file" AGENT_SECRET "$new_secret"
        fi
        
        # Copier nodes.yaml.example si absent
        if [[ ! -f "$cluster_nodes_path" && -f "$nodes_yaml" ]]; then
            cp "$nodes_yaml" "$cluster_nodes_path"
        fi
    fi
}

# deploy_secret_is_missing <secret>
# Vérifie si un secret est manquant ou à sa valeur par défaut.
deploy_secret_is_missing() {
    local secret="${1:-}"
    [[ -z "$secret" || "$secret" == "CHANGE_ME"* ]]
}
