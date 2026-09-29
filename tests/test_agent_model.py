import asyncio
import json
import unittest

from app.agent_model import OpenAICompatibleModelAdapter


class Response:
    status_code = 200

    def __init__(self, packets):
        self.packets = packets

    async def aiter_lines(self):
        for packet in self.packets:
            yield f"data: {json.dumps(packet)}"
        yield "data: [DONE]"


class Stream:
    def __init__(self, response): self.response = response
    async def __aenter__(self): return self.response
    async def __aexit__(self, *args): pass


class Client:
    def __init__(self, packets): self.packets = packets
    def stream(self, *args, **kwargs): return Stream(Response(self.packets))


class AgentModelTests(unittest.TestCase):
    def test_parses_streamed_text_encoded_tool_calls_and_hides_markup(self):
        content = "Busca clima Madrid"
        packets = [{"choices": [{"delta": {"content": char}}]} for char in content]
        markup = ("<tool_call><function=web_search><parameter=query>Madrid forecast</parameter></function></tool_call>"
                  "<tool_call><function=native.render_artifact><parameter=data>{&quot;type&quot;:&quot;bar&quot;}</parameter></function></tool_call>")
        packets.extend({"choices": [{"delta": {"content": char}}]} for char in markup)
        packets.append({"choices": [{"delta": {"content": " resultados listos."}}]})
        adapter = OpenAICompatibleModelAdapter(Client(packets), "http://provider/v1/chat/completions", {}, "model")
        tools = [{"type": "function", "function": {"name": "web_search", "parameters": {"type": "object"}}}]

        async def collect(): return [chunk async for chunk in adapter.stream([], tools)]
        chunks = asyncio.run(collect())
        self.assertEqual("".join(chunk.content for chunk in chunks), "Busca clima Madrid resultados listos.")
        calls = [call for chunk in chunks for call in (chunk.tool_calls or [])]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["function"]["name"], "web_search")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"query": "Madrid forecast"})
        self.assertEqual(calls[1]["function"]["name"], "native.render_artifact")
        self.assertEqual(json.loads(calls[1]["function"]["arguments"]), {"data": {"type": "bar"}})


if __name__ == "__main__": unittest.main()
