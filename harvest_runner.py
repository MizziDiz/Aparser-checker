#!/usr/bin/env python3
"""Автономный раннер подбора/добычи источников (A-Parser, узел .14).

Стратегия (по замерам, см. data/ops/CONTEXT.md):
  * глубина 5 страниц (задано в профилях парсеров парка);
  * DuckDuckGo = ГОЛЫЕ ключи, максимум ШИРИНЫ (themes × modifiers кейгена) —
    операторы на DDG дают 0;
  * Yahoo = сид × ФУТПРИНТ (footprints.yaml) — футпринты комплементарны,
    приносят домены, которых нет в голой выдаче;
  * гнать ОБА движка (Yahoo ∪ DDG почти не пересекаются).

Раунд (`--round`, вызывается по cron на контроллере .50):
  1. берёт следующий срез сидов (DDG) и пар сид×футпринт (Yahoo) из плана;
  2. раскладывает запросы в queries/<set>/ на узле, создаёт задачи БЕЗ пресета
     (выбором парсера), формат `$p1.serp.format('$query\\t$link\\n')`,
     «удалить задание по завершению» = да (очередь не копится);
  3. ждёт результат, считает уникальные домены, дедуплицирует против общей базы,
     дописывает строку в CSV и новые домены в master-файл.

Состояние (курсоры плана) — data/harvest/state.json; циклится по кругу.
Данные seed/footprint — твои: keygen/kwbuilder/config/{themes,footprints}.yaml.
"""
from __future__ import annotations
import argparse, csv, gzip, hashlib, json, random, re, shutil, sqlite3, subprocess, sys, time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aparser_monitor_ui as ui
from playwright.sync_api import sync_playwright, Error as PWError

REPO = Path(__file__).resolve().parent
KCFG = REPO / "keygen" / "kwbuilder" / "config"
OUT = REPO / "data" / "harvest"
OUT.mkdir(parents=True, exist_ok=True)
WORK = OUT / "queries"          # локальная раскладка (…/queries/<set>/<set>.txt)
WORK.mkdir(parents=True, exist_ok=True)

# скоринг проспектов (модель common-crawl-prospect-scoring, URL-часть) — прогоняем каждый URL
import harvest_score as _hscore
_URL_TERMS = _hscore.build_url_terms()


def uhash(url: str) -> str:
    """Компактный хэш URL (8 байт) — храним вместо полного URL (экономия места)."""
    return hashlib.blake2b(url.encode("utf-8", "ignore"), digest_size=8).hexdigest()


def ui_alive() -> bool:
    """Жив ли web-UI A-Parser (403 = жив, refused/timeout = лёг). Для контроля после Brave."""
    import urllib.request, urllib.error
    try:
        urllib.request.urlopen(UI_CFG["aparser_ui_url"], timeout=12)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def score_prospect(url: str) -> tuple[int, str]:
    """(score, family) по URL-футпринтам (55 при совпадении, иначе 0)."""
    s, fams = _hscore.score_url(url, _URL_TERMS)
    return s, (fams[0] if fams else "")

# ── что и где ───────────────────────────────────────────────────────────────
# Инфра узла (LAN-IP, путь к SMB-кредам, пути A-Parser) — в gitignored
# data/nodes/harvest_node.json. Плейсхолдеры-шаблон — data/nodes/harvest_node.example.json.
# Пересоздать при потере: data/ops/recreate-creds.sh
_NODE_CFG = Path(__file__).resolve().parent / "data" / "nodes" / "harvest_node.json"
NODE = json.loads(_NODE_CFG.read_text(encoding="utf-8")) if _NODE_CFG.exists() else {
    "smb": "//<LAN-IP>/C$", "creds": "/root/.smbcreds<N>",
    "queries_unc": r"soft\aparser\queries", "results_unc": r"soft\aparser\results",
    "ui_url": "http://<LAN-IP>:9092/",
}
_m = re.search(r"\.(\d+)/", NODE["smb"])
NODE_ID = _m.group(1) if _m else "0"   # номер сервера (последний октет), напр. "14"
UI_CFG = {
    "aparser_ui_url": NODE["ui_url"], "aparser_ui_password": "",
    "ui_nav_timeout_ms": 60000, "ui_cards_timeout_ms": 45000, "ui_page_change_ms": 12000,
    "queries_dir": str(WORK), "aparser_root": "",
}
TAGGED_FMT = "$p1.serp.format('$query\\t$link\\n')"
DDG_ENABLED = False     # DDG временно снят (с раунда ~220 отдаёт 0 — блок/сломан); Yahoo+Brave+футпринты тянут
DDG_BATCH = 200         # голых сидов в один DDG-набор
# Масштаб: задачи должны быть ОБЪЁМНЫМИ (как у соседних парсеров — 40МБ ключей нонстоп),
# а не напёрстком. flock -n в cron уже держит нонстоп. Первый ramp ×15 от 200/120 —
# дальше поднимать (цель 20k+) по мере того, как error-rate прокси в логе держится 0.
# Крупные батчи (объёмные задачи, как у соседей). Зависание было от СТАРОГО 90-мин wait,
# не от размера — теперь RESULT_WAIT_S=45мин обрезает недосчитавшуюся задачу, раунд не виснет.
YAHOO_BATCH = 3000      # запросов сид×оператор в один Yahoo-набор (из ~406k доступных)
SUGGEST_BATCH = 300     # сколько сидов раунда прогнать через Suggest (расширение пула)
FOOTPRINT_BATCH = 150   # футпринтов в раунд (сейчас доступно ~44 — берёт все)
BRAVE_BATCH = 2000      # голых сидов+кавычки-футпринты в Brave-набор
GZ_BATCH = 20000        # URL в файле-результате ≥ этого → пишем .txt.gz (экономия места)
RESULT_WAIT_S = 2700    # ждать до 45 мин: крупная задача settl-ится дольше (растущий файл)

STATE = OUT / "state.json"
CSV_LOG = OUT / "harvest_log.csv"
MASTER = OUT / "domains_master.txt"     # все уникальные домены за всё время


# ── твои данные: сиды и футпринты ────────────────────────────────────────────
def _load_yaml(p: Path):
    import yaml
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def load_geo_seeds() -> list[str]:
    """Нативные сиды тем на языках гео (geo_seeds.yaml — переведено вручную, без API).
    Suggest потом расширит их в нативные длинные хвосты."""
    f = KCFG / "geo_seeds.yaml"
    if not f.exists():
        return []
    data = (_load_yaml(f) or {}).get("seeds", {}) or {}
    return [str(s).strip() for seeds in data.values()
            for s in (seeds or []) if str(s).strip()]


def build_base_seeds() -> list[str]:
    """themes × modifiers (англ, кейген) + нативные сиды тем по языкам гео."""
    y = _load_yaml(KCFG / "themes.yaml")
    themes = [t["theme"] for t in y.get("themes", [])]
    mods = y.get("modifiers", []) or []
    seeds: list[str] = []
    for t in themes:
        if y.get("include_bare", True):
            seeds.append(t)
        for m in mods:
            seeds.append(f"{t} {m}")
    return seeds + load_geo_seeds()


def build_seeds() -> list[str]:
    """Базовые сиды кейгена + накопленный Suggest-пул (расширенные сиды)."""
    return build_base_seeds() + load_pool()


# Yahoo-трек = широкие операторы + ccTLD-операторы ВСЕХ гео из geo_plan.yaml.
# ccTLD-развитие по гео не требует перевода (англ. сид × site:.de/.fr/… → гео-домены);
# нативные переведённые сиды для DDG — отдельный уровень, нужен ANTHROPIC_API_KEY (translate.py).
YAHOO_BASE_OPS = [
    # intitle:{s} убран — замер 2026-07-21: intitle на Yahoo отдаёт 0. Рабочие на Yahoo:
    # inurl:, site:.tld+ключ, кавычки-фразы (site:+footprint ВМЕСТЕ = 0, не комбинируем).
    '{s} inurl:blog',
    '{s} inurl:forum',
    '{s} "powered by wordpress"',
    '{s} site:.org',
]


# «Протекающие» голые ccTLD: Yahoo плохо фильтрует site: по ним (замер 2026-07-17: 55–99% мимо
# целевого TLD), т.к. страны регистрируются под 2-м уровнем. Те же гео чисто покрывают .com.au/.co.uk/
# .com.br/.com.mx/.com.tr/.co.jp (0% мимо) → голые формы избыточны, исключаем.
GEO_OP_LEAKY = {"site:.au", "site:.br", "site:.uk", "site:.mx", "site:.tr", "site:.jp"}


def geo_operators() -> list[str]:
    """Уникальные ccTLD-операторы из geo_plan.yaml минус протекающие голые формы."""
    geos = _load_yaml(KCFG / "geo_plan.yaml").get("geos", []) or []
    ops = {op for g in geos for op in (g.get("operators") or [])}
    return sorted(ops - GEO_OP_LEAKY)


def build_yahoo_ops() -> list[str]:
    """сиды (база + Suggest-пул) × (базовые операторы + ccTLD всех гео) → запросы Yahoo.
    Приоритизация сама отберёт продуктивные пары (сид×ccTLD) и бросит насыщенные."""
    templates = YAHOO_BASE_OPS + [f"{{s}} {op}" for op in geo_operators()]
    return [t.format(s=s) for s in build_seeds() for t in templates]


# GSA-движковые футпринты (по verified-базе Base For GSA): находят ПОСТАБЕЛЬНЫЕ страницы под движки.
GSA_FOOTPRINTS = [
    # --- форумы (phpBB/SMF/vBulletin/MyBB/IPB/XenForo/Discuz/PunBB/FluxBB/…) ---
    '"powered by phpbb"', 'inurl:memberlist.php', 'inurl:viewtopic', 'inurl:"posting.php"',
    '"powered by smf"', '"simple machines forum"', 'inurl:"index.php?action=profile"',
    '"powered by vbulletin"', 'inurl:showthread.php', 'inurl:"member.php"',
    '"powered by mybb"', '"forum software by xenforo"', '"powered by ip.board"', '"invision power board"',
    '"powered by discuz"', 'inurl:"mod=space"', 'inurl:space-uid', 'inurl:"do=profile"',
    '"powered by punbb"', '"powered by fluxbb"', '"powered by yabb"', '"powered by vanilla"',
    '"powered by phorum"', '"powered by bbpress"',
    # --- Joomla + Kunena (замер buckets 2026-07-23: joomla распространён в Африке/LatAm/Asia) ---
    '"powered by joomla"', '"powered by kunena"', 'inurl:"option=com_kunena"', '"jcomments"',
    # --- гестбуки ---
    'inurl:guestbook', '"sign my guestbook"', 'inurl:gbook', 'inurl:"guestbook.php"',
    '"powered by advanced guestbook"', '"add to guestbook"', 'inurl:addguest',
    # --- блог-комменты (WP/MovableType/Serendipity/b2evolution/Drupal/…) ---
    '"leave a comment"', 'inurl:"wp-comments-post.php"', '"notify me of new"',
    '"your email address will not be published"', '"powered by wordpress"',
    '"powered by movable type"', '"powered by serendipity"', '"powered by b2evolution"',
    '"powered by drupal"', 'inurl:"comment/reply"', '"powered by nucleus"',
    # --- вики ---
    '"powered by mediawiki"', 'inurl:"index.php?title="', '"driven by dokuwiki"', 'inurl:"doku.php"',
    '"powered by pmwiki"', '"tiki wiki cms"', '"moinmoin powered"',
    # --- галереи (image comments) ---
    '"powered by coppermine"', '"powered by 4images"', '"powered by piwigo"',
    # --- Article-директории / submission (GSA Article-движки) ---
    '"submit article"', '"submit articles"', '"submit your article"', '"add article"',
    '"add new article"', '"post your article"', '"free article submission"', '"article directory"',
    '"powered by article dashboard"', '"powered by article friendly"', '"powered by articlems"',
    '"powered by seo-board"', '"powered by article directory"', 'inurl:"submit-article"',
    'inurl:"/articles/submit"', '"author login" "articles"',
    # --- директории / соц-закладки ---
    '"powered by pligg"', '"powered by phpld"', 'inurl:"submit.php?type=links"',
    '"powered by scuttle"', '"add your site"', '"submit your site"',
    # --- GnuBoard / профили ---
    'inurl:"index.php?a=profile"', '"powered by gnuboard"',
]


# Локализованные CMS-строки (кавычки-фразы) под целевые языки, сгруппированы по языку —
# язык даёт гео-таргет (без ccTLD, который зануляет выдачу; замер 2026-07-21: «deixe um
# comentário»=35/8, с site:.tld=0). Вес языка берётся из KPI-плана ниже.
GSA_FOOTPRINTS_LOCALIZED = {
    "es": ['"deja un comentario"', '"escribe un comentario"', '"deja tu comentario"', '"publicar un comentario"',
           '"libro de visitas"', '"firmar el libro de visitas"', '"crear cuenta"', '"registrarse"',
           '"con la tecnología de wordpress"', '"funciona con wordpress"', '"desarrollado por phpbb"'],
    "pt": ['"deixe um comentário"', '"escreva um comentário"', '"deixe seu comentário"', '"publicar comentário"',
           '"livro de visitas"', '"assine o livro de visitas"', '"criar conta"', '"registrar-se"',
           '"orgulhosamente desenvolvido"', '"funciona com wordpress"', '"desenvolvido por phpbb"'],
    "tr": ['"yorum yaz"', '"yorum yap"', '"yorum ekle"', '"ziyaretçi defteri"', '"deftere yaz"',
           '"üye ol"', '"hesap oluştur"', '"kayıt ol"'],
    "fr": ['"laisser un commentaire"', '"ajouter un commentaire"', '"poster un commentaire"',
           "\"livre d'or\"", "\"signer le livre d'or\"", '"créer un compte"', "\"s'inscrire\"",
           '"propulsé par phpbb"', '"propulsé par wordpress"'],
    "pl": ['"dodaj komentarz"', '"napisz komentarz"', '"zostaw komentarz"', '"księga gości"',
           '"wpis do księgi gości"', '"załóż konto"', '"zarejestruj się"', '"dumnie wspierane przez wordpress"'],
    "ja": ['"コメントを残す"', '"コメントする"', '"ゲストブック"', '"雑談掲示板"', '"掲示板"',
           '"新規登録"', '"アカウント作成"', '"掲示板に書き込む"'],
    "id": ['"tinggalkan komentar"', '"beri komentar"', '"kirim komentar"', '"buku tamu"', '"isi buku tamu"',
           '"buat akun"', '"daftar akun"', '"tinggalkan komen"', '"hantar komen"'],   # ID+MY
    "vi": ['"để lại bình luận"', '"viết bình luận"', '"gửi bình luận"', '"sổ lưu bút"',
           '"đăng ký"', '"tạo tài khoản"'],
    "th": ['"แสดงความคิดเห็น"', '"เขียนความคิดเห็น"', '"สมัครสมาชิก"', '"ลงทะเบียน"', '"สมุดเยี่ยม"'],
    "zh": ['"发表评论"', '"发表留言"', '"添加评论"', '"我要留言"', '"留言板"', '"注册账号"', '"用户注册"'],
}
LOCALIZED_FLAT = [fp for fps in GSA_FOOTPRINTS_LOCALIZED.values() for fp in fps]
FP_LANG = {fp: lang for lang, fps in GSA_FOOTPRINTS_LOCALIZED.items() for fp in fps}


# ── KPI-веса (артефакт claude.ai .../87e3185f «GSA verified — недельный прирост», 20.07.2026) ──
# План недельного прироста по 21 группе (Σ=440) задаёт распределение операторов и футпринтов.
# Generic-бакеты разнесены: Латам-75 → по es-LatAm; Др.Азия-25 → CN/SG; Африка-др.-20 → ZA; Океания-20 → AU.
KPI_UNIT = {   # гео (ccTLD site:-операторы Yahoo) → вес
    "CO": 26, "AR": 31, "CL": 16, "PE": 16, "EC": 16, "UY": 16, "MX": 16,   # es
    "BR": 40, "PT": 10,                                                     # pt
    "PL": 50, "FR": 20, "TR": 10, "JP": 20, "TH": 10, "VN": 20,             # eu/asia
    "ZA": 35, "AU": 20,                                                     # africa/oceania
    "ID": 22, "MY": 23, "CN": 15, "SG": 10, "PH": 3,                        # Азия-1 + Др.Азия
}
KPI_LANG = {   # язык локализованных футпринтов → вес (сумма гео языка)
    "es": 135, "pt": 50, "pl": 50, "fr": 20, "ja": 20, "vi": 20,
    "tr": 10, "th": 10, "id": 45, "zh": 15,                                 # id = ID+MY
}
_KPI_MEAN = 20.0          # ~средний вес; нормируем, чтобы KPI был ощутимым, но не давящим тилтом


def _op_kpi() -> dict:
    """ccTLD-оператор → KPI-вес (geo_plan unit→operators × KPI_UNIT)."""
    geos = _load_yaml(KCFG / "geo_plan.yaml").get("geos", []) or []
    m: dict[str, float] = {}
    for g in geos:
        w = KPI_UNIT.get(g.get("unit"), _KPI_MEAN)
        for op in (g.get("operators") or []):
            m[op] = max(m.get(op, 0), w)
    return m


_OP_KPI = _op_kpi()


def kpi_weight(query: str) -> float:
    """KPI-множитель запроса (нормирован в [0.4, 3.0]): по ccTLD-оператору или языку футпринта.
    Английский футпринт / голый сид → 1.0 (базовый). Тилтит prioritize к целевым по плану гео."""
    raw = 0.0
    for op, w in _OP_KPI.items():
        if op in query:
            raw = max(raw, w)
    if not raw:
        raw = KPI_LANG.get(FP_LANG.get(query.strip(), ""), 0)
    if not raw:
        return 1.0
    return min(3.0, max(0.4, raw / _KPI_MEAN))


FP_DENYLIST = OUT / "fp_denylist.txt"      # мёртвые футпринты (ведёт harvest_fpaudit.py)


def load_fp_denylist() -> set[str]:
    """Футпринты, помеченные аудитом как мёртвые (мало URL при прогонах) — исключаем."""
    if FP_DENYLIST.exists():
        return {l.strip() for l in FP_DENYLIST.read_text(encoding="utf-8").splitlines() if l.strip()}
    return set()


def build_footprint_queries() -> list[str]:
    """Футпринты БЕЗ ccTLD. Замер 2026-07-21: `footprint site:.tld`=0 на обоих движках,
    а голый `"powered by phpbb"`=34/41. Английские GSA-футпринты (широко) + локализованные
    CMS-строки (гео-таргет через язык). prioritize взвешивает по KPI-плану; мёртвые
    (по аудиту harvest_fpaudit.py) исключаются через fp_denylist.txt."""
    deny = load_fp_denylist()
    return [fp for fp in (GSA_FOOTPRINTS + LOCALIZED_FLAT) if fp not in deny]


# CJK/тайский/арабский/кириллица/деванагари/хангыль — Brave на них падает TypeError
_NONLATIN = re.compile(r"[぀-ヿ㐀-鿿가-힯฀-๿"
                       r"؀-ۿЀ-ӿऀ-ॿ]")


def build_brave_queries() -> list[str]:
    """Brave: голые сиды + кавычки-футпринты, ТОЛЬКО латиница. Замер 2026-07-23: Brave падает
    `TypeError: reading 'results'` на не-латинских запросах (电子商务/フィットネス/…) → 99% ошибок,
    жжёт потоки/прокси впустую. Нативные сиды/футпринты уходят в Yahoo (он их держит); Brave —
    латинские (вкл. es/pt/fr/pl/tr/vi с диакритикой). Brave игнорит inurl:/site: — только кавычки."""
    quoted = [fp for fp in GSA_FOOTPRINTS if fp.startswith('"')] + LOCALIZED_FLAT
    return [q for q in build_seeds() + quoted if not _NONLATIN.search(q)]


def build_footprint_pairs() -> list[str]:
    """ОПЦИОНАЛЬНЫЙ GSA-трек: тема × футпринт из footprints.yaml (`{тема} {футпринт}`).
    Узкий, для GSA-целей; не в основном плане объёма."""
    themes = [t["theme"] for t in _load_yaml(KCFG / "themes.yaml").get("themes", [])]
    fam = _load_yaml(KCFG / "footprints.yaml").get("families", {}) or {}
    fps = [str(l) for f in fam.values() if f.get("enabled", True)
           for l in (f.get("footprints", []) or []) if not str(l).startswith("!")]
    return [f"{t} {fp}" for t in themes for fp in fps]


# ── SMB ──────────────────────────────────────────────────────────────────────
def _smb(cmd: str) -> str:
    r = subprocess.run(["smbclient", NODE["smb"], "-A", NODE["creds"], "-c", cmd],
                       capture_output=True, text=True)
    return r.stdout + r.stderr


def deploy(set_name: str, lines: list[str]) -> Path:
    d = WORK / set_name
    d.mkdir(parents=True, exist_ok=True)
    local = d / f"{set_name}.txt"
    local.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _smb(f'prompt OFF; cd {NODE["queries_unc"]}; mkdir {set_name}; '
         f'cd {set_name}; lcd {d}; put {set_name}.txt {set_name}.txt')
    return local


def fetch_result(set_name: str, dest: Path) -> bool:
    """True, если удалённый файл результата уже появился (get его скачал) — даже
    если он пуст (0 находок): пустой файл — легитимный итог, не «не скачался»."""
    _smb(f'lcd {dest.parent}; cd {NODE["results_unc"]}\\{set_name}; '
         f'get {set_name}.txt {dest.name}')
    return dest.exists()


def cleanup_node(set_name: str) -> None:
    _smb(f'prompt OFF; cd {NODE["queries_unc"]}; deltree {set_name}')
    _smb(f'prompt OFF; cd {NODE["results_unc"]}; deltree {set_name}')


def purge_old() -> None:
    """Выместает все прошлые h_ddg_*/h_yah_* из queries и results (на случай, если
    cleanup_node не отработал: файл был занят A-Parser в момент завершения)."""
    for unc in (NODE["queries_unc"], NODE["results_unc"]):
        out = _smb(f'cd {unc}; ls')
        for name in re.findall(r"^\s+(\d*_?h_(?:ddg|yah|sug|fp|brave)_\S+)\s+D", out, re.M):
            _smb(f'prompt OFF; cd {unc}; deltree {name}')


# ── создание задачи без пресета ───────────────────────────────────────────────
SET_PARSER_JS = """(p)=>{const cs=Ext.ComponentQuery.query('combo').filter(c=>
  /^(Парсер|Parser|Select parser)$/i.test((c.getFieldLabel&&c.getFieldLabel())||'')&&c.isVisible(true));
  if(!cs.length)return false;const c=cs[0];const r=c.getStore().findRecord(c.displayField,p);
  if(!r)return false;c.select(r);return true;}"""
SET_PROFILE_JS = """(p)=>{const cs=Ext.ComponentQuery.query('combo').filter(c=>
  /^(Настройки|Settings|Select preset|Preset)$/i.test((c.getFieldLabel&&c.getFieldLabel())||'')&&c.isVisible(true));
  if(!cs.length)return false;const c=cs[0];const r=c.getStore().findRecord(c.displayField,p);
  if(!r)return false;c.select(r);return true;}"""
# task-пресет («Задание», store TasksPresets) грузит парсер + парсер-настройки + прокси-конфиг
# разом. Так Yahoo/Brave садятся на конфиг `aparser` (рабочие прокси), а не на дефолт.
SELECT_TASKPRESET_JS = """(name)=>{const cs=Ext.ComponentQuery.query('combo').filter(c=>
  /^(Задание|Task)$/i.test((c.getFieldLabel&&c.getFieldLabel())||'')&&c.isVisible(true));
  if(!cs.length)return false;const c=cs[0];const r=c.getStore().findRecord(c.displayField,name);
  if(!r)return false;c.select(r);c.fireEvent('select',c,r);return true;}"""
TP_YAHOO = "Aparser yahoo"     # task-пресет .14 (парсер SE::Yahoo + конфиг aparser)
TP_BRAVE = "Aparser brave"     # task-пресет .14 (парсер SE::Brave + конфиг aparser)
FINALIZE_JS = r"""(fmt)=>{
  const set=(re,val)=>{const f=Ext.ComponentQuery.query('field').filter(f=>re.test(
    (f.getFieldLabel&&f.getFieldLabel())||'')&&f.isVisible(true))[0];if(f)f.setValue(val);};
  set(/Общий формат результатов|General results format|Results format/i,fmt);
  const fn=Ext.ComponentQuery.query('field').filter(f=>/Имя файла|File name/i.test(
    (f.getFieldLabel&&f.getFieldLabel())||'')&&f.isVisible(true))[0];if(fn)fn.setValue('$queriesfile');
  // удалить задание по завершению = да (не копить очередь). БЕЗ isVisible — чекбокс под
  // «Больше опций» свёрнут, но Ext-компонент существует; ставим ВСЕ совпадения.
  Ext.ComponentQuery.query('checkbox').filter(c=>/Удалить задание по завершению|Delete task on complete/i.test(
    (c.getFieldLabel&&c.getFieldLabel())||(c.boxLabel||''))).forEach(c=>{if(c.setValue)c.setValue(true);});
}"""


def create_task(page, parser: str, set_name: str, local_file: Path,
                profile: str | None, fmt: str = TAGGED_FMT,
                task_preset: str | None = None, tries: int = 3) -> None:
    # ретрай всего заполнения редактора: ~10% раундов ловят транзиентный UI-флейк
    # («Task Editor не открылся» / «радио File не найдено») — Ext не устаканился к клику.
    # Повторное открытие редактора идемпотентно, пауза даёт SPA прийти в себя.
    err: Exception | None = None
    for attempt in range(tries):
        try:
            if not ui._click_text(page, ui.NAV_TASK_EDITOR, timeout=10000):
                raise RuntimeError("Task Editor не открылся")
            page.wait_for_timeout(2500)
            if task_preset:
                # «Задание»-пресет ставит парсер + настройки (aparser/прокси) сам; SET_PARSER не нужен
                if not page.evaluate(SELECT_TASKPRESET_JS, task_preset):
                    raise RuntimeError(f"task-пресет {task_preset!r} не найден на ноде")
                page.wait_for_timeout(2500)
            else:
                if not page.evaluate(SET_PARSER_JS, parser):
                    raise RuntimeError(f"парсер {parser} не выбран")
                page.wait_for_timeout(2500)
                if profile:
                    page.evaluate(SET_PROFILE_JS, profile)
                    page.wait_for_timeout(1500)
            if not page.evaluate(ui.SELECT_FILE_RADIO_JS):
                raise RuntimeError("радио File не найдено")
            ui._set_file_field(page, ui.LBL_SELECT_FILE, [ui._aparser_rel_path(UI_CFG, local_file)])
            page.evaluate(FINALIZE_JS, fmt)
            if not ui._click_btn(page, ui.BTN_ADD_TASK):
                raise RuntimeError("«Добавить задание» не найдена")
            page.wait_for_timeout(1500)
            return                                       # успех
        except (RuntimeError, PWError) as e:
            err = e
            if attempt < tries - 1:
                page.wait_for_timeout(3000)              # дать SPA устаканиться и пробуем заново
    raise err


# ── Suggest: расширение сидов ─────────────────────────────────────────────────
SUGGEST_PARSER = "SE::Yahoo::Suggest"                       # Yahoo Suggest жив (Google блокнут)
SUGGEST_FMT = "$p1.results.format('$suggest\\n')"           # коллекция results, поле $suggest
POOL = OUT / "seeds_expanded.txt"                            # накопленные расширенные сиды
_BAD_SEED = re.compile(r"""[<>{}\[\]|\\/@#~^*=+`"\x00-\x1f]|https?:""")  # мусор/разметка/URL в сиде


def load_pool() -> list[str]:
    return POOL.read_text(encoding="utf-8").splitlines() if POOL.exists() else []


def expand_seeds(pw, seeds: list[str], tag: str) -> list[str]:
    """Прогоняет сиды через Yahoo Suggest, возвращает подсказки (сырые)."""
    set_name = f"{NODE_ID}_h_sug_{tag}"
    deploy(set_name, seeds)
    b, page = ui.open_ui(pw, UI_CFG, headless=True)
    try:
        page.wait_for_function("typeof Ext !== 'undefined'", timeout=120000)
        create_task(page, SUGGEST_PARSER, set_name, WORK / set_name / f"{set_name}.txt",
                    None, fmt=SUGGEST_FMT)
    finally:
        b.close()
    # (раньше тут было удаление queries-папки через 4с, как в run_task до фикса —
    #  A-Parser не успевал прочитать файл → "Queries file not exists". Убрано;
    #  autosend на этом узле выключен, защищать от него нечего.)
    dest = WORK / set_name / f"{set_name}.result.txt"
    wait_settled(set_name, dest, timeout=400)
    sugg = [l.strip() for l in dest.read_text(encoding="utf-8", errors="ignore").splitlines()
            if l.strip()] if dest.exists() else []
    cleanup_node(set_name)
    dest.unlink(missing_ok=True)
    return sugg


def grow_pool(pw, seeds: list[str], tag: str) -> int:
    """Расширяет пул: Suggest(seeds) → фильтр → дозапись новых в POOL. Возвращает +новых."""
    raw = expand_seeds(pw, seeds, tag)
    known = {s.lower() for s in build_base_seeds()} | {s.lower() for s in load_pool()}
    fresh, seen = [], set()
    for s in raw:
        k = s.lower()
        # ДЕНИЛИСТ вместо ASCII-аллоулиста: принимаем любые письменности (нативные сиды
        # es/pt/ja/th/vi/zh — включая тайские/деванагари огласовки, что \w отсекал), режем
        # только разметку/URL/спецсимволы/управляющие. ≤5 слов — отсекаем мусорные длинные
        # хвосты Suggest (они же роняли Brave-парсер). Так пул растёт под целевые языки.
        if (3 <= len(s) <= 60 and len(s.split()) <= 5 and k not in known and k not in seen
                and not _BAD_SEED.search(k) and any(ch.isalpha() for ch in k)):
            fresh.append(s); seen.add(k)
    if fresh:
        with POOL.open("a", encoding="utf-8") as f:
            f.write("\n".join(fresh) + "\n")
    return len(fresh)


# ── замер ─────────────────────────────────────────────────────────────────────
def domains_of(path: Path) -> set[str]:
    out: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        url = line.split("\t", 1)[-1].strip()
        m = re.match(r"https?://(?:www\.)?([^/\s]+)", url)
        if m:
            out.add(m.group(1).lower())
    return out


def load_master() -> set[str]:
    return set(MASTER.read_text(encoding="utf-8").split()) if MASTER.exists() else set()


def append_master(new: set[str]) -> None:
    if new:
        with MASTER.open("a", encoding="utf-8") as f:
            f.write("\n".join(sorted(new)) + "\n")


def log_csv(row: dict) -> None:
    new = not CSV_LOG.exists()
    with CSV_LOG.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["ts", "engine", "set", "queries",
                                          "uniq_urls", "uniq_domains", "new_domains", "cum_domains"])
        if new:
            w.writeheader()
        w.writerow(row)


# ── состояние / план ─────────────────────────────────────────────────────────
def load_state() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {"ddg_cur": 0, "yahoo_cur": 0, "round": 0}


def save_state(s: dict) -> None:
    STATE.write_text(json.dumps(s, indent=1))


def slice_cyclic(items: list, cur: int, n: int) -> tuple[list, int]:
    if not items:
        return [], cur
    take = [items[(cur + i) % len(items)] for i in range(min(n, len(items)))]
    return take, (cur + len(take)) % len(items)


# ── один раунд ────────────────────────────────────────────────────────────────
# ── аналитика: построчная статистика результатов (SQLite) ─────────────────────
DB = OUT / "harvest.db"

# распознать оператор/футпринт и «чистый» сид из строки запроса Yahoo (любой ccTLD — свой оператор)
_RX_INTITLE = re.compile(r"^intitle:(.+)$")
_RX_TRAILOP = re.compile(r'^(.+?) (site:\S+|inurl:\S+|"powered by wordpress")$')


def classify(query: str, engine: str) -> tuple[str, str]:
    """(seed, op) из строки запроса. Голые движки (ddg/brave) — op=plain; Yahoo/footprint —
    распознаём оператор (в т.ч. любой ccTLD site:.de/.co.uk/… как отдельный op)."""
    if engine in ("ddg", "brave", "suggest"):
        return query, "plain"
    m = _RX_INTITLE.match(query)
    if m:
        return m.group(1).strip(), "intitle"
    m = _RX_TRAILOP.match(query)
    if m:
        op = m.group(2)
        return m.group(1).strip(), ("powered-by-wp" if op.startswith('"') else op)
    return query, "other"


def _zone(domain: str) -> str:
    import tldextract
    return tldextract.extract(domain).suffix or ""


def db_conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB)
    # схема со скорингом+хэшем+источником: URL хранится ХЭШЕМ (экономия места), плюс
    # source (откуда: ddg/yahoo/footprint/brave), ts (когда), score/family (проспект-скоринг).
    c.execute("""CREATE TABLE IF NOT EXISTS results(
        ts TEXT, source TEXT, uhash TEXT, domain TEXT, zone TEXT, seed TEXT, op TEXT,
        score INTEGER, family TEXT, new INTEGER,
        UNIQUE(source, seed, op, uhash))""")
    for col in ("domain", "seed", "op", "zone", "source", "ts", "family"):
        c.execute(f"CREATE INDEX IF NOT EXISTS ix_{col} ON results({col})")
    return c


RETENTION_DAYS = 14      # результаты старше — удаляются, БД не пухнет (была 309 МБ за сутки)


def purge_db() -> int:
    """Удаляет строки результатов старше RETENTION_DAYS. При удалении — VACUUM (вернуть место)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat(timespec="seconds")
    c = db_conn()
    n = c.execute("DELETE FROM results WHERE ts < ?", (cutoff,)).rowcount
    c.commit()
    if n:
        c.execute("VACUUM")
        c.commit()
    c.close()
    return n


def store_results(dest: Path, engine: str, ts: str, before: set[str], source: str = "") -> None:
    """Разбирает tagged-файл (query\\turl): каждый НОВЫЙ URL скорится (проспект-модель) и
    ложится в SQLite ХЭШЕМ + source(откуда) + ts(когда) + score/family."""
    src = source or engine
    rows = []
    for line in dest.read_text(encoding="utf-8", errors="ignore").splitlines():
        if "\t" not in line:
            continue
        query, url = line.split("\t", 1)
        url = url.strip()
        m = re.match(r"https?://(?:www\.)?([^/\s]+)", url)
        if not m:
            continue
        dom = m.group(1).lower()
        seed, op = classify(query.strip(), engine)
        score, family = score_prospect(url)
        rows.append((ts, src, uhash(url), dom, _zone(dom), seed, op, score, family,
                     0 if dom in before else 1))
    if rows:
        c = db_conn()
        c.executemany("INSERT OR IGNORE INTO results VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        c.commit(); c.close()


def wait_settled(set_name: str, dest: Path, timeout: int = RESULT_WAIT_S) -> bool:
    """Ждёт СТАБИЛИЗАЦИИ результата: размер не растёт 3 замера подряд (задача дописала).
    Иначе на крупных батчах забирается частичный результат до завершения задачи."""
    dest.unlink(missing_ok=True)
    prev, stable, t0 = -1, 0, time.time()
    while time.time() - t0 < timeout:
        time.sleep(15)
        if fetch_result(set_name, dest):
            sz = dest.stat().st_size
            # 0 байт не может стабилизироваться досрочно: задача ещё не начала писать
            # (частый случай на 200-запросных батчах) неотличима от «реально пусто» по
            # первым замерам — быстрый путь только для РАСТУЩЕГО контента; пустой файл
            # ждёт полный timeout, тогда пуст-легитимно.
            if sz > 0 and sz == prev:
                stable += 1
                if stable >= 3:
                    return True
            else:
                stable, prev = 0, sz
    return dest.exists()


def run_task(pw, parser: str, set_name: str, lines: list[str], profile: str | None,
             master: set[str], engine: str, source: str = "",
             task_preset: str | None = None) -> dict | None:
    deploy(set_name, lines)
    b, page = ui.open_ui(pw, UI_CFG, headless=True)
    try:
        page.wait_for_function("typeof Ext !== 'undefined'", timeout=120000)
        create_task(page, parser, set_name, WORK / set_name / f"{set_name}.txt",
                    profile, task_preset=task_preset)
    finally:
        b.close()
    # (раньше тут было удаление queries-папки через 4с — ломало DDG на 200 сидов:
    #  A-Parser не успевал прочитать файл → "Queries file not exists". Убрано.)
    # ждём ЗАВЕРШЕНИЯ (стабилизации файла), а не первого появления
    dest = WORK / set_name / f"{set_name}.result.txt"
    if not wait_settled(set_name, dest):
        print(f"  [{engine}] {set_name}: результат не получен/не стабилизировался за {RESULT_WAIT_S}с")
        cleanup_node(set_name)
        return None
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    doms = domains_of(dest)
    urls = len({l.split("\t", 1)[-1].strip() for l in dest.read_text(encoding="utf-8", errors="ignore").splitlines() if "http" in l})
    new = doms - master
    store_results(dest, engine, ts, set(master), source or engine)   # скоринг+хэш ДО обновления базы
    # СЫРОЙ результат парсера: чистые URL (все, без тегов/доменов) → Aparser results;
    # разложит по странам ваш Split. Suggest сюда НЕ идёт (у него отдельный путь).
    if SHARE.exists():
        try:
            urls_only = sorted({l.split("\t", 1)[-1].strip()
                                for l in dest.read_text(encoding="utf-8", errors="ignore").splitlines()
                                if "http" in l})
            if urls_only:                                # 0 URL → пустышку на шару не кладём (GSA не нужны пустые)
                blob = "\n".join(urls_only) + "\n"
                if len(urls_only) >= GZ_BATCH:           # большой батч → архивируем в .gz
                    with gzip.open(SHARE / f"{set_name}.txt.gz", "wt", encoding="utf-8") as g:
                        g.write(blob)
                else:
                    (SHARE / f"{set_name}.txt").write_text(blob, encoding="utf-8")
        except Exception:
            pass
    master |= doms
    append_master(new)
    cleanup_node(set_name)
    dest.unlink(missing_ok=True)
    row = {"ts": ts,
           "engine": engine, "set": set_name, "queries": len(lines),
           "uniq_urls": urls, "uniq_domains": len(doms),
           "new_domains": len(new), "cum_domains": len(master)}
    log_csv(row)
    print(f"  [{engine}] {set_name}: uniq_domains={len(doms)} new={len(new)} cum={len(master)}")
    return row


# ── (1) фильтр генериков: чистый список линк-целей ────────────────────────────
WIDTH_CUTOFF = 6            # домен, найденный ≥ стольким РАЗНЫМ сидам → генерик (авторитет)
DENYLIST = OUT / "denylist.txt"
TARGETS = OUT / "targets.txt"
BUILTIN_DENY = {
    "forbes.com", "amazon.com", "en.wikipedia.org", "wikipedia.org", "youtube.com",
    "facebook.com", "m.facebook.com", "twitter.com", "x.com", "linkedin.com",
    "instagram.com", "pinterest.com", "reddit.com", "quora.com", "medium.com",
    "yelp.com", "tripadvisor.com", "apple.com", "microsoft.com", "google.com",
    "play.google.com", "nerdwallet.com", "investopedia.com", "bankrate.com",
}


def load_denylist() -> set[str]:
    d = set(BUILTIN_DENY)
    if DENYLIST.exists():
        d |= {x.strip().lower() for x in DENYLIST.read_text(encoding="utf-8").split() if x.strip()}
    return d


def dynamic_generics(cutoff: int = WIDTH_CUTOFF) -> set[str]:
    """Домены, найденные ≥ cutoff разными сидами — авторитеты, не линк-цели."""
    if not DB.exists():
        return set()
    c = db_conn()
    g = {d for (d,) in c.execute(
        "SELECT domain FROM results GROUP BY domain HAVING COUNT(DISTINCT seed) >= ?", (cutoff,))}
    c.close()
    return g


def write_targets() -> tuple[int, int]:
    """targets.txt = все домены минус денлист и динамические генерики."""
    excl = load_denylist() | dynamic_generics()
    targets = sorted(load_master() - excl)
    TARGETS.write_text("\n".join(targets) + "\n", encoding="utf-8")
    return len(targets), len(excl)


SHARE = Path("/srv/share/Aparser results")   # шара (локальный маунт на .50)
TARGET_GEO = OUT / "target_geo.txt"


def target_zones() -> set[str]:
    """Зоны целевых гео (суффиксы ccTLD из geo_operators): com.co, ar, fr, pl, co.jp, …"""
    return {op[len("site:."):] for op in geo_operators()}


def write_target_geo() -> int:
    """Чистый целевой срез: домены базы, чей TLD ∈ целевым зонам (без сброса смешанной базы)."""
    import tldextract
    tz = target_zones()
    doms = sorted(d for d in load_master() if tldextract.extract(d).suffix in tz)
    TARGET_GEO.write_text("\n".join(doms) + "\n", encoding="utf-8")
    return len(doms)


def push_to_share() -> None:
    """Кладёт ПОДПИСАННЫЕ номером сервера обработанные результаты на шару (перезапись,
    без накопления). autosend сырые h_* дампы больше на шару не носит (раннер их быстро чистит)."""
    if not SHARE.exists():
        return
    for src, dst in [(TARGETS, f"{NODE_ID}_targets.txt"),
                     (TARGET_GEO, f"{NODE_ID}_target_geo.txt"),
                     (MASTER, f"{NODE_ID}_domains_master.txt"),
                     (OUT / "postable_targets.txt", f"{NODE_ID}_postable_targets.txt")]:
        try:
            if src.exists():
                shutil.copy(src, SHARE / dst)
        except Exception:
            pass


# ── (2) обратная связь: приоритет продуктивных сидов/операторов ────────────────
SAT_THRESHOLD = 3          # last_new < этого → сид/оператор «насыщен», в конец очереди


def unit_stats(source: str) -> dict[tuple[str, str], tuple[int, int]]:
    """{(seed,op): (новых_ДОМЕНОВ_на_последнем_прогоне, число_прогонов)} из БД по источнику.
    Считаем РАЗНЫЕ новые домены, а не SUM(new): иначе один многостраничный домен (new=1
    на каждой его странице) раздувал бы продуктивность сида/оператора (качество≠объём)."""
    if not DB.exists():
        return {}
    c = db_conn()
    tmp: dict[tuple[str, str], list] = {}
    for seed, op, ts, ns in c.execute(
            "SELECT seed, op, ts, COUNT(DISTINCT CASE WHEN new THEN domain END) "
            "FROM results WHERE source=? GROUP BY seed, op, ts ORDER BY ts", (source,)):
        tmp.setdefault((seed, op), []).append(ns or 0)
    c.close()
    return {k: (v[-1], len(v)) for k, v in tmp.items()}


def prioritize(candidates: list[str], source: str, batch: int) -> list[str]:
    """Сортирует запросы: не гонявшиеся (explore) → продуктивные (по new) → насыщенные."""
    stats = unit_stats(source)

    def score(q: str):
        w = kpi_weight(q)                       # KPI-план: тилт распределения к целевым гео/языкам
        st = stats.get(classify(q, source))
        if st is None:
            return (2, w)                       # ещё не гоняли — исследуем (высокий KPI первым)
        last_new, _ = st
        if last_new < SAT_THRESHOLD:
            return (0, last_new * w)            # насыщен — в конец
        return (1, last_new * w)                # продуктивен — по KPI×new
    return sorted(candidates, key=score, reverse=True)[:batch]


def do_round() -> None:
    st = load_state()
    st["round"] += 1
    purge_old()                       # вымести остатки прошлых раундов на узле
    aged = purge_db()                 # ретенция БД: результаты старше 2 недель
    if aged:
        print(f"  [ретенция] удалено строк старше {RETENTION_DAYS} дн: {aged}")
    stamp = datetime.now().strftime("%m%d_%H%M")
    master = load_master()
    ddg_seeds = prioritize(build_seeds(), "ddg", DDG_BATCH)
    yh_lines = prioritize(build_yahoo_ops(), "yahoo", YAHOO_BATCH)
    print(f"== раунд {st['round']} == база_доменов={len(master)} "
          f"сидов_всего={len(build_seeds())} (приоритет по new-выходу)")

    with sync_playwright() as pw:
        if DDG_ENABLED and ddg_seeds:
            run_task(pw, "SE::DuckDuckGo", f"{NODE_ID}_h_ddg_{stamp}", ddg_seeds, None, master, "ddg", "ddg")
        if yh_lines:
            run_task(pw, "SE::Yahoo", f"{NODE_ID}_h_yah_{stamp}", yh_lines, None, master, "yahoo", "yahoo", task_preset=TP_YAHOO)
        fp_q = prioritize(build_footprint_queries(), "footprint", FOOTPRINT_BATCH)
        if fp_q:
            run_task(pw, "SE::Yahoo", f"{NODE_ID}_h_fp_{stamp}", fp_q, None, master, "yahoo", "footprint", task_preset=TP_YAHOO)
        # РОТАЦИЯ: случайная выборка сидов вместо статичного prioritize('ddg') — иначе
        # Suggest жевал одни и те же топ-N каждый раунд → все подсказки известны → пул стоял.
        _sd = build_seeds()
        added = grow_pool(pw, random.sample(_sd, min(SUGGEST_BATCH, len(_sd))), stamp)
        print(f"  [suggest] +{added} новых сидов (пул={len(load_pool())})")
        # Brave — ПОСЛЕДНИМ (может уронить A-Parser); гоняем только если UI жив, после — контроль
        brave_seeds = prioritize(build_brave_queries(), "brave", BRAVE_BATCH)
        if brave_seeds and ui_alive():
            run_task(pw, "SE::Brave", f"{NODE_ID}_h_brave_{stamp}", brave_seeds, None, master, "brave", "brave", task_preset=TP_BRAVE)
            if not ui_alive():
                print("  [brave] ⚠️ A-Parser НЕ отвечает после Brave — watchdog поднимет, раунды восстановятся")
        elif not ui_alive():
            print("  [brave] пропуск — UI недоступен")
    tgt, excl = write_targets()          # локальные артефакты (не на шару)
    geo = write_target_geo()
    # на шару (Aparser results) идут ТОЛЬКО сырые URL-результаты задач (пишет run_task);
    # доменные сводки и разбивку по странам туда НЕ кладём (страны — ваш Split).
    save_state(st)
    print(f"== раунд {st['round']} готов; база={len(master)} ЦЕЛЕЙ={tgt} ЦЕЛЕВ.ГЕО={geo} "
          f"отсеяно_генериков={excl} пул_сидов={len(load_pool())}")


def report(top: int = 15) -> None:
    """Аналитика: движки, операторы/футпринты, ключи, зоны выдачи, широта доменов."""
    if not DB.exists():
        print("нет данных (data/harvest/harvest.db) — сначала прогоны раннера"); return
    c = db_conn()
    rows = c.execute("SELECT COUNT(*) FROM results").fetchone()[0]
    dom = c.execute("SELECT COUNT(DISTINCT domain) FROM results").fetchone()[0]
    gen = dynamic_generics()
    deny = load_denylist()
    targets = len(load_master() - gen - deny)
    print(f"== АНАЛИЗ ХАРВЕСТА ==  строк-результатов={rows}  уник.доменов={dom}")
    print(f"    ЛИНК-ЦЕЛЕЙ (после фильтра): {targets}  |  генериков отсеяно: "
          f"{len(gen)} (ширина≥{WIDTH_CUTOFF}) + {len(deny)} денлист  →  targets.txt")

    # ── СКОРИНГ (проспект-модель) ──
    total_scored = c.execute("SELECT COUNT(*) FROM results").fetchone()[0]
    prospects = c.execute("SELECT COUNT(*) FROM results WHERE score>=55").fetchone()[0]
    print(f"\n▸ Проспект-скоринг: {prospects} проспектов (score≥55) из {total_scored} "
          f"({round(100*prospects/total_scored,2) if total_scored else 0}%)")
    print("    по семействам:", ", ".join(
        f"{fam}={n}" for fam, n in c.execute(
            "SELECT family,COUNT(*) FROM results WHERE score>=55 AND family!='' "
            "GROUP BY family ORDER BY 2 DESC")) or "—")
    print("    плотность проспектов по источникам:")
    for src, tot, pr in c.execute(
            "SELECT source, COUNT(*), SUM(CASE WHEN score>=55 THEN 1 ELSE 0 END) "
            "FROM results GROUP BY source ORDER BY 3 DESC"):
        print(f"      {src:<10} {pr or 0}/{tot} = {round(100*(pr or 0)/tot,2) if tot else 0}%")

    print("\n▸ Источники (уник.домены / новых):")
    for e, d, n in c.execute("SELECT source,COUNT(DISTINCT domain),"
                             "COUNT(DISTINCT CASE WHEN new=1 THEN domain END) "
                             "FROM results GROUP BY source ORDER BY 2 DESC"):
        print(f"    {e:<10} {d:<6} new={n}")

    ops: dict[str, set] = {}
    for op, d in c.execute("SELECT op,domain FROM results"):
        ops.setdefault(op, set()).add(d)
    print("\n▸ Операторы/футпринты (уник.домены):")
    for op in sorted(ops, key=lambda k: -len(ops[k])):
        print(f"    {op:<16} {len(ops[op])}")
    cov, order, pool = set(), [], dict(ops)
    while pool:
        best = max(pool, key=lambda k: len(pool[k] - cov)); g = len(pool[best] - cov)
        if not g: break
        cov |= pool[best]; order.append(f"{best}(+{g})"); del pool[best]
    print("    жадный набор →", " ".join(order), f"= {len(cov)} доменов")

    print(f"\n▸ Топ-{top} ключей/сидов (уник.домены / новых):")
    for s, d, n in c.execute("SELECT seed,COUNT(DISTINCT domain),"
                             "COUNT(DISTINCT CASE WHEN new=1 THEN domain END) "
                             "FROM results GROUP BY seed ORDER BY 2 DESC LIMIT ?", (top,)):
        print(f"    {s[:30]:<30} {d:<5} new={n}")

    print("\n▸ Зоны выдачи (уник.домены):")
    for z, d in c.execute("SELECT zone,COUNT(DISTINCT domain) FROM results "
                          "GROUP BY zone ORDER BY 2 DESC LIMIT 12"):
        print(f"    .{z:<12} {d}")

    print("\n▸ Самые широкие домены (найдены N разными сидами):")
    for dm, r in c.execute("SELECT domain,COUNT(DISTINCT seed) r FROM results "
                           "GROUP BY domain ORDER BY r DESC LIMIT 12"):
        print(f"    {dm[:34]:<34} {r} сидов")
    c.close()


def show_status() -> None:
    st = load_state()
    master = load_master()
    print(f"раундов: {st.get('round',0)} | база доменов: {len(master)} | "
          f"курсоры ddg={st.get('ddg_cur',0)} yahoo={st.get('yahoo_cur',0)}")
    if CSV_LOG.exists():
        rows = list(csv.DictReader(CSV_LOG.open()))
        print(f"строк в логе: {len(rows)}; последние:")
        for r in rows[-6:]:
            print(f"  {r['ts']} {r['engine']:<5} {r['set']:<16} dom={r['uniq_domains']:<4} new={r['new_domains']:<4} cum={r['cum_domains']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--round", action="store_true", help="один раунд добычи")
    ap.add_argument("--status", action="store_true", help="показать прогресс")
    ap.add_argument("--report", action="store_true", help="аналитика: ключи/операторы/зоны/домены")
    a = ap.parse_args()
    if a.status:
        show_status()
    elif a.report:
        report()
    else:
        do_round()
