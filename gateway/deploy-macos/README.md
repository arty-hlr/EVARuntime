# EVARuntime — Déploiement sur macOS (Mac Studio)

> **IMPORTANT** : Le mode cluster n'est PAS supporté sur macOS. Seul le mode local (mono-nœud) est fonctionnel.

Ce répertoire contient les scripts de déploiement d'EVARuntime pour macOS, spécifiquement conçus pour Mac Studio avec puces Apple Silicon (M2/M3/M4).

## Prérequis

Avant d'installer EVARuntime, vous devez avoir :

1. **macOS 14+ (Sonoma)** sur un Mac Studio
2. **Homebrew** installé ([https://brew.sh](https://brew.sh))
   ```bash
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
   ```
3. **Python 3.11+** via Homebrew
   ```bash
   brew install python@3.12
   ```
4. **llama.cpp compilé** via Homebrew
   ```bash
   brew install llama.cpp
   ```

> **Note** : Le binaire `llama-server` doit être accessible dans votre `$PATH`. Homebrew l'installe généralement dans `/opt/homebrew/bin/llama-server` sur Apple Silicon.

## Architecture macOS vs Linux

| Composant | Linux (Ubuntu) | macOS |
|-----------|---------------|-------|
| Gestionnaire de service | systemd (`systemctl`) | launchd (`launchctl`) |
| Répertoire d'installation | `/opt/llm-gateway` | `~/Library/Application Support/evaruntime/gateway` |
| Données | `/var/lib/llm-gateway` | `~/Library/Application Support/evaruntime/data` |
| Logs | `/var/log/llm-gateway` | `~/Library/Application Support/evaruntime/logs` |
| Configuration | `/etc/llm-gateway/env` | `~/.config/evaruntime/env` |
| Modèles | `/models` | `~/Library/Application Support/evaruntime/models` |
| GPU | CUDA (NVIDIA) | Metal (Apple Silicon) |
| Reverse-proxy | nginx (systemd) | nginx (Homebrew, optionnel) |

## Installation rapide

```bash
# 1. Vérifier le plan d'installation (dry-run)
bash gateway/deploy-macos/install.sh --dry-run

# 2. Lancer l'installation (SEUL mode supporté)
bash gateway/deploy-macos/install.sh --mode local

# 3. Vérifier l'état de l'installation
bash gateway/deploy-macos/doctor-macos.sh
```

## Commandes disponibles

### install.sh — Installation complète

```bash
bash gateway/deploy-macos/install.sh --mode local [--dry-run]
```

**Options :**
- `--mode local` : Gateway mono-nœud (SEUL mode supporté sur macOS)
- `--dry-run` : Affiche le plan sans modifier le système

> **Note** : Le mode cluster n'est PAS supporté sur macOS. Toute tentative d'installer en mode cluster échouera.

**Ce que fait install.sh :**
1. Vérifie les prérequis (Homebrew, Python 3.11+, llama-server)
2. Détecte le GPU Apple Silicon (Metal) et la RAM unifiée
3. Crée les répertoires de données/logs/config
4. Copie le code source dans `~/Library/Application Support/evaruntime/gateway/`
5. Crée l'environnement virtuel Python et installe les dépendances
6. Génère le fichier de configuration avec des secrets aléatoires
7. Initialise la base de données SQLite
8. Installe et charge le service launchd (`com.evaruntime.gateway`)

### update.sh — Mise à jour transactionnelle

```bash
bash gateway/deploy-macos/update.sh [--nginx]
```

**Ce que fait update.sh :**
1. Sauvegarde la version actuelle (venv, configuration)
2. Synchronise le code source mis à jour
3. Met à jour les dépendances Python
4. Redémarre le service launchd
5. Vérifie le ready state après redémarrage
6. Nettoie les anciennes sauvegardes (> 3 conservées)

**Option `--nginx` :** Met aussi à jour la configuration nginx si installée.

### uninstall.sh — Désinstallation propre

```bash
bash gateway/deploy-macos/uninstall.sh [--keep-data] [--keep-config] [--force]
```

**Options :**
- `--keep-data` : Conserve les modèles GGUF et la base de données
- `--keep-config` : Conserve le fichier de configuration (secrets inclus)
- `--force` : Supprime tout sans confirmation interactive

### doctor-macos.sh — Diagnostic complet

```bash
bash gateway/deploy-macos/doctor-macos.sh [--json]
```

Vérifie l'état complet de l'installation et signale les problèmes potentiels :
- Système (macOS, Homebrew, Python)
- GPU / Accélération Metal
- Service launchd (chargé, en cours d'exécution, répondant)
- Installation (code, venv, dépendances)
- Configuration (clés essentielles, secrets)
- Registre des modèles et fichiers GGUF
- Logs (présence, erreurs récentes)
- Nginx (optionnel)

## Gestion du service

### Démarrer / Arrêter / Redémarrer

```bash
# Charger le service (démarrage automatique au login)
launchctl load ~/Library/LaunchDaemons/com.evaruntime.gateway.plist

# Arrêter le service
launchctl bootout system/com.evaruntime.gateway

# Redémarrer
launchctl bootout system/com.evaruntime.gateway 2>/dev/null || true
launchctl bootstrap system ~/Library/LaunchDaemons/com.evaruntime.gateway.plist

# Vérifier l'état
launchctl list com.evaruntime.gateway
```

### Consulter les logs

```bash
# Logs en temps réel
tail -f ~/Library/Application\ Support/evaruntime/logs/gateway.log

# Erreurs uniquement
tail -f ~/Library/Application\ Support/evaruntime/logs/gateway-error.log

# Dernières lignes
tail -n 50 ~/Library/Application\ Support/evaruntime/logs/gateway.log
```

## Configuration

Le fichier de configuration se trouve dans `~/.config/evaruntime/env`. Il est généré automatiquement lors de l'installation avec des valeurs adaptées à votre Mac Studio :

- **RAM unifiée** : détectée automatiquement via `sysctl hw.memsize`
- **llama-server** : chemin par défaut `/opt/homebrew/bin/llama-server`
- **Ports** : pool 8081–8085 (base + max_loaded_models)

### Variables d'environnement personnalisées

Vous pouvez surcharger les chemins par défaut via des variables d'environnement avant de lancer `install.sh` :

```bash
export EVARUNE_INSTALL_DIR=/custom/path/to/gateway
export EVARUNE_DATA_DIR=/custom/path/to/data
export EVARUNE_LOG_DIR=/custom/path/to/logs
export EVARUNE_MODELS_DIR=/custom/path/to/models
export LLAMA_BIN=/custom/path/to/llama-server

bash gateway/deploy-macos/install.sh
```

## Nginx (optionnel)

Nginx est un reverse-proxy optionnel qui ajoute :
- Rate limiting au niveau réseau
- Limitation des connexions SSE concurrentes par IP
- Support HTTPS/TLS (avec certificat)
- Journal d'accès anonymisé

### Installation de nginx via Homebrew

```bash
brew install nginx
brew services start nginx
```

### Configuration EVARuntime pour nginx

```bash
# Copier la configuration macOS dans le répertoire servers de Homebrew nginx
sudo cp gateway/deploy-macos/nginx.conf.macOS /opt/homebrew/etc/nginx/servers/llm-gateway

# Tester la configuration
nginx -t

# Recharger nginx
brew services restart nginx
```

### HTTPS local (auto-signé)

Pour un test en HTTPS avec un certificat auto-signé :

```bash
# Créer le répertoire SSL
mkdir -p /opt/homebrew/etc/nginx/ssl

# Générer un certificat auto-signé (valable 1 an)
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout /opt/homebrew/etc/nginx/ssl/server.key \
  -out /opt/homebrew/etc/nginx/ssl/server.crt \
  -subj "/CN=localhost"

# Décommenter le bloc HTTPS dans nginx.conf.macOS puis recharger
sudo brew services restart nginx
```

## Utilisation de la gateway

### Vérifier que tout fonctionne

```bash
# Health check
curl http://127.0.0.1:8000/health

# Readiness (indique si les modèles sont prêts)
curl http://127.0.0.1:8000/ready

# Dashboard admin
open http://127.0.0.1:8000/admin/dashboard
```

### Charger un modèle

Après avoir placé vos fichiers `.gguf` dans le répertoire de modèles :

```bash
# Récupérer le secret admin depuis la configuration
ADMIN_SECRET=$(grep "^ADMIN_SECRET=" ~/.config/evaruntime/env | cut -d'=' -f2-)

# Charger un modèle (remplacer {id} par l'ID du modèle dans models.yaml)
curl -X POST http://127.0.0.1:8000/admin/models/{id}/load \
  -H "Authorization: Bearer $ADMIN_SECRET"
```

### Tester avec un client OpenAI

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key="votre-clef-api"  # ou la clé configurée dans models.yaml
)

response = client.chat.completions.create(
    model="your-model-id",
    messages=[{"role": "user", "content": "Bonjour !"}],
    max_tokens=100,
    stream=True,
)

for chunk in response:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
```

## Dépannage

### Le service ne démarre pas

```bash
# Vérifier le statut launchd
launchctl list com.evaruntime.gateway

# Consulter les logs d'erreur
cat ~/Library/Application\ Support/evaruntime/logs/gateway-error.log

# Vérifier que le port 8000 n'est pas déjà utilisé
lsof -i :8000
```

### Erreur "llama-server not found"

```bash
# Vérifier l'installation de llama.cpp
brew list llama.cpp

# Trouver le binaire
which llama-server
# ou
find /opt/homebrew -name "llama-server" 2>/dev/null

# Mettre à jour la configuration si nécessaire
nano ~/.config/evaruntime/env
# Modifier LLAMA_SERVER_BIN avec le bon chemin
```

### Problèmes de mémoire (OOM)

Sur Mac Studio, la mémoire unifiée est partagée entre CPU et GPU. Si vous chargez des modèles trop gros :

1. Vérifiez la RAM disponible : `vm_stat | head -n 20`
2. Réduisez `MAX_LOADED_MODELS` dans `~/.config/evaruntime/env`
3. Utilisez des modèles quantisés (Q4_K_M ou Q5_K_M)
4. Augmentez `VRAM_OVERHEAD_GB` pour réserver plus de mémoire au système

### Les logs sont vides après redémarrage

```bash
# Vérifier les permissions du répertoire logs
ls -la ~/Library/Application\ Support/evaruntime/logs/

# Relancer le service avec les bons droits
launchctl bootout system/com.evaruntime.gateway 2>/dev/null || true
launchctl bootstrap system ~/Library/LaunchDaemons/com.evaruntime.gateway.plist
```

### Reset complet

Si vous devez repartir de zéro :

```bash
# Désinstaller en conservant les données (modèles GGUF)
bash gateway/deploy-macos/uninstall.sh --keep-data

# Réinstaller proprement
bash gateway/deploy-macos/install.sh
```

## Comparaison avec le déploiement Linux

Les scripts macOS sont une adaptation des scripts Linux (`gateway/deploy/`). Les différences principales :

| Aspect | Linux | macOS |
|--------|-------|-------|
| **Service** | systemd (root) | launchd (utilisateur courant) |
| **Privilèges** | `sudo` requis | Aucun sudo nécessaire |
| **GPU** | CUDA + nvidia-smi | Metal (natif Apple Silicon) |
| **Mémoire** | VRAM dédiée (NVIDIA) | RAM unifiée (CPU+GPU) |
| **Reverse-proxy** | nginx system-wide | nginx Homebrew (optionnel) |
| **Logs** | journald + fichiers | Fichiers uniquement |
| **Backup DB** | timer systemd quotidien | Non implémenté (à venir) |

## Prochaines étapes

1. **Configurer les modèles** : Modifiez `models.yaml` dans le répertoire de données pour ajouter vos GGUF
2. **Sécuriser l'accès** : Changez les secrets par défaut dans la configuration
3. **Activer HTTPS** : Installez nginx avec un certificat TLS si vous exposez la gateway au réseau
4. **Monitorer** : Consultez le dashboard admin à `http://127.0.0.1:8000/admin/dashboard`

## Support

Pour les problèmes spécifiques à macOS, consultez :
- [Documentation EVARuntime](../../docs/deployment.md) (générale)
- [Architecture technique](../../docs/architecture.md)
- Les scripts dans `gateway/` pour comprendre le comportement de la gateway elle-même (identique sur Linux et macOS)
