"""
Feedback routes — per-recommendation outcome tracking.

  POST /api/reports/{report_id}/feedback   submit feedback for a report
  GET  /api/reports/{report_id}/feedback   retrieve existing feedback
  GET  /api/feedback/summary               aggregated stats by category / priority
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Body, HTTPException, Security

from app import deps
from app.models import FeedbackSummary, ReportFeedback
from app.security import require_key

logger = logging.getLogger(__name__)

router = APIRouter(tags=["feedback"])

_FEEDBACK_EXAMPLE = {
    "example": {
        "summary": "Feedback on 3 recommendations",
        "value": {
            "items": [
                {"rec_index": 0, "status": "resolved",     "note": "Scaled the pod, CPU dropped immediately."},
                {"rec_index": 1, "status": "not_relevant", "note": None},
                {"rec_index": 2, "status": "not_tried",    "note": None},
            ]
        },
    }
}


@router.post(
    "/api/reports/{report_id}/feedback",
    response_model=ReportFeedback,
    status_code=201,
    summary="Submit recommendation feedback",
    description="""
Rate individual recommendations from a report.

Each item carries:
- **rec_index** — 0-based index into the report's `recommendations` array
- **status** — `resolved` | `partial` | `not_relevant` | `not_tried`
- **note** — optional free-text comment

Submitting multiple times for the same report is idempotent — the latest
status per recommendation wins.

The collected data feeds `GET /api/feedback/summary` to show which
recommendation categories actually work for this infrastructure.
""",
    responses={
        404: {"description": "Report not found."},
        401: {"description": "Missing or invalid X-API-Key."},
    },
)
def submit_feedback(
    report_id: str,
    body: ReportFeedback = Body(openapi_examples=_FEEDBACK_EXAMPLE),
    _: None = Security(require_key),
) -> ReportFeedback:
    report = deps.store.get_report(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"Report '{report_id}' not found.")

    now = datetime.now(timezone.utc).isoformat()

    # Denormalize category/priority/title from the report so aggregation
    # queries don't need to join back to the report payload.
    enriched_items = []
    for item in body.items:
        if item.rec_index >= len(report.recommendations):
            raise HTTPException(
                status_code=422,
                detail=f"rec_index {item.rec_index} out of range "
                       f"(report has {len(report.recommendations)} recommendations).",
            )
        rec = report.recommendations[item.rec_index]
        enriched_items.append(item.model_copy(update={
            "category":     rec.category,
            "priority":     rec.priority,
            "title":        rec.title,
            "submitted_at": now,
        }))

    feedback = ReportFeedback(
        report_id=report_id,
        items=enriched_items,
        submitted_at=now,
    )
    deps.store.save_feedback(feedback)
    logger.info("INFFBK000 feedback_saved", extra={
        "report_id": report_id,
        "items":     len(enriched_items),
    })
    return feedback


@router.get(
    "/api/reports/{report_id}/feedback",
    response_model=ReportFeedback,
    summary="Get feedback for a report",
    description="Returns previously submitted feedback for a report, or 404 if none has been submitted.",
    responses={
        404: {"description": "No feedback submitted for this report."},
        401: {"description": "Missing or invalid X-API-Key."},
    },
)
def get_feedback(
    report_id: str,
    _: None = Security(require_key),
) -> ReportFeedback:
    feedback = deps.store.get_feedback(report_id)
    if feedback is None:
        raise HTTPException(status_code=404, detail=f"No feedback for report '{report_id}'.")
    return feedback


@router.get(
    "/api/feedback/summary",
    response_model=FeedbackSummary,
    summary="Aggregated feedback summary",
    description="""
Returns resolution rates aggregated by **recommendation category** and **priority**.

Use this to answer questions like:
- Which category of recommendation tends to actually fix the problem?
- Are high-priority recommendations more effective than low-priority ones?
- What is the overall operator satisfaction rate?

`resolution_rate` = (resolved + partial) / total for each bucket.
""",
    responses={401: {"description": "Missing or invalid X-API-Key."}},
)
def feedback_summary(_: None = Security(require_key)) -> FeedbackSummary:
    return deps.store.feedback_summary()
