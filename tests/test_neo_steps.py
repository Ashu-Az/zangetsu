from naukri_bot.neo import _after_old_confirmation

CARD = {"company": "Aiwf Technologies", "title": "Data Science Engineer"}


def test_older_confirmation_is_dropped():
    text = "Successfully applied to\nTuring Global India\nPython\nRecruiter from Aiwf Technologies\nAdd missing details"
    out = _after_old_confirmation(text.replace("Aiwf Technologies", "someone"), CARD)
    assert "Successfully applied" not in out and "Add missing details" in out


def test_this_cards_confirmation_is_kept():
    text = "Add missing details\nSuccessfully applied to\nAiwf Technologies"
    assert _after_old_confirmation(text, CARD) == text


def test_no_confirmation_leaves_text_alone():
    assert _after_old_confirmation("Add missing details", CARD) == "Add missing details"
