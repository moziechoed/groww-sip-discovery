"""Schema enums, word limits, checkpoint resume, and Gemini response_schema sanitization."""
from typing import Any, List

import pytest
from pydantic import ValidationError

from core.checkpoint import Checkpoint
from core.llm_client import GeminiProvider, LLMClient, ProviderError, clean_schema_for_gemini, extract_error_info
from core.schemas import EnrichedPost, EnrichmentFields, Opportunity, SmokeTestResult
from tests.conftest import FakeProvider, ok_response


def _find_forbidden_keys(obj: Any, forbidden=frozenset({"additionalProperties", "additional_properties"})) -> List[str]:
    found: List[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in forbidden:
                found.append(k)
            found.extend(_find_forbidden_keys(v, forbidden))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_find_forbidden_keys(item, forbidden))
    return found


def test_minimal_enrichment_defaults_to_unknown():
    e = EnrichmentFields(post_id="p1", is_relevant=True)
    assert e.outcome == "unknown" and e.journey_stage == "unknown" and e.trigger_type == "unknown"
    assert e.segment_signals.employment == "unknown"
    assert e.platform_tag == "unspecified"


def test_invalid_enum_rejected():
    with pytest.raises(ValidationError):
        EnrichmentFields(post_id="p1", is_relevant=True, outcome="quit")


def test_extra_fields_rejected():
    with pytest.raises(ValidationError):
        EnrichmentFields(post_id="p1", is_relevant=True, text="echoed post text")


def test_word_limits_truncate_free_text_but_not_evidence():
    long = " ".join(["word"] * 40)
    e = EnrichmentFields(post_id="p1", is_relevant=True, checking_what=long, unmet_need=long, evidence_span=long)
    assert len(e.checking_what.split()) == 15
    assert len(e.unmet_need.split()) == 25
    assert e.evidence_span == long


def test_checkpoint_resume(tmp_path):
    c = Checkpoint("enrich", tmp_path)
    c.mark_done(["a", "b"])
    c2 = Checkpoint("enrich", tmp_path)
    assert c2.pending(["a", "b", "c"]) == ["c"]
    assert c2.done_count == 2


@pytest.mark.parametrize(
    "schema_type",
    [SmokeTestResult, EnrichmentFields, List[EnrichmentFields], EnrichedPost, Opportunity],
)
def test_schema_sent_to_gemini_has_no_additional_properties(schema_type):
    from google.genai import _transformers as t

    cleaned = clean_schema_for_gemini(schema_type)
    assert _find_forbidden_keys(cleaned) == []

    # Also verify after google-genai's t_schema transformation into types.Schema
    sdk_schema = t.t_schema(None, cleaned)
    dumped = sdk_schema.model_dump(exclude_none=True)
    assert _find_forbidden_keys(dumped) == []


def test_llm_client_passes_cleaned_schema_and_disables_afc(settings, tracker):
    provider = FakeProvider([ok_response()])
    client = LLMClient(settings, tracker, provider=provider, sleep=lambda s: None)
    client.generate_json(prompt="p", schema=SmokeTestResult, batch_id="b0", max_output_tokens=200)
    sent_schema = provider.calls[0]["response_schema"]
    assert isinstance(sent_schema, dict)
    assert _find_forbidden_keys(sent_schema) == []

    # Verify GeminiProvider.build_generate_config disables automatic_function_calling and strips additionalProperties
    gp = GeminiProvider(api_key="dummy-key-not-called")
    cfg = gp.build_generate_config(
        system="sys",
        max_output_tokens=200,
        temperature=0.1,
        thinking={"thinking_level": "minimal"},
        response_schema=EnrichmentFields,
    )
    assert cfg.automatic_function_calling is not None
    assert cfg.automatic_function_calling.disable is True
    assert _find_forbidden_keys(cfg.response_schema) == []


def test_error_diagnosis_extracts_actual_field_and_message(settings, capsys):
    import run

    class DummyAPIError(Exception):
        def __init__(self):
            self.code = 400
            self.message = 'Invalid JSON payload received. Unknown name "additional_properties" at \'generation_config.response_schema\': Cannot find field.'
            self.details = {
                "error": {
                    "code": 400,
                    "message": self.message,
                    "status": "INVALID_ARGUMENT",
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.BadRequest",
                            "fieldViolations": [
                                {
                                    "field": "generation_config.response_schema",
                                    "description": self.message,
                                }
                            ],
                        }
                    ],
                }
            }
            super().__init__(self.message)

    code, field, msg = extract_error_info(DummyAPIError())
    assert code == 400
    assert field == "generation_config.response_schema"
    assert "additional_properties" in msg

    err = ProviderError(msg, code=code, retryable=False, error_field=field)
    rc = run.cmd_test_call(settings, input_fn=lambda _: "yes", provider=FakeProvider([err]))
    out = capsys.readouterr().out
    assert rc == 1
    assert "error_field  : generation_config.response_schema" in out
    assert "Unknown name \"additional_properties\"" in out
    assert "thinking setting was rejected" not in out
