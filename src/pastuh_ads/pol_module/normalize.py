# -*- coding: utf-8 -*-
"""
Нормализация display name и username + извлечение признаков оформления.

Порядок принципиален: сначала снимаем признаки оформления, потом чистим строку.
Если чистить сразу, эмодзи и декоративные шрифты пропадут, а это сигнал.
"""
import re
import unicodedata

# ---------------------------------------------------------------- эмодзи
# Диапазоны взяты широко: нам нужно их посчитать и собрать, а гендерность
# каждого эмодзи выводится из данных, а не задаётся здесь.
EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"   # символы, пиктограммы, доп. эмодзи
    "\U00002600-\U000027BF"   # разное + дингбаты
    "\U0001F000-\U0001F2FF"   # маджонг, карты, закрытые буквы
    "\U00002B00-\U00002BFF"   # стрелки, звёзды
    "\U0000FE00-\U0000FE0F"   # селекторы вариаций
    "\U0001F1E6-\U0001F1FF"   # флаги
    "\U00002190-\U000021FF"   # стрелки
    "\U00002700-\U000027BF"
    "]"
)

# Декоративные блоки: математические алфавиты, рамки, звёздочки-каомодзи
FANCY_RANGES = [
    (0x1D400, 0x1D7FF),   # Mathematical Alphanumeric Symbols
    (0x1F100, 0x1F1FF),   # Enclosed Alphanumeric Supplement
    (0x2460, 0x24FF),     # Enclosed Alphanumerics
    (0xFF00, 0xFFEF),     # Halfwidth and Fullwidth Forms
    (0x1D00, 0x1D7F),     # Phonetic Extensions (ᴍᴀsᴋ-стиль)
    (0x0250, 0x02AF),     # IPA Extensions
]

CYR = set("абвгдеёжзийклмнопрстуфхцчшщъыьэюя")
LAT = set("abcdefghijklmnopqrstuvwxyz")

# Phonetic Extensions и прочие «мелкие капсы»: NFKD их НЕ раскладывает,
# поэтому ᴅᴀsʜᴀ без этой таблицы терялся молча.
SMALLCAPS = str.maketrans({
    "ᴀ": "a", "ʙ": "b", "ᴄ": "c", "ᴅ": "d", "ᴇ": "e", "ꜰ": "f", "ɢ": "g",
    "ʜ": "h", "ɪ": "i", "ᴊ": "j", "ᴋ": "k", "ʟ": "l", "ᴍ": "m", "ɴ": "n",
    "ᴏ": "o", "ᴘ": "p", "ꞯ": "q", "ʀ": "r", "ꜱ": "s", "ᴛ": "t", "ᴜ": "u",
    "ᴠ": "v", "ᴡ": "w", "x": "x", "ʏ": "y", "ᴢ": "z",
    "ᴠ": "v", "ʍ": "w", "ɑ": "a", "ɐ": "a", "ɛ": "e", "ɨ": "i", "ʉ": "u",
    # кириллические мелкие капсы встречаются реже, но встречаются
    "ᴦ": "г", "ᴧ": "л", "ᴨ": "п", "ᴩ": "р", "ᴪ": "пс", "ᴫ": "л",
})


def is_fancy(ch):
    o = ord(ch)
    return any(lo <= o <= hi for lo, hi in FANCY_RANGES)


# ---------------------------------------------------------------- признаки
def decor_features(s):
    """Снимается ДО нормализации."""
    if not s:
        return {}
    emo = EMOJI_RE.findall(s)
    letters = sum(1 for c in s if c.isalpha())
    fancy = sum(1 for c in s if is_fancy(c))
    combining = sum(1 for c in s if unicodedata.combining(c))
    # небуквенные и непробельные: звёздочки, точки, тильды, скобки каомодзи
    deco = sum(1 for c in s if not c.isalnum() and not c.isspace())
    return {
        "emoji_list": emo,
        "n_emoji": len(emo),
        "has_fancy_font": int(fancy > 0),
        "n_combining": combining,
        "deco_ratio": round(deco / max(len(s), 1), 3),
        "is_caps": int(bool(re.search(r"[A-ZА-ЯЁ]{3,}", s)) and s == s.upper()),
        "starts_hash": int(s.strip().startswith("#")),
        "n_letters": letters,
        "len_raw": len(s),
    }


# ---------------------------------------------------------------- очистка
def strip_decor(s):
    """
    NFKD раскладывает математические шрифты в обычные буквы.
    Мелкие капсы NFKD не берёт — для них отдельная таблица.
    Лит-замены применяются ДО фильтра символов: иначе «@» и «$» успевают
    пропасть, и записи таблицы LEET для них мертвы («d@sha» → «д ша»).
    """
    s = unicodedata.normalize("NFKD", s)
    s = s.translate(SMALLCAPS)
    s = EMOJI_RE.sub(" ", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = unleet(s)
    # оставляем буквы, цифры, пробел, дефис, подчёркивание, точку
    s = re.sub(r"[^\w\s.\-]", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------- лит-замены
# Применяются только когда вокруг кириллица или строка похожа на транслит.
LEET = {"4": "ч", "3": "з", "0": "о", "6": "б", "9": "я", "1": "и", "@": "а", "$": "s"}


def unleet(s):
    """
    Заменяем только в буквенном окружении и только если рядом нет цифры.
    Условие «или» вместо «и» ломало хвостовые номера: «катя11» превращалось
    в «катяии» и имя пропадало. Цифра рядом — значит это номер, не замена.
    """
    out = []
    for i, ch in enumerate(s):
        if ch in LEET:
            prev = s[i - 1].lower() if i else ""
            nxt = s[i + 1].lower() if i + 1 < len(s) else ""
            if (not prev.isdigit() and not nxt.isdigit()
                    and (prev.isalpha() or nxt.isalpha())):
                out.append(LEET[ch])
                continue
        out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------- транслит
# Многобуквенные сочетания идут первыми — порядок важен.
TRANSLIT = [
    # четырёх- и трёхбуквенные сочетания
    ("shch", "щ"), ("sch", "щ"), ("ight", "айт"), ("tch", "ч"),
    # английские диграфы. Без них имена, записанные по-английски, рассыпались:
    # Elizabeth → «елизабетх», Catherine → «катхерине», Michael → «мичаел».
    # В русской транслитерации эти сочетания не встречаются, так что
    # добавлять их безопасно: «dasha» и «katya» не задеты.
    ("th", "т"), ("wh", "в"), ("gh", "г"), ("ph", "ф"), ("ck", "к"),
    ("oo", "у"), ("ou", "у"), ("qu", "кв"),
    ("ce", "се"), ("ci", "си"),
    # русская транслитерация
    ("yo", "ё"), ("zh", "ж"), ("kh", "х"),
    ("ts", "ц"), ("ch", "ч"), ("sh", "ш"), ("yu", "ю"), ("ya", "я"),
    ("iy", "ий"), ("ey", "ей"), ("ai", "ай"), ("ei", "ей"),
    ("aya", "ая"), ("aye", "айе"), ("ayi", "айи"), ("oyi", "ойи"),
    ("ay", "ай"), ("oy", "ой"), ("uy", "уй"),
    ("uya", "уя"), ("iya", "ия"),
    ("ja", "я"), ("je", "же"), ("ju", "ю"),
    ("a", "а"), ("b", "б"), ("c", "к"), ("d", "д"), ("e", "е"), ("f", "ф"),
    ("g", "г"), ("h", "х"), ("i", "и"), ("j", "дж"), ("k", "к"), ("l", "л"),
    ("m", "м"), ("n", "н"), ("o", "о"), ("p", "п"), ("q", "к"), ("r", "р"),
    ("s", "с"), ("t", "т"), ("u", "у"), ("v", "в"), ("w", "в"), ("x", "кс"),
    ("y", "ы"), ("z", "з"),
]


_VOW = "aeiouy"

# Хвостовые правила. Применяются к латинице ДО таблицы, иначе буквы уже
# заменены и зацепиться не за что. Каждое правило — отдельная частая ошибка.
_TAIL = [
    # Maria → Mariya → «мария». Иначе получалось «мариа» и имя не находилось.
    (re.compile(r"i[ae]$"), "iya"),
    # Hannah → Hanna. Конечная «h» после гласной не читается.
    (re.compile(r"(?<=[aeiou])h$"), ""),
    # ОТКЛОНЕНО: срезание конечной «e». Ставилось ради Catherine → катерин,
    # но делало из английских слов мужские фамилии: sunshine → суншин,
    # offline → оффлин, caroline → каролин, alice → алик. На 240к это
    # ~513 выдуманных мужских решений и 59 женщин, записанных в мужчины.
    # Замер точности этого не видел: у таких профилей поле фамилии пустое,
    # и в проверочную выборку они не попадают — ровно тот же дефект,
    # по которому отклонено правило рода прилагательного в namedict.probe.
    # Нужные имена дешевле добрать в словарь напрямую.
    # Molly → Molli. Но Vasiliy и Sergey не трогаем: там «y» часть «iy»/«ey».
    (re.compile(r"(?<=[bcdfghjklmnpqrstvwxz])y$"), "i"),
    # Хвостовое «ww» — украшение ника, а не «вв»: foxieww, katieww, lilieww.
    # Без этого «foxieww» превращался в «фоксиев» и уверенно уходил в мужчин.
    # Настоящие фамилии с удвоением пишут через «v» (ismayilovv), их не трогаем.
    (re.compile(r"w{2,}$"), ""),
]


def translit(s):
    s = s.lower()
    for rx, rep in _TAIL:
        if rx.pattern.endswith("e$") and len(s) < 5:
            continue
        s = rx.sub(rep, s)
    for a, b in TRANSLIT:
        s = s.replace(a, b)
    return s


def script_of(s):
    """Какой письменностью написано имя."""
    c = sum(1 for ch in s.lower() if ch in CYR)
    l = sum(1 for ch in s.lower() if ch in LAT)
    other = sum(1 for ch in s if ch.isalpha() and ch.lower() not in CYR and ch.lower() not in LAT)
    if other > max(c, l):
        return "other"
    if c and c >= l:
        return "cyr"
    if l:
        return "lat"
    return "none"


# ---------------------------------------------------------------- главное
def normalize(raw):
    """
    Возвращает признаки оформления + токены-кандидаты на имя.
    tokens_translit — какие из токенов получены транслитерацией с латиницы.
    Для них действует порог длины основы: транслит случайного английского
    слова легко садится на короткую основу («yandex» → «яндекс» → «ян»).
    """
    raw = raw if isinstance(raw, str) else ""
    f = decor_features(raw)
    clean = strip_decor(raw)          # unleet уже внутри
    sc = script_of(clean)
    f["script"] = sc

    toks = [t for t in re.split(r"[\s._\-]+", clean) if t]
    cand, from_lat = [], set()
    for t in toks:
        tl = t.lower()
        if not any(ch.isalpha() for ch in tl):
            continue
        # «аня2000», «катя11», «anna01»: цифры в поле имени — это номер,
        # а не часть имени. В нике они срезались, здесь — нет.
        if any(ch.isdigit() for ch in tl):
            stripped = re.sub(r"\d+", "", tl)
            if len(stripped) >= 2:
                tl = stripped
        if sc == "lat" or (script_of(tl) == "lat"):
            tr = translit(tl)
            cand.append(tr)
            from_lat.add(tr)
        else:
            cand.append(tl)
    f["clean"] = clean
    f["tokens"] = cand
    f["tokens_translit"] = from_lat
    f["n_tokens"] = len(cand)
    return f


def variants(tok):
    """
    Варианты написания одного токена для поиска по словарю.
    Ники часто удваивают буквы и путают е/ё — ищем по всем формам сразу.
    """
    out = [tok]
    collapsed = re.sub(r"(.)\1+", r"\1", tok)        # аннка → анка, ксюююша → ксюша
    if collapsed != tok:
        out.append(collapsed)
    for v in list(out):
        if "ё" in v:
            out.append(v.replace("ё", "е"))
    seen, res = set(), []
    for v in out:
        if v not in seen:
            seen.add(v)
            res.append(v)
    return res


def username_tokens(u):
    """Режем ник на куски: по разделителям, цифрам и смене регистра."""
    if not isinstance(u, str) or not u:
        return []
    u = re.sub(r"\d+", " ", u)
    u = re.sub(r"(?<=[a-zа-яё])(?=[A-ZА-ЯЁ])", " ", u)
    parts = [p.lower() for p in re.split(r"[\s._\-]+", u) if len(p) > 1]
    out = []
    for p in parts:
        out.append(p)
        if script_of(p) == "lat":
            out.append(translit(p))
    return out
