"""Hard input limits (enforced before any parsing)."""
MAX_BYTES = 5 * 1024 * 1024
MAX_BASE64_CHARS = (MAX_BYTES * 4) // 3 + 64
MAX_ROWS = 5000
MAX_REF_LEN = 2000
MAX_XLSX_UNCOMPRESSED = 50 * 1024 * 1024
MAX_ISSUES_SHOWN = 20
EXISTING_LINES_CAP = 10000
DUP_WINDOW_DAYS = 3
OUTPUT_SHEET = "Bank Transactions"
OUTPUT_COLUMNS = ("Date", "Label", "Amount")                       # "Label" is what Odoo's importer maps to payment_ref
LEGACY_OUTPUT_COLUMNS = ("Date", "Payment Reference", "Amount")   # v1 headers: Odoo maps them to the hidden payment_reference
ODOO_FIELD_MAP = {"Date": "date", "Label": "payment_ref", "Amount": "amount"}
NORMALIZER_VERSION = "2"
