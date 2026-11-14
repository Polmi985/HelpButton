"""End-to-end scénáře z design dokumentů proti simulovaným zařízením."""

from __future__ import annotations

from helpbutton.agents.android import AndroidAgent
from helpbutton.agents.mock import MockPc, MockPhone
from helpbutton.agents.pc import PcAgent
from helpbutton.common.safety import TOOL_LEVELS, Level, looks_like_scam
from helpbutton.server.brain import keyword_understand

from .conftest import Env


async def test_whatsapp_missing_asks_then_restores(env: Env):
    r = await env.say("Zmizel mi WhatsApp")
    assert r.screen == "potvrzeni"
    assert "WhatsApp" in r.say and "ano" in r.say.lower()
    assert env.phone.apps["com.whatsapp"] == "disabled"

    r = await env.say("Ano")
    assert r.screen == "hotovo"
    assert env.phone.apps["com.whatsapp"] == "ok"
    assert env.phone.foreground == "com.whatsapp"
    assert any(e.kind == "success" and "WhatsApp" in e.text for e in env.family.events)


async def test_no_cancels(env: Env):
    await env.say("zmizel mi vocap")
    r = await env.say("ne")
    assert "nic neměním" in r.say
    assert env.phone.apps["com.whatsapp"] == "disabled"


async def test_missing_app_opens_store(env: Env):
    env.phone.apps.pop("com.whatsapp")
    r = await env.say("Nemůžu najít WhatsApp")
    assert "obchod" in r.say.lower()
    r = await env.say("ano")
    assert "Instalovat" in r.say
    assert "store com.whatsapp" in env.phone.actions


async def test_pc_dns_repair_and_open_site(env: Env):
    r = await env.say("Na počítači mi nejde otevřít Seznam")
    assert r.screen == "potvrzeni"
    assert env.pc.dns_broken
    r = await env.say("ano")
    assert r.screen == "hotovo"
    assert not env.pc.dns_broken
    assert env.pc.opened == ["https://www.seznam.cz"]
    assert "Seznam" in r.say


async def test_sound_low_phone(env: Env):
    r = await env.say("Nic neslyším, nezvoní mi to")
    assert r.screen == "potvrzeni"
    await env.say("ano")
    assert env.phone.ringer_mode == "normal" and not env.phone.dnd


async def test_send_message_trusted_contact(env: Env):
    r = await env.say("Napiš Lence, že přijdu později")
    assert "přijdu později" in r.say
    r = await env.say("jo")
    assert r.screen == "hotovo"
    assert env.phone.actions[-1] == "whatsapp +420 777 123 456"
    assert not any("přijdu" in e.text for e in env.family.events)


async def test_send_message_asks_for_text(env: Env):
    r = await env.say("Pošli zprávu Lence")
    assert r.expects_answer and "napsat" in r.say
    r = await env.say("Ať mi koupí chleba")
    assert "chleba" in r.say and r.screen == "potvrzeni"


async def test_battery_is_green_no_confirmation(env: Env):
    r = await env.say("Rychle se mi to vybíjí")
    assert "34 procent" in r.say
    assert r.screen == "info"


async def test_open_app_green(env: Env):
    r = await env.say("Otevři mi fotky")
    assert r.screen == "hotovo"
    assert env.phone.foreground == "com.google.android.apps.photos"


async def test_font_bigger_then_undo(env: Env):
    await env.say("Je to moc malé, nevidím")
    await env.say("ano")
    assert env.phone.font_scale == 1.3
    r = await env.say("Vrať to zpátky")
    assert "zvětšení písma" in r.say and r.screen == "potvrzeni"
    r = await env.say("ano")
    assert env.phone.font_scale == 1.0
    r = await env.say("vrať to zpátky")
    assert "Nemám co vracet" in r.say


async def test_font_smaller(env: Env):
    env.phone.font_scale = 1.3
    await env.say("Písmo je moc velké")
    await env.say("ano")
    assert env.phone.font_scale == 1.0


async def test_prosim_repeats_question_and_does_not_confirm(env: Env):
    """Přepis: HelpButton se ptala „Mám začít?“ a „prosím?“ to spustilo."""
    r1 = await env.say("Zmizel mi WhatsApp")
    r = await env.say("prosím?")
    assert r.say.startswith("Zopakuji to.") and r.screen == "potvrzeni"
    assert env.phone.apps["com.whatsapp"] == "disabled"
    r = await env.say("ano")
    assert r.screen == "hotovo"
    assert r1.say in f"Zopakuji to. {r1.say}"


async def test_question_is_answered_without_model(env: Env):
    """Přepis: „teď mi odpovídá jaké AI?“ nabídlo klikání po telefonu."""
    r = await env.say("teď mi odpovídá jaké AI?")
    assert r.screen == "info" and "HelpButton" in r.say and "rozpoznávání slov" in r.say
    assert "Mám začít" not in r.say


async def test_question_uses_llm_answer(env: Env):
    async def fake_answer(q, context=""):
        return "Fotku pošlete tak, že otevřete WhatsApp. Mám to udělat za vás?"

    env.orch.brain.answer = fake_answer
    r = await env.say("Jak pošlu fotku vnoučeti?")
    assert r.say.startswith("Fotku pošlete") and r.screen == "info"


async def test_scam_is_refused_and_family_alerted(env: Env):
    r = await env.say("Volal mi někdo z banky, ať si nainstaluju AnyDesk")
    assert r.screen == "odmitnuto" and r.level == "červená"
    assert env.family.events[-1].kind == "alert"
    assert env.phone.actions == []


async def test_blocked_app_refused_even_if_classified(env: Env, cfg):
    from helpbutton.server.config import App

    cfg.apps.insert(0, App("George", "cz.csas.georgego", ["george", "dzordz"]))
    r = await env.say("Otevři mi George")
    assert r.screen == "odmitnuto"
    assert env.phone.actions == []


async def test_no_changes_during_call(cfg):
    e = Env(cfg)
    e.phone.in_call = True
    await e.start()
    r = await e.say("Zmizel mi WhatsApp")
    assert r.screen == "odmitnuto"
    assert "hovoru" in r.say
    assert e.phone.apps["com.whatsapp"] == "disabled"


async def test_daemon_refuses_blocked_package_directly():
    phone = MockPhone("t", delay=0)
    res = await phone.invoke("restore_and_launch_app", {"package_name": "com.anydesk.anydeskandroid"})
    assert res["refused"]


async def test_stop_resets(env: Env):
    await env.say("zmizel mi whatsapp")
    r = await env.say("stop")
    assert "Zastavila" in r.say
    r = await env.say("ano")
    assert env.phone.apps["com.whatsapp"] == "disabled"


async def test_unknown_clarifies_then_offers_family(env: Env):
    r = await env.say("blablabla")
    assert r.screen == "nerozumi"
    r = await env.say("hmm tamto")
    assert "Petrovi" in r.say and r.screen == "potvrzeni"
    r = await env.say("ano")
    assert env.family.events[-1].kind == "alert"


async def test_low_confidence_confirms_intent(env: Env):
    r = await env.say("WhatsApp")
    assert r.say.startswith("Myslíte aplikaci WhatsApp")
    r = await env.say("ano")
    assert r.screen == "potvrzeni"


async def test_app_name_alone_opens_it(env: Env):
    await env.say("Fotky")
    r = await env.say("ano")
    assert r.screen == "hotovo" and r.say == "Aplikace Fotky je otevřená."
    assert env.phone.foreground == "com.google.android.apps.photos"


async def test_message_finds_contact_in_phone(env: Env):
    """Přepis z Pixelu: „posli zpravu polyfracek mi ze prijdu pozdeji, pres zpravy“ – adresát není v helpbutton.toml."""
    r = await env.say("posli zpravu polyfracek mi ze prijdu pozdeji, pres zpravy")
    assert r.screen == "potvrzeni"
    assert "Polyfráček Mi" in r.say and "esemesku" in r.say and "prijdu pozdeji" in r.say
    assert "přes zprávy" not in r.say
    r = await env.say("ano")
    assert r.screen == "hotovo"
    assert env.phone.actions[-1] == "sms +420 722 000 111"
    assert not any("Polyfráček" in e.text for e in env.family.events)


async def test_ambiguous_contact_asks_which(env: Env):
    r = await env.say("Zavolej Adamovi")
    assert "Adam Dvořák" in r.say and "Adam Kučera" in r.say and r.expects_answer
    r = await env.say("Kučerovi")
    assert r.say.startswith("Mám zavolat: Adam Kučera?")
    await env.say("ano")
    assert env.phone.actions[-1] == "call +420 604 777 888"


async def test_contact_mobile_number_preferred(env: Env):
    await env.say("Napiš Dvořákovi, že jsem doma")
    await env.say("ano")
    assert env.phone.actions[-1] == "whatsapp +420 603 555 666"


async def test_unknown_contact_is_asked_again(env: Env):
    r = await env.say("Zavolej Zdeňkovi")
    assert "nenašla" in r.say and r.expects_answer
    r = await env.say("zubaři")
    assert r.say.startswith("Mám zavolat: Zubař Novotný?")


async def test_trusted_family_contact_still_wins(env: Env):
    r = await env.say("Pošli Lence SMS, že přijdu později")
    assert r.say.startswith("Napíšu esemesku Lence")
    await env.say("ano")
    assert env.phone.actions[-1] == "sms +420 777 123 456"


async def test_reply_without_vision_model_offers_family(env: Env):
    r = await env.say("odepis prosim na zpravu, v zpravy, ze adam je v poradku")
    assert "Odepsat na zprávu zatím neumím" in r.say and "Petrovi" in r.say


async def test_app_grammar(env: Env):
    r = await env.say("Zmizel mi WhatsApp")
    assert "Aplikace WhatsApp je v telefonu vypnutá" in r.say
    r = await env.say("ano")
    assert r.say == "Hotovo. Aplikace WhatsApp je zpátky a je otevřená na obrazovce."


async def test_device_offline(cfg):
    e = await Env(cfg).start(phone=False)
    r = await e.say("Zmizel mi WhatsApp")
    assert "není připojený" in r.say


async def test_forget_my_log(env: Env):
    await env.say("Rychle se mi to vybíjí")
    assert env.family.events
    await env.say("Smaž můj záznam")
    await env.say("ano")
    assert env.family.events == []


def test_scam_detector():
    assert looks_like_scam("Volal mi pán z policie, mám převést peníze na bezpečný účet")
    assert looks_like_scam("chtějí po mně kód z SMS")
    assert not looks_like_scam("Zmizel mi WhatsApp")
    assert not looks_like_scam("Nejde mi internet")
    assert looks_like_scam("Přišla mi SMS, že mi zablokují účet, ať kliknu na odkaz")
    assert looks_like_scam("Píšou mi, že mi zablokovali kartu")
    assert looks_like_scam("Mám kliknout na ten odkaz?")
    assert not looks_like_scam("Rychle se mi to vybíjí")
    assert not looks_like_scam("Pusť mi na YouTube video")


def test_keywords(cfg):
    assert keyword_understand("Je to moc malé, nevidím", cfg).intent == "text_too_small"
    assert keyword_understand("Zavolej Petrovi", cfg).intent == "call_person"
    u = keyword_understand("Na počítači mi nejde otevřít Seznam", cfg)
    assert (u.intent, u.target, u.url) == ("no_internet", "pc", "https://www.seznam.cz")


def test_real_and_mock_daemons_expose_same_tools():
    def names(cls):
        return {getattr(cls, n)._helpbutton_tool.name for n in dir(cls) if hasattr(getattr(cls, n), "_helpbutton_tool")}

    assert names(AndroidAgent) == names(MockPhone)
    assert names(PcAgent) == names(MockPc)
    for n in names(AndroidAgent) | names(PcAgent):
        assert n in TOOL_LEVELS, n


def test_open_menu_items_always_numbered():
    """Na plné obrazovce (stovky prvků) nesmí položky otevřeného kontextového menu vypadnout."""
    from helpbutton.common import som

    window = [som.Element(0, f"Button {i} (Button)", (10, 10 + i * 8, 200, 17 + i * 8)) for i in range(200)]
    menu = [
        som.Element(0, "Zobrazit (MenuItem, open menu)", (900, 1500, 1100, 1530), priority=2),
        som.Element(0, "Zobrazit ikony na ploše (MenuItem, open menu)", (1100, 1500, 1400, 1530), priority=2),
    ]
    out = som.number(window + menu, limit=80)
    labels = [e.label for e in out]
    assert len(out) == 80 and all(m.label in labels for m in menu)
    assert [e.id for e in out] == list(range(1, 81))


def test_daemon_cannot_lower_level():
    phone = MockPhone("t")
    specs = {s.name: s for s in phone.tool_specs}
    assert specs["restore_and_launch_app"].level == Level.ORANGE
    assert specs["check_app_state"].level == Level.GREEN


async def test_result_delivered_after_reconnect():
    """Ochrana proti sebeodpojení: výsledek dorazí v hello po obnovení spojení."""
    import asyncio

    from helpbutton.server.registry import Registry

    reg = Registry()
    sent = []

    async def send(msg):
        sent.append(msg)

    dev = await reg.attach({"device_id": "pc-x", "name": "PC", "kind": "pc", "tools": []}, send)
    call = asyncio.create_task(reg.call(dev, "atomic_network_repair", {}, timeout=5))
    await asyncio.sleep(0)
    await reg.detach("pc-x")
    await reg.attach(
        {
            "device_id": "pc-x",
            "name": "PC",
            "kind": "pc",
            "tools": [],
            "undelivered": [{"type": "result", "id": sent[0]["id"], "ok": True, "data": {"ok": True}}],
        },
        send,
    )
    assert await call == {"ok": True}
