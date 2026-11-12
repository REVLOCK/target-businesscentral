from typing import List

# ── Required config keys ────────────────────────────────────────────────────────
REQUIRED_CONFIG_KEYS: List[str] = [
    "tenant_domain",    # e.g. contoso.onmicrosoft.com
    "client_id",        # Azure AD app client ID
    "client_secret",    # Azure AD app client secret
    "environment",      # BC environment, e.g. Sandbox_UK
    "company_id",       # BC company GUID
    "input_path",       # directory containing JournalEntries.csv
]

# ── Auth (Microsoft OAuth2) ─────────────────────────────────────────────────────
AUTH_URL_TEMPLATE = (
    "https://login.microsoftonline.com/{tenant_domain}/oauth2/v2.0/token"
)
BC_SCOPE = "https://api.businesscentral.dynamics.com/.default"
GRANT_TYPE = "client_credentials"

# ── Business Central bulk batch API ────────────────────────────────────────────
# Replaces single-record bc_gj_lines. Accepts up to 100 records per call.
BC_BATCH_URL_TEMPLATE = (
    "https://api.businesscentral.dynamics.com/v2.0/{tenant_domain}/{environment}"
    "/api/Booksy/Chargebee/v1.0/$batch"
)

# URL used inside each batch request item (relative, no base URL)
BC_BATCH_ITEM_URL_TEMPLATE = "companies({company_id})/bc_gj_lines"

BATCH_SIZE = 100  # max records per $batch call

DEFAULT_TIMEOUT_SECONDS = 30
MAX_RETRIES = 4
RETRY_BACKOFF_BASE = 2  # seconds, doubles each attempt

# HTTP statuses treated as a successful create inside a $batch response item
BATCH_SUCCESS_STATUSES = (200, 201, 204)

# ── Input CSV ───────────────────────────────────────────────────────────────────
INPUT_FILENAME = "JournalEntries.csv"

# ── BC API payload field names ──────────────────────────────────────────────────
BC_POSTING_DATE = "posting_date"
BC_DOCUMENT_DATE = "document_date"
BC_EXT_DOCUMENT_NO = "ext_document_no"
BC_ACCOUNT_NO = "account_no"
BC_BAL_ACCOUNT_NO = "bal_account_no"
BC_DESCRIPTION = "description"
BC_AMOUNT = "amount"
BC_CURRENCY_CODE = "currency_code"
BC_DIM_COUNTRY = "dim_country"  # Country Code
BC_DIM_PROJECT = "dim_project"  # Project Code (always blank)

# ── Amount sign convention ──────────────────────────────────────────────────────
# RevRec journal: positive = Credit, negative = Debit
POSTING_TYPE_DEBIT = "DEBIT"
POSTING_TYPE_CREDIT = "CREDIT"
