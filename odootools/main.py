import logging
import sys

from .cli import run

if not logging.getLogger().handlers:
    # Odoo's own logging setup (odoo.netsvc.init_logger) is never invoked by
    # this standalone CLI, so without this the _logger.info/warning/error
    # calls in core are silently dropped by Python's default logging config.
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')


def main():
    return run(sys.argv[1:])


if __name__ == '__main__':
    sys.exit(main())
