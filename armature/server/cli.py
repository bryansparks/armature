"""`armature server` commands: init, migrate, create-admin, run, export, pack, runs, backfill-locations, and
import-ops."""
from __future__ import annotations

import getpass
from pathlib import Path

import typer

server_app = typer.Typer(name="server", help="Run and administer the Armature server (needs armature\\[server])",
                         no_args_is_help=True)

pack_app = typer.Typer(name="pack", help="Install and remove workflow packs on this host", no_args_is_help=True)
server_app.add_typer(pack_app, name="pack")
runs_app = typer.Typer(name="runs", help="Maintain recorded runs", no_args_is_help=True)
server_app.add_typer(runs_app, name="runs")

ConfigOption = typer.Option(None, "--config", help="Server config file (default $ARMATURE_SERVER_CONFIG "
                                                   "or ~/.armature/server.yaml)")


def _fail(message: str) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(1)


def _load(config: Path | None):
    from pydantic import ValidationError

    from armature.server.config import load_config
    try:
        return load_config(config)
    except (ValidationError, ValueError) as exc:
        _fail(f"Invalid server config: {exc}")


def _keyring(cfg):
    from armature.server.crypto import MasterKeyError, keyring_from_source
    try:
        return keyring_from_source(cfg.master_key)
    except (MasterKeyError, NotImplementedError) as exc:
        _fail(f"Master key unavailable: {exc}")


def _prompt_password() -> str:
    from armature.server.passwords import check_password_policy
    while True:
        password = getpass.getpass("Password: ")
        try:
            check_password_policy(password)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            continue
        if getpass.getpass("Repeat password: ") == password:
            return password
        typer.echo("Passwords differ.", err=True)


CONFIRM_TRIES = 3


def _create_admin(cfg, username: str | None) -> None:
    from armature.server.db import make_engine
    from armature.server.users import create_admin
    keyring = _keyring(cfg)
    username = username or typer.prompt("Admin username")
    password = _prompt_password()
    engine = make_engine(cfg.database_url)
    try:
        try:
            with engine.begin() as conn:
                admin = create_admin(conn, keyring, username=username, password=password,
                                     require_totp=cfg.require_totp)
        except ValueError as exc:
            _fail(str(exc))
        if admin.otpauth_uri is None:
            typer.echo(f"Created admin {username}.")
            return
        typer.echo(f"Created admin {username}. Add this to an authenticator app now. It is not shown again:")
        typer.echo(admin.otpauth_uri)
        confirmed = False
        try:
            confirmed = _confirm_admin(engine, keyring, admin.user_id)
        finally:
            # Any way out without a confirmed code, Ctrl-C included, removes the admin it made.
            if not confirmed:
                _remove_unconfirmed(engine, admin.user_id)
        if not confirmed:
            _fail("No code matched, so the admin was removed. Run the command again.")
    finally:
        engine.dispose()
    typer.echo("Authenticator confirmed.")


def _confirm_admin(engine, keyring, user_id: str) -> bool:
    """Ask for codes until one confirms the authenticator. Returns False after three misses."""
    from sqlalchemy import select

    from armature.server import audit
    from armature.server.auth import confirm_enrollment, match_totp, totp_secret
    from armature.server.db import utcnow
    from armature.server.db.schema import users
    for _ in range(CONFIRM_TRIES):
        code = typer.prompt("Code from the authenticator app")
        now = utcnow()
        with engine.begin() as conn:
            user = conn.execute(select(users).where(users.c.id == user_id)).one()
            step = match_totp(totp_secret(keyring, user), code, now)
            if step is not None and confirm_enrollment(conn, user_id, step, now):
                audit.record(conn, action="user.totp_confirm", actor_id=user_id, target_kind="user", target_id=user_id)
                return True
        typer.echo("That code did not match.", err=True)
    return False


def _remove_unconfirmed(engine, user_id: str) -> None:
    from sqlalchemy import delete

    from armature.server import audit
    from armature.server.db.schema import users
    with engine.begin() as conn:
        conn.execute(delete(users).where(users.c.id == user_id, users.c.totp_confirmed_at.is_(None)))
        audit.record(conn, action="user.delete_unconfirmed", actor_id=None, target_kind="user", target_id=user_id)


@server_app.command("init")
def init(
    config: Path | None = ConfigOption,
    database_url: str | None = typer.Option(None, "--database-url",
                                            help="SQLAlchemy URL, used only when writing a new config. "
                                                 "Prefer $ARMATURE_SERVER_DATABASE_URL for a URL with a password"),
    key_file: Path | None = typer.Option(None, "--key-file",
                                         help="Master key file to create, used only when writing a new config"),
    username: str | None = typer.Option(None, "--username", help="First admin's username (prompted if omitted)"),
    no_admin: bool = typer.Option(False, "--no-admin", help="Skip creating the first admin"),
):
    """Write a config if missing, create the master key, migrate, and create the first admin."""
    from pydantic import ValidationError
    from sqlalchemy.engine import make_url

    from armature.server.config import ENV_PREFIX, config_path
    from armature.server.crypto import MasterKeyError
    from armature.server.setup import SetupError, prepare

    path = (config or config_path()).expanduser()
    if not path.exists() and database_url and make_url(database_url).password:
        typer.echo(f"Warning: --database-url holds a password, which shell history and ps can show. "
                   f"Set {ENV_PREFIX}DATABASE_URL instead.", err=True)
    try:
        prepared = prepare(path, database_url=database_url, key_file=key_file)
    except (ValidationError, ValueError) as exc:
        _fail(f"Invalid server config: {exc}")
    except SetupError as exc:
        _fail(str(exc))
    except (MasterKeyError, NotImplementedError) as exc:
        _fail(f"Master key unavailable: {exc}")
    if prepared.wrote_config:
        typer.echo(f"Wrote {path}")
    typer.echo("Database is at the latest schema.")
    if prepared.new_key:
        typer.echo(f"Generated master key {prepared.new_key} (mode 0600). Back it up: without it no secret can be read.")
    cfg, exists = prepared.cfg, prepared.has_admin
    if no_admin:
        return
    if exists:
        typer.echo("An admin already exists. Use `armature server create-admin` to add another.")
        return
    _create_admin(cfg, username)


@server_app.command("migrate")
def migrate_cmd(config: Path | None = ConfigOption):
    """Upgrade the database to the latest schema."""
    from armature.server.db import migrate
    cfg = _load(config)
    migrate(cfg.database_url)
    typer.echo("Database is at the latest schema.")


@server_app.command("create-admin")
def create_admin_cmd(
    config: Path | None = ConfigOption,
    username: str | None = typer.Option(None, "--username", help="Admin username (prompted if omitted)"),
):
    """Create an admin. The password is always prompted, never taken from arguments."""
    _create_admin(_load(config), username)


def _interactive() -> bool:
    import sys
    return sys.stdin.isatty()


def _serve_setup(path: Path, cfg):
    """Serve the setup page on loopback until an admin exists. Returns the config to serve next, or None when done."""
    import secrets

    import uvicorn

    from armature.server.app import create_app
    from armature.server.app.routes_setup import SETUP_HOST, Swappable, can_swap, create_setup_app, setup_origin
    from armature.server.crypto import keyring_from_source
    from armature.server.db import make_engine

    # A service manager's log is no place for a link that makes an admin.
    if not _interactive():
        _fail("No admin exists yet. Run `armature server run` in a terminal to set up.")
    origin = setup_origin(cfg)
    token = secrets.token_urlsafe(32)
    after: dict = {}

    async def finish(ready) -> bool:
        if can_swap(ready, origin):
            await holder.swap(create_app(ready, make_engine(ready.database_url), keyring_from_source(ready.master_key)))
            return True
        after["cfg"] = ready
        server.should_exit = True
        return False

    holder = Swappable(create_setup_app(path, origin=origin, token=token, finish=finish, base_path=cfg.base_path))
    server = uvicorn.Server(uvicorn.Config(holder, host=SETUP_HOST, port=cfg.port, workers=1, proxy_headers=False,
                                           server_header=False, log_level="warning", timeout_graceful_shutdown=5))
    typer.echo("Armature needs a first admin. Open this link on this computer to set it up. It works once:", err=True)
    typer.echo(f"  {origin}{cfg.base_path}/setup#{token}", err=True)
    server.run()
    return after.get("cfg")


@server_app.command("run")
def run(config: Path | None = ConfigOption):
    """Serve the API and the browser UI. With no admin yet, serve the setup page on loopback first."""
    from sqlalchemy.exc import SQLAlchemyError

    from armature.server.config import config_path
    from armature.server.db import make_engine
    from armature.server.setup import needs_setup
    from armature.server.users import has_admin
    path = (config or config_path()).expanduser()
    cfg = _load(path)
    try:
        setup = needs_setup(cfg)
    except SQLAlchemyError as exc:
        _fail(f"Database unavailable ({exc.__class__.__name__}). Check database_url in {path}.")
    if setup and cfg.require_totp:
        _fail("No admin exists. Run `armature server create-admin` first.")
    if setup:
        cfg = _serve_setup(path, cfg)
        if cfg is None:
            return
    keyring = _keyring(cfg)
    engine = make_engine(cfg.database_url)
    try:
        with engine.connect() as conn:
            exists = has_admin(conn, cfg.require_totp)
    except SQLAlchemyError as exc:
        engine.dispose()
        _fail(f"Database unavailable or not migrated ({exc.__class__.__name__}). Run `armature server migrate`.")
    if not exists:
        engine.dispose()
        _fail("No admin exists. Run `armature server create-admin` first.")
    import uvicorn

    from armature.server.app import create_app
    app = create_app(cfg, engine, keyring)
    typer.echo(f"Armature server on http://{cfg.host}:{cfg.port}{cfg.base_path}/ (public origin {cfg.public_origin})")
    # proxy_headers off: the app trusts X-Real-IP only from a loopback peer and never X-Forwarded-For.
    uvicorn.run(app, host=cfg.host, port=cfg.port, workers=1, proxy_headers=False, server_header=False,
                log_level="info", timeout_graceful_shutdown=5)


@server_app.command("export")
def export_cmd(
    dest: Path = typer.Argument(..., help="Directory to write. An earlier export there is replaced whole"),
    config: Path | None = ConfigOption,
    team: str | None = typer.Option(None, "--team", help="Only this team's definitions, by slug, and the instance's"),
):
    """Write workflows, roles, prompts, missions, teams, settings, and providers to files. Never secrets."""
    from armature.server.db import make_engine
    from armature.server.export import ExportRefused, export_definitions

    cfg = _load(config)
    engine = make_engine(cfg.database_url)
    try:
        with engine.connect() as conn:
            manifest = export_definitions(conn, dest, team=team)
    except (ExportRefused, ValueError) as exc:
        _fail(f"Export refused: {exc}")
    finally:
        engine.dispose()
    typer.echo(f"Exported {len(manifest['objects'])} definitions and {len(manifest['files'])} files to {dest}.")


@server_app.command("backfill-locations")
def backfill_locations_cmd(config: Path | None = ConfigOption):
    """Record where each run without a location executed, from its job and step outputs. Safe to rerun."""
    from armature.server.db import make_engine
    from armature.server.locations import backfill

    cfg = _load(config)
    engine = make_engine(cfg.database_url)
    try:
        counts = backfill(engine)
    finally:
        engine.dispose()
    total = sum(counts.values())
    detail = ", ".join(f"{n} {kind}" for kind, n in sorted(counts.items()))
    typer.echo(f"Located {total} run{'' if total == 1 else 's'}{f' ({detail})' if detail else ''}.")


@pack_app.command("install")
def pack_install(
    source: str = typer.Argument(..., help="A pack directory, or <git-url-or-repo-path>@<40-character commit sha>"),
    config: Path | None = ConfigOption,
    force: bool = typer.Option(False, "--force", help="Install over a newer installed version"),
):
    """Copy a pack into packs_dir, record it in the database, and audit it."""
    from armature.server.db import make_engine, utcnow
    from armature.server.packs import InstallError, install

    cfg = _load(config)
    engine = make_engine(cfg.database_url)
    try:
        installed = install(engine, Path(cfg.packs_dir), source, now=utcnow(), force=force)
    except InstallError as exc:
        _fail(f"Install refused, nothing changed: {exc}")
    finally:
        engine.dispose()
    was = f" (was {installed.previous_version})" if installed.previous_version else ""
    typer.echo(f"Installed {installed.name} {installed.version}{was} at {installed.path}, "
               f"pinned to {installed.pinned_ref}.")


@pack_app.command("remove")
def pack_remove(
    name: str = typer.Argument(..., help="The installed pack's name"),
    config: Path | None = ConfigOption,
):
    """Delete an installed pack's files and its database row, and audit it."""
    from armature.server.db import make_engine
    from armature.server.packs import InstallError, remove

    cfg = _load(config)
    engine = make_engine(cfg.database_url)
    try:
        version = remove(engine, Path(cfg.packs_dir), name)
    except InstallError as exc:
        _fail(f"Remove refused: {exc}")
    finally:
        engine.dispose()
    typer.echo(f"Removed {name}{f' {version}' if version else ''}.")


def _scope_text(conn, row) -> str:
    from sqlalchemy import select

    from armature.server.db.schema import projects, teams
    if row.project_id is not None:
        return f"project {conn.execute(select(projects.c.name).where(projects.c.id == row.project_id)).scalar()}"
    if row.team_id is not None:
        team = conn.execute(select(teams.c.slug, teams.c.name).where(teams.c.id == row.team_id)).first()
        return f"team {team.slug or team.name}"
    return "instance"


def _relink_target(conn, wanted: str):
    from sqlalchemy import or_, select

    from armature.server.db.schema import workflows
    rows = [r for r in conn.execute(select(workflows).where(or_(workflows.c.id == wanted, workflows.c.name == wanted)))
            if wanted in (r.id, r.name)]
    by_id = [r for r in rows if r.id == wanted]
    if by_id:
        return by_id[0]
    if not rows:
        _fail(f"No workflow has the id or name {wanted}.")
    if len(rows) > 1:
        listed = "\n".join(f"  {r.id}  {_scope_text(conn, r)}" for r in rows)
        _fail(f"{len(rows)} workflows are named {wanted}. Pass one of these ids to --to:\n{listed}")
    return rows[0]


@runs_app.command("relink")
def runs_relink(
    old_name: str = typer.Option(..., "--from", help="The workflow name the runs were recorded under"),
    wanted: str = typer.Option(..., "--to", help="The workflow to link them to, by id or by a unique name"),
    config: Path | None = ConfigOption,
    dry_run: bool = typer.Option(False, "--dry-run", help="Count the runs without changing them"),
):
    """Link runs recorded under an old name, and linked to no workflow, to the workflow they now resolve to."""
    from armature.server import defs
    from armature.server.db import make_engine

    cfg = _load(config)
    engine = make_engine(cfg.database_url)
    try:
        with engine.begin() as conn:
            row = _relink_target(conn, wanted)
            count = defs.relink(conn, old_name, row, dry_run=dry_run)
            where = _scope_text(conn, row)
    finally:
        engine.dispose()
    verb = "Would link" if dry_run else "Linked"
    typer.echo(f"{verb} {count} run{'' if count == 1 else 's'} of {old_name} to {row.name} ({where}).")


@server_app.command("import-ops")
def import_ops_cmd(
    workflow: str = typer.Option(..., "--workflow", help="The armature-ops workflow, such as pr-merged"),
    project: str = typer.Option(..., "--project", help="The project as team/project, or its repository"),
    config: Path | None = ConfigOption,
    dry: bool = typer.Option(False, "--dry", help="Validate and show the changes, then roll them back"),
    force: bool = typer.Option(False, "--force", help="Overwrite rows edited since the last import"),
):
    """Upsert an armature-ops workflow, its documents, and its webhook rules into a project. Never deletes."""
    from armature.server import defs
    from armature.server.db import make_engine, utcnow
    from armature.server.migrate_ops import MigrateError, import_ops, resolve_project
    from armature.server.webhooks import WebhookError

    cfg = _load(config)
    engine = make_engine(cfg.database_url)
    try:
        with engine.connect() as conn, conn.begin() as tx:
            try:
                team_id, project_id = resolve_project(conn, project)
                changes, notes = import_ops(conn, workflow, team_id=team_id, project_id=project_id, now=utcnow(),
                                            catalog=defs.load_catalog(cfg.packs_dir), force=force)
            except (MigrateError, WebhookError) as exc:
                _fail(f"Import refused, nothing changed: {exc}")
            except defs.DefError as exc:
                found = (exc.extra.get("validation") or {}).get("errors") or []
                _fail("Import refused, nothing changed: " + "; ".join(
                    [exc.message, *(f"{e.get('path') or 'workflow'}: {e['message']}" for e in found)]))
            if dry:
                tx.rollback()
    finally:
        engine.dispose()
    for change in changes:
        typer.echo(("Dry run, rolled back: " if dry else "") + change.line())
    for note in notes:
        typer.echo(f"Next: {note}")


env_app = typer.Typer(name="env", help="Build images for AWS environment types (needs armature\\[server])",
                      no_args_is_help=True)


@env_app.command("build")
def env_build(
    type_name: str = typer.Argument(..., help="The environment type, as named on the AWS page"),
    base_installs: Path = typer.Option(..., "--base-installs", exists=True, dir_okay=False,
                                       help="JSON of the installs every image gets: Claude Code, Codex, armature"),
    var_file: list[Path] = typer.Option([], "--var-file", exists=True, dir_okay=False,
                                        help="A Packer var file for values the AWS connection lacks. Repeat for each"),
    constraints: Path | None = typer.Option(None, "--constraints", exists=True, dir_okay=False,
                                            help="A pip constraints file with hashes for the armature wheel"),
    key_file: Path | None = typer.Option(None, "--key-file", exists=True, dir_okay=False,
                                         help="ec2-mac only: the VM's private key"),
    vm_name: str | None = typer.Option(None, "--vm-name", help="ec2-mac only: the VM to build (default: the type)"),
    prefix: str = typer.Option("armature-platform", "--prefix", help="The stacks' Prefix parameter"),
    deploy_dir: Path = typer.Option(None, "--deploy-dir", help="deploy/aws in a checkout (default: this one's)"),
    config: Path | None = ConfigOption,
):
    """Bake an environment type's image with its installs: an AMI with Packer, or a Tart VM for ec2-mac."""
    import subprocess
    import tempfile

    from sqlalchemy import select

    from armature.server import envbuild
    from armature.server.db import make_engine
    from armature.server.db.schema import aws_connections, environment_types
    cfg = _load(config)
    engine = make_engine(cfg.database_url)
    try:
        with engine.connect() as conn:
            kind = conn.execute(select(environment_types).where(environment_types.c.name == type_name)).first()
            connection = conn.execute(select(aws_connections).order_by(aws_connections.c.created_at)).first()
    finally:
        engine.dispose()
    if kind is None:
        _fail(f"No environment type named {type_name}.")
    with tempfile.TemporaryDirectory(prefix="armature-env-build-") as scratch:
        installs = envbuild.write_installs(kind, Path(scratch))
        try:
            command = envbuild.command(kind, connection, installs=installs, base_installs=base_installs,
                                       deploy_dir=deploy_dir or envbuild.DEPLOY_DIR, prefix=prefix,
                                       var_files=var_file, constraints=constraints, key_file=key_file,
                                       vm_name=vm_name)
        except envbuild.BuildError as exc:
            _fail(str(exc))
        typer.echo(f"Building {kind.kind} image for {kind.name}: {' '.join(command[:2])} ...")
        try:
            result = subprocess.run(command, check=False)
        except FileNotFoundError:
            _fail(f"{command[0]} isn't installed.")
    if result.returncode != 0:
        raise typer.Exit(result.returncode)
