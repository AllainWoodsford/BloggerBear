from unittest.mock import patch

from common import compliance


def test_is_financial_topic_true():
    assert compliance.is_financial_topic({"is_financial": True}) is True


def test_is_financial_topic_false_when_absent():
    assert compliance.is_financial_topic({}) is False


def test_is_financial_topic_false_explicit():
    assert compliance.is_financial_topic({"is_financial": False}) is False


def test_regex_redact_email():
    text = "Contact me at jane.doe+test@example.co.uk for details."
    redacted = compliance.regex_redact(text)
    assert "jane.doe" not in redacted
    assert "[REDACTED]" in redacted


def test_regex_redact_ssn():
    text = "My SSN is 123-45-6789, please keep it safe."
    redacted = compliance.regex_redact(text)
    assert "123-45-6789" not in redacted
    assert "[REDACTED]" in redacted


def test_regex_redact_credit_card():
    text = "Card number: 4111 1111 1111 1111 expires soon."
    redacted = compliance.regex_redact(text)
    assert "4111 1111 1111 1111" not in redacted
    assert "[REDACTED]" in redacted


def test_regex_redact_phone_number():
    text = "Call me at (555) 123-4567 tomorrow."
    redacted = compliance.regex_redact(text)
    assert "123-4567" not in redacted
    assert "[REDACTED]" in redacted


def test_regex_redact_leaves_ordinary_text_untouched():
    text = "This article discusses trends in open-source tooling."
    assert compliance.regex_redact(text) == text


def test_review_draft_financial_topic_always_routes_to_moderation():
    topic = {"topic_id": "t1", "name": "Crypto", "is_financial": True}
    with patch("common.compliance.invoke_claude") as mock_invoke:
        result = compliance.review_draft("Some perfectly fine draft.", topic, "model-id")

    mock_invoke.assert_not_called()
    assert result["compliant"] is False
    assert result["reasons"] == [
        "financial topic - routed to manual moderation regardless of content"
    ]


def test_review_draft_compliant_response():
    topic = {"topic_id": "t1", "name": "GitHub Trending", "is_financial": False}
    with patch("common.compliance.invoke_claude", return_value="COMPLIANT\n") as mock_invoke:
        result = compliance.review_draft("Some draft text.", topic, "model-id")

    mock_invoke.assert_called_once()
    assert result == {"compliant": True, "reasons": []}


def test_review_draft_compliant_with_minor_concerns():
    topic = {"topic_id": "t1", "is_financial": False}
    response = "COMPLIANT\nConsider softening one claim."
    with patch("common.compliance.invoke_claude", return_value=response):
        result = compliance.review_draft("Draft.", topic, "model-id")

    assert result["compliant"] is True
    assert result["reasons"] == ["Consider softening one claim."]


def test_review_draft_non_compliant_response():
    topic = {"topic_id": "t1", "is_financial": False}
    response = "NON-COMPLIANT\nContains an unsubstantiated claim.\nTone is not neutral."
    with patch("common.compliance.invoke_claude", return_value=response):
        result = compliance.review_draft("Draft.", topic, "model-id")

    assert result["compliant"] is False
    assert result["reasons"] == [
        "Contains an unsubstantiated claim.",
        "Tone is not neutral.",
    ]


def test_review_draft_ambiguous_response_fails_closed():
    topic = {"topic_id": "t1", "is_financial": False}
    response = "This looks fine to me overall."
    with patch("common.compliance.invoke_claude", return_value=response):
        result = compliance.review_draft("Draft.", topic, "model-id")

    assert result["compliant"] is False
    assert result["reasons"] == [response]


def test_review_draft_empty_response_fails_closed():
    topic = {"topic_id": "t1", "is_financial": False}
    with patch("common.compliance.invoke_claude", return_value=""):
        result = compliance.review_draft("Draft.", topic, "model-id")

    assert result["compliant"] is False
    assert result["reasons"] == ["empty compliance review response"]


def test_review_draft_redacts_before_calling_bedrock():
    topic = {"topic_id": "t1", "is_financial": False}
    draft = "Reach out at someone@example.com with questions."
    with patch("common.compliance.invoke_claude", return_value="COMPLIANT") as mock_invoke:
        compliance.review_draft(draft, topic, "model-id")

    called_prompt = mock_invoke.call_args[0][0]
    assert "someone@example.com" not in called_prompt
    assert "[REDACTED]" in called_prompt


# --- bedrock_redact_review (Phase 5 feedback redaction) ---------------------


def test_bedrock_redact_review_safe_same_line():
    with patch("common.compliance.invoke_claude", return_value="SAFE: Great article, thanks!") as mock_invoke:
        result = compliance.bedrock_redact_review("Great article, thanks!", "model-id")

    mock_invoke.assert_called_once()
    assert result == "Great article, thanks!"


def test_bedrock_redact_review_safe_multiline():
    response = "SAFE:\nThis was a really helpful read.\nLooking forward to more."
    with patch("common.compliance.invoke_claude", return_value=response):
        result = compliance.bedrock_redact_review("This was a really helpful read.", "model-id")

    assert result == "This was a really helpful read.\nLooking forward to more."


def test_bedrock_redact_review_reject_returns_none():
    with patch("common.compliance.invoke_claude", return_value="REJECT") as mock_invoke:
        result = compliance.bedrock_redact_review("Call John Smith at [REDACTED].", "model-id")

    mock_invoke.assert_called_once()
    assert result is None


def test_bedrock_redact_review_ambiguous_response_fails_closed():
    with patch("common.compliance.invoke_claude", return_value="This comment seems fine to me."):
        result = compliance.bedrock_redact_review("Some comment.", "model-id")

    assert result is None


def test_bedrock_redact_review_empty_response_fails_closed():
    with patch("common.compliance.invoke_claude", return_value=""):
        result = compliance.bedrock_redact_review("Some comment.", "model-id")

    assert result is None


def test_bedrock_redact_review_safe_with_no_trailing_text_fails_closed():
    # "SAFE:" with nothing after it on any line is malformed -- fail closed
    # rather than store an empty comment.
    with patch("common.compliance.invoke_claude", return_value="SAFE:"):
        result = compliance.bedrock_redact_review("Some comment.", "model-id")

    assert result is None


def test_bedrock_redact_review_does_not_call_regex_redact_again():
    # bedrock_redact_review must not re-run regex_redact itself -- the two
    # passes are the caller's responsibility to sequence.
    with (
        patch("common.compliance.invoke_claude", return_value="SAFE: fine") as mock_invoke,
        patch("common.compliance.regex_redact") as mock_regex,
    ):
        compliance.bedrock_redact_review("already redacted text", "model-id")

    mock_regex.assert_not_called()
    called_prompt = mock_invoke.call_args[0][0]
    assert "already redacted text" in called_prompt
