# -*- coding: utf-8 -*-
"""
Определение пола по личному сигналу. Версия 2.
Порядок: отчество → фамилия и имя → ник.

    from classify import classify
    classify(first_name="Ксю", username="Ksuksuksu_ksu")
    → {"gender": "f", "source": "имя", "confident": True}

Три исхода: "f", "m", None. None — рабочий ответ «не знаю».
"""
import normalize as N
import namedict as D

_PATR = ("овна", "евна", "ична", "ович", "евич")


def _toks(raw):
    """
    Токены, пометка склеенных удвоенных букв и пометка транслитерированных.
    Транслит с латиницы надо ограничивать по длине основы: «dashboard» без
    этого превращается в «дашбоард» и садится на основу «даш».
    """
    nf = N.normalize(raw)
    out, collapsed, tr = [], set(), set()
    for t in nf["tokens"]:
        vs = N.variants(t)
        out += vs
        for v in vs[1:]:
            collapsed.add(v)
        if t in nf.get("tokens_translit", ()):
            tr.update(vs)
    return out, collapsed, tr


def _nick_toks(u):
    out, collapsed = [], set()
    for t in N.username_tokens(u):
        vs = N.variants(t)
        out += vs
        for v in vs[1:]:
            collapsed.add(v)
    return out, collapsed


def classify(first_name="", last_name="", username="", bio=""):
    # 1. отчество — самый надёжный сигнал.
    # Только в поле ИМЕНИ: «Филиппович», «Петкевич», «Гринкевич» — это
    # белорусские фамилии, их носят и женщины. Проверка по полю фамилии
    # давала 22 заведомые ошибки вида «Мария Нехвядович» → м.
    for raw in (first_name,):
        t, _c, _tr = _toks(raw)
        for v in t:
            if len(v) > 5 and v.endswith(_PATR):
                g = D.lookup(v)
                if g in ("f", "m"):
                    return {"gender": g, "source": "отчество", "confident": True}

    # 2. имя и фамилия — в обоих полях, фамилия встречается и в поле имени
    t1, c1, r1 = _toks(first_name)
    t2, c2, r2 = _toks(last_name)
    g = D.lookup_any(t1 + t2, c1 | c2, r1 | r2)
    if g in ("f", "m"):
        return {"gender": g, "source": "имя", "confident": True}
    if g == "conflict":
        # мужское имя при женской фамилии и наоборот: накрутка или опечатка
        return {"gender": None, "source": "противоречие полей", "confident": False}

    # 3. ник — флаг склеенных букв раньше считался и выбрасывался,
    #    из-за чего «foxieww» и «katieww» уверенно уходили в мужчин
    t3, c3 = _nick_toks(username)
    g2 = D.lookup_any_nick(t3, c3)
    if g2 in ("f", "m"):
        return {"gender": g2, "source": "ник", "confident": True}

    # 4. гендерное слово внутри ника: prettyboy586, cute_girl_99
    g3 = D.nick_word(username)
    if g3:
        return {"gender": g3, "source": "слово в нике", "confident": True}

    if g2 == "conflict":
        return {"gender": None, "source": "противоречие в нике", "confident": False}
    return {"gender": None,
            "source": "неоднозначное" if "ambig" in (g, g2) else "нет сигнала",
            "confident": False}
