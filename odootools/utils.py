import sys, os, logging, traceback
from pathlib import Path
from packaging import version


_logger = logging.getLogger(__name__)


try:
    # TODO: Better implementation
    CURRENT_DIR = str(Path(__file__).resolve().parent)
    VERSIONS = ['12', '13', '14', '15', '16', '17', '18', '19']
    root_path = [f"/opt/odoo{version}" for version in VERSIONS if f"odoo{version}" in CURRENT_DIR]

    odoo_path = os.path.join(root_path[0], "odoo")
    odoo_conf = os.path.join(root_path[0], "odoo.conf")
except Exception:
    _logger.error("Error retrieving the Odoo path")
    
sys.path.append(CURRENT_DIR) # Resolves Jupyter error
sys.path.append(odoo_path)
import odoo
from odoo import api, SUPERUSER_ID

class Tools():

    def __init__(self, db_name, odoo_conf=odoo_conf, uid=SUPERUSER_ID, context={'lang': 'es_ES'}):        
        odoo.tools.config.parse_config(['-c', odoo_conf])
        registry = odoo.modules.registry.Registry(db_name)
        cursor = registry.cursor()
        self.env = api.Environment(cursor, uid, context)
        
    def close(self):
        if self.env.cr and not self.env.cr.closed:
            self.env.cr.close()
            print("Cursor closed.")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        
    def get_env(self):
        return self.env
    
    def dump_db(self, path):
        """
        Creates a backup of the current database in ZIP format.

        This method generates a backup of the database associated with the current
        environment. The backup is saved as a .zip file in the "backups" directory
        within the base directory. If the directory does not exist, it is created.

        The backup file is named after the database name with a .zip extension.

        Raises:
            OSError: If there is an error creating the directory or writing the file.
        """

        os.makedirs(path, exist_ok=True)
        backup_file = path / f"{self.env.cr.dbname}.zip"
        with open(backup_file, "wb") as destiny:
            odoo.service.db.dump_db(self.env.cr.dbname, destiny, "zip")
        
    def print_report(self, report_xml_id: str, res_id: int, report_file: str="report.pdf"):
        """
        Prints a report to a PDF file.
        Note: If the report prints without format be sure the odoo service is running.
        
        :param report_xml_id: The XML ID of the report to print.
        :param res_id: The ID of the record to print the report for.
        :param report_file: The path to save the PDF file.
        """
        if not report_file.endswith('.pdf'):
            raise Exception('The report_file must end with .pdf')
        
        report = self.env.ref(report_xml_id)

        pdf_content, _ = self.env['ir.actions.report']._render_qweb_pdf(report.id, res_id)

        with open(report_file, "wb") as f:
            f.write(pdf_content)

        print(f"PDF generated and saved in: {report_file}")
        
    
    def update_records_from_xml(self, module_name: str, file_name: str):
        """
        Update the records in the .xml file.

        :param module_name: Name of the module containing the .xml file
        :param file_name: Path to the .xml file in the module's manifest
        """
        if version.parse(odoo.release.version) >= version.parse('17.0'):
            odoo.tools.convert_file(self.env, module_name, file_name, {})
        else:    
            odoo.tools.convert_file(self.env.cr, module_name, file_name, {})
        
    def report_editor(self, module_name: str, report_file: str, action_xml_id: str, res_id: int, file_name: str ):
        """
        Edits the records in the .xml file and prints the report.

        This method loads the records from the .xml file and prints the report associated with the
        given action_xml_id. The report is saved in a .pdf file with the given file_name.

        The method will continue to ask for input until the user presses 'c' to close.

        :param module_name: Name of the module containing the .xml file
        :param report_file: Path to the .xml file in the module's manifest
        :param action_xml_id: The XML ID of the report to print
        :param res_id: The ID of the record to print the report for
        :param file_name: The path to save the PDF file
        """
        while True:
            try:
                self.update_records_from_xml(module_name, report_file)
                self.print_report(f'{module_name}.{action_xml_id}', res_id, file_name)
                if input("Press enter to continue (c to close)") == 'c': break
            except Exception as e:
                print(traceback.format_exc())
                if input("Press enter to continue (c to close)") == 'c': break
        
    def print_sale_order(self, res_id: int):
        """
        Prints a sale order report.

        :param res_id: id of the sale order record
        """
        self.print_report('sale', 'sale.action_report_saleorder', res_id, report_file='sale_order')
        
    def print_invoice(self, res_id: int):
        """
        Prints an invoice report.

        :param res_id: id of the invoice record
        """
        self.print_report('account', 'account.account_invoices', res_id, report_file='invoice')
        
    def print_delivery(self, res_id: int):
        """
        Print a delivery report.

        :param res_id: id of the move
        """
        self.print_report('stock', 'stock.action_report_delivery', res_id, report_file='delivery')
        
    def print_picking(self, res_id: int):
        """
        Print a picking report.

        :param res_id: id of the picking record
        """
        self.print_report('stock', 'stock.action_report_picking', res_id, report_file='picking')
        
    def print_packages(self, res_id: int):
        """
        Print a picking packages report.

        :param res_id: id of the picking record
        """
        self.print_report('stock', 'stock.action_report_picking_packages', res_id, report_file='packages')
        
    def uninstall_module(self, module: str):
        """
        Uninstall module.

        :param module: name of the module to uninstall
        """
        module_id = self.env['ir.module.module'].search([('name', '=', module)])
        
        if module_id:
            print(f"Uninstalling {module_id.name}.")
            module_id.sudo().button_immediate_uninstall()
        else:
            print(f"Module {module} not found.")