#!/usr/bin/env bash
# nginx-lib.sh — déploiement de la configuration nginx (OPS-009).
#
# Sourcé par install.sh et update.sh. Testé par
# gateway/tests/test_nginx_http2_lib.py.
#
# ── Pourquoi la conf livrée ne peut pas décider seule ────────────────────────
# Aucune écriture de HTTP/2 n'est valide sur tout le socle supporté :
#   - « listen … ssl http2 » est DÉPRÉCIÉ depuis nginx 1.25.1 : deux warnings à
#     chaque `nginx -t` et à chaque reload, qui noient les avertissements utiles ;
#   - « http2 on; » n'existe PAS avant 1.25.1 : « unknown directive », nginx
#     refuse de démarrer sur Ubuntu 22.04 LTS (1.18) et 24.04 LTS (1.24).
#
# `deploy/nginx.conf` est donc livrée dans la seule forme valide partout : un
# `listen` nu, sans HTTP/2, avec « http2 on; » commenté. C'est le bon défaut
# pour une copie manuelle, mais ce serait une régression d'en rester là : les
# deux socles supportés perdraient HTTP/2 pour faire taire un avertissement qui
# ne les concerne pas.
#
# Les scripts de déploiement écrivent donc la forme adaptée à la version
# réellement installée. Personne ne perd HTTP/2, et `nginx -t` est silencieux
# sur toute la plage 1.18 → 1.29.

# nginx_installed_version
# Version de nginx sur cet hôte, au format « 1.24.0 ». Vide si indéterminable.
# `nginx -v` écrit sur STDERR, d'où la redirection.
nginx_installed_version() {
    local raw
    raw="$(nginx -v 2>&1)" || return 0
    # « nginx version: nginx/1.24.0 (Ubuntu) » → « 1.24.0 »
    printf '%s\n' "$raw" | sed -n 's|.*nginx/\([0-9][0-9.]*\).*|\1|p' | head -n 1
}

# nginx_supports_http2_directive <version>
# Vrai (0) si la directive « http2 on; » existe, c'est-à-dire nginx >= 1.25.1.
# Une version vide ou non reconnue est traitée comme NON supportée : sur un
# doute, on préfère la forme dépréciée qui démarre partout à la forme moderne
# qui empêche nginx de démarrer.
nginx_supports_http2_directive() {
    local version="$1"
    [[ -n "$version" ]] || return 1
    # sort -V place la plus petite en premier : si 1.25.1 sort avant, alors
    # version >= 1.25.1.
    local oldest
    oldest="$(printf '%s\n%s\n' "$version" "1.25.1" | sort -V | head -n 1)"
    [[ "$oldest" == "1.25.1" ]]
}

# nginx_render_conf <source> <destination>
# Écrit la conf en activant HTTP/2 sous la forme que comprend le nginx local.
# Sans nginx détectable, la source est copiée telle quelle : elle est valide
# partout, simplement sans HTTP/2.
nginx_render_conf() {
    local src="$1" dst="$2"
    local version
    version="$(nginx_installed_version)"

    if nginx_supports_http2_directive "$version"; then
        # nginx >= 1.25.1 : directive moderne, on décommente « http2 on; ».
        sed 's|^\([[:space:]]*\)# http2 on;|\1http2 on;|' "$src" > "$dst"
        NGINX_HTTP2_FORM="http2 on;"
    elif [[ -n "$version" ]]; then
        # nginx < 1.25.1 : forme historique, seule à activer HTTP/2 ici. Elle
        # n'émet aucun avertissement sur ces versions — la dépréciation
        # n'existe qu'à partir de 1.25.1.
        sed -e 's|^\([[:space:]]*\)listen 443 ssl;|\1listen 443 ssl http2;|' \
            -e 's|^\([[:space:]]*\)listen \[::\]:443 ssl;|\1listen [::]:443 ssl http2;|' \
            "$src" > "$dst"
        NGINX_HTTP2_FORM="listen … ssl http2"
    else
        cp "$src" "$dst"
        NGINX_HTTP2_FORM="aucune (HTTP/1.1)"
    fi

    NGINX_DETECTED_VERSION="$version"
}
