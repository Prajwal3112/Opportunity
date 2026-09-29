"""Pure adapters from normalized source records into candidate-engine input."""
from wb_connector.models import ProcurementNotice

from .models import CandidateInput


def candidate_input_from_procurement(
    notice: ProcurementNotice, *, project_metadata: dict[str, str] | None = None
) -> CandidateInput:
    """Map normalized procurement data without touching its original raw payload."""
    return CandidateInput(
        source_external_id=notice.external_id,
        project_id=notice.project_id,
        bid_description=notice.bid_description,
        notice_text=notice.notice_text,
        project_name=notice.project_name,
        notice_type=notice.notice_type,
        procurement_method_name=notice.procurement_method_name,
        project_country_name=notice.project_country_name,
        project_metadata=project_metadata or {},
        # Carried so the engine can rank a live tender above a better-matching but
        # already-awarded one, and so a result row can say how to respond.
        notice_status=notice.notice_status,
        submission_deadline_date=notice.submission_deadline_date,
        bid_reference_no=notice.bid_reference_no,
        contact_email=notice.contact_email,
        source_url=notice.source_url,
    )
