import sys
import os
import logging
import traceback
from pathlib import Path
from typing import List, Optional, Union
from packaging import version as pkg_version
from .discovery import discover_odoo, find_conf_file


_logger = logging.getLogger(__name__)


_odoo_path, _odoo_conf = discover_odoo()

sys.path.append(str(Path(__file__).resolve().parent))  # Resolves Jupyter error
if _odoo_path:
    sys.path.append(_odoo_path)

import odoo
from odoo import api, SUPERUSER_ID

# Module-level defaults used by Tools.__init__ when the caller does not specify them
odoo_conf = _odoo_conf or ""


class Tools:

    def __init__(self, db_name, odoo_conf=odoo_conf, uid=SUPERUSER_ID, context=None):
        if context is None:
            context = {'lang': 'es_ES'}
        odoo.tools.config.parse_config(['-c', odoo_conf])
        self._odoo_version = pkg_version.parse(odoo.release.version)
        registry = odoo.modules.registry.Registry(db_name)
        cursor = registry.cursor()

        # Odoo 12-14 stores active environments in a werkzeug LocalStack.
        # Outside a real request (scripts, Jupyter) that stack is empty, so
        # api.Environment.__new__ raises AttributeError: 'environments'.
        # Entering manage() initialises the stack for the current greenlet/thread.
        # The method no longer exists in Odoo 15+, so we guard with hasattr().
        self._env_manager = None
        if hasattr(api.Environment, 'manage'):
            self._env_manager = api.Environment.manage()
            self._env_manager.__enter__()

        self.env = api.Environment(cursor, uid, context)

    def close(self):
        if self.env.cr and not self.env.cr.closed:
            self.env.cr.close()
            print("Cursor closed.")
        if self._env_manager is not None:
            self._env_manager.__exit__(None, None, None)
            self._env_manager = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def get_env(self):
        return self.env

    def dump_db(self, path):
        """Creates a backup of the current database in ZIP format."""
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        backup_file = path / f"{self.env.cr.dbname}.zip"
        with open(backup_file, "wb") as destiny:
            odoo.service.db.dump_db(self.env.cr.dbname, destiny, "zip")

    def print_report(self, report_xml_id: str, res_id: int, report_file: str = "report.pdf"):
        """
        Generates a PDF report and saves it to a file.
        Note: If the report prints without formatting, ensure the Odoo service is running.

        :param report_xml_id: Full XML ID of the report action (e.g. 'sale.action_report_saleorder')
        :param res_id: ID of the record to render
        :param report_file: Destination path for the PDF file (must end with .pdf)
        """
        if not report_file.endswith('.pdf'):
            raise ValueError('The report_file must end with .pdf')

        report = self.env.ref(report_xml_id)

        # Report rendering API changed across Odoo versions:
        # Odoo 12-13: render_qweb_pdf (public) on the report record
        # Odoo 14-16: _render_qweb_pdf on the report record
        # Odoo 17+:   _render_qweb_pdf as @api.model with explicit report_ref
        if self._odoo_version >= pkg_version.parse('17.0'):
            pdf_content, _ = self.env['ir.actions.report']._render_qweb_pdf(report, [res_id])
        elif self._odoo_version >= pkg_version.parse('14.0'):
            pdf_content, _ = report._render_qweb_pdf([res_id])
        else:
            pdf_content, _ = report.render_qweb_pdf([res_id])

        with open(report_file, "wb") as f:
            f.write(pdf_content)

        print(f"PDF generated and saved in: {report_file}")

    def update_records_from_xml(self, module_name: str, file_name: str):
        """
        Reloads records from an XML file within a module.

        :param module_name: Name of the module containing the XML file
        :param file_name: Path to the XML file as listed in the module manifest
        """
        # convert_file first argument changed in Odoo 17: env instead of cr
        if self._odoo_version >= pkg_version.parse('17.0'):
            odoo.tools.convert_file(self.env, module_name, file_name, {})
        else:
            odoo.tools.convert_file(self.env.cr, module_name, file_name, {})

    def report_editor(self, module_name: str, report_file: str, action_xml_id: str, res_id: int, file_name: str):
        """
        Interactive loop that reloads XML records and regenerates a PDF report on each iteration.

        :param module_name: Module containing the report template
        :param report_file: XML file path within the module
        :param action_xml_id: XML ID of the report action (without module prefix)
        :param res_id: ID of the record to render
        :param file_name: Destination PDF file path (must end with .pdf)
        """
        while True:
            try:
                self.update_records_from_xml(module_name, report_file)
                self.print_report(f'{module_name}.{action_xml_id}', res_id, file_name)
                if input("Press enter to continue (c to close): ") == 'c':
                    break
            except Exception:
                print(traceback.format_exc())
                if input("Press enter to continue (c to close): ") == 'c':
                    break

    def print_sale_order(self, res_id: int):
        """Generates a sale order PDF report."""
        self.print_report('sale.action_report_saleorder', res_id, report_file='sale_order.pdf')

    def print_invoice(self, res_id: int):
        """Generates an invoice PDF report."""
        self.print_report('account.account_invoices', res_id, report_file='invoice.pdf')

    def print_delivery(self, res_id: int):
        """Generates a delivery order PDF report."""
        self.print_report('stock.action_report_delivery', res_id, report_file='delivery.pdf')

    def print_picking(self, res_id: int):
        """Generates a picking PDF report."""
        self.print_report('stock.action_report_picking', res_id, report_file='picking.pdf')

    def print_packages(self, res_id: int):
        """Generates a picking packages PDF report."""
        self.print_report('stock.action_report_picking_packages', res_id, report_file='packages.pdf')

    def uninstall_module(self, module: Union[str, List[str]]):
        """
        Uninstalls one or more Odoo modules.

        :param module: Module name or list of module names to uninstall
        """
        if isinstance(module, str):
            module = [module]
        for mod in module:
            module_id = self.env['ir.module.module'].search([('name', '=', mod)])
            if module_id and module_id.state in ('installed', 'to upgrade'):
                print(f"Uninstalling {module_id.name}.")
                module_id.sudo().button_immediate_uninstall()
            else:
                print(f"Module {mod} not found.")
