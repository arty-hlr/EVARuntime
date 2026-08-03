#!/usr/bin/env bash
# Décision GPU du préflight d'installation (OPS-012).
#
# Pourquoi cette bibliothèque existe
# ----------------------------------
# Le préflight `command -v nvidia-smi` a bloqué les DEUX déploiements réels sur
# banc CPU, et les deux ont dû relâcher la ligne du script pour installer. Un
# garde-fou que tout le monde contourne ne protège plus personne : il apprend
# seulement à passer outre. On lui donne donc une échappatoire EXPLICITE
# (`--allow-no-gpu`), tracée dans l'environnement généré et remontée par
# `doctor` — plutôt qu'un contournement silencieux, invisible au diagnostic.
#
# La décision est isolée ici pour une raison de testabilité : le préflight
# d'`install.sh` vit derrière un contrôle root, donc inexerçable en test. Sourcée,
# cette fonction se pilote pour de vrai, dans ses deux branches, sans privilège
# ni GPU — cf. `gateway/tests/test_deploy_no_gpu.py`.

# Clé écrite dans /etc/llm-gateway/env quand l'absence de GPU est assumée.
# `gateway/doctor.py` lit EXACTEMENT cette clé (doctor.GPU_WAIVER_ENV_KEY).
GPU_WAIVER_ENV_KEY="ALLOW_NO_GPU"

# deploy_gpu_verdict <mode> <allow_no_gpu> [commande_de_sonde]
#
# Écrit le verdict sur stdout et retourne 0 :
#   delegated  mode cluster — les GPU vivent sur les nœuds, rien à exiger ici
#   detected   la sonde GPU répond sur cet hôte
#   waived     pas de GPU, absence ASSUMÉE par --allow-no-gpu
#
# Retourne 1 et écrit la conduite à tenir sur stderr quand le GPU manque sans
# échappatoire : c'est le refus historique, conservé tel quel, mais qui dit
# désormais quoi faire.
deploy_gpu_verdict() {
    local mode="$1" allow_no_gpu="$2" probe="${3:-nvidia-smi}"

    if [[ "$mode" == "cluster" ]]; then
        printf 'delegated\n'
        return 0
    fi
    # La sonde passe AVANT l'échappatoire : sur un hôte réellement équipé,
    # `--allow-no-gpu` ne doit pas faire croire à une machine sans GPU.
    if command -v "$probe" >/dev/null 2>&1; then
        printf 'detected\n'
        return 0
    fi
    if [[ "$allow_no_gpu" == true ]]; then
        printf 'waived\n'
        return 0
    fi

    cat >&2 <<EOF
Préflight local : $probe introuvable — aucun GPU NVIDIA utilisable sur cet hôte.

Conduite à tenir, au choix :
  1. Installer le pilote NVIDIA, puis vérifier que « $probe » répond :
       sudo apt install -y nvidia-driver-535 && $probe
  2. Installer malgré tout sur un banc CPU, en assumant l'absence de GPU :
       sudo bash install.sh --mode local --allow-no-gpu
     Ce choix est inscrit dans /etc/llm-gateway/env ($GPU_WAIVER_ENV_KEY=true)
     et remonté par « evaruntime doctor » : aucun chargement de modèle ne sera
     offloadé sur GPU, et les temps de génération seront ceux d'un CPU.
  3. Déployer cet hôte en orchestrateur, sans GPU local :
       sudo bash install.sh --mode cluster
EOF
    return 1
}
