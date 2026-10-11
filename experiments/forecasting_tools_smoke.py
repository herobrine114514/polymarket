"""Read-only, offline compatibility check for Metaculus forecasting-tools.

Run with an isolated Python environment containing forecasting-tools. This
builds five binary questions and one exclusive multiple-choice question from
one locally stored Fed event. The optional forecaster uses fixed test values;
it never reads a credential or sends a request.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from forecasting_tools import (BinaryQuestion, MultipleChoiceQuestion,
                               PredictedOption, PredictedOptionList,
                               ReasonedPrediction, TemplateBot)

from polycast.analysis import make_event_payload
from polycast.config import Config
from polycast.market import complete_fed_event, parse_utc
from polycast.store import digest_text, evidence_for_market


class OfflineFixtureBot(TemplateBot):
    """Exercise third-party report assembly without a model or web request."""

    async def run_research(self, question: BinaryQuestion) -> str:
        return question.background_info or ""

    async def _run_forecast_on_binary(
        self, question: BinaryQuestion, research: str
    ) -> ReasonedPrediction[float]:
        return ReasonedPrediction(
            prediction_value=0.25,
            reasoning="Synthetic adapter fixture; not a prediction.",
        )

    async def _run_forecast_on_multiple_choice(
        self, question: MultipleChoiceQuestion, research: str
    ) -> ReasonedPrediction[PredictedOptionList]:
        weights = (0.05, 0.15, 0.55, 0.20, 0.05)
        return ReasonedPrediction(
            prediction_value=PredictedOptionList(predicted_options=[
                PredictedOption(option_name=name, probability=weight)
                for name, weight in zip(question.options, weights, strict=True)
            ]),
            reasoning="Synthetic coherent fixture; not a prediction.",
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--framework-fixture", action="store_true",
                        help="run local synthetic reports through forecasting-tools")
    args = parser.parse_args()
    path = Config().database
    if not path.is_file():
        raise SystemExit(f"Local database does not exist: {path}")
    conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""SELECT * FROM markets WHERE category='macro'
            AND in_watchlist=1 ORDER BY end_at,event_id,id""").fetchall()
        groups: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            groups.setdefault(row["event_id"], []).append(row)
        group = next((items for items in groups.values()
                      if complete_fed_event([dict(item) for item in items])), None)
        if group is None:
            raise SystemExit("No complete five-bracket Fed event in the local watchlist")
        as_of = datetime.now(timezone.utc).isoformat(timespec="seconds")
        evidence = [dict(item) for item in evidence_for_market(conn, group[0])]
        evidence = [item for item in evidence
                    if all((stamp := parse_utc(item.get(field))) is not None and
                           stamp <= parse_utc(as_of)
                           for field in ("published_at", "first_seen_at", "updated_at"))]
        payload = make_event_payload([dict(row) for row in group], evidence, as_of)
    finally:
        conn.close()

    source_context = "\n".join(
        f"[{item['id']}] {item['source']} | published {item['published_at']} | "
        f"first seen {item['first_seen_at']} | {item['title']} | "
        f"{item['summary']} | {item['url']}"
        for item in payload["evidence"]
    )
    questions = [BinaryQuestion(
        question_text=item["question"],
        resolution_criteria=payload["resolution_rules"],
        fine_print=f"Polymarket event {payload['event_id']}; cutoff {payload['end_at']}. "
                   "These five outcomes form one exclusive event and must be assessed jointly.",
        background_info=f"Evidence frozen at {as_of} UTC.\n{source_context}",
        scheduled_resolution_time=parse_utc(payload["end_at"]),
    ) for item in payload["markets"]]
    assert len(questions) == 5
    assert all(item.question_text and item.resolution_criteria for item in questions)
    slots = [item["slot"] for item in payload["markets"]]
    assert len(slots) == len(set(slots)) == 5
    joint_question = MultipleChoiceQuestion(
        question_text=payload["event_title"],
        options=slots,
        resolution_criteria=payload["resolution_rules"],
        fine_print="Each option maps to one Polymarket outcome: " + ", ".join(
            f"{item['slot']}={item['market_id']}" for item in payload["markets"]
        ),
        background_info=f"Evidence frozen at {as_of} UTC.\n{source_context}",
        scheduled_resolution_time=parse_utc(payload["end_at"]),
    )
    result = {
        "status": "offline_mapping_ok",
        "event_id": payload["event_id"],
        "as_of": as_of,
        "binary_questions": len(questions),
        "joint_options": len(joint_question.options),
        "evidence_items": len(payload["evidence"]),
        "input_digest": digest_text(payload),
        "model_calls": 0,
        "external_requests_from_script": 0,
        "integration_limit": "No real probability generated; use one multiple-choice question for the exclusive Fed event.",
    }
    if args.framework_fixture:
        bot = OfflineFixtureBot(
            research_reports_per_question=1,
            predictions_per_research_report=2,
            enable_summarize_research=False,
            publish_reports_to_metaculus=False,
            folder_to_save_reports_to=None,
        )
        reports = asyncio.run(bot.forecast_questions(questions))
        joint_report = asyncio.run(bot.forecast_question(joint_question))
        total = sum(report.prediction for report in reports)
        joint_probabilities = joint_report.prediction.to_dict()
        invalid_joint_rejected = False
        try:
            PredictedOptionList(predicted_options=[
                PredictedOption(option_name=name, probability=0.25)
                for name in slots
            ])
        except ValueError:
            invalid_joint_rejected = True
        zero_tail_fixture = PredictedOptionList(predicted_options=[
            PredictedOption(option_name=name, probability=value)
            for name, value in zip(slots, (0.0, 0.1, 0.8, 0.1, 0.0), strict=True)
        ]).to_dict()
        result.update({
            "status": "offline_framework_fixture_ok",
            "framework_reports": len(reports),
            "fixture_probability_sum": round(total, 6),
            "joint_consistency": "accepted" if abs(total - 1.0) < 1e-6 else "rejected",
            "multiple_choice_probability_sum": round(sum(joint_probabilities.values()), 6),
            "invalid_multiple_choice_sum_rejected": invalid_joint_rejected,
            "zero_tail_after_library_normalization": round(zero_tail_fixture[slots[0]], 6),
        })
        assert (len(reports) == 5 and result["joint_consistency"] == "rejected"
                and result["multiple_choice_probability_sum"] == 1.0
                and invalid_joint_rejected
                and result["zero_tail_after_library_normalization"] > 0)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
