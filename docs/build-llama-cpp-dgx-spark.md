# Compiler llama.cpp sur un Lenovo PGX / NVIDIA GB10

Ce document couvre uniquement la compilation et l'installation du binaire
`llama-server`. Le déploiement EVARuntime est décrit dans
[`deployment.md`](deployment.md).

Le checkout utilisé partout ici est `/opt/src/llama.cpp`. Le binaire est publié
dans `/opt/llama.cpp/releases/<release>/llama-server`, puis exposé par le lien
atomique `/opt/llama.cpp/current/llama-server`. Ne lancez pas un serveur
permanent à la main : EVARuntime gère le cycle de vie de ses processus.

## Vérifier le bon GPU et le bon compilateur

```bash
uname -m
nvidia-smi --query-gpu=name,driver_version --format=csv,noheader

# Sur certains PGX, /usr/bin/nvcc vient encore du paquet CUDA 12.
# Sélectionner explicitement le toolkit CUDA 13 installé pour le GB10.
export CUDA_HOME=/usr/local/cuda-13.0
export PATH="$CUDA_HOME/bin:$PATH"
export CUDACXX="$CUDA_HOME/bin/nvcc"

nvcc --version
```

Le résultat attendu est `aarch64`, `NVIDIA GB10` et CUDA 13.x. Le pilote
NVIDIA peut afficher une version CUDA 13 même lorsque la commande `nvcc` par
défaut pointe encore vers CUDA 12 : c'est le chemin du compilateur qui compte
pour la compilation.

Le GB10 utilise la compute capability 12.1. Dans CMake, sa cible est donc
`121`. `89` est la cible des GPU Ada (par exemple L40S/RTX 4090) et ne convient
pas au PGX.

## Dépendances

```bash
sudo apt update
sudo apt install -y build-essential cmake ninja-build git libcurl4-openssl-dev
```

## Récupérer les sources

Utilisez un checkout permanent, avec votre utilisateur normal :

```bash
sudo install -d -m 0755 /opt/src
sudo chown "$USER":"$(id -gn)" /opt/src

# Si la sortie Internet passe par le proxy UPPA :
export http_proxy=http://cache.univ-pau.fr:3128
export https_proxy=http://cache.univ-pau.fr:3128

git clone https://github.com/ggml-org/llama.cpp.git /opt/src/llama.cpp
export LLAMA_SRC=/opt/src/llama.cpp
```

Si le checkout existe déjà :

```bash
export LLAMA_SRC=/opt/src/llama.cpp
git -C "$LLAMA_SRC" status --short
```

Avant une mise à jour, le checkout doit être propre. Le dépôt ne doit pas être
cloné dans `/tmp` : le chemin est réutilisé pour les builds suivants.

## Build CUDA du serveur

```bash
export CUDA_HOME=/usr/local/cuda-13.0
export PATH="$CUDA_HOME/bin:$PATH"
export CUDACXX="$CUDA_HOME/bin/nvcc"
export LLAMA_SRC=/opt/src/llama.cpp

cd "$LLAMA_SRC"
cmake -S . -B build -G Ninja \
  -DGGML_CUDA=ON \
  -DCMAKE_CUDA_ARCHITECTURES=121 \
  -DBUILD_SHARED_LIBS=OFF \
  -DLLAMA_BUILD_UI=OFF \
  -DLLAMA_USE_PREBUILT_UI=OFF \
  -DCMAKE_BUILD_TYPE=Release

cmake --build build --target llama-server --parallel 8
./build/bin/llama-server --version

RELEASE="manual-gb10-$(git rev-parse --short HEAD)"
sudo install -d -m 0755 "/opt/llama.cpp/releases/$RELEASE"
sudo install -m 0755 build/bin/llama-server \
  "/opt/llama.cpp/releases/$RELEASE/llama-server"
sudo ln -sfn "releases/$RELEASE" /opt/llama.cpp/.current-new
sudo mv -Tf /opt/llama.cpp/.current-new /opt/llama.cpp/current

/opt/llama.cpp/current/llama-server --version
```

La commande CMake officielle construit `llama-server` depuis la racine du
dépôt ; la sortie attendue est `build/bin/llama-server`. Le target explicite
réduit le temps de compilation par rapport à la construction de tous les
outils. `LLAMA_BUILD_UI=OFF` évite de construire les assets Web de l'interface
llama.cpp ; `LLAMA_USE_PREBUILT_UI=OFF` désactive aussi le repli vers le bucket
Hugging Face. `BUILD_SHARED_LIBS=OFF` produit un binaire qui ne dépend pas des
bibliothèques générées dans le répertoire `build/` lorsqu'il est publié. Le
lien `current` est le seul chemin lu par EVARuntime et permet un retour
arrière vers une release précédente. EVARuntime possède déjà son propre
dashboard et n'en dépend pas.

Si la compilation manque de mémoire, ne changez pas l'architecture CUDA.
Relancez seulement le build avec moins de jobs :

```bash
cd /opt/src/llama.cpp
cmake --build build --target llama-server --parallel 4
```

## Problèmes de compilation

| Message | Cause probable | Action |
|---|---|---|
| `unsupported gpu architecture 'compute_121'` | CMake utilise CUDA 12 ou un autre `nvcc`. | Vérifier `command -v nvcc`, `nvcc --version` et `echo "$CUDACXX"`; réexporter CUDA 13 puis supprimer uniquement `build/CMakeCache.txt` et relancer CMake. |
| `nvcc` affiche `release 12.0` | Le wrapper `/usr/bin/nvcc` masque CUDA 13. | Utiliser `CUDA_HOME=/usr/local/cuda-13.0`, mettre `$CUDA_HOME/bin` en tête de `PATH` et définir `CUDACXX`. |
| `Killed` pendant la compilation | Trop de compilations CUDA simultanées. | Utiliser `--parallel 4`, puis `--parallel 2` si nécessaire. |
| `cmake` ne trouve pas CUDA | Toolkit absent ou chemin non standard. | Vérifier `/usr/local/cuda-13.0/bin/nvcc`; installer le toolkit PGX fourni par NVIDIA avant de continuer. |
| `libcuda.so` introuvable au link | Environnement CUDA incomplet. | Vérifier `ls /usr/local/cuda-13.0/compat /usr/local/cuda-13.0/targets/sbsa-linux/lib`; ne pas remplacer le toolkit par CUDA 12. |

Si CMake a été configuré avec le mauvais compilateur, repartez d'un cache
propre sans supprimer les sources :

```bash
cd /opt/src/llama.cpp
rm -f build/CMakeCache.txt
rm -rf build/CMakeFiles
export CUDA_HOME=/usr/local/cuda-13.0
export PATH="$CUDA_HOME/bin:$PATH"
export CUDACXX="$CUDA_HOME/bin/nvcc"
cmake -S . -B build -G Ninja \
  -DGGML_CUDA=ON \
  -DCMAKE_CUDA_ARCHITECTURES=121 \
  -DBUILD_SHARED_LIBS=OFF \
  -DLLAMA_BUILD_UI=OFF \
  -DLLAMA_USE_PREBUILT_UI=OFF \
  -DCMAKE_BUILD_TYPE=Release
cmake --build build --target llama-server --parallel 4
```

## Mise à jour du binaire

```bash
export CUDA_HOME=/usr/local/cuda-13.0
export PATH="$CUDA_HOME/bin:$PATH"
export CUDACXX="$CUDA_HOME/bin/nvcc"
cd /opt/src/llama.cpp
git pull --ff-only

cmake -S . -B build -G Ninja \
  -DGGML_CUDA=ON \
  -DCMAKE_CUDA_ARCHITECTURES=121 \
  -DBUILD_SHARED_LIBS=OFF \
  -DLLAMA_BUILD_UI=OFF \
  -DLLAMA_USE_PREBUILT_UI=OFF \
  -DCMAKE_BUILD_TYPE=Release
cmake --build build --target llama-server --parallel 8
RELEASE="manual-gb10-$(git rev-parse --short HEAD)"
sudo install -d -m 0755 "/opt/llama.cpp/releases/$RELEASE"
sudo install -m 0755 build/bin/llama-server \
  "/opt/llama.cpp/releases/$RELEASE/llama-server"
sudo ln -sfn "releases/$RELEASE" /opt/llama.cpp/.current-new
sudo mv -Tf /opt/llama.cpp/.current-new /opt/llama.cpp/current
sudo systemctl restart llm-gateway
```

Après toute mise à jour, redémarrez EVARuntime puis exécutez le smoke test du
[guide de déploiement](deployment.md#recette-du-premier-token-smoke_testsh).
