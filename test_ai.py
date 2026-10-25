"""Opt-in live Groq smoke test; normal tests use a mocked model."""
import os
import pytest

pytestmark = pytest.mark.skipif(not os.getenv("RUN_GROQ_TESTS"), reason="set RUN_GROQ_TESTS=1 to use Groq credits")

def test_groq_smoke():
    from groq import Groq
    client=Groq(api_key=os.environ["GROQ_API_KEY"])
    response=client.chat.completions.create(model="openai/gpt-oss-20b",messages=[{"role":"user","content":"Reply with exactly: INVOICE_AGENT_AI_READY"}])
    assert "INVOICE_AGENT_AI_READY" in response.choices[0].message.content
