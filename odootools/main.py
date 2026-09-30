import os
import readline
import sys
import traceback
import logging

from . import core, ui
from .discovery import discover_all_installations, find_conf_file

if not logging.getLogger().handlers:
    # Odoo's own logging setup (odoo.netsvc.init_logger) is never invoked by
    # this standalone CLI, so without this the _logger.info/warning/error
    # calls in core are silently dropped by Python's default logging config.
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')

RED_TEXT = "\033[91m{}\033[0m"
GREEN_TEXT = "\033[92m{}\033[0m"
BLUE_TEXT = "\033[94m{}\033[0m"
YELLOW_TEXT = "\033[93m{}\033[0m"


def clear():
    os.system('clear')


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


def print_modules(module_names):
    columns = 3
    indexed_names = [f"{i + 1}) {name}" for i, name in enumerate(module_names)]
    max_cell_length = max((len(s) for s in indexed_names), default=20) + 2
    for i in range(0, len(indexed_names), columns):
        row = indexed_names[i:i + columns]
        print("".join(cell.ljust(max_cell_length) for cell in row))


def select_odoo_installation():
    """Ask which Odoo to use. Returns (odoo_path, odoo_conf, all_installations)."""
    odoo_paths = discover_all_installations()

    if len(odoo_paths) == 1:
        odoo_path = odoo_paths[0]
    elif len(odoo_paths) > 1:
        odoo_path = ui.select(odoo_paths, prompt="Select Odoo path:")
    else:
        odoo_path = input(
            'No Odoo installation found automatically.\n'
            'Specify the path to the directory containing odoo-bin: '
        ).strip()
        # Accept both /opt/odoo18 and /opt/odoo18/odoo
        if os.path.exists(os.path.join(odoo_path, 'odoo', 'odoo-bin')):
            odoo_path = os.path.join(odoo_path, 'odoo')

    odoo_conf = find_conf_file(odoo_path)
    if not odoo_conf:
        odoo_conf = input("Specify the path to the Odoo conf file: ").strip()

    clear()
    print(GREEN_TEXT.format(f"Odoo path: {odoo_path}"))
    print(GREEN_TEXT.format(f"Odoo conf: {odoo_conf}"))
    return odoo_path, odoo_conf, odoo_paths


def select_db():
    dbs = core.list_dbs() + ['Cancel']
    option = ui.select(dbs)
    clear()
    if option == 'Cancel':
        return None
    return option


def attempt(func, *args, done=None, **kwargs):
    """Run a core operation, printing the traceback instead of leaving the menu on failure."""
    try:
        func(*args, **kwargs)
    except Exception:
        print(traceback.format_exc())
        return False
    if done:
        print(done)
    return True


def main():
    if len(sys.argv) > 1:
        from .cli import run
        return run(sys.argv[1:])

    odoo_path, odoo_conf, odoo_paths = select_odoo_installation()
    try:
        core.init(odoo_path, odoo_conf)
    except Exception as e:
        print(e)
        return 1

    modules_updated_for = None

    def select_modules(env, states, selection_text):
        nonlocal modules_updated_for
        if modules_updated_for != env.cr.dbname:
            # Rescans the addons path on disk, which is expensive: only do it
            # once per environment (i.e. once per "Get Environment"/DB), not
            # every time this submenu is opened.
            print(YELLOW_TEXT.format('Updating modules list...'))
            try:
                core.update_module_list(env)
            except Exception:
                pass
            modules_updated_for = env.cr.dbname
        print("******************************")
        names = core.module_names(env, states)
        print_modules(names)
        set_completer(make_modules_completer(names))
        user_input = input(selection_text)
        if user_input == 'c':
            return None
        try:
            return core.get_modules(env, user_input.split(), states).mapped('name')
        except ValueError as e:
            print(e)
            return None

    env = None
    env_ctx = None

    def close_env():
        nonlocal env, env_ctx
        if env_ctx:
            env_ctx.__exit__(None, None, None)
            print("Cursor closed.")
        env = env_ctx = None

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

            option = ui.select(options, prompt=prompt)
            clear()

            if option == 'Restore DB':
                dump_path = input('Specify the file path: ')
                db_name = input('Enter the name of the database (c to cancel): ')
                if db_name == 'c':
                    continue
                copy = ui.confirm('Is it a copy?', 'y')
                neutralize = ui.confirm('Neutralize DB?:', 'n')
                attempt(core.restore_db, db_name, dump_path, copy=copy, neutralize=neutralize,
                        done=GREEN_TEXT.format(f"Database {db_name} restored."))

            elif option == 'Drop DB':
                print(RED_TEXT.format("Drop DB"))
                db_name = select_db()
                if db_name and ui.confirm(RED_TEXT.format(f"Are you sure you want to drop database {db_name}?")):
                    print(RED_TEXT.format("Dropping database..."))
                    attempt(core.drop_db, db_name, done=RED_TEXT.format(f"Database {db_name} dropped."))

            elif option == 'Backup DB':
                print(BLUE_TEXT.format("Backup DB"))
                db_name = select_db()
                if not db_name:
                    continue
                backup_file = input(f'Specify the path to the backup (default: {db_name}.zip): ') or f"{db_name}.zip"
                if not backup_file.endswith('.zip'):
                    backup_file += '.zip'
                print(BLUE_TEXT.format("Starting database dump..."))
                if not attempt(core.dump_db, db_name, backup_file,
                               done=f"Database {db_name} dumped to {backup_file}."):
                    print(RED_TEXT.format(f"Database {db_name} dump failed."))

            elif option == 'Duplicate DB':
                print(BLUE_TEXT.format("Duplicate DB"))
                db_name = select_db()
                if not db_name:
                    continue
                new_db_name = input('Enter the name of the new DB: ')
                neutralize = ui.confirm('Neutralize DB?:', 'n')
                attempt(core.duplicate_db, db_name, new_db_name, neutralize=neutralize,
                        done=GREEN_TEXT.format(f"Database {db_name} duplicated to {new_db_name}."))

            elif option == 'Send DB':
                print(BLUE_TEXT.format("Send DB"))
                db_name = select_db()
                if not db_name:
                    continue
                if len(odoo_paths) < 2:
                    print(RED_TEXT.format("No other Odoo installation found to send the DB to."))
                    continue
                to = ui.select(odoo_paths, prompt="Select destination Odoo path:")
                dest_conf = os.path.join(os.path.dirname(to), 'odoo.conf')
                if not os.path.isfile(dest_conf):
                    dest_conf = input("Specify the path to destination Odoo conf file: ")
                new_db_name = input('Enter the name of the new DB: ')
                print(BLUE_TEXT.format(f"Creating database {new_db_name} from template {db_name}..."))
                attempt(core.send_db, db_name, new_db_name, dest_conf, done=GREEN_TEXT.format("DB copied."))

            elif option == 'Change DB user':
                print(BLUE_TEXT.format("Change DB user"))
                db_name = select_db()
                if not db_name:
                    continue
                user = ui.select(core.list_pg_users() + ['Cancel'], prompt="Select the new DB user:")
                if user != 'Cancel':
                    attempt(core.change_owner, db_name, user,
                            done=GREEN_TEXT.format(f"DB {db_name} owner changed to {user}."))

            elif option == 'Migrate DB':
                print(BLUE_TEXT.format("Migrate DB"))
                db_name = select_db()
                if not db_name:
                    continue
                upgrade_path = core.default_upgrade_path()
                if not os.path.exists(upgrade_path):
                    print(RED_TEXT.format(f"OpenUpgrade path not found: {upgrade_path}"))
                    upgrade_path = input("Specify the path to OpenUpgrade scripts: ")
                print(RED_TEXT.format("Migrating database..."))
                attempt(core.migrate_db, db_name, upgrade_path,
                        done=GREEN_TEXT.format(f"Database {db_name} migrated."))

            elif option == 'List DBs':
                for i, db in enumerate(core.list_dbs(), 1):
                    print(i, db)

            elif option == 'Get Environment':
                close_env()
                print(GREEN_TEXT.format("Get Environment"))
                db_name = select_db()
                if not db_name:
                    continue
                env_ctx = core.environment(db_name)
                env = env_ctx.__enter__()

            elif option == 'Uninstall Module':
                names = select_modules(
                    env,
                    ['installed', 'to upgrade'],
                    RED_TEXT.format('Specify the module(s) to uninstall (space-separated, c to cancel): '),
                )
                if names and ui.confirm(RED_TEXT.format(f'Are you sure you want to uninstall {names}?: ')):
                    clear()
                    print(RED_TEXT.format(f"Uninstalling {names}..."))
                    attempt(core.uninstall_modules, env, names, done=RED_TEXT.format(f"Uninstalled {names}."))

            elif option == 'Install Module':
                names = select_modules(
                    env,
                    ['uninstalled'],
                    GREEN_TEXT.format('Specify the module(s) to install (space-separated, c to cancel): '),
                )
                if names:
                    clear()
                    print(GREEN_TEXT.format(f"Installing {names}..."))
                    attempt(core.install_modules, env, names, done=f"Installed {names}.")

            elif option == 'Update Module':
                names = select_modules(
                    env,
                    ['installed'],
                    BLUE_TEXT.format('Specify the module(s) to update (space-separated, c to cancel): '),
                )
                if names:
                    clear()
                    print(BLUE_TEXT.format(f"Updating {names}..."))
                    attempt(core.upgrade_modules, env, names, done=BLUE_TEXT.format(f"Updated {names}."))

            elif option == 'Export translation':
                names = select_modules(
                    env,
                    ['installed'],
                    BLUE_TEXT.format('Specify the module to export translation (c to cancel): '),
                )
                if not names:
                    continue
                clear()
                lang = input('Indicate the language (default: es_ES): ') or 'es_ES'
                set_completer(path_completer)
                export_path = input('Specify the destination path (default: es.po, c to cancel): ') or "es.po"
                if export_path == 'c':
                    continue
                if not export_path.endswith('.po'):
                    export_path += '.po'
                print(BLUE_TEXT.format(f"Exporting translation for {names}..."))
                attempt(core.export_translation, env, names, lang, export_path,
                        done=f"Translation exported to {export_path}.")

            elif option == 'Exit':
                break

    except Exception:
        print(traceback.format_exc())

    finally:
        try:
            close_env()
        except Exception:
            print(traceback.format_exc())


if __name__ == '__main__':
    sys.exit(main())
