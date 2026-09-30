"""Non-interactive entry point: `otools <action> ...`. Without arguments otools opens the menu."""
import argparse
import logging
import os
import sys
import traceback

from . import core
from .discovery import discover_odoo, find_conf_file


EXAMPLES = """\
examples:
  otools -l
  otools -b my_db -o /backups/my_db.zip
  otools -r /backups/my_db.zip my_db_test --neutralize
  otools --duplicate my_db my_db_test
  otools --drop my_db_test -y
  otools -i my_db sale stock
  otools -t my_db my_module --lang es_ES -o es.po
  otools -p my_db account.move.line stock.warehouse
  otools --odoo-path /opt/odoo18 -l      # pick the installation when there are several
"""


def build_parser():
    parser = argparse.ArgumentParser(
        prog='otools',
        usage='otools                   interactive menu\n'
              '       otools ACTION [SETTINGS]  run one action and exit',
        description='Odoo database and module tools. Only the result is printed; '
                    'Odoo logs are hidden unless -v is given.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EXAMPLES,
    )
    actions = parser.add_argument_group('actions (choose one)').add_mutually_exclusive_group()
    actions.add_argument('-l', '--list', action='store_true', help='list databases')
    actions.add_argument('-b', '--backup', metavar='DB',
                         help='back up DB to a zip with dump + filestore (see -o)')
    actions.add_argument('-r', '--restore', nargs=2, metavar=('FILE', 'DB'),
                         help='restore a zip or pg_dump file into a new database DB')
    actions.add_argument('--drop', metavar='DB', help='drop a database (asks unless -y)')
    actions.add_argument('--duplicate', nargs=2, metavar=('DB', 'NEW_DB'),
                         help='duplicate a database with its filestore')
    actions.add_argument('--send', nargs=2, metavar=('DB', 'NEW_DB'),
                         help='copy DB as NEW_DB owned by the db_user of --dest-conf')
    actions.add_argument('--owner', nargs=2, metavar=('DB', 'USER'),
                         help='change the PostgreSQL owner of a database')
    actions.add_argument('--pg-users', action='store_true',
                         help='list PostgreSQL roles that can log in')
    actions.add_argument('--migrate', metavar='DB',
                         help='migrate DB with OpenUpgrade (see --upgrade-path)')
    actions.add_argument('-m', '--modules', nargs=2, metavar=('DB', 'STATE'),
                         help='list modules in STATE (installed, uninstalled, ...)')
    actions.add_argument('-i', '--install', nargs='+', metavar=('DB', 'MODULE'),
                         help='install modules: DB MODULE [MODULE ...]')
    actions.add_argument('-u', '--update', nargs='+', metavar=('DB', 'MODULE'),
                         help='update modules: DB MODULE [MODULE ...]')
    actions.add_argument('--uninstall', nargs='+', metavar=('DB', 'MODULE'),
                         help='uninstall modules (asks unless -y): DB MODULE [MODULE ...]')
    actions.add_argument('-t', '--export-po', nargs='+', metavar=('DB', 'MODULE'),
                         help='export translations to a .po file (see --lang, -o)')
    actions.add_argument('-p', '--pathfinder', nargs=3, metavar=('DB', 'ORIGIN', 'DESTINATION'),
                         help='shortest relational paths between two models')

    opts = parser.add_argument_group('settings')
    opts.add_argument('--odoo-path', help='directory containing odoo-bin '
                      '(default: $ODOO_PATH or the first installation found)')
    opts.add_argument('-c', '--conf', help='odoo.conf (default: $ODOO_CONF or next to odoo-bin)')
    opts.add_argument('-o', '--output', help='output file for --backup (DB.zip) and --export-po (LANG.po)')
    opts.add_argument('--no-copy', dest='copy', action='store_false',
                      help="--restore: keep the database UUID (it was moved, not copied)")
    opts.add_argument('--neutralize', action='store_true', help='--restore/--duplicate: neutralize (Odoo 16+)')
    opts.add_argument('--dest-conf', help='--send: odoo.conf of the destination installation')
    opts.add_argument('--upgrade-path', help='--migrate: OpenUpgrade scripts directory')
    opts.add_argument('--lang', default='es_ES', help='--export-po language (default: es_ES)')
    opts.add_argument('-y', '--yes', action='store_true', help='do not ask for confirmation')
    opts.add_argument('-v', '--verbose', action='store_true', help='show Odoo logs and tracebacks')
    return parser


def _resolve_installation(args, parser):
    if args.odoo_path:
        path = args.odoo_path
        # Accept both /opt/odoo18 and /opt/odoo18/odoo
        if os.path.exists(os.path.join(path, 'odoo', 'odoo-bin')):
            path = os.path.join(path, 'odoo')
        conf = args.conf or find_conf_file(path)
    else:
        path, conf = discover_odoo()
        conf = args.conf or conf
    if not conf:
        parser.error('no odoo.conf found, use -c/--conf')
    return path, conf


def _confirm(question, args):
    if args.yes:
        return True
    try:
        return input(f"{question} [y/N] ").strip().lower() in ('y', 'yes')
    except EOFError:
        return False


def _db_and_modules(values, flag, parser):
    if len(values) < 2:
        parser.error(f'{flag} needs DB and at least one MODULE')
    return values[0], values[1:]


def _dispatch(args, parser):
    if args.list:
        print('\n'.join(core.list_dbs()))

    elif args.pg_users:
        print('\n'.join(core.list_pg_users()))

    elif args.backup:
        output = args.output or f"{args.backup}.zip"
        if not output.endswith('.zip'):
            output += '.zip'
        core.dump_db(args.backup, output)
        print(f"Database {args.backup} dumped to {output}")

    elif args.restore:
        dump_file, db = args.restore
        core.restore_db(db, dump_file, copy=args.copy, neutralize=args.neutralize)
        print(f"Database {db} restored from {dump_file}")

    elif args.drop:
        if not _confirm(f"Drop database {args.drop}?", args):
            return 1
        core.drop_db(args.drop)
        print(f"Database {args.drop} dropped")

    elif args.duplicate:
        db, new_db = args.duplicate
        core.duplicate_db(db, new_db, neutralize=args.neutralize)
        print(f"Database {db} duplicated to {new_db}")

    elif args.send:
        if not args.dest_conf:
            parser.error('--send requires --dest-conf')
        db, new_db = args.send
        core.send_db(db, new_db, args.dest_conf)
        print(f"Database {db} copied to {new_db}")

    elif args.owner:
        db, user = args.owner
        core.change_owner(db, user)
        print(f"Database {db} owner changed to {user}")

    elif args.migrate:
        core.migrate_db(args.migrate, args.upgrade_path)
        print(f"Database {args.migrate} migrated")

    elif args.modules:
        db, state = args.modules
        with core.environment(db) as env:
            print('\n'.join(sorted(core.module_names(env, [state]))))

    elif args.install:
        db, names = _db_and_modules(args.install, '--install', parser)
        with core.environment(db) as env:
            core.update_module_list(env)
            core.install_modules(env, names)
        print(f"Installed: {' '.join(names)}")

    elif args.update:
        db, names = _db_and_modules(args.update, '--update', parser)
        with core.environment(db) as env:
            core.upgrade_modules(env, names)
        print(f"Updated: {' '.join(names)}")

    elif args.uninstall:
        db, names = _db_and_modules(args.uninstall, '--uninstall', parser)
        if not _confirm(f"Uninstall {' '.join(names)} from {db}?", args):
            return 1
        with core.environment(db) as env:
            core.uninstall_modules(env, names)
        print(f"Uninstalled: {' '.join(names)}")

    elif args.export_po:
        db, names = _db_and_modules(args.export_po, '--export-po', parser)
        output = args.output or f"{args.lang.split('_')[0]}.po"
        if not output.endswith('.po'):
            output += '.po'
        with core.environment(db, lang=args.lang) as env:
            core.export_translation(env, names, args.lang, output)
        print(f"Translation exported to {output}")

    elif args.pathfinder:
        from .pathfinder import pathfinder, format_path
        db, origin, destination = args.pathfinder
        with core.environment(db) as env:
            paths = pathfinder(env, origin, destination)
        if not paths:
            print(f"No path found from {origin} to {destination}", file=sys.stderr)
            return 1
        print('\n\n'.join(format_path(p) for p in paths))

    else:
        parser.error('choose an action')
    return 0


def run(argv):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.verbose:
        # Odoo configures levels/handlers but never re-enables this.
        logging.disable(logging.CRITICAL)
    path, conf = _resolve_installation(args, parser)
    try:
        core.init(path, conf)
        return _dispatch(args, parser)
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        if args.verbose:
            traceback.print_exc()
        print(f"Error: {str(e).strip() or type(e).__name__}", file=sys.stderr)
        return 1
