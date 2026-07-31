"""
CLI d'administration — utilisation en ligne de commande sur le serveur.

Usage :
  python cli.py add-user alice --email alice@univ-pau.fr --rpm 30
  python cli.py create-key alice --name "these-2025"
  python cli.py revoke-key llmgw-abc12345
  python cli.py list-users
  python cli.py anonymize-user alice --yes   # RGPD, irréversible
  python cli.py usage-report --month 2025-03
  python cli.py usage-report --from 2025-01-01 --to 2025-03-31
  python cli.py status
"""
from __future__ import annotations

import asyncio
import sys
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

import database as db
from config import settings
from model_registry import ModelRegistry

app = typer.Typer(
    name="llm-gateway",
    help="CLI d'administration du LLM Inference Gateway UPPA",
    no_args_is_help=True,
)
console = Console()


def _run(coro):
    """Helper pour exécuter un coroutine depuis le CLI synchrone."""
    return asyncio.run(coro)


# ── Bootstrap DB ──────────────────────────────────────────────────────────────

async def _ensure_db():
    await db.init_db()


# ── Utilisateurs ──────────────────────────────────────────────────────────────

@app.command("add-user")
def add_user(
    username: str = typer.Argument(..., help="Nom d'utilisateur (alphanumérique)"),
    email: Optional[str] = typer.Option(None, "--email", "-e", help="Email institutionnel"),
    rpm: Optional[int] = typer.Option(None, "--rpm", help="Limite req/minute (défaut: config)"),
    monthly_tokens: Optional[int] = typer.Option(None, "--monthly-tokens", help="Quota tokens/mois (0=illimité)"),
    notes: Optional[str] = typer.Option(None, "--notes", help="Notes libres"),
):
    """Crée un nouvel utilisateur."""
    async def _do():
        await _ensure_db()
        try:
            user = await db.create_user(
                username=username,
                email=email,
                rpm_limit=rpm,
                monthly_token_limit=monthly_tokens,
                notes=notes,
            )
            console.print(f"[green]Utilisateur créé :[/green] {user['username']} (ID: {user['id']})")
            console.print(f"  RPM limit     : {user['rpm_limit']}")
            console.print(f"  Token quota   : {user['monthly_token_limit'] or 'illimité'}")
        except Exception as exc:
            if "UNIQUE" in str(exc):
                console.print(f"[red]Erreur :[/red] Un utilisateur '{username}' existe déjà.")
                raise typer.Exit(1)
            raise

    _run(_do())


@app.command("list-users")
def list_users(
    show_inactive: bool = typer.Option(False, "--all", "-a", help="Inclure les utilisateurs désactivés"),
):
    """Liste tous les utilisateurs."""
    async def _do():
        await _ensure_db()
        users = await db.list_users()
        if not show_inactive:
            users = [u for u in users if u["is_active"]]

        table = Table(title="Utilisateurs", show_header=True, header_style="bold cyan")
        table.add_column("ID", style="dim", width=4)
        table.add_column("Username", min_width=16)
        table.add_column("Email", min_width=24)
        table.add_column("Actif", width=6)
        table.add_column("RPM", width=5)
        table.add_column("Créé le", width=12)

        for u in users:
            active = "[green]oui[/green]" if u["is_active"] else "[red]non[/red]"
            table.add_row(
                str(u["id"]),
                u["username"],
                u["email"] or "—",
                active,
                str(u["rpm_limit"]),
                u["created_at"][:10],
            )

        console.print(table)

    _run(_do())


@app.command("disable-user")
def disable_user(username: str = typer.Argument(...)):
    """Désactive un utilisateur (toutes ses clés deviennent invalides immédiatement)."""
    async def _do():
        await _ensure_db()
        user = await db.get_user_by_username(username)
        if not user:
            console.print(f"[red]Utilisateur '{username}' introuvable.[/red]")
            raise typer.Exit(1)
        await db.update_user(user["id"], is_active=False)
        console.print(f"[yellow]Utilisateur '{username}' désactivé.[/yellow]")

    _run(_do())


@app.command("enable-user")
def enable_user(username: str = typer.Argument(...)):
    """Réactive un utilisateur désactivé."""
    async def _do():
        await _ensure_db()
        user = await db.get_user_by_username(username)
        if not user:
            console.print(f"[red]Utilisateur '{username}' introuvable.[/red]")
            raise typer.Exit(1)
        await db.update_user(user["id"], is_active=True)
        console.print(f"[green]Utilisateur '{username}' réactivé.[/green]")

    _run(_do())


@app.command("anonymize-user")
def anonymize_user(
    username: str = typer.Argument(..., help="Nom d'utilisateur à anonymiser"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirmer sans demander"),
):
    """
    Anonymise un utilisateur — IRRÉVERSIBLE. Droit à l'effacement RGPD.

    Efface définitivement le nom d'utilisateur, l'e-mail, les notes et le nom
    des clés API ; désactive le compte et révoque toutes ses clés.
    CONSERVE la ligne utilisateur et tout l'historique usage_log, pour que la
    facturation et l'audit restent exploitables (politique DEC-001).

    Ce n'est PAS une suppression de ligne : l'historique d'usage reste agrégeable
    sous un pseudonyme, la personne n'est plus ré-identifiable.
    """
    async def _do():
        await _ensure_db()
        user = await db.get_user_by_username(username)
        if not user:
            console.print(f"[red]Utilisateur '{username}' introuvable.[/red]")
            raise typer.Exit(1)

        if not yes:
            console.print(
                "[yellow]Anonymisation irréversible :[/yellow] nom, e-mail, notes et "
                "noms de clés seront effacés définitivement ; les clés seront "
                "révoquées. L'historique d'usage est conservé sous un pseudonyme."
            )
            if not typer.confirm(f"Anonymiser '{username}' ?", default=False):
                console.print("Annulé.")
                return

        result = await db.anonymize_user(user["id"])
        if result is None:
            console.print(f"[red]Utilisateur '{username}' introuvable.[/red]")
            raise typer.Exit(1)

        if result["already_anonymized"]:
            console.print(
                f"[yellow]Déjà anonymisé le {result['anonymized_at']}[/yellow] — "
                "horodatage initial conservé, clés revérifiées."
            )
        else:
            console.print("[green]Utilisateur anonymisé.[/green]")
        console.print(f"  Pseudonyme      : {result['username']}")
        console.print(f"  Anonymisé le    : {result['anonymized_at']}")
        console.print(f"  Clés révoquées  : {result['keys_revoked']} / {result['keys_total']}")
        console.print("  Effacé          : username, email, notes, nom des clés")
        console.print("  Conservé        : id, created_at, journal usage_log")

    _run(_do())


# ── Clés API ──────────────────────────────────────────────────────────────────

@app.command("create-key")
def create_key(
    username: str = typer.Argument(..., help="Nom de l'utilisateur"),
    name: Optional[str] = typer.Option(None, "--name", "-n", help="Nom de la clé (ex: 'these-2025')"),
    expires: Optional[str] = typer.Option(None, "--expires", help="Date d'expiration ISO 8601"),
):
    """
    Génère une nouvelle clé API pour un utilisateur.
    La clé brute est affichée UNE SEULE FOIS — copiez-la maintenant.
    """
    async def _do():
        await _ensure_db()
        user = await db.get_user_by_username(username)
        if not user:
            console.print(f"[red]Utilisateur '{username}' introuvable.[/red]")
            raise typer.Exit(1)

        raw_key, key_row = await db.create_api_key(
            user_id=user["id"],
            name=name,
            expires_at=expires,
        )

        console.print()
        console.print("[bold green]Clé API créée avec succès[/bold green]")
        console.print(f"  Utilisateur : [cyan]{username}[/cyan]")
        console.print(f"  Nom         : {key_row['name'] or '—'}")
        console.print(f"  Préfixe     : {key_row['key_prefix']}")
        console.print(f"  Expire le   : {key_row['expires_at'] or 'jamais'}")
        console.print()
        console.print("[bold yellow]CLEF API (à copier maintenant — non récupérable) :[/bold yellow]")
        console.print(f"  [bold white]{raw_key}[/bold white]")
        console.print()

    _run(_do())


@app.command("list-keys")
def list_keys(username: str = typer.Argument(...)):
    """Liste les clés API d'un utilisateur."""
    async def _do():
        await _ensure_db()
        user = await db.get_user_by_username(username)
        if not user:
            console.print(f"[red]Utilisateur '{username}' introuvable.[/red]")
            raise typer.Exit(1)

        keys = await db.list_keys_for_user(user["id"])
        if not keys:
            console.print(f"Aucune clé pour '{username}'.")
            return

        table = Table(title=f"Clés API — {username}", header_style="bold cyan")
        table.add_column("Préfixe", width=16)
        table.add_column("Nom", min_width=14)
        table.add_column("Active", width=7)
        table.add_column("Dernière utilisation", width=20)
        table.add_column("Expire le", width=12)

        for k in keys:
            active = "[green]oui[/green]" if k["is_active"] else "[red]non[/red]"
            table.add_row(
                k["key_prefix"],
                k["name"] or "—",
                active,
                k["last_used"] or "jamais",
                k["expires_at"] or "jamais",
            )

        console.print(table)

    _run(_do())


@app.command("revoke-key")
def revoke_key(
    key_prefix: str = typer.Argument(..., help="Préfixe de la clé (ex: llmgw-abc12345)"),
):
    """Révoque une clé API. Immédiatement effectif."""
    async def _do():
        await _ensure_db()
        ok = await db.revoke_key(key_prefix)
        if ok:
            console.print(f"[yellow]Clé '{key_prefix}' révoquée.[/yellow]")
        else:
            console.print(f"[red]Aucune clé active avec le préfixe '{key_prefix}'.[/red]")
            raise typer.Exit(1)

    _run(_do())


# ── Rapports d'usage ──────────────────────────────────────────────────────────

@app.command("usage-report")
def usage_report(
    username: Optional[str] = typer.Option(None, "--user", "-u", help="Filtrer par utilisateur"),
    from_date: Optional[str] = typer.Option(None, "--from", help="Date début (ex: 2025-01-01)"),
    to_date: Optional[str] = typer.Option(None, "--to", help="Date fin (ex: 2025-12-31)"),
    month: Optional[str] = typer.Option(None, "--month", "-m", help="Mois YYYY-MM (ex: 2025-03)"),
    summary: bool = typer.Option(False, "--summary", "-s", help="Vue agrégée par utilisateur"),
):
    """Rapport d'usage (tokens consommés, requêtes, etc.)."""
    async def _do():
        await _ensure_db()

        # Convertir --month en --from/--to
        _from = from_date
        _to = to_date
        if month:
            import calendar
            year, mon = int(month.split("-")[0]), int(month.split("-")[1])
            last_day = calendar.monthrange(year, mon)[1]
            _from = f"{year:04d}-{mon:02d}-01"
            _to = f"{year:04d}-{mon:02d}-{last_day:02d}"

        if summary:
            rows = await db.get_usage_summary(from_date=_from, to_date=_to)
            table = Table(title="Résumé d'usage", header_style="bold cyan")
            table.add_column("Utilisateur", min_width=16)
            table.add_column("Requêtes", width=10, justify="right")
            table.add_column("Tokens prompt", width=14, justify="right")
            table.add_column("Tokens réponse", width=15, justify="right")
            table.add_column("Total tokens", width=13, justify="right")
            table.add_column("Durée moy. (ms)", width=16, justify="right")
            table.add_column("Dernière req.", width=14)

            for r in rows:
                avg = f"{r['avg_duration_ms']:.0f}" if r["avg_duration_ms"] else "—"
                table.add_row(
                    r["username"],
                    str(r["request_count"]),
                    f"{r['total_prompt_tokens']:,}",
                    f"{r['total_completion_tokens']:,}",
                    f"{r['total_tokens']:,}",
                    avg,
                    (r["last_request"] or "—")[:16],
                )
            console.print(table)
        else:
            user_id = None
            if username:
                user = await db.get_user_by_username(username)
                if not user:
                    console.print(f"[red]Utilisateur '{username}' introuvable.[/red]")
                    raise typer.Exit(1)
                user_id = user["id"]

            rows = await db.get_usage_report(
                user_id=user_id,
                from_date=_from,
                to_date=_to,
                limit=500,
            )

            table = Table(title="Journal d'usage", header_style="bold cyan")
            table.add_column("Date", width=18)
            table.add_column("Utilisateur", min_width=14)
            table.add_column("Modèle", min_width=16)
            table.add_column("Prompt", width=8, justify="right")
            table.add_column("Réponse", width=8, justify="right")
            table.add_column("Total", width=8, justify="right")
            table.add_column("ms", width=6, justify="right")
            table.add_column("HTTP", width=4, justify="right")

            for r in rows:
                table.add_row(
                    r["timestamp"][:16],
                    r["username"],
                    r["model"],
                    str(r["prompt_tokens"]),
                    str(r["completion_tokens"]),
                    str(r["total_tokens"]),
                    str(r["duration_ms"] or "—"),
                    str(r["status_code"] or "—"),
                )
            console.print(table)
            console.print(f"  {len(rows)} entrée(s)")

    _run(_do())


# ── Rétention / purge ─────────────────────────────────────────────────────────

@app.command("purge-usage")
def purge_usage(
    older_than_days: int = typer.Option(
        ..., "--older-than-days", help="Supprime les entrées usage_log plus anciennes que N jours"
    ),
):
    """
    Purge de rétention MANUELLE du journal d'usage puis VACUUM.
    À exécuter hors ligne : VACUUM verrouille la base.
    """
    if older_than_days < 0:
        console.print("[red]--older-than-days doit être >= 0.[/red]")
        raise typer.Exit(1)

    async def _do():
        await _ensure_db()
        deleted = await db.purge_usage_older_than(older_than_days)
        console.print(
            f"[green]Purge terminée :[/green] {deleted} entrée(s) usage_log "
            f"supprimée(s) (> {older_than_days} jours)."
        )

    _run(_do())


# ── Statut ────────────────────────────────────────────────────────────────────

@app.command("status")
def status():
    """Affiche le registre des modèles et la configuration VRAM."""
    try:
        registry = ModelRegistry(
            config_path=settings.models_config_path,
            allowed_model_dirs=settings.allowed_model_dirs if settings.allowed_model_dirs else None,
        )
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]Erreur registre :[/red] {exc}")
        raise typer.Exit(1)

    budget = settings.effective_vram_budget_gb()
    console.print()
    console.print("[bold cyan]Configuration VRAM[/bold cyan]")
    console.print(f"  Total GPU       : {settings.total_vram_gb:.1f} GB")
    console.print(f"  Overhead        : {settings.vram_overhead_gb:.1f} GB")
    console.print(f"  Marge sécurité  : {settings.vram_safety_margin * 100:.0f}%")
    console.print(f"  Budget net      : [bold green]{budget:.1f} GB[/bold green]")
    console.print(f"  Max modèles     : {settings.max_loaded_models}")
    console.print(f"  Pool de ports   : {settings.base_llama_port}–{settings.base_llama_port + settings.max_loaded_models - 1}")
    console.print(f"  Idle timeout    : {settings.idle_timeout_seconds}s")
    console.print()

    models = registry.list_all()
    table = Table(title="Registre des modèles", header_style="bold cyan")
    table.add_column("ID", min_width=20)
    table.add_column("VRAM", width=8, justify="right")
    table.add_column("Activé", width=8)
    table.add_column("Capacités", min_width=24)
    table.add_column("Chemin", min_width=30)

    for m in models:
        enabled = "[green]oui[/green]" if m.enabled else "[red]non[/red]"
        caps = ", ".join(m.capabilities)
        table.add_row(m.id, f"{m.vram_gb:.1f} GB", enabled, caps, str(m.path))

    console.print(table)
    console.print()
    console.print("[dim]Note : L'état live (READY/LOADING) n'est visible que via GET /admin/status[/dim]")
    console.print()


# ── Diagnostic préflight (AUT-012) ────────────────────────────────────────────

@app.command("doctor")
def doctor(
    json_output: bool = typer.Option(
        False, "--json", help="Rapport JSON (schéma stable) au lieu du texte"
    ),
    env_file: Optional[str] = typer.Option(
        None, "--env-file",
        help="EnvironmentFile à valider (défaut : EnvironmentFile= de l'unité systemd, sinon /etc/llm-gateway/env)",
    ),
    nginx_conf: Optional[str] = typer.Option(
        None, "--nginx-conf",
        help="Configuration nginx à contrôler (défaut : /etc/nginx/sites-available/llm-gateway)",
    ),
    systemd_unit: Optional[str] = typer.Option(
        None, "--systemd-unit",
        help="Unité systemd à contrôler (défaut : /etc/systemd/system/llm-gateway.service)",
    ),
    verify_hashes: bool = typer.Option(
        False, "--verify-hashes",
        help="Vérifie les empreintes SHA-256 des GGUF — COÛTEUX (lecture intégrale, plusieurs centaines de Go)",
    ),
    strict: bool = typer.Option(
        False, "--strict", help="Traite les avertissements comme des échecs bloquants"
    ),
):
    """
    Diagnostic préflight de l'hôte et de la configuration (aucun service requis).

    Exit codes : 0 conforme, 1 échec bloquant, 3 avertissements seulement,
    4 erreur interne de doctor (2 est réservé aux erreurs d'usage de la CLI).
    Aucun secret n'apparaît dans le rapport. Détails : docs/admin.md.
    """
    from pathlib import Path as _Path

    import doctor as doctor_module

    try:
        options = doctor_module.DoctorOptions(
            env_file=_Path(env_file) if env_file else None,
            nginx_conf=_Path(nginx_conf) if nginx_conf else None,
            systemd_unit=_Path(systemd_unit) if systemd_unit else None,
            verify_hashes=verify_hashes,
        )
        report = _run(doctor_module.run_doctor(options))
    except Exception as exc:
        console.print(f"[red]doctor a échoué :[/red] {type(exc).__name__}: {exc}")
        raise typer.Exit(doctor_module.EXIT_ERROR)

    if json_output:
        # Écriture brute : le rendu rich reformaterait le document JSON.
        sys.stdout.write(doctor_module.render_json(report, strict=strict) + "\n")
    else:
        console.print(
            doctor_module.render_human(report, strict=strict),
            markup=False,
            highlight=False,
            soft_wrap=True,
        )

    raise typer.Exit(report.exit_code(strict=strict))


# ── Planificateur de bootstrap (AUT-001 → AUT-005, AUT-013) ───────────────────

@app.command("bootstrap-plan")
def bootstrap_plan(
    json_output: bool = typer.Option(
        False, "--json", help="Plan JSON (schéma versionné) au lieu du texte"
    ),
    mode: str = typer.Option("local", "--mode", help="Topologie visée : local ou cluster"),
    catalog: Optional[str] = typer.Option(
        None, "--catalog", help="Catalogue de modèles approuvés (défaut : celui du dépôt)"
    ),
    hardware_profile: Optional[str] = typer.Option(
        None, "--hardware-profile",
        help="Profil matériel DÉCLARÉ (JSON §5) au lieu de sonder — VM, passthrough, hôte où les outils constructeur échouent",
    ),
    models_dir: Optional[str] = typer.Option(
        None, "--models-dir", help="Volume où atterriraient les GGUF (défaut : /models)"
    ),
    model: Optional[list[str]] = typer.Option(
        None, "--model", help="Restreindre à ces identifiants de catalogue (répétable)"
    ),
    max_models: int = typer.Option(1, "--max-models", help="Nombre maximal de modèles retenus"),
    llama_bin: Optional[str] = typer.Option(
        None, "--llama-bin", help="Binaire llama-server déjà en place, à évaluer (`--version` sera exécuté)"
    ),
    pin_version: Optional[str] = typer.Option(
        None, "--pin-version", help="Version llama.cpp épinglée, au format « bNNNNN »"
    ),
    pin_commit: Optional[str] = typer.Option(
        None, "--pin-commit", help="Commit git correspondant à --pin-version"
    ),
    min_build: int = typer.Option(
        0, "--min-build",
        help="Premier build patché connu — plancher de sécurité d'où LLAMA_SERVER_MIN_BUILD est généré",
    ),
    strict: bool = typer.Option(
        False, "--strict", help="Traite les avertissements comme des blocages"
    ),
):
    """
    Calcule le plan d'amorçage de cet hôte — SANS RIEN APPLIQUER.

    Inventorie le matériel, résout le runtime llama-server, consulte LLMfit s'il
    est présent, filtre le catalogue de modèles approuvés et rend la séquence
    d'étapes qu'une application exécuterait, avec ses décisions et leurs raisons.
    Aucun téléchargement, aucune compilation, aucune écriture : le seul
    sous-processus possible est `llama-server --version`, et seulement si
    `--llama-bin` est fourni.

    Sans `--pin-version`/`--pin-commit`, le runtime ne peut pas être résolu et le
    plan sort bloqué : le planificateur refuse d'inventer un numéro de build, qui
    se propagerait dans les manifestes de provenance avec l'apparence d'un fait.

    Exit codes : 0 applicable, 1 bloqué, 3 avertissements seulement,
    4 erreur interne (2 est réservé aux erreurs d'usage de la CLI).
    """
    from datetime import datetime, timezone
    from pathlib import Path as _Path

    from bootstrap import planner as planner_module
    from bootstrap import runtime_resolver as _runtime
    from bootstrap import schema as _schema

    if mode not in ("local", "cluster"):
        console.print(f"[red]--mode doit valoir « local » ou « cluster », reçu {mode!r}.[/red]")
        raise typer.Exit(_schema.EXIT_USAGE)
    if (pin_version is None) != (pin_commit is None):
        console.print("[red]--pin-version et --pin-commit vont ensemble : l'un sans l'autre n'épingle rien.[/red]")
        raise typer.Exit(_schema.EXIT_USAGE)

    release = None
    if pin_version is not None:
        try:
            release = _runtime.ReleasePolicy(
                pinned_version=pin_version,
                pinned_commit=pin_commit or "",
                security_floor_build=min_build,
            )
        except _runtime.ProvenanceError as exc:
            console.print(f"[red]Politique de release refusée :[/red] {exc}")
            raise typer.Exit(_schema.EXIT_USAGE)

    options = planner_module.PlannerOptions(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        mode=mode,
        catalog_path=_Path(catalog) if catalog else None,
        hardware_profile_path=_Path(hardware_profile) if hardware_profile else None,
        models_dir=_Path(models_dir) if models_dir else planner_module.PlannerOptions.models_dir,
        selected_ids=tuple(model) if model else None,
        max_models=max_models,
        existing_binary=_Path(llama_bin) if llama_bin else None,
        release_policy=release,
    )

    try:
        plan = _run(planner_module.build_plan(options))
        rendered = (
            _schema.render_json(plan, strict=strict) if json_output
            else _schema.render_human(plan, strict=strict)
        )
    except planner_module.PlannerUsageError as exc:
        # Faute de saisie de l'opérateur — pas une panne du planificateur.
        console.print(f"[red]Demande impossible à honorer :[/red] {exc}")
        raise typer.Exit(_schema.EXIT_USAGE)
    except _schema.PlanError as exc:
        console.print(f"[red]Plan refusé :[/red] {exc}")
        raise typer.Exit(_schema.EXIT_ERROR)
    except Exception as exc:
        console.print(f"[red]bootstrap-plan a échoué :[/red] {type(exc).__name__}: {exc}")
        raise typer.Exit(_schema.EXIT_ERROR)

    if json_output:
        # Écriture brute : le rendu rich reformaterait le document JSON.
        sys.stdout.write(rendered + "\n")
    else:
        console.print(rendered, markup=False, highlight=False, soft_wrap=True)

    raise typer.Exit(plan.exit_code(strict=strict))


if __name__ == "__main__":
    app()
