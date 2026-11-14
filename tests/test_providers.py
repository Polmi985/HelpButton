"""Služby ve stylu OpenAI (Groq, Z.ai, OpenRouter…) – klient a záložní řetězce, s falešným serverem."""

from __future__ import annotations

import base64
import io
import json

import httpx
import pytest

from helpbutton.server import openai_compat
from helpbutton.server.agent import ChainVlm, CompatVlm, Vlm, make_vlm
from helpbutton.server.brain import Brain
from helpbutton.server.config import Config
from helpbutton.server.openai_compat import Provider, ProviderError


def webp(w: int = 2000, h: int = 1000) -> str:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 200, 200)).save(buf, "WEBP")
    return base64.b64encode(buf.getvalue()).decode()


def server(handler):  # noqa: ANN001
    seen: list[dict] = []

    def h(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else {}
        seen.append({"url": str(req.url), "auth": req.headers.get("authorization"), "body": body})
        return handler(req, body)

    return httpx.MockTransport(h), seen


def ok(text: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


P = Provider(
    "groq", "https://api.example.com/openai/v1", "vision-model", api_key="k", vision=True, use=["vlm"]
)


async def test_chat_sends_image_as_jpeg_and_returns_scale():
    transport, seen = server(lambda req, body: ok('{"action": "done"}'))
    out, factor = await openai_compat.chat(P, "sys", "task", webp(), transport=transport)
    assert out == '{"action": "done"}' and factor == pytest.approx(2000 / 1280)
    s = seen[0]
    assert s["url"] == "https://api.example.com/openai/v1/chat/completions"
    assert s["auth"] == "Bearer k"
    assert s["body"]["response_format"] == {"type": "json_object"}
    img = s["body"]["messages"][1]["content"][1]["image_url"]["url"]
    assert img.startswith("data:image/jpeg;base64,")


async def test_chat_retries_without_json_mode():
    def handler(req, body):
        if "response_format" in body:
            return httpx.Response(400, json={"error": {"message": "response_format is not supported"}})
        return ok("{}")

    transport, seen = server(handler)
    out, _ = await openai_compat.chat(P, "sys", "x", transport=transport)
    assert out == "{}" and len(seen) == 2


async def test_wrong_model_lists_available_models():
    def handler(req, body):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "llama-3.3-70b"}, {"id": "qwen2.5-vl-72b"}]})
        return httpx.Response(404, json={"error": {"message": "The model `x` does not exist"}})

    transport, _ = server(handler)
    with pytest.raises(ProviderError) as e:
        await openai_compat.chat(P, "sys", "x", transport=transport)
    assert "qwen2.5-vl-72b" in str(e.value)


async def test_text_only_model_refuses_image():
    with pytest.raises(ProviderError):
        await openai_compat.chat(Provider("cerebras", "u", "m", api_key="k"), "s", "t", webp())


async def test_chain_vlm_falls_back():
    class Busy(Vlm):
        name = "gemini"

        async def decide(self, *a):
            raise RuntimeError("503 high demand")

    class Fine(Vlm):
        name = "zai"

        async def decide(self, *a):
            return {"action": "done"}

    chain = ChainVlm([Busy(), Fine()])
    assert chain.name == "gemini → zai"
    assert (await chain.decide("s", "t", None))["action"] == "done"


async def test_compat_vlm_rescales_coordinates(monkeypatch):
    async def fake_chat(p, system, text, image_b64=None, **kw):
        return '{"action": "click", "x": 100, "y": 50}', 2.0

    monkeypatch.setattr(openai_compat, "chat", fake_chat)
    d = await CompatVlm(P).decide("s", "t", "img")
    assert (d["x"], d["y"]) == (200, 100)


async def test_make_vlm_builds_chain(monkeypatch):
    from helpbutton.server import agent

    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    async def no_ollama(cfg):
        return None

    monkeypatch.setattr(agent, "_ollama_has", no_ollama)
    cfg = Config(providers=[P, Provider("cerebras", "u", "m", api_key="k")])
    v = await make_vlm(cfg)
    assert v.name == "gemini → groq"
    cfg.vlm_provider = "groq"
    assert (await make_vlm(cfg)).name == "groq"


async def test_brain_falls_back_to_groq(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from helpbutton.server import gemini

    async def busy(*a, **kw):
        raise gemini.GeminiError("503")

    async def groq(p, system, text, image_b64=None, **kw):
        return (
            json.dumps(
                {
                    "intent": "battery",
                    "target": "phone",
                    "app": "",
                    "contact": "",
                    "text": "",
                    "url": "",
                    "channel": "",
                    "confidence": 0.9,
                    "say": "",
                }
            ),
            1.0,
        )

    monkeypatch.setattr(gemini, "generate", busy)
    monkeypatch.setattr(openai_compat, "chat", groq)
    brain = Brain(
        Config(
            llm_local_first=False,
            ollama_url="http://127.0.0.1:9",
            providers=[Provider("groq", "u", "m", api_key="k")],
        )
    )
    assert await brain.chain() == ["gemini", "compat:groq"]
    u = await brain.understand("Rychle se mi to vybíjí", [])
    assert u.intent == "battery" and u.source == "compat:groq"


async def test_brain_local_ollama_is_main(monkeypatch):
    """Stažený model v Ollamě je hlavní i pro porozumění větám, cloud je záloha."""
    monkeypatch.setenv("GEMINI_API_KEY", "x")

    async def has(self):
        return True

    monkeypatch.setattr(Brain, "_ollama_has", has)
    brain = Brain(Config(providers=[Provider("groq", "u", "m", api_key="k")]))
    assert await brain.chain() == ["ollama", "gemini", "compat:groq"]
    brain = Brain(Config(llm_local_first=False, providers=[Provider("groq", "u", "m", api_key="k")]))
    assert await brain.chain() == ["gemini", "compat:groq"]


async def test_keywords_mode_never_calls_ai(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    brain = Brain(Config(llm_provider="keywords", providers=[Provider("groq", "u", "m", api_key="k")]))
    assert await brain.chain() == []


async def test_make_vlm_local_ollama_is_main(monkeypatch):
    """Stažený qwen3-vl v Ollamě je hlavní Vision model, cloud je záloha."""
    from helpbutton.server import agent

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    async def has(cfg):
        return True

    monkeypatch.setattr(agent, "_ollama_has", has)
    cfg = Config(providers=[P])
    assert (await make_vlm(cfg)).name == "ollama:qwen3-vl:2b-instruct → groq"
    cfg.vlm_local_first = False
    assert (await make_vlm(cfg)).name == "groq"


async def test_ollama_qwen3_relative_coordinates(monkeypatch):
    """Qwen3-VL vrací souřadnice 0–1000 → přepočet na pixely snímku ze zařízení."""
    import base64
    import io

    import httpx
    from PIL import Image

    from helpbutton.server.agent import OllamaVlm

    buf = io.BytesIO()
    Image.new("RGB", (1600, 900)).save(buf, "WEBP")
    img = base64.b64encode(buf.getvalue()).decode()
    sent: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        import json

        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"message": {"content": '{"action": "click", "x": 500, "y": 250}'}})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    d = await OllamaVlm("http://x", "qwen3-vl:2b-instruct").decide("s", "t", img)
    assert (d["x"], d["y"]) == (800, 225)
    assert "RELATIVE coordinates 0-1000" in sent[0]["messages"][1]["content"]
    d = await OllamaVlm("http://x", "qwen2.5vl:3b").decide("s", "t", img)
    assert (d["x"], d["y"]) == (round(500 * 1600 / 1344), round(250 * 900 / 736))
