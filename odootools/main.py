import sys
import os
import re
import inspect
import readline
import traceback
import zipfile
import shutil
import subprocess
import tempfile
import configparser
from contextlib import closing
from pathlib import Path
from bullet import Bullet, YesNo
from datetime import datetime
import logging
import psycopg2
from tqdm import tqdm
from psycopg2 import sql as psql_sql
import base64
from .discovery import discover_all_installations, find_conf_file

_logger = logging.getLogger(__name__)

RED_TEXT = "\033[91m{}\033[0m"
GREEN_TEXT = "\033[92m{}\033[0m"
BLUE_TEXT = "\033[94m{}\033[0m"
YELLOW_TEXT = "\033[93m{}\033[0m"

ODOO_PATH = None
ODOO_CONF = None
ODOO_PATHS = []


def _pg_quote_ident(name):
    """Quote a PostgreSQL identifier to prevent SQL injection."""
    return '"' + name.replace('"', '""') + '"'


def _validate_db_name(name):
    """Raise ValueError if the database name contains characters unsafe for SQL identifiers."""
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_\-]*$', name):
        raise ValueError(
            f"Invalid database name '{name}'. "
            "Use only letters, digits, underscores, and hyphens."
        )


def main():

    def clear():
        os.system('clear')

    def get_odoo_path():
        global ODOO_PATH, ODOO_CONF, ODOO_PATHS

        ODOO_PATHS = discover_all_installations()

        if len(ODOO_PATHS) == 1:
            ODOO_PATH = ODOO_PATHS[0]
        elif len(ODOO_PATHS) > 1:
            ODOO_PATH = Bullet("Select Odoo path:", choices=ODOO_PATHS).launch()
        else:
            ODOO_PATH = input(
                'No Odoo installation found automatically.\n'
                'Specify the path to the directory containing odoo-bin: '
            ).strip()
            # Accept both /opt/odoo18 and /opt/odoo18/odoo
            if os.path.exists(os.path.join(ODOO_PATH, 'odoo', 'odoo-bin')):
                ODOO_PATH = os.path.join(ODOO_PATH, 'odoo')

        ODOO_CONF = find_conf_file(ODOO_PATH)
        if not ODOO_CONF:
            ODOO_CONF = input("Specify the path to the Odoo conf file: ").strip()

        clear()
        print(GREEN_TEXT.format(f"Odoo path: {ODOO_PATH}"))
        print(GREEN_TEXT.format(f"Odoo conf: {ODOO_CONF}"))

    get_odoo_path()

    # Fallbacks in case the try block below fails or the symbols are absent
    # in an older Odoo version — must be defined before they are used as
    # decorators/closures further down the function.
    def check_db_management_enabled(fn):
        return fn

    SUBPROCESS_ENV = {**os.environ}

    def find_pg_tool(tool):
        return tool

    try:
        sys.path.append(ODOO_PATH)
        import odoo
        # Odoo 19 is a namespace package (no __init__.py), so submodules are
        # not auto-imported — we must import odoo.tools explicitly.
        import odoo.tools
        odoo.tools.config.parse_config(['-c', ODOO_CONF, '--logfile='])
        from odoo import SUPERUSER_ID

        try:
            from odoo.tools.misc import exec_pg_environ, find_pg_tool
            SUBPROCESS_ENV = exec_pg_environ()
        except (ImportError, Exception):
            SUBPROCESS_ENV = {**os.environ}
            if odoo.tools.config.get('db_password'):
                SUBPROCESS_ENV['PGPASSWORD'] = odoo.tools.config['db_password']
            if odoo.tools.config.get('db_host'):
                SUBPROCESS_ENV['PGHOST'] = odoo.tools.config['db_host']
            if odoo.tools.config.get('db_port'):
                SUBPROCESS_ENV['PGPORT'] = str(odoo.tools.config['db_port'])
            if odoo.tools.config.get('db_user'):
                SUBPROCESS_ENV['PGUSER'] = odoo.tools.config['db_user']

            def find_pg_tool(tool):
                return tool

    except Exception as e:
        print(e)

    def path_completer(text, state):
        """Complete absolute or relative paths."""
        expanded_text = os.path.expanduser(text)
        partial_dir = os.path.dirname(expanded_text)
        if partial_dir == '':
            partial_dir = '.'

        try:
            files = os.listdir(partial_dir)
        except FileNotFoundError:
            return None

        complete_files = [
            os.path.join(partial_dir, f)
            for f in files
            if f.startswith(os.path.basename(expanded_text))
        ]
        results = [x + '/' if os.path.isdir(x) else x for x in complete_files]

        if state < len(results):
            return results[state]
        return None

    def make_modules_completer(modules):
        def modules_completer(text, state):
            matches = [s for s in modules if s.startswith(text)]
            try:
                return matches[state]
            except IndexError:
                return None
        return modules_completer

    def set_completer(func):
        readline.set_completer_delims(' \t\n;')
        readline.parse_and_bind("tab: complete")
        readline.set_completer(func)

    def pg_terminate_backend(db_name):
        # Single-quote escaping for string literals in SQL (doubling single quotes is the SQL standard)
        escaped = db_name.replace("'", "''")
        cmd = [
            'psql', '-d', 'postgres', '-c',
            f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{escaped}';"
        ]
        subprocess.run(cmd, check=True, env=SUBPROCESS_ENV)

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
                raise Exception(f"database {name!r} already exists!")
            else:
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

    @check_db_management_enabled
    def restore_db(db, dump_file):
        """Override of odoo.service.db.restore_db to ensure the filestore is restored."""
        try:
            assert isinstance(db, str)
            if exp_db_exist(db):
                _logger.warning('RESTORE DB: %s already exists', db)
                return False

            _logger.info('RESTORING DB: %s', db)
            _create_empty_database(db)

            filestore_path = None
            with tempfile.TemporaryDirectory() as dump_dir:
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
                    _logger.error("Couldn't restore database")
                    return False

                # Determine whether neutralization is supported in this Odoo version
                neutralize_database = False
                sig = inspect.signature(odoo.service.db.restore_db)
                copy = YesNo('Is it a copy?', 'y').launch()
                if len(sig.parameters) >= 4:
                    neutralize_database = YesNo('Neutralize DB?:', 'n').launch()

                if filestore_path:
                    data_dir = odoo.tools.config.get('data_dir')
                    filestore_dest = os.path.join(data_dir, 'filestore', db)
                    shutil.move(filestore_path, filestore_dest)
                    _logger.info('RESTORE DB: %s filestore restored', db)

                try:
                    registry = odoo.modules.registry.Registry.new(db)
                    with registry.cursor() as cr:
                        if neutralize_database:
                            try:
                                odoo.modules.neutralize.neutralize_database(cr)
                            except (AttributeError, ImportError):
                                _logger.warning(
                                    "Database neutralization not available in this Odoo version"
                                )
                        env = odoo.api.Environment(cr, 1, {})
                        if copy:
                            try:
                                env['ir.config_parameter'].init(force=True)
                            except Exception:
                                pass
                except Exception:
                    pass

            _logger.info('RESTORE DB: %s done', db)
            return True

        except Exception:
            _logger.error(traceback.format_exc())
            return False

    def drop_db(db_name):
        if YesNo(RED_TEXT.format(f"Are you sure you want to drop database {db_name}?")).launch():
            print(RED_TEXT.format("Dropping database..."))
            try:
                odoo.service.db.exp_drop(db_name)
                print(RED_TEXT.format(f"Database {db_name} dropped."))
            except Exception:
                print(traceback.format_exc())

    def dump_db(db_name):
        backup_file = input(f'Specify the path to the backup (default: {db_name}.zip): ') or f"{db_name}.zip"
        if not backup_file.endswith('.zip'):
            backup_file += '.zip'
        try:
            with open(backup_file, "wb") as destiny:
                print(BLUE_TEXT.format("Starting database dump..."))
                odoo.service.db.dump_db(db_name, destiny, "zip")
        except Exception:
            print(traceback.format_exc())
        print(f"Database {db_name} dumped to {backup_file}.")

    def duplicate_db(db_name):
        new_db_name = input('Enter the name of the new DB: ')
        try:
            sig = inspect.signature(odoo.service.db.exp_duplicate_database)
            if len(sig.parameters) >= 3:
                neutralize_database = YesNo('Neutralize DB?:', 'n').launch()
                odoo.service.db.exp_duplicate_database(db_name, new_db_name, neutralize_database)
            else:
                odoo.service.db.exp_duplicate_database(db_name, new_db_name)
            print(GREEN_TEXT.format(f"Database {db_name} duplicated to {new_db_name}."))
        except Exception:
            print(traceback.format_exc())

    def send_db(db_name):
        try:
            if len(ODOO_PATHS) < 2:
                print(RED_TEXT.format("No other Odoo installation found to send the DB to."))
                return

            to = Bullet("Select destination Odoo path:", choices=ODOO_PATHS).launch()
            odoo_conf_dest = os.path.join(os.path.dirname(to), 'odoo.conf')

            if not os.path.isfile(odoo_conf_dest):
                odoo_conf_dest = input("Specify the path to destination Odoo conf file: ")

            config = configparser.ConfigParser()
            config.read(odoo_conf_dest)
            db_user = config.get('options', 'db_user', fallback=None)
            next_db_name = input('Enter the name of the new DB: ')

            if exp_db_exist(next_db_name):
                print(RED_TEXT.format(f'The DB {next_db_name} already exists'))
                return

            try:
                _validate_db_name(next_db_name)
                _validate_db_name(db_name)
            except ValueError as e:
                print(RED_TEXT.format(str(e)))
                return

            pg_terminate_backend(db_name)

            # Quoted identifiers prevent SQL injection
            sql_query = "CREATE DATABASE {} WITH TEMPLATE {} OWNER {};".format(
                _pg_quote_ident(next_db_name),
                _pg_quote_ident(db_name),
                _pg_quote_ident(db_user) if db_user else 'CURRENT_USER',
            )
            print(sql_query)
            cmd = ["psql", "-d", "postgres", "-c", sql_query]

            with subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env=SUBPROCESS_ENV
            ) as p:
                for line in p.stdout:
                    print(line, end="")
                p.wait()
                if p.returncode != 0:
                    return

            data_dir = odoo.tools.config.get('data_dir')
            src_filestore = os.path.join(data_dir, 'filestore', db_name)
            dst_filestore = os.path.join(data_dir, 'filestore', next_db_name)

            total_size = sum(
                os.path.getsize(os.path.join(root, f))
                for root, _, files in os.walk(src_filestore)
                for f in files
            )

            def copy_with_progress(src, dst):
                shutil.copy2(src, dst)
                pbar.update(os.path.getsize(src))

            print(GREEN_TEXT.format("Copying filestore..."))
            with tqdm(total=total_size, unit='B', unit_scale=True, unit_divisor=1024) as pbar:
                shutil.copytree(
                    src_filestore, dst_filestore,
                    copy_function=copy_with_progress,
                    dirs_exist_ok=True,
                )

            print(GREEN_TEXT.format("DB copied."))

        except Exception:
            print(traceback.format_exc())

    def migrate_db(db_name):

        def colorize(line):
            if "ERROR" in line or "CRITICAL" in line:
                return f"\033[91m{line}\033[0m"
            elif "WARNING" in line:
                return f"\033[93m{line}\033[0m"
            elif "DEBUG" in line:
                return f"\033[94m{line}\033[0m"
            return line

        print(RED_TEXT.format("Migrating database..."))
        openupgrade_path = os.path.join(
            os.path.dirname(ODOO_PATH), 'custom_addons', 'oca', 'OpenUpgrade',
            'openupgrade_scripts', 'scripts'
        )
        odoobin_path = os.path.join(ODOO_PATH, 'odoo-bin')

        if not os.path.exists(openupgrade_path):
            print(RED_TEXT.format(f"OpenUpgrade path not found: {openupgrade_path}"))
            openupgrade_path = input("Specify the path to OpenUpgrade scripts: ")
        if not os.path.exists(odoobin_path):
            print(RED_TEXT.format(f"odoo-bin not found at: {odoobin_path}"))
            return

        try:
            cmd = [
                odoobin_path,
                "-c", ODOO_CONF,
                "-d", db_name,
                f"--upgrade-path={openupgrade_path}",
                "--update", "all",
                "--stop-after-init",
                "--load=base,web,openupgrade_framework",
            ]
            print(BLUE_TEXT.format(str(cmd)))
            with subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
            ) as p:
                for line in p.stdout:
                    print(colorize(line), end="")
                p.wait()
                if p.returncode != 0:
                    print(RED_TEXT.format("Migration ended with errors."))
                    return

            print(GREEN_TEXT.format(f"Database {db_name} migrated."))

        except Exception:
            print(traceback.format_exc())

    def change_db_user(db_name):
        cmd = [
            'psql',
            '-d', 'postgres',
            "-t", "-A", "--no-psqlrc",
            '-c',
            'SELECT rolname FROM pg_roles WHERE rolcanlogin = true ORDER BY rolname;',
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, env=SUBPROCESS_ENV)
        users = [u for u in result.stdout.strip().split("\n") if u]
        users.append('Cancel')
        user = Bullet("Select the new DB user:", choices=users).launch()
        if user == 'Cancel':
            return
        cmd = ['psql', '-d', 'postgres', '-c', f"ALTER DATABASE {_pg_quote_ident(db_name)} OWNER TO {_pg_quote_ident(user)};"]
        subprocess.run(cmd, capture_output=True, check=True, env=SUBPROCESS_ENV)
        print(GREEN_TEXT.format(f"DB {db_name} owner changed to {user}."))

    def print_modules(modules):
        module_names = [m['name'] for m in modules]
        columns = 3
        indexed_names = [f"{i + 1}) {name}" for i, name in enumerate(module_names)]
        max_cell_length = max((len(s) for s in indexed_names), default=20) + 2
        for i in range(0, len(indexed_names), columns):
            row = indexed_names[i:i + columns]
            print("".join(cell.ljust(max_cell_length) for cell in row))

    def select_module(env, state, selection_text):
        print(YELLOW_TEXT.format('Updating modules list...'))
        try:
            env['base.module.update'].create({}).update_module()
        except Exception:
            pass
        print("******************************")
        modules = env['ir.module.module'].search_read([('state', 'in', state)], ['name'])
        print_modules(modules)
        set_completer(make_modules_completer([m['name'] for m in modules]))
        user_input = input(selection_text)
        if user_input == 'c':
            return None

        modules_list = user_input.split()
        module_ids = env['ir.module.module'].search(
            [('name', 'in', modules_list), ('state', 'in', state)]
        )
        if not module_ids:
            print("Module not found.")
            return None

        return module_ids

    def select_db():
        try:
            dbs = odoo.service.db.list_dbs(force=True)
        except TypeError:
            dbs = odoo.service.db.list_dbs()
        dbs.append('Cancel')
        option = Bullet(choices=dbs).launch()
        clear()
        if option == 'Cancel':
            return None
        return option

    set_completer(path_completer)
    env = None

    try:
        while True:
            options = [
                'Restore DB',
                'Drop DB',
                'Backup DB',
                'Duplicate DB',
                'Send DB',
                'Change DB user',
                'Migrate DB',
                'List DBs',
                'Get Environment',
            ]
            if env:
                options += ['Uninstall Module', 'Install Module', 'Update Module', 'Export translation']
            options.append('Exit')
            set_completer(path_completer)
            print('#####################')
            prompt = "Odootools"
            if env:
                prompt = f"Odootools (env: {BLUE_TEXT.format(env.cr.dbname)})"

            option = Bullet(prompt=prompt, choices=options).launch()
            clear()

            if option == 'Restore DB':
                set_completer(path_completer)
                dump_path = input('Specify the file path: ')
                if not dump_path.endswith('.zip'):
                    print('The dump must be a .zip file')
                    continue
                db_name = input('Enter the name of the database (c to cancel): ')
                if db_name == 'c':
                    continue
                restore_db(db_name, dump_path)

            elif option == 'Drop DB':
                print(RED_TEXT.format("Drop DB"))
                db_name = select_db()
                if not db_name:
                    continue
                drop_db(db_name)

            elif option == 'Backup DB':
                print(BLUE_TEXT.format("Backup DB"))
                db_name = select_db()
                if not db_name:
                    continue
                dump_db(db_name)

            elif option == 'Duplicate DB':
                print(BLUE_TEXT.format("Duplicate DB"))
                db_name = select_db()
                if not db_name:
                    continue
                duplicate_db(db_name)

            elif option == 'Send DB':
                print(BLUE_TEXT.format("Send DB"))
                db_name = select_db()
                if not db_name:
                    continue
                send_db(db_name)

            elif option == 'Change DB user':
                print(BLUE_TEXT.format("Change DB user"))
                db_name = select_db()
                if not db_name:
                    continue
                change_db_user(db_name)

            elif option == 'Migrate DB':
                print(BLUE_TEXT.format("Migrate DB"))
                db_name = select_db()
                if not db_name:
                    continue
                migrate_db(db_name)

            elif option == 'List DBs':
                try:
                    dbs = odoo.service.db.list_dbs(force=True)
                except TypeError:
                    dbs = odoo.service.db.list_dbs()
                for i, db in enumerate(dbs, 1):
                    print(i, db)

            elif option == 'Get Environment':
                if env:
                    env.cr.close()
                print(GREEN_TEXT.format("Get Environment"))
                db_name = select_db()
                if not db_name:
                    continue
                registry = odoo.modules.registry.Registry(db_name)
                cursor = registry.cursor()
                # Odoo <15 requires manage() context to initialize thread-local environments storage
                if hasattr(odoo.api.Environment, 'manage'):
                    odoo.api.Environment.manage().__enter__()
                env = odoo.api.Environment(cursor, SUPERUSER_ID, {'lang': 'es_ES'})

            elif option == 'Uninstall Module':
                module_ids = select_module(
                    env,
                    ['installed', 'to upgrade'],
                    RED_TEXT.format('Specify the module(s) to uninstall (space-separated, c to cancel): '),
                )
                if module_ids is None:
                    continue
                if not YesNo(RED_TEXT.format(
                    f'Are you sure you want to uninstall {[m.name for m in module_ids]}?: '
                )).launch():
                    continue
                clear()
                try:
                    for module in module_ids:
                        print(RED_TEXT.format(f"Uninstalling module {module.name}..."))
                        module.button_immediate_uninstall()
                        print(RED_TEXT.format(f"Module {module.name} uninstalled."))
                except Exception:
                    print(traceback.format_exc())

            elif option == 'Install Module':
                module_ids = select_module(
                    env,
                    ['uninstalled'],
                    GREEN_TEXT.format('Specify the module(s) to install (space-separated, c to cancel): '),
                )
                if module_ids is None:
                    continue
                clear()
                for module in module_ids:
                    try:
                        print(GREEN_TEXT.format(f"Installing module {module.name}..."))
                        module.button_immediate_install()
                        print(f"Module {module.name} installed.")
                    except Exception:
                        print(traceback.format_exc())

            elif option == 'Update Module':
                module_ids = select_module(
                    env,
                    ['installed'],
                    BLUE_TEXT.format('Specify the module(s) to update (space-separated, c to cancel): '),
                )
                if module_ids is None:
                    continue
                clear()
                for module in module_ids:
                    try:
                        print(BLUE_TEXT.format(f"Updating module {module.name}..."))
                        module.button_immediate_upgrade()
                        print(BLUE_TEXT.format(f"Module {module.name} updated."))
                    except Exception:
                        print(traceback.format_exc())

            elif option == 'Export translation':
                module_ids = select_module(
                    env,
                    ['installed'],
                    BLUE_TEXT.format('Specify the module to export translation (c to cancel): '),
                )
                if module_ids is None:
                    continue
                clear()
                lang = input('Indicate the language (default: es_ES): ') or 'es_ES'
                set_completer(path_completer)
                export_path = input('Specify the destination path (default: es.po, c to cancel): ') or "es.po"
                if export_path == 'c':
                    continue
                if not export_path.endswith('.po'):
                    export_path += '.po'

                try:
                    print(BLUE_TEXT.format(f"Exporting translation for {[m.name for m in module_ids]}..."))
                    export = env["base.language.export"].create(
                        {"lang": lang, "format": "po", "modules": [(6, 0, module_ids.ids)]}
                    )
                    export.act_getfile()
                    data = base64.b64decode(export.data)
                    with open(export_path, 'wb') as f:
                        f.write(data)
                    print(f"Translation exported to {export_path}.")
                except Exception:
                    print(traceback.format_exc())

            elif option == 'Exit':
                break

    except Exception:
        print(traceback.format_exc())

    finally:
        if env:
            try:
                env.cr.close()
                print("Cursor closed.")
            except Exception:
                print(traceback.format_exc())


if __name__ == '__main__':
    main()
