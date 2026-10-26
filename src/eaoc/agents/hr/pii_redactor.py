"""
EAOC HR Agent PII Redactor & Data Minimization Engine
Enforces strict PII minimization across HR datasets: masks sensitive fields
(DOB, salary, home address, SSN, personal contact details) when caller lacks
explicit PII permission, and enforces non-PII projections for org chart queries.
"""

from typing import Any, Dict, List, Set, Tuple

# Sensitive personal fields specified in Phase 0 (Agent 8, AGT-HR)
SENSITIVE_PII_FIELDS: Set[str] = {
    "dob",
    "date_of_birth",
    "birth_date",
    "salary",
    "compensation",
    "base_salary",
    "bonus",
    "home_address",
    "address",
    "street_address",
    "ssn",
    "social_security_number",
    "personal_email",
    "personal_phone",
    "phone_number",
    "home_phone",
    "emergency_contact",
}

# Permitted non-PII fields for org chart representations
ORG_CHART_ALLOWED_FIELDS: Set[str] = {
    "employee_id",
    "name",
    "first_name",
    "last_name",
    "title",
    "job_title",
    "department",
    "manager_id",
    "manager_name",
    "reporting_level",
    "location",
}


class HrPiiRedactor:
    """
    Engine for PII minimization and field redaction in HR queries.
    """

    @classmethod
    def redact_record(
        cls, record: Dict[str, Any], has_pii_permission: bool
    ) -> Tuple[Dict[str, Any], bool]:
        """
        Redacts sensitive fields in a single employee record if has_pii_permission is False.
        Returns (sanitized_record, pii_redacted_flag).
        """
        if has_pii_permission:
            return dict(record), False

        clean_record = {}
        redacted = False

        for k, v in record.items():
            k_lower = k.strip().lower()
            if k_lower in SENSITIVE_PII_FIELDS:
                clean_record[k] = "[REDACTED]"
                redacted = True
            else:
                clean_record[k] = v

        return clean_record, redacted

    @classmethod
    def redact_records(
        cls, records: List[Dict[str, Any]], has_pii_permission: bool
    ) -> Tuple[List[Dict[str, Any]], bool]:
        """
        Applies PII redaction across a list of employee records.
        """
        if has_pii_permission:
            return [dict(r) for r in records], False

        sanitized_list = []
        any_redacted = False

        for rec in records:
            clean_rec, was_redacted = cls.redact_record(rec, has_pii_permission=False)
            sanitized_list.append(clean_rec)
            if was_redacted:
                any_redacted = True

        return sanitized_list, any_redacted

    @classmethod
    def filter_org_chart(cls, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Enforces strict non-PII field projection for org chart hierarchies.
        Strips any field not in ORG_CHART_ALLOWED_FIELDS regardless of caller permissions.
        """
        filtered_records = []
        for rec in records:
            clean_node = {}
            for k, v in rec.items():
                if k.strip().lower() in ORG_CHART_ALLOWED_FIELDS:
                    clean_node[k] = v
            filtered_records.append(clean_node)
        return filtered_records
