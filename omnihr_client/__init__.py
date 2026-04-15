"""OmniHR API client — schema-driven, multi-tenant.

Mirrors the bash recipes we proved end-to-end:
- POST /expense/1.0/document/                      (file upload, multipart)
- POST /expense/2.0/expense-metadata-v2/draft/     (draft create)
- POST /expense/2.0/expense-metadata/{id}/quick-actions/  (delete/submit/etc)
- GET  /expense/2.0/expense-metadata/metadata/submissions/?status_filters=... (list)
- GET  /expense/3.0/category/policy-tree/{employee_id}/   (policies)
- GET  /expense/3.0/user/{uid}/policy/{pid}/expense-form-config/?receipt_date=YYYY-MM-DD  (per-policy schema)
- POST /auth/token/                                (password login)
- POST /auth/token/google/                         (Google ID-token login)
- POST /auth/token/refresh/                        (refresh access)
- POST /auth/logout/

NEVER hardcode field_id, policy_id, option_id. Discover via /policy-tree/ +
/expense-form-config/ and resolve by form_data_type + label.
"""

from .client import OmniHRClient
from .schema import FormSchema, get_schema, invalidate_schema
from .exceptions import OmniHRError, AuthError, SchemaDriftError, ValidationError

__all__ = [
    "OmniHRClient",
    "FormSchema",
    "get_schema",
    "invalidate_schema",
    "OmniHRError",
    "AuthError",
    "SchemaDriftError",
    "ValidationError",
]
