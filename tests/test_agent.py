from pathlib import Path

from neo.agent import Agent
from neo.models import ContentBlock, Response
from neo.tools import default_registry


class FakeProvider:
    name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        if self.calls == 1:
            return Response([ContentBlock("tool_use", id="call-1", name="write_file", input={"path": str(Path(request.messages[0].content[0].text)), "content": "hello"})], "tool_use")
        return Response([ContentBlock("text", text="done")])


def test_agent_executes_tool_and_keeps_matching_result(tmp_path: Path) -> None:
    provider = FakeProvider()
    agent = Agent(provider, "model", "system", default_registry(tmp_path, tmp_path))
    target = tmp_path / "created.txt"
    assert agent.send(str(target)) == "done"
    assert target.read_text(encoding="utf-8") == "hello"
    assert agent.messages[1].content[0].id == "call-1"
    assert agent.messages[2].content[0].tool_use_id == "call-1"

