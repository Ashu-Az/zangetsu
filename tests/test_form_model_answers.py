from naukri_bot.ats.generic_form import GenericFormApplier
from naukri_bot.llm import FormFieldAnswer


def _app():
    a = GenericFormApplier.__new__(GenericFormApplier)
    a.llm_answers = {}
    return a


def test_model_answer_matches_option_text():
    a = _app()
    a.llm_answers[a._key("Are you currently eligible to work here?*")] = FormFieldAnswer(index=0, option="Yes")
    assert a._llm_value("Are you currently eligible to work here?", ["Select...", "Yes", "No"]) == "Yes"


def test_model_answer_free_text_and_skip():
    a = _app()
    a.llm_answers[a._key("Why us")] = FormFieldAnswer(index=0, answer_text="Because of the product.")
    a.llm_answers[a._key("Signature")] = FormFieldAnswer(index=1, skip=True)
    assert a._llm_value("Why us", []) == "Because of the product."
    assert a._llm_value("Signature", []) is None
    assert a._llm_value("Unasked", []) is None
