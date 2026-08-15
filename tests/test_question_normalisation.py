"""One normalisation, used by everything that routes on the question.

`llm._direct_telemetry_answer` decides from the normalised question whether a
turn is answered deterministically; `evals.grade_record` decides from the same
string what that turn should have said. They were separate copies of the same
idiom, in five modules. A copy that drifts does not fail loudly — it grades a
route that was never taken.
"""

from ai_race_engineer import evals, llm, prompt_builder
from ai_race_engineer.orchestrator import _is_repeat_request
from ai_race_engineer.text import normalise_question, question_words


def test_punctuation_and_case_come_from_the_recogniser_not_the_driver():
    assert normalise_question("  What's my POSITION?! ") == "what s my position"
    assert normalise_question(None) == ""
    assert question_words("Tyres, and brakes?") == {"tyres", "and", "brakes"}


def test_the_router_and_the_grader_normalise_identically():
    """The pair that has to agree, checked on the same string."""
    assert llm.normalise_question is evals.normalise_question
    assert llm.normalise_question("Where am I?") == "where am i"


def test_every_router_reads_the_same_helper():
    """A sixth hand-rolled copy would pass every test above."""
    import inspect

    for module in (llm, evals, prompt_builder):
        assert "ch.isalnum() else" not in inspect.getsource(module), \
            f"{module.__name__} normalises the question itself again"


def test_a_repeat_request_survives_the_recognisers_punctuation():
    """Scribe writes "Say again?" — the set holds "say again"."""
    assert _is_repeat_request("Say again?")
    assert _is_repeat_request("  REPEAT THAT.  ")
    assert not _is_repeat_request("say again what my fuel is")
