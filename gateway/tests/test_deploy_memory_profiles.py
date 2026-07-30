"""
COR-007 — cohérence entre les limites mémoire systemd et models.yaml.

Ce module est un test de NON-RÉGRESSION de déploiement, pas un test de code : il
lit les unités systemd livrées et le registre de modèles, puis échoue si un
modèle ACTIVÉ exige plus de RAM hôte que ce que la limite systemd autorise sur
l'hôte de référence documenté.

Pourquoi ce test existe
-----------------------
Les `llama-server` sont des sous-processus de la gateway (mode local) ou du
node-agent (mode cluster) : ils vivent dans le MÊME cgroup que le service et
comptent donc dans `MemoryHigh`/`MemoryMax`. Un modèle MoE avec `cpu_moe: true`
garde ses experts FFN résidents en RAM hôte (lus depuis le mmap du GGUF à chaque
token). Si la limite du cgroup est inférieure à ce working set, il n'y a pas
nécessairement d'OOM-kill — les pages mmap propres sont réclamables — mais il y a
du thrashing NVMe garanti et un TTFT effondré, SANS erreur visible. Le défaut ne
se manifeste donc pas au démarrage : il dérive silencieusement dès qu'on active
un modèle. D'où un test.

Base de la table de profils
---------------------------
Les valeurs ci-dessous sont des ESTIMATIONS d'architecture, pas des mesures :
- `vram_gb` est repris de models.yaml (source de vérité, vérifié par un test) ;
- les tailles de GGUF sont celles annoncées par la documentation du dépôt et les
  releases amont (`basis="declared"`), ou déduites de la quantisation et du
  nombre de paramètres (`basis="reasoned"`) ;
- `host_ram_resident_gb` est le working set qui doit rester en RAM hôte APRÈS
  chargement ; `host_ram_transient_gb` est le page cache traversé PENDANT le
  chargement (propre, jetable : il influence le temps de chargement, pas la
  correction).
Elles doivent être remplacées par des mesures dès que la calibration AUT-008 /
PERF-006 existe. La table est dupliquée de façon lisible dans
`docs/deployment.md` §15 — les deux doivent être mises à jour ensemble.
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from model_registry import ModelDefinition, ModelRegistry


REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY_ROOT = REPO_ROOT / "gateway"

LOCAL_UNIT = GATEWAY_ROOT / "deploy" / "llm-gateway.service"
CLUSTER_UNIT = GATEWAY_ROOT / "deploy" / "llm-gateway-cluster.service"
AGENT_UNIT = REPO_ROOT / "node_agent" / "deploy" / "llm-gateway-agent.service"
MODELS_YAML = GATEWAY_ROOT / "models.yaml"

# Hôte de référence documenté (docs/deployment.md §1 et §15) : serveur GPU L40S
# 48 Go avec 128 Go de RAM. C'est la cible contre laquelle un profil de modèles
# est déclaré compatible ou non. Un opérateur avec plus de RAM peut activer
# davantage de modèles, mais il doit le décider explicitement (models.yaml +
# table de la doc + ce profil).
REFERENCE_HOST_RAM_GB = 128.0

# RAM hôte consommée hors poids de modèle : processus gateway/agent (~0.3 Go),
# RSS des llama-server (allocations hôte CUDA, buffers épinglés proportionnels à
# batch/ubatch, threads HTTP), buffers de requêtes.
BASELINE_HOST_RAM_GB = 2.0


class MemoryProfile:
    """Profil mémoire hôte d'un modèle du registre."""

    def __init__(
        self,
        *,
        vram_gb: float,
        gguf_size_gb: float,
        host_ram_resident_gb: float,
        host_ram_transient_gb: float,
        basis: str,
        note: str,
    ) -> None:
        self.vram_gb = vram_gb
        self.gguf_size_gb = gguf_size_gb
        self.host_ram_resident_gb = host_ram_resident_gb
        self.host_ram_transient_gb = host_ram_transient_gb
        self.basis = basis
        self.note = note


# Profil par ID de modèle. TOUT modèle présent dans models.yaml doit avoir une
# entrée ici, activé ou non : c'est ce qui force la revue de déploiement quand un
# modèle est ajouté au registre.
MEMORY_PROFILES: dict[str, MemoryProfile] = {
    "llama-3.3-70b-instruct": MemoryProfile(
        vram_gb=42.0,
        gguf_size_gb=42.5,
        host_ram_resident_gb=1.5,
        host_ram_transient_gb=42.5,
        basis="declared",
        note=(
            "Dense Q4_K_M, offload total (-ngl 999, pas de cpu_moe) : poids et KV "
            "cache en VRAM. Les pages mmap sont propres après chargement."
        ),
    ),
    "minimax-m2.7": MemoryProfile(
        vram_gb=32.0,
        gguf_size_gb=248.0,
        host_ram_resident_gb=236.0,
        host_ram_transient_gb=248.0,
        basis="declared",
        note=(
            "MoE avec cpu_moe: true — les experts FFN restent RÉSIDENTS en RAM "
            "hôte (~95 % du GGUF de 248 Go). Incompatible avec l'hôte de "
            "référence 128 Go : doit rester enabled: false."
        ),
    ),
    "gemma-4-26b-a4b": MemoryProfile(
        vram_gb=26.9,
        gguf_size_gb=16.0,
        host_ram_resident_gb=1.5,
        host_ram_transient_gb=16.0,
        basis="reasoned",
        note=(
            "MoE 26B A4B Q4_K_M SANS cpu_moe : tout est offloadé en VRAM, la RAM "
            "hôte n'est traversée qu'au chargement."
        ),
    ),
    "qwen3.5-9b-q5_k_m": MemoryProfile(
        vram_gb=7.0,
        gguf_size_gb=19.0,
        host_ram_resident_gb=16.0,
        host_ram_transient_gb=19.0,
        basis="reasoned",
        note=(
            "MoE 27B Q5_K_M avec cpu_moe: true — attention et embeddings en VRAM "
            "(7 Go), experts FFN résidents en RAM hôte (~15-16 Go)."
        ),
    ),
    "llama-3.1-8b-instruct": MemoryProfile(
        vram_gb=5.5,
        gguf_size_gb=4.9,
        host_ram_resident_gb=0.7,
        host_ram_transient_gb=4.9,
        basis="declared",
        note="Dense Q4_K_M, offload total. Modèle de test, désactivé par défaut.",
    ),
}


# ── Lecture des unités systemd ────────────────────────────────────────────────


def parse_unit(path: Path) -> dict[str, list[str]]:
    """
    Parse minimaliste d'une unité systemd : clé → liste des valeurs déclarées.

    Gère les commentaires (`#`, `;`) et les continuations de ligne (`\\`).
    Plusieurs assignations d'une même clé sont conservées dans l'ordre : pour
    `MemoryMax` la dernière gagne, pour `ReadWritePaths` elles s'accumulent.
    """
    directives: dict[str, list[str]] = {}
    pending = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not pending and (not line or line.startswith("#") or line.startswith(";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            continue
        if line.endswith("\\"):
            pending += line[:-1].rstrip() + " "
            continue
        line = pending + line
        pending = ""
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        directives.setdefault(key.strip(), []).append(value.strip())
    return directives


def last_value(directives: dict[str, list[str]], key: str) -> str | None:
    values = directives.get(key)
    return values[-1] if values else None


_SUFFIXES = {"K": 1 / 1024**2, "M": 1 / 1024, "G": 1.0, "T": 1024.0}


def memory_allowance_gb(value: str, physical_ram_gb: float) -> float | None:
    """
    Convertit une valeur `MemoryHigh=`/`MemoryMax=` en gigaoctets autorisés.

    - `infinity` → None (aucune limite) ;
    - `80%`      → pourcentage de la RAM physique de l'hôte ;
    - `64G`      → valeur absolue.
    """
    value = value.strip()
    if value == "infinity":
        return None
    if value.endswith("%"):
        return physical_ram_gb * float(value[:-1]) / 100.0
    suffix = value[-1].upper()
    if suffix in _SUFFIXES:
        return float(value[:-1]) * _SUFFIXES[suffix]
    # Valeur nue = octets.
    return float(value) / 1024**3


def required_physical_ram_gb(host_ram_gb: float, limit_value: str) -> float:
    """
    RAM physique minimale de l'hôte pour que `host_ram_gb` + baseline tienne
    sous la limite exprimée par `limit_value`.

    Avec une limite en pourcentage la réponse dépend de la fraction ; avec une
    limite absolue, la limite elle-même est le plafond et la RAM physique doit au
    moins l'égaler.
    """
    needed = BASELINE_HOST_RAM_GB + host_ram_gb
    value = limit_value.strip()
    if value == "infinity":
        return needed
    if value.endswith("%"):
        fraction = float(value[:-1]) / 100.0
        return needed / fraction
    absolute = memory_allowance_gb(value, REFERENCE_HOST_RAM_GB)
    assert absolute is not None
    if needed > absolute:
        # La limite absolue est le facteur bloquant : aucune quantité de RAM
        # physique ne rend le modèle compatible sans changer l'unité.
        return math.inf
    return absolute


@pytest.fixture(scope="module")
def registry() -> ModelRegistry:
    return ModelRegistry(MODELS_YAML)


@pytest.fixture(scope="module")
def local_unit() -> dict[str, list[str]]:
    return parse_unit(LOCAL_UNIT)


@pytest.fixture(scope="module")
def agent_unit() -> dict[str, list[str]]:
    return parse_unit(AGENT_UNIT)


@pytest.fixture(scope="module")
def cluster_unit() -> dict[str, list[str]]:
    return parse_unit(CLUSTER_UNIT)


def enabled_models(registry: ModelRegistry) -> list[ModelDefinition]:
    return sorted(registry.list_enabled(), key=lambda m: m.id)


# ── Cohérence table de profils ↔ registre ─────────────────────────────────────


def test_every_registry_model_has_a_memory_profile(registry: ModelRegistry) -> None:
    """Un modèle ajouté au registre sans profil mémoire fait échouer le test."""
    missing = sorted(m.id for m in registry.list_all() if m.id not in MEMORY_PROFILES)
    assert not missing, (
        f"Modèles sans profil mémoire hôte : {missing}. Ajouter une entrée dans "
        f"MEMORY_PROFILES ({Path(__file__).name}) ET dans la table "
        f"« modèle → RAM hôte requise » de docs/deployment.md §15 avant "
        f"d'activer le modèle."
    )


def test_profile_vram_matches_registry(registry: ModelRegistry) -> None:
    """La table de profils ne doit pas dériver du vram_gb du registre."""
    drift = {
        m.id: (m.vram_gb, MEMORY_PROFILES[m.id].vram_gb)
        for m in registry.list_all()
        if m.id in MEMORY_PROFILES
        and abs(m.vram_gb - MEMORY_PROFILES[m.id].vram_gb) > 0.01
    }
    assert not drift, (
        f"vram_gb divergent entre models.yaml et MEMORY_PROFILES {drift}. "
        f"Mettre les deux à jour ensemble (cf. CLAUDE.md : vram_gb et docs "
        f"changent dans le même commit)."
    )


def test_profile_resident_ram_is_consistent_with_cpu_moe(registry: ModelRegistry) -> None:
    """
    Invariant d'architecture : un modèle `cpu_moe: true` garde une part
    substantielle de son GGUF en RAM hôte ; un modèle entièrement offloadé non.
    Protège contre un profil recopié à l'aveugle.
    """
    for model in registry.list_all():
        profile = MEMORY_PROFILES[model.id]
        if model.llama_params.cpu_moe:
            assert profile.host_ram_resident_gb >= 0.5 * profile.gguf_size_gb, (
                f"[{model.id}] cpu_moe: true mais host_ram_resident_gb="
                f"{profile.host_ram_resident_gb} Go pour un GGUF de "
                f"{profile.gguf_size_gb} Go : les experts FFN restent lus depuis "
                f"le mmap à chaque token, ils sont résidents."
            )
        else:
            assert profile.host_ram_resident_gb <= 4.0, (
                f"[{model.id}] offload total attendu (pas de cpu_moe) : la RAM "
                f"hôte résidente devrait rester le surcoût de processus, pas "
                f"{profile.host_ram_resident_gb} Go."
            )


# ── Politique mémoire des unités qui hébergent llama-server ───────────────────


@pytest.mark.parametrize("unit_name", ["local", "agent"])
def test_llama_hosting_units_declare_the_memory_policy(
    unit_name: str,
    local_unit: dict[str, list[str]],
    agent_unit: dict[str, list[str]],
) -> None:
    """
    Les deux unités dans le cgroup desquelles tournent des llama-server (gateway
    en mode local, node-agent en mode cluster) doivent déclarer explicitement
    leur politique mémoire. Une unité muette est le défaut COR-007 dans l'autre
    sens : personne ne sait quel profil elle autorise.
    """
    directives = local_unit if unit_name == "local" else agent_unit

    for key in ("MemoryHigh", "MemoryMax", "MemorySwapMax", "OOMPolicy", "TasksMax"):
        assert last_value(directives, key) is not None, (
            f"Unité {unit_name} : directive {key} absente. Les llama-server "
            f"vivent dans ce cgroup, la politique mémoire doit être explicite."
        )

    assert last_value(directives, "MemorySwapMax") == "0", (
        "MemorySwapMax doit valoir 0 : swapper une allocation anonyme de "
        "llama.cpp coûte une écriture puis une lecture et dégrade durablement le "
        "TTFT, alors que recycler une page mmap propre ne coûte qu'un refault."
    )

    high = memory_allowance_gb(
        last_value(directives, "MemoryHigh") or "infinity", REFERENCE_HOST_RAM_GB
    )
    hard = memory_allowance_gb(
        last_value(directives, "MemoryMax") or "infinity", REFERENCE_HOST_RAM_GB
    )
    if high is not None and hard is not None:
        assert high <= hard, (
            f"Unité {unit_name} : MemoryHigh ({high} Go) doit rester ≤ MemoryMax "
            f"({hard} Go), sinon la pression douce ne précède plus la limite dure."
        )

    tasks = last_value(directives, "TasksMax")
    assert tasks == "infinity" or int(tasks) >= 512, (
        f"Unité {unit_name} : TasksMax={tasks} est trop bas — un llama-server "
        f"consomme ~64 tâches (threads + threads_http + threads CUDA) et "
        f"MAX_LOADED_MODELS vaut 5 par défaut."
    )


def test_local_unit_does_not_grant_memlock(local_unit: dict[str, list[str]]) -> None:
    """
    `build_llama_cmd` ne passe pas `--mlock` : le GGUF est lu via mmap et ses
    pages restent réclamables. Relever LimitMEMLOCK n'est donc justifié par aucun
    besoin de modèle, et le faire rendrait MemoryMax un plafond réellement dur
    (pages non réclamables → OOM-kill certain au lieu du thrashing).
    """
    assert last_value(local_unit, "LimitMEMLOCK") is None, (
        "LimitMEMLOCK a été ajouté à llm-gateway.service. Cela ne se justifie "
        "que si llama-server est lancé avec --mlock, et dans ce cas le modèle "
        "entier doit tenir en RAM : revoir docs/deployment.md §15 d'abord."
    )


# ── Invariant central : profil de modèles ↔ limite mémoire ────────────────────


def test_each_enabled_model_fits_the_local_memory_limit(
    registry: ModelRegistry, local_unit: dict[str, list[str]]
) -> None:
    """
    Critère d'acceptation COR-007 : aucun modèle activé n'est incompatible avec
    la limite mémoire de l'unité locale sur l'hôte de référence.
    """
    limit = last_value(local_unit, "MemoryHigh") or "infinity"
    violations: list[str] = []
    for model in enabled_models(registry):
        profile = MEMORY_PROFILES[model.id]
        needed_ram = required_physical_ram_gb(profile.host_ram_resident_gb, limit)
        if needed_ram > REFERENCE_HOST_RAM_GB:
            violations.append(
                f"{model.id}: working set RAM hôte {profile.host_ram_resident_gb} Go "
                f"(+ {BASELINE_HOST_RAM_GB} Go de baseline) exige un hôte de "
                f"{needed_ram:.0f} Go avec MemoryHigh={limit}, or l'hôte de "
                f"référence a {REFERENCE_HOST_RAM_GB:.0f} Go"
            )
    assert not violations, (
        "Modèle(s) activé(s) incompatibles avec les limites mémoire de "
        f"{LOCAL_UNIT.name} :\n  - " + "\n  - ".join(violations) + "\n"
        "Corriger en désactivant le modèle dans models.yaml, ou en documentant "
        "un hôte de référence plus grand dans docs/deployment.md §15 (et non en "
        "relevant la limite au-delà de la RAM réellement installée)."
    )


def test_enabled_profile_as_a_whole_fits_the_local_memory_limit(
    registry: ModelRegistry, local_unit: dict[str, list[str]]
) -> None:
    """
    Borne haute conservatrice : tous les modèles activés résidents en même temps.
    Le budget VRAM limite en pratique la co-résidence, donc ce total est un
    majorant — s'il passe, aucune combinaison ne peut dépasser la limite.
    """
    limit = last_value(local_unit, "MemoryHigh") or "infinity"
    total_resident = sum(
        MEMORY_PROFILES[m.id].host_ram_resident_gb for m in enabled_models(registry)
    )
    needed_ram = required_physical_ram_gb(total_resident, limit)
    assert needed_ram <= REFERENCE_HOST_RAM_GB, (
        f"Profil activé complet : {total_resident} Go résidents + "
        f"{BASELINE_HOST_RAM_GB} Go de baseline exigent {needed_ram:.0f} Go de RAM "
        f"avec MemoryHigh={limit}, or l'hôte de référence a "
        f"{REFERENCE_HOST_RAM_GB:.0f} Go. Réduire le nombre de modèles activés "
        f"simultanément (surtout ceux en cpu_moe)."
    )


def test_minimax_profile_is_explicitly_handled(registry: ModelRegistry) -> None:
    """
    COR-007 exige que le profil MiniMax soit traité explicitement. Son working
    set (~236 Go d'experts FFN résidents) dépasse la RAM de l'hôte de référence :
    la seule issue correcte est `enabled: false`, pas un MemoryMax relevé.
    """
    minimax = registry.get("minimax-m2.7")
    if minimax is None:
        pytest.skip("minimax-m2.7 retiré du registre — plus rien à arbitrer.")

    profile = MEMORY_PROFILES["minimax-m2.7"]
    assert profile.host_ram_resident_gb > REFERENCE_HOST_RAM_GB, (
        "Le profil MiniMax n'est plus censé dépasser l'hôte de référence : si la "
        "taille du GGUF ou le mode d'offload a changé, revoir la table et la "
        "raison d'être de ce test."
    )
    assert not minimax.enabled, (
        "minimax-m2.7 est activé alors que son working set RAM hôte est de "
        f"~{profile.host_ram_resident_gb:.0f} Go (experts FFN via cpu_moe) pour un "
        f"hôte de référence de {REFERENCE_HOST_RAM_GB:.0f} Go. Ce n'est pas un "
        "problème de VRAM et ce n'est pas réparable en relevant MemoryMax : les "
        "pages mmap seraient réclamées en boucle (thrashing NVMe, TTFT effondré) "
        "y compris au détriment des autres modèles. Activer uniquement sur un "
        "hôte ≥ 320 Go de RAM, en mettant à jour docs/deployment.md §15 et "
        "MEMORY_PROFILES."
    )


# ── Topologie cluster ─────────────────────────────────────────────────────────


def test_each_enabled_model_fits_the_node_agent_memory_limit(
    registry: ModelRegistry, agent_unit: dict[str, list[str]]
) -> None:
    """
    En mode cluster, les llama-server tournent sur les nœuds sous le node-agent :
    c'est la limite de CETTE unité qui doit couvrir le profil de modèles.
    """
    limit = last_value(agent_unit, "MemoryHigh") or "infinity"
    violations = [
        f"{m.id}: {MEMORY_PROFILES[m.id].host_ram_resident_gb} Go résidents → "
        f"{required_physical_ram_gb(MEMORY_PROFILES[m.id].host_ram_resident_gb, limit):.0f} Go de RAM nœud"
        for m in enabled_models(registry)
        if required_physical_ram_gb(
            MEMORY_PROFILES[m.id].host_ram_resident_gb, limit
        )
        > REFERENCE_HOST_RAM_GB
    ]
    assert not violations, (
        f"Modèle(s) activé(s) incompatibles avec {AGENT_UNIT.name} "
        f"(MemoryHigh={limit}) :\n  - " + "\n  - ".join(violations)
    )


def test_cluster_orchestrator_hosts_no_llama_server(
    cluster_unit: dict[str, list[str]]
) -> None:
    """
    L'unité orchestrateur peut garder des limites basses et absolues PARCE QUE
    aucun llama-server ne vit dans son cgroup. Ce test verrouille cette
    hypothèse : si l'orchestrateur gagnait un accès GPU ou aux répertoires de
    modèles, ses limites mémoire devraient être redimensionnées.
    """
    assert last_value(cluster_unit, "PrivateDevices") == "true", (
        "L'orchestrateur cluster doit rester sans accès aux devices : c'est ce "
        "qui garantit qu'aucun llama-server ne tourne dans son cgroup."
    )
    assert "DeviceAllow" not in cluster_unit, (
        "DeviceAllow apparaît dans l'unité orchestrateur : si le GPU devient "
        "accessible, MemoryHigh/MemoryMax=4G/8G ne couvrent plus rien."
    )
    read_write = " ".join(cluster_unit.get("ReadWritePaths", []))
    assert "models" not in read_write, (
        "L'orchestrateur ne doit pas monter de répertoire de modèles en écriture."
    )
    for key in ("MemoryHigh", "MemoryMax"):
        value = last_value(cluster_unit, key)
        assert value is not None and not value.endswith("%"), (
            f"{key} de l'orchestrateur doit rester une valeur absolue et basse : "
            f"sa consommation ne dépend pas de models.yaml."
        )


def test_enabled_model_directories_are_declared_in_the_local_unit(
    registry: ModelRegistry, local_unit: dict[str, list[str]]
) -> None:
    """
    `ProtectSystem=strict` + `ReadWritePaths` : tout répertoire de modèles utilisé
    par un modèle activé doit apparaître dans l'unité, préfixé `-` s'il peut être
    absent (un ReadWritePaths non préfixé vers un chemin inexistant fait échouer
    le démarrage de l'unité). Force la revue de l'unité quand un modèle est
    déclaré dans un nouvel emplacement.
    """
    declared: set[str] = set()
    for entry in local_unit.get("ReadWritePaths", []):
        for token in entry.split():
            declared.add(token.lstrip("-+!:"))

    missing = {
        m.id: str(m.path.parent)
        for m in enabled_models(registry)
        if not any(
            str(m.path).startswith(path.rstrip("/") + "/") for path in declared
        )
    }
    assert not missing, (
        f"Répertoires de modèles absents de ReadWritePaths de {LOCAL_UNIT.name} : "
        f"{missing}. Ajouter le répertoire (préfixé `-` s'il n'est pas créé par "
        f"install.sh) et documenter le chemin dans docs/deployment.md §15."
    )
