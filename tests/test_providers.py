"""The optional live provider, exercised with a fake client so no API key is needed."""
from types import SimpleNamespace

import pytest

from helpers import NOW
from viewer_agent.agent import Budget
from viewer_agent.config import Settings
from viewer_agent.planner import Advice, AdviceRequest, OpenAIProvider, ProviderError, ProviderTimeout


class FakeCompletions:
    def __init__(self, content=None, raise_exc=None):
        self.content, self.raise_exc, self.calls = content, raise_exc, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_exc:
            raise self.raise_exc
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))])


class APITimeoutError(Exception):
    pass


def _request(budget):
    return AdviceRequest("U-A1", "trg-1", NOW, "prompt", Advice(goal_id="G02"), timeout_s=10.0, budget=budget)


def _provider(completions):
    return OpenAIProvider(model="any-model", client=SimpleNamespace(chat=SimpleNamespace(completions=completions)))


def test_json_answer_is_validated_into_advice():
    completions = FakeCompletions('{"goal_id": "G02", "hero_title_id": "T0001", "reason": "most played"}')
    advice = _provider(completions).advise(_request(Budget(NOW, Settings())))
    assert advice.goal_id == "G02" and advice.hero_title_id == "T0001"
    assert completions.calls[0]["timeout"] == 10.0
    assert completions.calls[0]["response_format"] == {"type": "json_object"}


def test_timeout_is_charged_to_the_budget():
    budget = Budget(NOW, Settings())
    with pytest.raises(ProviderTimeout):
        _provider(FakeCompletions(raise_exc=APITimeoutError("Request timed out"))).advise(_request(budget))
    assert budget.simulated_wait_s == 10.0


def test_other_client_errors_become_provider_errors():
    with pytest.raises(ProviderError):
        _provider(FakeCompletions(raise_exc=RuntimeError("boom"))).advise(_request(Budget(NOW, Settings())))


def test_non_json_answer_is_rejected():
    with pytest.raises(ValueError):
        _provider(FakeCompletions("sure, go with G02")).advise(_request(Budget(NOW, Settings())))
