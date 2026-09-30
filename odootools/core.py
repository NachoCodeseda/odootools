"""Odoo database and module operations, shared by the interactive menu and the CLI.

Call init() once before anything else: it puts the chosen Odoo on sys.path,
imports it and loads its conf. Nothing here prompts the user; failures raise.
"""
import base64
import configparser
import importlib
import inspect
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from contextlib import closing, contextmanager
from datetime import datetime

import psycopg2
from psycopg2 import sql as psql_sql
from tqdm import tqdm

_logger = logging.getLogger(__name__)

# /tmp may be tmpfs (RAM-backed) and too small for large dumps/filestores.
# Also applies to the TemporaryDirectory used inside odoo.service.db.dump_db.
tempfile.tempdir = '/var/tmp'

SUPERUSER_ID = 1  # same in every version; odoo.SUPERUSER_ID is gone in Odoo 19

odoo = None
ODOO_PATH = None
ODOO_CONF = None
SUBPROCESS_ENV = {**os.environ}


def find_pg_tool(tool):
    return tool


def init(odoo_path, odoo_conf):
    """Import the Odoo found at odoo_path and load odoo_conf."""
    global odoo, ODOO_PATH, ODOO_CONF, SUBPROCESS_ENV, find_pg_tool
    ODOO_PATH, ODOO_CONF = odoo_path, odoo_conf
    if odoo_path:
        sys.path.append(odoo_path)
    import odoo
    # Odoo 19 is a namespace package (no __init__.py), so submodules are
    # not auto-imported — we must import them explicitly.
    import odoo.tools
    odoo.tools.config.parse_config(['-c', odoo_conf, '--logfile='])
    import odoo.api
    import odoo.modules.registry
    import odoo.service.db
    import odoo.sql_db

    try:
        from odoo.tools.misc import exec_pg_environ, find_pg_tool
        SUBPROCESS_ENV = exec_pg_environ()
    except ImportError:
        SUBPROCESS_ENV = {**os.environ}
        for key, var in (('db_password', 'PGPASSWORD'), ('db_host', 'PGHOST'),
                         ('db_port', 'PGPORT'), ('db_user', 'PGUSER')):
            if odoo.tools.config.get(key):
                SUBPROCESS_ENV[var] = str(odoo.tools.config[key])


def validate_db_name(name):
    """Raise ValueError if the database name contains characters unsafe for SQL identifiers."""
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_\-]*$', name):
        raise ValueError(
            f"Invalid database name '{name}'. "
            "Use only letters, digits, underscores, and hyphens."
        )


def _supports_neutralize(func, min_params):
    """Neutralization (Odoo 16+) shows up as an extra parameter in the db service functions."""
    return len(inspect.signature(func).parameters) >= min_params


# ---------------------------------------------------------------------------
# Databases
# ---------------------------------------------------------------------------

def list_dbs():
    try:
        return odoo.service.db.list_dbs(force=True)
    except TypeError:
        return odoo.service.db.list_dbs()


def list_pg_users():
    db = odoo.sql_db.db_connect('postgres')
    with closing(db.cursor()) as cr:
        cr.execute("SELECT rolname FROM pg_roles WHERE rolcanlogin = true ORDER BY rolname")
        return [row[0] for row in cr.fetchall()]


def pg_terminate_backend(db_name):
    db = odoo.sql_db.db_connect('postgres')
    with closing(db.cursor()) as cr:
        cr.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
            (db_name,),
        )


def _check_faketime_mode(db_name):
    if os.getenv('ODOO_FAKETIME_TEST_MODE') and db_name in odoo.tools.config['db_name'].split(','):
        try:
            db = odoo.sql_db.db_connect(db_name)
            with db.cursor() as cursor:
                cursor.execute("SELECT (pg_catalog.now() AT TIME ZONE 'UTC');")
                server_now = cursor.fetchone()[0]
                time_offset = (datetime.now() - server_now).total_seconds()
                cursor.execute("""
                    CREATE OR REPLACE FUNCTION public.now()
                        RETURNS timestamp with time zone AS $$
                            SELECT pg_catalog.now() + %s * interval '1 second';
                        $$ LANGUAGE sql;
                """, (int(time_offset),))
                cursor.execute("SELECT (now() AT TIME ZONE 'UTC');")
                new_now = cursor.fetchone()[0]
                _logger.info("Faketime mode, new cursor now is %s", new_now)
                cursor.commit()
        except psycopg2.Error as e:
            _logger.warning("Unable to set faketimedNOW(): %s", e)


def _create_empty_database(name):
    db = odoo.sql_db.db_connect('postgres')
    with closing(db.cursor()) as cr:
        chosen_template = odoo.tools.config['db_template']
        cr.execute(
            "SELECT datname FROM pg_database WHERE datname = %s",
            (name,), log_exceptions=False
        )
        if cr.fetchall():
            _check_faketime_mode(name)
            raise ValueError(f"Database {name!r} already exists")
        cr.rollback()
        cr._cnx.autocommit = True
        collate = psql_sql.SQL("LC_COLLATE 'C'" if chosen_template == 'template0' else "")
        cr.execute(
            psql_sql.SQL("CREATE DATABASE {} ENCODING 'unicode' {} TEMPLATE {}").format(
                psql_sql.Identifier(name), collate, psql_sql.Identifier(chosen_template)
            )
        )

    try:
        db = odoo.sql_db.db_connect(name)
        with db.cursor() as cr:
            cr.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            if odoo.tools.config['unaccent']:
                cr.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
                cr.execute("ALTER FUNCTION unaccent(text) IMMUTABLE")
    except psycopg2.Error as e:
        _logger.warning("Unable to create PostgreSQL extensions: %s", e)
    _check_faketime_mode(name)

    # Restore legacy public schema access on PostgreSQL 15+
    try:
        db = odoo.sql_db.db_connect(name)
        with db.cursor() as cr:
            cr.execute("GRANT CREATE ON SCHEMA PUBLIC TO PUBLIC")
    except psycopg2.Error as e:
        _logger.warning("Unable to make public schema public-accessible: %s", e)


def restore_db(db, dump_file, copy=True, neutralize=False):
    """Restore a zip (dump.sql + filestore) or pg_dump custom-format file into a new database.

    Replacement for odoo.service.db.restore_db that also restores the filestore.
    """
    validate_db_name(db)
    if not os.path.isfile(dump_file):
        raise FileNotFoundError(f"Dump file not found: {dump_file}")
    if odoo.service.db.exp_db_exist(db):
        raise ValueError(f"Database {db!r} already exists")
    if neutralize and not _supports_neutralize(odoo.service.db.restore_db, 4):
        raise ValueError("Database neutralization is not available in this Odoo version")

    _logger.info('RESTORING DB: %s', db)
    _create_empty_database(db)

    with tempfile.TemporaryDirectory() as dump_dir:
        filestore_path = None
        if zipfile.is_zipfile(dump_file):
            with zipfile.ZipFile(dump_file, 'r') as z:
                filestore = [m for m in z.namelist() if m.startswith('filestore/')]
                z.extractall(dump_dir, ['dump.sql'] + filestore)
                if filestore:
                    filestore_path = os.path.join(dump_dir, 'filestore')
            pg_cmd = 'psql'
            pg_args = ['-q', '-f', os.path.join(dump_dir, 'dump.sql')]
        else:
            pg_cmd = 'pg_restore'
            pg_args = ['--no-owner', dump_file]

        r = subprocess.run(
            [find_pg_tool(pg_cmd), f'--dbname={db}', *pg_args],
            env=SUBPROCESS_ENV,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        if r.returncode != 0:
            odoo.service.db.exp_drop(db)  # we just created it empty, don't leave it behind
            raise RuntimeError(f"Couldn't restore database {db!r} ({pg_cmd} exit code {r.returncode})")

        if filestore_path:
            shutil.move(filestore_path, odoo.tools.config.filestore(db))
            _logger.info('RESTORE DB: %s filestore restored', db)

    if copy or neutralize:
        registry = odoo.modules.registry.Registry.new(db)
        with _env_manager(), registry.cursor() as cr:  # commits on exit
            if neutralize:
                importlib.import_module('odoo.modules.neutralize').neutralize_database(cr)
            if copy:
                env = odoo.api.Environment(cr, SUPERUSER_ID, {})
                env['ir.config_parameter'].init(force=True)

    _logger.info('RESTORE DB: %s done', db)


def drop_db(db_name):
    if not odoo.service.db.exp_drop(db_name):
        raise ValueError(f"Database {db_name!r} does not exist or is not owned by this Odoo's db_user")


def dump_db(db_name, backup_file):
    """Write a zip backup (dump.sql + filestore + manifest.json) to backup_file."""
    opened = False
    try:
        with open(backup_file, "wb") as destiny:
            opened = True
            odoo.service.db.dump_db(db_name, destiny, "zip")
    except BaseException:
        if opened:
            os.remove(backup_file)  # don't leave a truncated zip behind
        raise


def duplicate_db(db_name, new_db_name, neutralize=False):
    validate_db_name(new_db_name)
    func = odoo.service.db.exp_duplicate_database
    if _supports_neutralize(func, 3):
        func(db_name, new_db_name, neutralize)
    elif neutralize:
        raise ValueError("Database neutralization is not available in this Odoo version")
    else:
        func(db_name, new_db_name)


def send_db(db_name, new_db_name, dest_conf):
    """Copy a database (template copy + hardlinked filestore) owned by dest_conf's db_user."""
    validate_db_name(db_name)
    validate_db_name(new_db_name)
    if not os.path.isfile(dest_conf):
        raise FileNotFoundError(f"Destination conf not found: {dest_conf}")
    if odoo.service.db.exp_db_exist(new_db_name):
        raise ValueError(f"Database {new_db_name!r} already exists")

    config = configparser.ConfigParser()
    config.read(dest_conf)
    db_user = config.get('options', 'db_user', fallback=None)

    pg_terminate_backend(db_name)

    owner_clause = psql_sql.Identifier(db_user) if db_user else psql_sql.SQL('CURRENT_USER')
    query = psql_sql.SQL("CREATE DATABASE {} WITH TEMPLATE {} OWNER {}").format(
        psql_sql.Identifier(new_db_name),
        psql_sql.Identifier(db_name),
        owner_clause,
    )
    db_conn = odoo.sql_db.db_connect('postgres')
    with closing(db_conn.cursor()) as cr:
        cr._cnx.autocommit = True
        cr.execute(query)

    src_filestore = odoo.tools.config.filestore(db_name)
    dst_filestore = odoo.tools.config.filestore(new_db_name)
    if not os.path.isdir(src_filestore):
        return

    file_sizes = {}
    for root, _, files in os.walk(src_filestore):
        for f in files:
            path = os.path.join(root, f)
            file_sizes[path] = os.path.getsize(path)

    with tqdm(total=sum(file_sizes.values()), unit='B', unit_scale=True,
              unit_divisor=1024, desc='Copying filestore') as pbar:
        def copy_with_progress(src, dst):
            try:
                os.link(src, dst)
            except OSError:
                shutil.copy2(src, dst)
            pbar.update(file_sizes.get(src, 0) or os.path.getsize(src))

        shutil.copytree(src_filestore, dst_filestore,
                        copy_function=copy_with_progress, dirs_exist_ok=True)


def change_owner(db_name, user):
    db = odoo.sql_db.db_connect('postgres')
    with closing(db.cursor()) as cr:
        cr.execute(
            psql_sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                psql_sql.Identifier(db_name), psql_sql.Identifier(user)
            )
        )
        cr.commit()


def default_upgrade_path():
    return os.path.join(
        os.path.dirname(ODOO_PATH or ''), 'custom_addons', 'oca', 'OpenUpgrade',
        'openupgrade_scripts', 'scripts'
    )


def _colorize(line):
    if "ERROR" in line or "CRITICAL" in line:
        return f"\033[91m{line}\033[0m"
    elif "WARNING" in line:
        return f"\033[93m{line}\033[0m"
    elif "DEBUG" in line:
        return f"\033[94m{line}\033[0m"
    return line


def migrate_db(db_name, upgrade_path=None):
    """Run OpenUpgrade on db_name with odoo-bin, streaming its output."""
    upgrade_path = upgrade_path or default_upgrade_path()
    odoobin_path = os.path.join(ODOO_PATH or '', 'odoo-bin')
    for path in (upgrade_path, odoobin_path):
        if not os.path.exists(path):
            raise FileNotFoundError(f"Not found: {path}")

    cmd = [
        odoobin_path,
        "-c", ODOO_CONF,
        "-d", db_name,
        f"--upgrade-path={upgrade_path}",
        "--update", "all",
        "--stop-after-init",
        "--load=base,web,openupgrade_framework",
    ]
    print(' '.join(cmd))
    with subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    ) as p:
        for line in p.stdout:
            print(_colorize(line), end="")
    if p.returncode != 0:
        raise RuntimeError("Migration ended with errors")


# ---------------------------------------------------------------------------
# Environment and modules
# ---------------------------------------------------------------------------

@contextmanager
def _env_manager():
    # Odoo 12-14 needs manage() to initialise thread-local environment
    # storage outside a request; it no longer exists in newer versions.
    if hasattr(odoo.api.Environment, 'manage'):
        with odoo.api.Environment.manage():
            yield
    else:
        yield


@contextmanager
def environment(db_name, lang='es_ES'):
    """Superuser env on db_name. Nothing is committed unless the caller (or Odoo) does it."""
    registry = odoo.modules.registry.Registry(db_name)
    with _env_manager(), closing(registry.cursor()) as cr:
        yield odoo.api.Environment(cr, SUPERUSER_ID, {'lang': lang})


def update_module_list(env):
    """Rescan the addons path so modules added on disk become installable."""
    env['base.module.update'].create({}).update_module()


def module_names(env, states):
    return env['ir.module.module'].search([('state', 'in', states)]).mapped('name')


def get_modules(env, names, states):
    modules = env['ir.module.module'].search([('name', 'in', names), ('state', 'in', states)])
    missing = set(names) - set(modules.mapped('name'))
    if missing:
        raise ValueError(
            f"Module(s) {', '.join(sorted(missing))} not found in state {'/'.join(states)}"
        )
    return modules


def install_modules(env, names):
    get_modules(env, names, ['uninstalled']).button_immediate_install()


def upgrade_modules(env, names):
    get_modules(env, names, ['installed']).button_immediate_upgrade()


def uninstall_modules(env, names):
    get_modules(env, names, ['installed', 'to upgrade']).button_immediate_uninstall()


def export_translation(env, names, lang, po_file):
    modules = get_modules(env, names, ['installed'])
    export = env["base.language.export"].create(
        {"lang": lang, "format": "po", "modules": [(6, 0, modules.ids)]}
    )
    export.act_getfile()
    with open(po_file, 'wb') as f:
        f.write(base64.b64decode(export.data))
