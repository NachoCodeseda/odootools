import sys, os, inspect, readline, traceback, zipfile, shutil, subprocess, tempfile, configparser
from contextlib import closing
from bullet import Bullet, YesNo
from datetime import datetime
import logging
import psycopg2
from tqdm import tqdm
from psycopg2 import sql
import base64

_logger = logging.getLogger(__name__)

RED_TEXT="\033[91m{}\033[0m"
GREEN_TEXT="\033[92m{}\033[0m"
BLUE_TEXT="\033[94m{}\033[0m"
YELLOW_TEXT="\033[93m{}\033[0m"
USUAL_ODOO_PATHS = [
    "/opt/odoo/odoo",
    "/opt/odoo",
    "/etc/odoo",
    "/opt/odoo12",
    "/opt/odoo13",
    "/opt/odoo14",
    "/opt/odoo15",
    "/opt/odoo16",
    "/opt/odoo17",
    "/opt/odoo18",
    "/opt/odoo19"
    ]
ODOO_PATH = None
ODOO_CONF = None
ODOO_PATHS = []

def main():
    
    def clear():
        os.system('clear')
        
    def get_odoo_path():
    # TODO: Better implementation
        global ODOO_PATH
        global ODOO_CONF
        global ODOO_PATHS

        for path in USUAL_ODOO_PATHS:
            if os.path.exists(os.path.join(path, "odoo-bin")):
                ODOO_PATHS.append(path)
            
            elif os.path.exists(os.path.join(path, "odoo/odoo-bin")):
                ODOO_PATHS.append(os.path.join(path, "odoo"))
        
        if len(ODOO_PATHS) == 1:
            ODOO_PATH = ODOO_PATHS[0]
        
        elif len(ODOO_PATHS) > 1:
            ODOO_PATH = Bullet("Select odoo path:",choices=ODOO_PATHS).launch()

        if not ODOO_PATH:
            ODOO_PATH = input('Specify the path to the Odoo installation: ')
            
        if os.path.isfile(os.path.join(ODOO_PATH, "odoo.conf")):
            ODOO_CONF = os.path.join(ODOO_PATH, "odoo.conf")
        elif os.path.isfile(os.path.join(os.path.dirname(ODOO_PATH), "odoo.conf")):
            ODOO_CONF = os.path.join(os.path.dirname(ODOO_PATH), "odoo.conf")

        if not ODOO_CONF:
            ODOO_CONF = input("Specify the path to Odoo conf file: ")
        
        clear()
        
        print(GREEN_TEXT.format("Odoo path: {}".format(ODOO_PATH)))
        print(GREEN_TEXT.format("Odoo conf: {}".format(ODOO_CONF)))
    
    get_odoo_path()

    try:
        sys.path.append(ODOO_PATH)
        import odoo
        odoo.tools.config.parse_config(['-c', ODOO_CONF, '--logfile='])
        from odoo.service.db import exp_db_exist, check_db_management_enabled
        from odoo.tools.misc import exec_pg_environ, find_pg_tool
        from odoo import SUPERUSER_ID
        SUBPROCESS_ENV = {"PGPASSWORD": odoo.tools.config['db_password']}
        
    except Exception as e:
        print(e)
        
    
    def path_completer(text, state):
        """Complete absolute or relative paths."""
        expanded_text = os.path.expanduser(text)
        parcial_dir = os.path.dirname(expanded_text)
        if parcial_dir == '':
            parcial_dir = '.'

        try:
            files = os.listdir(parcial_dir)
        except FileNotFoundError:
            return None

        complete_files = [
            os.path.join(parcial_dir, f)
            for f in files
            if f.startswith(os.path.basename(expanded_text))
        ]

        resultados = [x + '/' if os.path.isdir(x) else x for x in complete_files]

        if state < len(resultados):
            return resultados[state]
        else:
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
        cmd = [
            'psql',
            '-c',
            f"SELECT pg_terminate_backend(pg_stat_activity.pid) FROM pg_stat_activity WHERE pg_stat_activity.datname = '{db_name}';"
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
                                SELECT pg_catalog.now() +  %s * interval '1 second';
                            $$ LANGUAGE sql;
                    """, (int(time_offset), ))
                    cursor.execute("SELECT (now() AT TIME ZONE 'UTC');")
                    new_now = cursor.fetchone()[0]
                    _logger.info("Faketime mode, new cursor now is %s", new_now)
                    cursor.commit()
            except psycopg2.Error as e:
                _logger.warning("Unable to set fakedtimed NOW() : %s", e)
    
    def _create_empty_database(name):
        db = odoo.sql_db.db_connect('postgres')
        with closing(db.cursor()) as cr:
            chosen_template = odoo.tools.config['db_template']
            cr.execute("SELECT datname FROM pg_database WHERE datname = %s",
                    (name,), log_exceptions=False)
            if cr.fetchall():
                _check_faketime_mode(name)
                raise Exception("database %r already exists!" % (name,))
            else:
                # database-altering operations cannot be executed inside a transaction
                cr.rollback()
                cr._cnx.autocommit = True

                # 'C' collate is only safe with template0, but provides more useful indexes
                collate = sql.SQL("LC_COLLATE 'C'" if chosen_template == 'template0' else "")
                cr.execute(
                    sql.SQL("CREATE DATABASE {} ENCODING 'unicode' {} TEMPLATE {}").format(
                    sql.Identifier(name), collate, sql.Identifier(chosen_template)
                ))

        try:
            db = odoo.sql_db.db_connect(name)
            with db.cursor() as cr:
                cr.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
                if odoo.tools.config['unaccent']:
                    cr.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
                    cr.execute("ALTER FUNCTION unaccent(text) IMMUTABLE")
        except psycopg2.Error as e:
            _logger.warning("Unable to create PostgreSQL extensions : %s", e)
        _check_faketime_mode(name)

        # restore legacy behaviour on pg15+
        try:
            db = odoo.sql_db.db_connect(name)
            with db.cursor() as cr:
                cr.execute("GRANT CREATE ON SCHEMA PUBLIC TO PUBLIC")
        except psycopg2.Error as e:
            _logger.warning("Unable to make public schema public-accessible: %s", e)
            
            
    @check_db_management_enabled
    def restore_db(db, dump_file):
        "Override odoo.service.db.restore_db method to be sure the filestore is restored"
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
                    # v8 format
                    with zipfile.ZipFile(dump_file, 'r') as z:
                        # only extract known members!
                        filestore = [m for m in z.namelist() if m.startswith('filestore/')]
                        z.extractall(dump_dir, ['dump.sql'] + filestore)

                        if filestore:
                            filestore_path = os.path.join(dump_dir, 'filestore')

                    pg_cmd = 'psql'
                    pg_args = ['-q', '-f', os.path.join(dump_dir, 'dump.sql')]

                else:
                    # <= 7.0 format (raw pg_dump output)
                    pg_cmd = 'pg_restore'
                    pg_args = ['--no-owner', dump_file]

                r = subprocess.run(
                    [find_pg_tool(pg_cmd), '--dbname=' + db, *pg_args],
                    env=exec_pg_environ(),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.STDOUT,
                )
                if r.returncode != 0:
                    _logger.error("Couldn't restore database")
                    return False
                
                signature = inspect.signature(odoo.service.db.restore_db)
                copy = YesNo('Is it a copy?', 'y').launch()
                if len(signature.parameters) == 4:
                    neutralize_database = YesNo('Neutralize DB?:', 'n').launch()
                    
                if filestore_path:
                    data_dir = odoo.tools.config.get('data_dir')
                    filestore_dest = os.path.join(data_dir,'filestore', db_name)
                    shutil.move(filestore_path, filestore_dest)
                    _logger.info('RESTORE DB: %s filestore', db)
                
                try:
                    registry = odoo.modules.registry.Registry.new(db)
                    with registry.cursor() as cr:
                        if neutralize_database:
                            odoo.modules.neutralize.neutralize_database(cr)
                            
                        env = odoo.api.Environment(cr, 1, {})
                        if copy:
                            # if it's a copy of a database, force generation of a new dbuuid
                            env['ir.config_parameter'].init(force=True)
                except Exception:
                    pass
            _logger.info('RESTORE DB: %s', db)
            return True

        except Exception:
            _logger.error(traceback.format_exc())
            return False

    def drop_db(db_name):
        if YesNo(RED_TEXT.format("Are you sure you want to drop database {}?".format(db_name))).launch():
            print(RED_TEXT.format("Dropping database..."))
            try:
                odoo.service.db.exp_drop(db_name)
                print(RED_TEXT.format("Database {} dropped.".format(db_name)))
                
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
            
        print("Database {} dumped.".format(db_name))
    
    def send_db(db_name):
        try:
            to = Bullet("Select odoo path:",choices=ODOO_PATHS).launch()
            odoo_conf =  os.path.join(os.path.dirname(to), 'odoo.conf')
            
            if not os.path.isfile(odoo_conf):
                odoo_conf = input("Specify the path to Odoo conf file: ")
                
            config = configparser.ConfigParser()
            config.read(odoo_conf)
            db_user = config.get('options', 'db_user', fallback=None)
            next_db_name = input(f'Enter the name of the new DB: ')
            
            if exp_db_exist(next_db_name):
                print(RED_TEXT.format(f'The DB {next_db_name} already exists'))
                return
            
            pg_terminate_backend(db_name)
            sql = f"CREATE DATABASE {next_db_name} WITH TEMPLATE {db_name} OWNER {db_user};"
            print(sql)
            cmd = [
                "psql",
                "-c",
                sql
            ]
            
            with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=SUBPROCESS_ENV) as p:
                for line in p.stdout:
                    print(line, end="")
                
                p.wait()
                if p.returncode != 0:
                    return
                    
            data_dir = odoo.tools.config.get('data_dir')
            total_size = 0
            for root, dirs, files in os.walk(os.path.join(data_dir,'filestore', db_name)):
                for file in files:
                    total_size += os.path.getsize(os.path.join(root, file))

            copied_bytes = 0
            def copy_with_progress(src, dst):
                nonlocal copied_bytes
                shutil.copy2(src, dst)
                copied_bytes += os.path.getsize(src)
                pbar.update(os.path.getsize(src))

            print(GREEN_TEXT.format("Copying filestore..."))
            with tqdm(total=total_size, unit='B', unit_scale=True, unit_divisor=1024) as pbar:
                shutil.copytree(os.path.join(data_dir,'filestore', db_name),
                                os.path.join(data_dir,'filestore', next_db_name),
                                copy_function=copy_with_progress,
                                dirs_exist_ok=True)
            
            print(GREEN_TEXT.format("DB copied."))
            
        except Exception:
            print(traceback.format_exc())
    
    def migrate_db(db_name):
        
        def colorize(line):
            if "ERROR" in line or "CRITICAL" in line:
                return f"\033[91m{line}\033[0m"   # Red
            elif "WARNING" in line:
                return f"\033[93m{line}\033[0m"   # Yellow
            elif "DEBUG" in line:
                return f"\033[94m{line}\033[0m"   # Blue
            else:
                return line
            
        print(RED_TEXT.format("Migrating database..."))
        openupgrade_path = os.path.join(os.path.dirname(ODOO_PATH), 'custom_addons', 'oca', 'OpenUpgrade', 'openupgrade_scripts', 'scripts')
        print(openupgrade_path)
        odoobin_path = os.path.join(ODOO_PATH, 'odoo-bin')
        if not os.path.exists(openupgrade_path):
            print(RED_TEXT.format("OpenUpgrade path not found"))
            openupgrade_path = input("Specify the path to OpenUpgrade scripts: ")
        if not os.path.exists(odoobin_path):
            print(RED_TEXT.format("odoo-bin path not found"))
            return
        try:
            cmd = [
                odoobin_path,
                "-c", ODOO_CONF,
                "-d", db_name,
                f"--upgrade-path={openupgrade_path}",
                "--update", "all",
                "--stop-after-init",
                "--load=base,web,openupgrade_framework"
            ]
            print(BLUE_TEXT.format(cmd))
            with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1) as p:
                for line in p.stdout:
                    print(colorize(line), end="")
                p.wait()
                if p.returncode != 0:
                    return
            
            print(GREEN_TEXT.format("Base de datos {} migrada.".format(db_name)))
                    
        except Exception:
            print(traceback.format_exc())
    
    def change_db_user(db_name):
        cmd = [
            'psql',
            "-t", "-A", "--no-psqlrc",
            '-c',
            'SELECT rolname FROM pg_roles WHERE rolcanlogin = true ORDER BY rolname;'
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, env=SUBPROCESS_ENV)
        users = result.stdout.strip().split("\n")
        users_list = [u for u in users if u]
        users_list.append('Cancel')
        user = Bullet("Select the new DB user:",choices=users_list).launch()
        if user == 'Cancel':
            return None
        cmd = [
            'psql',
            '-c',
            f"ALTER DATABASE {db_name} OWNER TO {user};"
        ]
        result = subprocess.run(cmd, capture_output=True, check=True, env=SUBPROCESS_ENV)
        print(GREEN_TEXT.format("DB {} owner changed to {}.".format(db_name, user)))
        
    
    def print_modules(modules):
        module_names = [m['name'] for m in modules]

        columns = 3
        indexed_names = [f"{i+1}) {name}" for i, name in enumerate(module_names)]
        max_cell_length = max(len(s) for s in indexed_names) + 2

        # Show columns
        for i in range(0, len(indexed_names), columns):
            row = indexed_names[i:i+columns]
            line = "".join(cell.ljust(max_cell_length) for cell in row)
            print(line)
            
    def select_module(env, state, selection_text):
        print(YELLOW_TEXT.format('Updating modules list...'))
        env['base.module.update'].create({}).update_module()
        print("******************************")
        modules = env['ir.module.module'].search_read([('state', 'in', state)], ['name'])
        print_modules(modules)
        set_completer(make_modules_completer([m['name'] for m in modules]))
        modules = input(selection_text)
        if modules == 'c':
            return None
        
        modules_list = modules.split(' ')
        module_ids = env['ir.module.module'].search([('name', 'in', modules_list), ('state', '=', state)])
        if not module_ids:
            print("Module not found.")
            return None
        
        return module_ids
    
    def select_db():
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
                'Send DB',
                'Change DB user',
                'Migrate DB',
                'List DBs',
                'Get Environment'
            ]
            if env:
                options += ['Uninstall Module', 'Install Module', 'Update Module', 'Export translation']
            options.append('Exit')
            set_completer(path_completer)
            print('#####################')
            prompt = "Odootools"
            if env:
                prompt = "Odootools (env: {})".format(BLUE_TEXT.format(env.cr.dbname))
            
            option = Bullet(prompt=prompt, choices=options).launch()
            clear()
            
            if option == 'Restore DB':
                dump_path = input('Specify the file path: ')
                if not dump_path.endswith('.zip'):
                    print('The dump must be a .zip file')
                    continue
                db_name = input('Enter the name of the database (c cancel): ')
                if db_name == 'c':
                    continue
                restore_db(db_name, dump_path)
                
            elif option == 'Drop DB':
                print(RED_TEXT.format("Drop DB"))
                db_name = select_db()
                if not db_name: continue
                drop_db(db_name)
                
            elif option == 'Backup DB':
                print(BLUE_TEXT.format("Backup DB"))
                db_name = select_db()
                if not db_name: continue
                dump_db(db_name)
            
            elif option == 'Send DB':
                print(BLUE_TEXT.format("Send DB"))
                db_name = select_db()
                if not db_name: continue
                send_db(db_name)
                
            elif option == 'Change DB user':
                print(BLUE_TEXT.format("Change DB user"))
                db_name = select_db()
                if not db_name: continue
                change_db_user(db_name)
            
            elif option == 'Migrate DB':
                print(BLUE_TEXT.format("Migrate DB"))
                db_name = select_db()
                if not db_name: continue
                migrate_db(db_name)
                
            elif option == 'List DBs':
                dbs = odoo.service.db.list_dbs()
                for db in dbs:
                    print(dbs.index(db) + 1, db)
                
            elif option == 'Get Environment':
                if env:
                    env.cr.close()
                print(GREEN_TEXT.format("Get Environment"))
                db_name = select_db()
                if not db_name: continue
                registry = odoo.modules.registry.Registry(db_name)
                cursor = registry.cursor()
                env = odoo.api.Environment(cursor, SUPERUSER_ID, {'lang': 'es_ES'})
            
            elif option == 'Uninstall Module':                
                module_ids = select_module(env, ['installed', 'to upgrade'], RED_TEXT.format('Specify the module to uninstall (c cancel): '))
                
                if module_ids is None:
                    continue
                
                if not YesNo(RED_TEXT.format('Are you sure you want to uninstall the modules? {}?: '.format([m.name for m in module_ids]))).launch():
                    continue
                
                clear()
                try:
                    for module in module_ids:
                        print(RED_TEXT.format("Uninstalling module {}...".format(module.name)))
                        module.button_immediate_uninstall()
                        print(RED_TEXT.format(f"Module {module.name} uninstalled."))
                    
                except Exception:
                    print(traceback.format_exc())
                
            elif option == 'Install Module':                
                module_ids = select_module(env, ['uninstalled'], GREEN_TEXT.format('Specify the module to install (name or index) (c cancel): '))
                
                if module_ids is None:
                    continue
                clear()
                for module in module_ids:
                    try:
                        print(GREEN_TEXT.format("Installing module {}...".format(module.name)))
                        module.button_immediate_install()
                        print(f"Module {module.name} installed.")
                    except Exception:
                        print(traceback.format_exc())
                
            elif option == 'Update Module':
                module_ids = select_module(env, ['installed'], BLUE_TEXT.format('Specify the module to update (c cancel): '))
                
                if module_ids is None:
                    continue
                clear()
                for module in module_ids:
                    try:    
                        print(BLUE_TEXT.format("Updating module {}...".format(module.name)))
                        module.button_immediate_upgrade()
                        print(BLUE_TEXT.format(f"Module {module.name} updated."))
                    except Exception:
                        print(traceback.format_exc())
                    
            elif option == 'Export translation':
                module = select_module(env, ['installed'], BLUE_TEXT.format('Specify the module to export translation (c cancel): '))
                
                if module is None:
                    continue
                clear()
                #TODO Create a selector of installed languages
                lang = input('Indicate the language (es_ES): ') or 'es_ES'
                
                export_path = input('Specify the destination path (default: es.po) (c cancel): ') or "es.po"
                if export_path == 'c':
                    continue
                if not export_path.endswith('.po'):
                    export_path += '.po'
                
                try:    
                    print(BLUE_TEXT.format("Exporting translation of the module {}...".format(module.name)))
                    export = env["base.language.export"].create(
                        {"lang": lang, "format": "po", "modules": [(6,0, module.ids)]}
                    )
                    export.act_getfile()
                    po_file = export.data
                    data = base64.b64decode(po_file)
                    with open(export_path, 'wb') as f:
                        f.write(data)
                    print(f"Translation of module {module.name} exported.")
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