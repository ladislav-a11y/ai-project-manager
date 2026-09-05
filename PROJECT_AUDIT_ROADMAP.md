# AI Project Manager — audit zdrojů, historie nápadů a návrh roadmapy

Vypracováno jako výstup karty **„P4 — Revize MD/JSON stavových souborů“**. Původní verze (iterace 1,
2026-08-28) vznikla bez živého přístupu k Trello API a její závěr o dvou P5 stavových kartách byl
proto založen jen na odvozeném snímku (`runtime/specs/*.md`). Druhá verze byla **ZNOVU OTEVŘENA PO
ŽIVÉM OVĚŘENÍ** (stejné datum, navazující iterace): `TRELLO_KEY`/`TRELLO_TOKEN`/`TRELLO_BOARD_ID`
byly v tomto běhu skutečně dostupné v prostředí procesu, na rozdíl od dřívějšího předpokladu — živé
Trello tedy bylo možné dotázat přímo a závěry níže z něj vycházejí, ne z odvozeného specu.

**Stav: ZNOVU OTEVŘENO PO NEZÁVISLÉM OVĚŘENÍ (třetí iterace, 2026-08-28).** Nezávislé ověření
opravilo dvě věci ve druhé verzi tohoto dokumentu — viz sekci 4.3a a 4.4 pro plné znění opravy:
1. `CODEX_DOD.md` (`D:\orchestrator\ai-orchestrator\CODEX_DOD.md`) je historicky nezaškrtnutý
   checklist, ale kód (`orchestrator/agents/codex.py`) a testy (`tests/test_codex_agent.py`,
   nezávislý plný běh 2026-08-28: **238 passed, 2 skipped**) autoritativně dokazují, že konkrétní
   položka P5 AI Orchestrator #4 („zlepšit zpracování neúplného CLI výstupu") je splněná — `CODEX_DOD.md`
   se tedy klasifikuje jako **stale checklist**, ne jako důkaz otevřené práce.
2. Dlouhodobý persistentní Windows scheduler **není live prokázaný**: `schtasks /Query /TN "AI
   Project Manager Scheduler"` úlohu nenajde, PID 21500 zmíněný ve Slack zprávě „watchdog online" už
   neběží, a historické logy přes více dnů samy o sobě nejsou důkaz nepřetržitého běhu. Toto je
   konkrétní, otevřený **provozní** bod, ale patří do **P4 — Viditelnost scheduleru a akční lidský
   zásah**, ne do obecné P5 stavové karty AI Project Manager.

Živé Trello zůstává i v této iteraci nejvyšší autoritou (sekce 0a beze změny) — obě opravy výše jsou
doplněním/upřesněním živě ověřeného stavu z 4.1–4.3, ne jeho popřením.

---

## 0a. Živá autorita Trella vs. historické lokální specy (přečti první)

**Pravidlo priority zdrojů použité v celém tomto dokumentu:**

1. **Živé Trello API** (dotázáno tímto během přes `RealTrelloClient`/`fetch_all_projects` s reálnými
   `TRELLO_KEY`/`TRELLO_TOKEN`/`TRELLO_BOARD_ID`) — **jediný skutečný zdroj pravdy o aktuálním stavu
   karet** (status, `blocked_by`, `checkpoint.completed_dod_indices`, `lifecycle_status`, plný text
   popisu karty). Cokoliv níže označené jako „živě ověřeno" pochází odsud, z běhu provedeného v rámci
   této iterace.
2. **`runtime/specs/*.md`** (gitignored, generované PM při každém dispatchi) — užitečná, ale
   **odvozená a potenciálně zastaralá** momentka toho, co bylo odesláno orchestrátoru při posledním
   dispatchi dané karty. Není totéž co aktuální stav karty — karta se mohla mezitím posunout dál
   (dokončit, přesunout, změnit `blocked_by`), aniž by se nový spec vygeneroval. Minulá verze tohoto
   dokumentu tuto vrstvu omylem používala jako náhradu za živé Trello a to vedlo k zastaralému závěru
   v sekci 4 (viz níže) — obě P1 auditní karty byly ve skutečnosti dávno Hotovo, zatímco odvozený spec
   pro P1 AI Project Manager stále ukazoval `completed_dod_indices: []`.
3. **Root DoD soubory** (`NEXT_DOD.md`, `AUTOMATION_DOD.md`, `HANDOFF_FIX_DOD.md`, `LIVE_HANDOFF_DOD.md`)
   a **`specs/*`** — historické zadávací dokumenty z počátku projektu, žádný z nich se od svého vzniku
   nemění; validní jen jako dobový záznam, nikdy jako aktuální stav.
4. **Provozní logy** (`runtime/scheduled-start.log`, `runtime/scheduler/scheduler-*.log`) — přímý
   živý důkaz *chování za běhu* (restarty, self-update, recovery cykly), ale ne o aktuálním stavu
   karet samotných — k tomu slouží bod 1.

Kdykoliv se dále v textu objeví rozpor mezi (1) a čímkoliv jiným, vítězí vždy (1).

---

## 0. Inventář relevantních zdrojů

| Zdroj | Účel | Aktuálnost | Autorita |
|---|---|---|---|
| **Živé Trello API** (tento běh) | Přímý dotaz na `fetch_all_projects` přes `RealTrelloClient` — 29 karet, plný popis, checkpoint, `blocked_by` | Živé, dotázáno v rámci této iterace | **Nejvyšší — jediná skutečná autorita o aktuálním stavu karet** |
| `README.md` (tracked) | Provozní dokumentace: instalace, konfigurace, self-update, watchdog, stavový model | Aktuální, odpovídá skutečnému kódu (ověřeno proti `cli.py`, `daemon.py`, `self_update.py`, `watchdog.py`) | Autoritativní pro provoz |
| `ai_project_manager/*.py` | Skutečná implementace | Aktuální (pracovní strom má rozpracované, ale funkční změny) | Autoritativní — zdroj pravdy o tom, co je hotové |
| `tests/*.py` | Regresní pokrytí | Aktuální, rozšiřuje se souběžně s kódem | Autoritativní pro ověření chování |
| `runtime/specs/*.md` (gitignored, generované) | Poslední odeslané zadání pro `ai-orchestrator` na kartu — momentka v čase dispatche | Živě generované, ale **odvozená a může zaostávat za živým Trellem** (viz sekce 0a) | Nízká/kontextová — nikdy nepoužívat místo živého Trella |
| `runtime/scheduled-start.log`, `runtime/scheduler/scheduler-*.log` | Skutečný provozní log naplánovaného scheduleru (2026-08-26 až 2026-08-28) | Živý | Vysoká — přímý důkaz chování za běhu, včetně jednoho zachyceného self-update restartu s obnovou checkpointu (17:13–17:14, 27. 8., viz sekce 4) |
| `NEXT_DOD.md`, `AUTOMATION_DOD.md`, `HANDOFF_FIX_DOD.md` (tracked, root) | Historické zadávací DoD z počáteční fáze projektu (22.–23. 8.) | Zastaralé — checklisty beze zaškrtnutí, ale popsaná funkčnost už v kódu existuje | Historická, ne provozní |
| `LIVE_HANDOFF_DOD.md` (tracked, root) | DoD pro opravu live handoffu (22. 8.) | Téměř kompletní ([x] u 11/12 bodů), poslední bod je procesní („spustit celou sadu testů", přenechává se orchestrátorovi) | Historická |
| `specs/station-agent.md`, `specs/p5-station-agent.md`, `specs/p5-station-agent.json` (tracked) | Staré, ručně/jednorázově uložené vzory zadání pro Station Agent | Zastaralé — nahrazeny novějším `runtime/specs/p5-station-agent.md` | Nízká, jen jako historický vzor formátu |
| `D:\orchestrator\ai-orchestrator\PROJECT_HANDOVER_2026-08-25.md` | Ruční předávací checkpoint napříč AI Project Manager / ai-orchestrator / Station Agent | 25. 8. 2026, historický, ale potvrzuje stejný obraz jako živé Trello (P1 audity chystané po obnově limitů) | Historický kontext, potvrzující |
| `D:\orchestrator\ai-orchestrator\CODEX_DOD.md` | Samostatný DoD pro Codex CLI adaptér (17 bodů), checklist beze zaškrtnutí | **Stale** — checklist se od vzniku nezaškrtává, ale kód (`orchestrator/agents/codex.py`) detekuje neúplný CLI výstup jako řízenou chybu a `tests/test_codex_agent.py` má regresní testy pro starý i současný JSONL schema výstup; nezávislý plný běh 2026-08-28: 238 passed, 2 skipped | Kód a testy jsou autoritativnější než tento nezaškrtnutý checklist — viz sekce 4.3a |
| `D:\orchestrator\station-agent\*.md` (`PROJECT_NOTES.md`, `NEXT_DOD.md`, `dod-station-agent-v1.md`) | Provozní poznámky a DoD Station Agentu | `NEXT_DOD.md` (iterační opravy) téměř celé `[x]`; `dod-station-agent-v1.md` (celé v1 GUI/funkční rozsah) téměř celé `[ ]` | Mimo přímý rozsah této karty (týká se P0 Station Agent karet, ne P5 karet auditovaných zde) — ponecháno jako kontext |
| `.tmp/pm_control.py` | Ručně psaný operátorský nástroj pro jednorázovou opravu/čtení produkčního Trella | Živý, funkční — použit jako vzor pro živý snapshot v této iteraci | Provozní hodnota — není generovaný odpad |
| `.codex/hooks.json` | Guard, který v tomto sandboxu blokuje spouštění testů agentem (ne obecně `python`) | Aktuální, aktivně použitá | Provozní konfigurace, ne dokument k revizi |
| `pyproject.toml` | Balíček + `console_scripts` (`ai-project-manager`, `ai-project-manager-watchdog`) | Aktuální, odpovídá kódu | Autoritativní |

---

## 1. Historické nápady s původním zdrojem

Totéž jako v předchozí verzi (odvozeno z `runtime/specs/*.md`, doplněné o root DoD soubory) — tato
tabulka je jen inventářem *nápadů*, ne aktuálního stavu karet, a rozpor popsaný v sekci 0a se jí
netýká.

| # | Nápad / karta | Zdroj | Repo |
|---|---|---|---|
| 1 | P0 — Trello jako jediný zdroj pravdy pro všechny AI projekty | `runtime/specs/p0-*.md` | AI Project Manager |
| 2 | P1 — Audit a stabilizace ai-orchestrator | `runtime/specs/p1-audit-a-stabilizace-ai-orchestrator.md`; živě ověřeno Hotovo (sekce 4) | ai-orchestrator |
| 3 | P1 — Audit a stabilizace AI Project Manager | `runtime/specs/p1-audit-a-stabilizace-ai-project-manager.md`; živě ověřeno Hotovo (sekce 4) | AI Project Manager |
| 4 | P2 — Lokální AI pomocník (malý LLM na RTX 2060 pro klasifikaci/sumarizaci/routing) | `runtime/specs/p2-lok-ln-ai-pomocn-k-pro-pm-orchestrator.md` | AI Project Manager / orchestrator |
| 5 | P2 — Self-update: bezpečná aktivace nové verze runtime | `runtime/specs/p2-self-update-*.md`; živě Hotovo | AI Project Manager |
| 6 | P3 — Provider routing a quota fallback | `runtime/specs/p3-provider-routing-*.md`; živě `blocked`, `completed_dod_indices=[0]` | AI Project Manager |
| 7 | P3 — Recovery/revisit zablokovaných a vadných karet | `runtime/specs/p3-recovery-revisit-*.md`; živě Hotovo | AI Project Manager |
| 8 | P4 — AI Orchestrator: měření tokenů a nákladů běhů | `runtime/specs/p4-ai-orchestrator-m-en-*.md`; živě `blocked` | ai-orchestrator |
| 9 | P4 — Revize MD/JSON stavových souborů | `runtime/specs/p4-revize-*.md` = **tato karta** | AI Project Manager |
| 10 | P5 — AI Orchestrator (stavová karta) | živě `blocked`, blokace na Hotovo P1 audit — **znovu vyhodnoceno v sekci 4** | ai-orchestrator (stavová karta v AI Project Manager Trellu) |
| 11–24 | (stejné jako v předchozí verzi — viz git historie souboru pro plný seznam) | `runtime/specs/*.md`, root DoD soubory | různé |

---

## 2. Klasifikace (AI Project Manager, ověřeno proti kódu i živému Trellu)

| Nápad (#) | Klasifikace | Důkaz |
|---|---|---|
| 1 (P0 Trello = zdroj pravdy) | **realizováno** | `trello_sync.py`, `config.py`; živě potvrzeno kartou „P5 - Řídicí systém: Trello jako jediný zdroj pravdy" = Hotovo |
| 3 (P1 audit PM) | **realizováno — živě Hotovo** | Živé Trello: `lifecycle_status="done"`, `completed_dod_indices=[0,1,2,3,4,5]` (všech 6 bodů). Poznámka: minulá verze tohoto dokumentu tvrdila „rozpor mezi stavem kódu a stavem karty" na základě `runtime/specs/p1-audit-a-stabilizace-ai-project-manager.md` s `completed_dod_indices: []` — to byl odvozený, zastaralý snímek; živé Trello ukazuje kartu dávno dokončenou i se synchronizovaným checkpointem |
| 4 (lokální LLM) | **zapomenuto / nezahájeno** | V kódu není žádný adaptér, benchmark ani odkaz na lokální model |
| 5 (self-update) | **realizováno** | `self_update.py`, `watchdog.py`; živě Hotovo; přímý provozní důkaz restartu s obnovou checkpointu v `runtime/scheduled-start.log` 27. 8. 17:13–17:14 (viz sekce 4) |
| 6 (provider routing/fallback) | **částečně** | `providers.py`; živě `blocked`, `completed_dod_indices=[0]` — karta sama v Trellu není dokončená |
| 7 (recovery/revisit blocked) | **realizováno** | `ai_project_manager/recovery.py` (netrackováno); živě karta Hotovo, `completed_dod_indices=[0..7]`; log dokládá i vlastní starý bug (nekonečně vnořený `blocked_by` text), který tato oprava odstraňuje — živý snapshot v této iteraci ukazuje čistý, nezanořený `blocked_by` text u obou P5 karet, takže oprava v runtime skutečně funguje |
| 9 (tato karta) | **v Trellu / probíhá** | Právě zpracovávaná karta |
| 13 (bootstrap project_key) | **realizováno** | `daemon.py:_bootstrap_project_keys`; živě Hotovo |
| 14 (izolace Slack v testech) | **realizováno** | `tests/conftest.py`; živě Hotovo, `completed_dod_indices=[0..6]` |
| 18 (stabilní mapování projekt→cesta) | **realizováno** | Živě Hotovo, `completed_dod_indices=[0..17]` (18 bodů) |
| 21–24 (MVP/entrypoint/handoff, root DoD soubory) | **realizováno**, dokumenty zastaralé | Beze změny oproti předchozí verzi |

---

## 3. Duplicity a rozpory — sloučeno / opraveno

- **Duplicitní Station Agent specs**: beze změny oproti předchozí verzi — `specs/station-agent.md`,
  `specs/p5-station-agent.md`, `specs/p5-station-agent.json` zůstávají kandidátem k archivaci ve
  prospěch `runtime/specs/p5-station-agent.md`.
- **4 samostatné root DoD soubory**: beze změny — navazující fáze téhož vývoje, sloučeno v sekci 1.
- **OPRAVENO — „rozpor kód vs. karta u P1 — Audit a stabilizace AI Project Manager" již neplatí.**
  Minulá verze tohoto dokumentu tvrdila, že karta na Trellu „zaostává za skutečným stavem kódu",
  protože odvozený `runtime/specs/p1-audit-a-stabilizace-ai-project-manager.md` měl
  `completed_dod_indices: []`. Živé Trello dotázané v této iteraci ukazuje přesný opak: karta má
  `lifecycle_status="done"` a `completed_dod_indices=[0,1,2,3,4,5]` — checkpoint byl dávno
  zpětně synchronizován, jen se to nepromítlo do staršího odvozeného specu, který minulá iterace
  omylem považovala za nejlepší dostupnou náhradu za živé Trello. Toto byla chyba metody (spoléhání
  na odvozený, potenciálně zastaralý zdroj), ne chyba PM kódu samotného.
- **Recovery reason-nesting bug vs. P3 recovery karta**: beze změny — a živě potvrzeno v této
  iteraci, že oprava funguje (viz sekce 2, bod 7): aktuální `blocked_by` text obou P5 karet je čistý,
  nezanořený, přestože `runtime/scheduled-start.log` z 27. 8. ukazuje, že bug byl v tu chvíli ještě
  aktivní (zanoření rostlo). Někdo/něco (pravděpodobně navazující recovery tick po nasazení opravy)
  text mezitím vyčistil zpět na jednu větu.

---

## 4. ZNOVU OTEVŘENO: Vyhodnocení dvou P5 stavových karet po živém ověření

### 4.1 Živě ověřený stav obou gating P1 auditů (2026-08-28, toto běh)

| Karta | `status` | `lifecycle_status` | `completed_dod_indices` | Klíčový důkaz z popisu karty |
|---|---|---|---|---|
| **P1 - Audit a stabilizace AI Project Manager** | `done` | `done` | `[0,1,2,3,4,5]` (všech 6) | `last_output`: „Vsech 6 bodu Definition of Done je nyni splneno... Testy prosly (potvrzeno orchestratorem)." |
| **P1 - Audit a stabilizace ai-orchestrator** | `done` | `done` | `[0,1,2,3,4,5,6]` (všech 7) | Popis karty: „reálný E2E přes AI Project Manager → ai-orchestrator → provider → Trello ověřen 27. 8. 2026 08:39–08:45", „Slack start/provider/done ověřen" |

Obě karty tedy **jsou Hotovo v živém Trellu**, přesně jak popisuje zadání této iterace. Toto je
ověřeno přímým dotazem na Trello API v rámci tohoto běhu (`RealTrelloClient.get_card` na oba card ID),
ne odvozeno z `runtime/specs/*`.

### 4.2 Stav obou P5 stavových karet — `blocked_by` je zastaralý

| Karta | `status` (živě) | `blocked_by` (živě) |
|---|---|---|
| **P5 — AI Orchestrator** | `blocked` | „Stavová karta; aktivní práci řídí P1 - Audit a stabilizace ai-orchestrator" |
| **P5 — AI Project Manager** | `blocked` | „Stavová karta; aktivní práci řídí P1 - Audit a stabilizace AI Project Manager" |

Obě karty stále odkazují na P1 audit jako na „aktivní práci, která je řídí" — ale ten P1 audit je,
podle 4.1, dávno Hotovo. **Blokující podmínka je tedy zastaralá u obou karet.** Toto přesně
odpovídá premise zadání této iterace a ruší dřívější závěr („obě karty jsou v Čeká na AI právem"),
který vycházel z odvozeného specu ukazujícího P1 PM jako nedokončený.

### 4.3 Položka po položce: co už dokazuje Hotovo/P1 a dnešní live běhy

**P5 — AI Orchestrator** (10 položek DoD v popisu karty):

| # | Položka | Zaškrtnuto v kartě | Vyhodnocení |
|---|---|---|---|
| 0 | run-id podpora pro autonomní běhy (commit b545d94) | ano | Prokázáno P1 auditem ai-orchestrator |
| 1 | Fallback řetězec claude-code → antigravity → codex | ano | Prokázáno P1 auditem |
| 2 | Claude limit → Antigravity | ano | Prokázáno P1 auditem |
| 3 | Antigravity limit → Codex | ano | Prokázáno P1 auditem |
| 4 | Codex fallback: zlepšit zpracování neúplného CLI výstupu | **ne** (checklist v kartě nezaškrtnut) | **OPRAVENO v této iteraci — prokázáno Hotovo kódem a testy, `CODEX_DOD.md` je stale checklist.** Viz sekci 4.3a: `orchestrator/agents/codex.py` detekuje neúplný CLI výstup jako řízenou chybu, `tests/test_codex_agent.py` má regresní testy pro neúplný starý i současný JSONL schema výstup, prompt byl převeden na stdin (Windows command-line limit už dlouhé iterace nezastaví), a nezávislý plný běh 2026-08-28 ukázal 238 passed, 2 skipped. Předchozí verze tohoto dokumentu tuto položku označovala jako „skutečně otevřenou" jen na základě nezaškrtnutého `CODEX_DOD.md` — to byla chyba metody (spoléhání na historický checklist místo na aktuální kód a testy). |
| 5 | Autonomní iterace a checkpointy | ano | Prokázáno P1 auditem (checkpoint/resume ověřeny) |
| 6 | Provider registry a práce s limity | ano | Prokázáno P1 auditem |
| 7 | Napojení na Trello přes AI Project Manager | ano | Prokázáno reálným E2E 27. 8. |
| 8 | Stabilní předávání run-id | ano | Prokázáno P1 auditem |
| 9 | „Další rozvoj podle reálných potřeb projektů" | ne | **Neurčitá položka — vyloučena z DoD dle zadání této iterace.** Není to konkrétní, ověřitelný požadavek, jen otevřený placeholder pro budoucí práci; nesmí se počítat jako důvod, proč karta zůstává blokovaná. |

Souhrn (opraveno v této iteraci): **9/10 konkrétních položek prokázáno Hotovo** (8 P1 auditem + živým
E2E, + položka #4 nyní prokázána kódem/testy/CODEX_DOD.md klasifikovaným jako stale); jediná zbývající
položka (#9) je neurčitý placeholder vyloučený z DoD. **Žádná konkrétní, ověřitelná položka karty P5 —
AI Orchestrator už není otevřená.**

### 4.3a Detail opravy: CODEX_DOD.md je stale, ne důkaz otevřené práce

`D:\orchestrator\ai-orchestrator\CODEX_DOD.md` je 17bodový checklist, historicky beze zaškrtnutí —
ale nezaškrtnutý checklist není totéž co neprovedená práce, pokud kód a testy od jeho vzniku pokročily
a checklist se prostě nikdy zpětně neaktualizoval. Konkrétní důkazy z kódu a testů (autoritativnější
zdroj než statický MD soubor — viz sekce 0a, bod 3):
- `D:\orchestrator\ai-orchestrator\orchestrator\agents\codex.py` detekuje neúplný CLI výstup (chybějící
  `task_complete`/nevalidní JSONL) jako řízenou chybu s hláškou obsahující „neúplný výstup", ne jako
  pád procesu nebo tiché selhání.
- `D:\orchestrator\ai-orchestrator\tests\test_codex_agent.py` obsahuje regresní testy pro obě schémata:
  `test_run_incomplete_output_without_task_complete_is_an_error` (starší, vnořené `msg` schéma) a
  `test_run_incomplete_current_schema_output_is_an_error` (současné 0.149.x thread/item/turn schéma) —
  obě očekávají řízenou chybu obsahující „neúplný výstup".
- Prompt je nyní posílán na stdin (`codex exec ... -`), ne jako argument příkazové řádky — Windows
  command-line délkový limit tedy už dlouhé autonomní iterace nezastaví.
- Nezávislý plný běh testovací sady 2026-08-28: **238 passed, 2 skipped** (ai-orchestrator).

**Závěr:** položka P5 AI Orchestrator #4 („zlepšit zpracování neúplného CLI výstupu") je splněná.
`CODEX_DOD.md` se klasifikuje jako **stale checklist** — historický zdroj z doby před touto opravou,
ne aktuální stav zbývající práce. Toto nemění nic na položce #9 (vágní „další rozvoj"), která zůstává
vyloučena z DoD jako neurčitá.

**P5 — AI Project Manager** (17 položek DoD v popisu karty):

| # | Položka | Zaškrtnuto v kartě | Vyhodnocení |
|---|---|---|---|
| 0–6 | (Trello řízení, scheduler --once, sync, provider state, handoff, testy 119/119, Slack izolace) | ano (vše) | Prokázáno P1 auditem / dílčími Hotovo kartami |
| 7 | „Čeká na obnovení AI limitů" | **ne** | **Nyní bezpředmětné.** Tato iterace sama běží autonomně (iterace 1/10 aktuálního běhu) — AI limity jsou zjevně obnovené, jinak by tento běh neprobíhal. |
| 8 | „Provést audit a stabilizační běh" | **ne** | **Prokázáno Hotovo.** Toto je doslova karta „P1 - Audit a stabilizace AI Project Manager", která je dle 4.1 živě Hotovo. |
| 9 | „Provést plný autonomní E2E test se skutečným providerem" | **ne** | **Prokázáno Hotovo.** P1 audit ai-orchestrator dokládá reálný E2E PM → orchestrator → provider → Trello → Slack, ověřený 27. 8. 2026 08:39–08:45, se skutečným providerem (ne mock). |
| 10–13 | (Trello řízení, scheduler/sync, provider state, run-id handoff) | ano (vše) | Prokázáno, duplicitní s body 0–4 |
| 14 | „Ověřený dlouhodobý unattended běh" | **ne** | **OPRAVENO v této iteraci — NENÍ live prokázáno jako persistentní.** Předchozí verze tohoto dokumentu považovala vícedenní logy (`runtime/scheduler/scheduler-{20260826,20260827,20260828}.log`, `runtime/scheduled-start.log`) samy o sobě za dostatečný důkaz „dlouhodobého" běhu. Nezávislé ověření 2026-08-28 ukázalo opak: `schtasks /Query /TN "AI Project Manager Scheduler"` úlohu na tomto stroji nenajde a PID 21500, který Slack oznámil jako „watchdog online", už neběží. Historické logy dokazují, že scheduler *v minulosti* běžel a přežil restart (viz bod 15), ale nejsou důkazem, že běží *nepřetržitě teď* jako nainstalovaná persistentní Windows úloha. **Toto je konkrétní, otevřený provozní bod — patří do P4 („Viditelnost scheduleru a akční lidský zásah"), ne do této obecné P5 stavové karty** (viz sekci 4.3a a 4.4). |
| 15 | „Ověřený resume po přerušení" | **ne** | **Prokázáno přímým log důkazem, ne odvozeninou.** `runtime/scheduled-start.log:15-30` zachycuje živý self-update restart 27. 8. 17:13:52–17:14:09: proces detekuje změnu kódu, ověří bezpečnost (testy + git checkpoint), požádá watchdog o restart, watchdog restartuje, provede post-restart smoke test (`--once`), ten projde, a provoz normálně pokračuje se zachovaným `run_id`/checkpoint stavem. Toto je přesně „přerušení + resume" v provozním smyslu. |
| 16 | „Audit uzavřen" | **ne** | **Prokázáno Hotovo.** Karta „P1 - Audit a stabilizace AI Project Manager" má živě `lifecycle_status="done"`. |

Souhrn (opraveno v této iteraci): 5 z 6 dosud nezaškrtnutých položek (#7, #8, #9, #15, #16) mají
přímý živý důkaz Hotovo — P1 audit, E2E s reálným providerem i resume po self-update restartu jsou
prokázané. **Položka #14 („ověřený dlouhodobý unattended běh") ale zůstává skutečně otevřená**: live
Windows scheduler task nebyl nezávisle nalezen (`schtasks` úlohu nenajde) a poslední známý watchdog
PID už neběží — vícedenní logy dokazují minulý provoz, ne aktuální persistentní instalaci. Na rozdíl
od minulé verze tohoto dokumentu se tento jediný konkrétní bod **nemá řešit jako obecná P5 stavová
položka**, ale má se přenést do P4 karty pro viditelnost scheduleru (viz sekci 4.4).

### 4.4 OPRAVENÝ ZÁVĚR (třetí iterace): archivovat obě obecné P5 karty, ale až po přenesení scheduler gapu do P4 — k rozhodnutí uživatele, nic neprovedeno

Toto je opravená verze doporučení z předchozí iterace. Obě dřívější doporučení (P5 AI Project Manager
→ rovnou archivovat; P5 AI Orchestrator → převést kvůli Codexu) se ukázala jako nepřesná ve světle
nezávislého ověření:

- **P5 — AI Orchestrator**: dřívější důvod k „převedení místo archivace" (položka #4, Codex neúplný
  výstup) je dle sekce 4.3a **vyřešen kódem a testy** — `CODEX_DOD.md` je stale checklist, ne důkaz
  zbývající práce. Jediná zbývající položka karty (#9) je neurčitý placeholder vyloučený z DoD. Tato
  karta už nemá žádný konkrétní důvod zůstat mimo Hotovo.
- **P5 — AI Project Manager**: dřívější závěr „rovnou archivovat" přehlížel, že položka #14
  (dlouhodobý unattended běh) není live prokázaná — persistentní Windows scheduler task nebyl
  nezávisle nalezen (viz sekci 4.3). Tato karta tedy **ještě obsahuje jeden skutečně otevřený bod**,
  ale ten bod je ryze provozní (nainstalovat/ověřit scheduler jako trvalou Windows úlohu), ne
  vývojový, a nepatří tematicky do obecné P5 stavové karty — patří do **P4 — Viditelnost scheduleru a
  akční lidský zásah**.

**Doporučený postup (v tomto pořadí, žádný krok zatím neproveden):**
1. Přenést jediný konkrétní zbývající gap — „ověřit/nainstalovat persistentní Windows scheduler task,
   protože `schtasks` úlohu aktuálně nenajde a poslední známý watchdog PID 21500 už neběží" — jako
   novou položku (nebo aktivaci existující) do **P4 — Viditelnost scheduleru a akční lidský zásah**.
2. Teprve poté **archivovat obě obecné P5 stavové karty** (P5 — AI Orchestrator i P5 — AI Project
   Manager) — jejich gating P1 audity jsou Hotovo a po kroku 1 už žádná z nich nenese nepřenesenou
   otevřenou položku.
3. `CODEX_DOD.md` ponechat na disku jako historický záznam (nemazat), ale nepoužívat ho dál jako
   zdroj pravdy o zbývající Codex práci — viz sekce 0a, bod 3 a sekci 4.3a.

**Nic z výše uvedeného nebylo v rámci této iterace na Trellu provedeno** — ani archivace, ani
přesun, ani založení/aktivace karty v P4. Toto je pouze doporučení čekající na explicitní rozhodnutí
uživatele. Živé Trello (sekce 0a) zůstává jedinou autoritou o tom, kdy a jak se karty skutečně
posunou — tento dokument pouze navrhuje.

---

## 5. Stale / generated / authoritative dokumenty a kandidáti k archivaci

| Dokument | Typ | Doporučení |
|---|---|---|
| `NEXT_DOD.md`, `AUTOMATION_DOD.md`, `HANDOFF_FIX_DOD.md` | stale (hotovo, nedohledáno zpět) | Kandidát k archivaci (např. do `docs/history/`) |
| `LIVE_HANDOFF_DOD.md` | téměř hotové (11/12) | Kandidát k archivaci po doplnění posledního bodu orchestrátorem |
| `specs/station-agent.md`, `specs/p5-station-agent.md`, `specs/p5-station-agent.json` | stale, nahrazeno | Kandidát k archivaci — `runtime/specs/p5-station-agent.md` je aktuálnější zdroj |
| `runtime/specs/*.md` | generated, ale kontextově užitečná | **Ponechat**, ale nikdy nepoužívat místo živého Trella (viz sekce 0a — to byla chyba minulé verze tohoto dokumentu) |
| `runtime/scheduled-start.log`, `runtime/scheduler/scheduler-*.log` | generated, provozní důkaz | **Ponechat** — přímý důkaz chování za běhu (použito v sekci 4.3) |
| `README.md` | authoritative | Udržovat dál jako hlavní zdroj pravdy o provozu |
| `.tmp/pm_control.py` | authoritative/provozní nástroj | **Ponechat** |
| `D:\orchestrator\ai-orchestrator\CODEX_DOD.md` | stale checklist (kód+testy pokročily dál, checklist se nezaškrtl) | **Ponechat jako historický záznam**, ale neklasifikovat jako zdroj pravdy o zbývající práci — viz sekci 4.3a |
| **P5 — AI Project Manager** (Trello karta) | stavová, gating P1 Hotovo, 5/6 položek doloženo, 1 (#14 scheduler) reálně otevřená | **OPRAVENO: kandidát k archivaci až PO přenesení scheduler gapu (#14) do P4 karty** — viz sekci 4.4, k rozhodnutí uživatele |
| **P5 — AI Orchestrator** (Trello karta) | stavová, gating P1 Hotovo, jediná zbývající položka (#9) je neurčitý placeholder | **OPRAVENO: kandidát k rovnou archivaci** (položka #4 vyřešena kódem/testy — viz sekci 4.3a; převod na pracovní kartu už není potřeba) — viz sekci 4.4, k rozhodnutí uživatele |

Toto je pouze doporučení k rozhodnutí uživatele — žádný tracked dokument ani Trello karta nebyly
v rámci této iterace přesunuty, archivovány ani smazány.

---

## 6. Provedené smazání prokazatelně dočasných/generovaných artefaktů

Beze změny oproti předchozí verzi (viz git historie tohoto souboru) — žádný tracked soubor nebyl
smazán, jen negitované jednorázové patch skripty a zálohy z ladicí session 23. 8. 2026.

---

## 7. Návrh budoucí roadmapy — pouze k rozhodnutí uživatele

### 7.1 Cílový provozní model: Personal Trello Inbox → PM → orchestrátor → agenti

Toto je základní pravidlo budoucího chování systému, nalezené při ruční archeologii plánovacích
dokumentů a potvrzené uživatelem 28. 8. 2026:

- **Personal Trello Inbox je read-only vstupní místo**, odkud PM smí číst nové požadavky, nápady,
  opravy a doplnění. Do této doručené pošty PM, orchestrátor ani agent nesmí zapisovat, kartu
  upravovat, přesouvat, mazat ani archivovat.
- PM smí obsah bezpečně klasifikovat a použít neměnnou identitu zdrojové položky jako referenci.
  Po validaci připraví samostatný kanonický záznam na hlavním Trello boardu; zdrojová položka
  v osobní doručené poště zůstává beze změny a její další správu provádí pouze uživatel.
- Nová realizovatelná práce přechází do `Připraveno`; aktivní běh do `Pracuje se`; skutečné čekání
  do `Čeká na AI`; ověřování do `Testování`; kompletně ověřená práce do `Hotovo`.
- PM nesmí vytvořit duplicitní pracovní kartu jen proto, že chybně přečetl ID, URL, název nebo
  workspace původní karty. Zdrojové ID a kanonická URL osobní položky se uloží jako reference;
  identita cílové pracovní karty a její URL jsou součástí verzovaného kontraktu a každý zápis se
  ověřuje readbackem.
- Opakované zpracování stejné Inbox položky musí být idempotentní: ID příjmu se uloží do cílového
  kontraktu a nesmí podruhé přidat stejný feedback nebo založit druhý úkol.
- Nejasná položka existujícího projektu se nesmí odhadnout ani smazat. PM ji ponechá v Inboxu a
  viditelně uvede, jaké rozhodnutí potřebuje od člověka. Skutečně nový nápad bez projektové identity
  se smí připravit autonomně jako izolovaný projekt pod explicitním `AI_PM_PROJECTS_ROOT`; identita
  obsahuje neměnné zdrojové ID, aby se dvě podobné ideje neslily do jednoho checkoutu.
- **Trello je jediným zdrojem pravdy** pro prioritu, lifecycle, DoD, blokaci a dokončení.
- Pevná hierarchie řízení je **AI Project Manager → ai-orchestrator → agenti**. PM vybírá a řídí
  workflow; orchestrátor rozděluje práci a jako jediný provádí audit a vydává accepted/rejected
  verdikt; agenti implementují a dodávají důkazy.

Personal Trello Inbox a boardový seznam `INBOX / Nápady` nejsou zaměnitelné. Současný kód pracuje
primárně s boardovým seznamem; plnohodnotné napojení Personal Inboxu, jeho oprávnění a bezpečný
přesun mezi workspaces zůstávají samostatnou neimplementovanou schopností. Do jejího schválení se
nemá Personal Inbox automaticky měnit.

### 7.2 Zapomenuté nebo nedotažené schopnosti nalezené v historii

1. **Personal Trello Inbox intake a automatická příprava úkolu** — read-only klasifikace nestačí;
   výstup musí obsahovat správný projekt/repozitář, prioritu, `main_task`,
   `orchestrator_ready_task`, plný DoD, testy, live důkaz, bezpečnostní omezení, referenci na
   zdrojovou položku a cílové workflow. Osobní položka se při tom nemění.
2. **Review agent / cross-provider review** — samostatná review fáze; druhý provider jako nezávislý
   reviewer, nikoli pouze fallback. Auditní autoritou zůstává ai-orchestrator.
3. **Project locking** — zabránit souběžné práci dvou providerů na stejné kartě a bezpečně uvolnit
   zámek po chybě či timeoutu. Současná implementace existuje, ale má zůstat krytá regresními a
   live testy.
4. **Skutečně používané GitHub/Google Drive reference** — pole v modelu nestačí; PM musí reference
   bezpečně přiřazovat a orchestrátor je používat bez vytvoření druhého zdroje pravdy.
5. **Řízené recovery podle strojového block code** — `provider_limit`, `protocol_contract`,
   `missing_live_evidence`, `dependency` a `human_hold` mají odlišná pravidla requeue; `human_hold`
   se nikdy automaticky neodblokuje.
6. **Allowlistovaný LIVE-EVIDENCE kontrakt** — bezpečný read-only ověřovací typ a očekávaný výsledek;
   žádné odvozování spustitelného shellu z volného textu karty.
7. **Verzovaný Trello Card Contract a migrační vrstva** — schema verze, precedence viditelného textu
   a PM-DATA, zachování neznámých polí, fail-safe pro neplatnou/novější verzi a neměnná identita.
8. **Historický `inbox/`/`outbox/` most** — ověřit, zda má proti dnešnímu Trello workflow ještě
   samostatnou hodnotu; SQLite zůstává pouze interní fronta, nikdy zdroj pravdy o kartách.

### 7.3 Již dříve evidované provozní kroky

1. **Přenést scheduler gap (P5 AI Project Manager #14) do P4 — Viditelnost scheduleru a akční lidský
   zásah**, poté **archivovat obě obecné P5 stavové karty** — toto je hlavní opravený výstup této
   znovu otevřené (třetí) iterace, viz sekci 4.4. Pořadí kroků záměrně: nejdřív přenést konkrétní
   gap, teprve pak archivovat, aby se otevřený bod neztratil.
2. **Nainstalovat/ověřit persistentní Windows scheduler task** — `schtasks /Query /TN "AI Project
   Manager Scheduler"` úlohu aktuálně nenajde a poslední známý watchdog PID 21500 už neběží; toto je
   jediný skutečně otevřený bod z bývalé P5 AI Project Manager karty (viz sekci 4.3, položka #14).
3. **Commit rozpracovaného `recovery.py` + navazujících testů** — funkční a otestované (viz sekce 2,
   bod 7), živě potvrzeno, že oprava v runtime skutečně funguje, ale dosud jen v pracovním stromu.
4. **Rozhodnout o `P2 — Lokální AI pomocník pro PM/orchestrator`** — nezahájený, čistě exploratorní
   nápad.
5. **Dokončit `P3 — Provider routing a quota fallback`** — živě `blocked`, jen `completed_dod_indices=[0]`.
6. **Archivace stale dokumentů** dle sekce 5.
7. **Úklid `.pytest-basetemp-*`/`.pytest_cache`/`.tmp` scratch adresářů** — mechanický úkol, blokovaný
   guardem v tomto sandboxu (viz sekce 0 o `.codex/hooks.json`).

### 7.4 Podněty z tohoto vlákna — nově zařazené do roadmapy

Tyto body jsou budoucí práce, nikoli důvod obcházet současný workflow. Každý
se má před realizací objevit jako jediná kanonická Trello karta nebo jako
jednoznačný podúkol existující karty; nevytvářet paralelní duplicity.

1. **Adaptivní iterace a projektová paměť** — po každé iteraci uložit do
   Trella výsledek, příčinu neúspěchu, důkaz, otevřenou zpětnou vazbu a další
   strategii. Další iterace i další karta stejného projektu musí tento kontext
   načíst a zvolit změněný postup; nesmí opakovat stejný neúspěšný pokus.
2. **Výběr konkrétního modelu v rámci providera** — PM/orchestrátor má podle
   typu úkolu, fáze workflow, požadované kvality a rozpočtu zvolit konkrétní
   model, ne pouze providera. Běžné implementační kroky mají používat
   úsporný model; složitá oprava, testování a audit mohou vyžádat kvalitnější
   model. Skutečně použitý provider/model, důvod volby, fallback a odhad či
   skutečná spotřeba se musí propsat do Trella a observability; při nejistotě
   nesmí úspora obejít testy ani nezávislý audit.
   **Živý nález 28. 8. 2026:** v běhu Station Agentu měly implementační PM
   prompty dohromady 20 420 znaků a závěrečný auditní prompt 2 469 znaků,
   ale Codex vykázal 4 136 540 vstupních tokenů; samotná iterace 2 po
   fallbacku vykázala 2 826 998.
   Je proto nutné oddělit skutečný prompt od interního tool/context replay,
   zachytit jeho zdroj a zavést rozpočtový guard pro jednu iteraci i celý běh.
   Dokud nebude vysvětleno, co tato čísla obsahují, nepovažovat je za běžnou
   spotřebu odpovídající velikosti PM promptu.
3. **Automatický scheduler preflight pro LIVE-EVIDENCE** — ve scheduleru
   provádět allowlistované, read-only a nízkonákladové ověření reálného CLI
   kontraktu (včetně Codexu), výsledek zapsat do Trella a nepouštět jej v každé
   běžné iteraci. Placené volání musí být explicitně konfigurovatelné a nikdy
   nesmí obejít auditní autoritu ai-orchestrátoru.
4. **Jednotný a vynucený workflow** — pravidla z `WORKFLOW.md` udržovat jako
   jedinou provozní specifikaci a doplnit regresní/live testy všech přechodů:
   `Testování → Čeká na AI → Pracuje se → Připraveno` v obráceném pořadí
   zpracování, přijetí auditem a teprve potom `Hotovo`.
5. **Logické dělení velkých úkolů** — před dispatchí rozpoznat příliš široký
   úkol a rozdělit jej na malé navazující části s vlastním DoD, testem,
   prioritou a checkpointem. Části musí zůstat součástí jednoho projektu a
   nesmí umožnit přeskočení testování nebo auditu.
6. **Prioritizace při Inbox intake** — AI má při přijetí do `Připraveno`
   přidělit jedinečné, odůvodněné priority `P5–P0`; opravné a PM úkoly mají
   tehdy přednost před běžnými úkoly. Po zařazení je priorita neměnný údaj
   Card Contractu a PM ji nesmí přečíslovat podle aktuálního listu, fáze,
   providera ani textu karty.
7. **Dokončovací úklid** — po úspěšném auditu a Git checkpointu bezpečně
   identifikovat a odstranit pouze prokazatelně nepotřebné dočasné soubory a
   složky, výsledek úklidu zapsat do DoD a zachovat možnost obnovy. Nikdy
   nemaž tracked nebo nejasně vlastněné soubory automaticky.
8. **Úplná provozní observabilita** — sjednotit Slack/Trello/outbox údaje o
   startu, provideru, přepnutí, iteraci, testech, auditu, spotřebě tokenů,
   nákladech, čekání a důvodu zastavení; notifikace nesmí být pouze lokální
   log.
9. **Hermes/lokální nebo bezplatný provider PoC** — pokračovat v již
   obnovené kartě `P0 — PoC Hermes Agent jako základ lokální AI`; izolovaný
   PoC vedle PM/orchestrátoru, Ollama/LM Studio/OpenAI-compatible endpoint,
   měření kvality/rychlosti/VRAM/nákladů, fallback a GO/NO-GO. Produkční
   migrace až po live E2E důkazu.
10. **Optimalizace kódu a využití AI v AI Project Manager a jeho komplexní
    kontrola a úklid** — provést systematickou kontrolu struktury a kvality
    kódu, odstranit prokazatelně mrtvé nebo duplicitní části, zjednodušit
    zbytečnou složitost, zkontrolovat testy, logování, stavovou správu,
    bezpečnost, výkon a spotřebu tokenů. Součástí bude i kontrola promptů,
    kontextu a využití providerů tak, aby AI nepálila prostředky bez přínosu.
    Změny musí zachovat Card Contract, audit-only pravidla a neměnnost priorit
    po zařazení do `Připraveno`; před úklidem i po něm bude proveden úplný
    readback a ověření provozní stability.

### 7.5 Ověřená blokace Gemini a přechod na Antigravity (1. 9. 2026)

Gemini se dále nezkouší. Izolovaný forced tick s `--agent gemini` a bez AO
failoveru byl skutečně proveden na kartě `P5.01 — oprava project manager
[Inbox 6a966a72] — požadavek 1`. Gemini CLI vrátil `IneligibleTierError` a
`UNSUPPORTED_CLIENT` s vysvětlením, že tento klient již není podporován pro
Gemini Code Assist pro jednotlivce a že je nutné migrovat na sadu produktů
Antigravity (`https://antigravity.google`). Nešlo o běžné vyčerpání kvóty;
AO proto nepřepnul na jiného providera. Samostatné REST ověření stejného
projektu navíc skončilo `PERMISSION_DENIED` / „Your project has been denied
access“.

**Rozhodnutí:** Gemini je pro PM dočasně veden jako nedostupný (`ERROR` s
retry backoffem); další Gemini tick se nespouští. Další diagnostika a live E2E
ověření pokračuje izolovaným forced tickem pouze s Antigravity. Za fungujícího
providera se Antigravity označí až po skutečném PM → AO handoffu, úspěšném
výsledku `agy`, zápisu outboxu/checkpointu a konzistentním readbacku Trella a
Slacku. Tento záznam je provozní důkaz a nepředstavuje auditní verdikt
`Testování`.

### 7.6 Obnova priorit po historickém chybném re-rankingu (1. 9. 2026)

### 8.6 Aktuální kontrakt: model vybírá provider, Inbox Hermes nikdy

- PM předává pouze providera, typ úkolu a bezpečný prompt; do planneru,
  implementačního ani auditního argv se nepřidává `--model` z PM katalogu.
- Provider si zvolí konkrétní model podle typu úkolu. AO musí skutečně použitý
  model vrátit v outboxu; PM jej pouze zapíše do Trello/Slack evidence a při
  absenci výstupu uvede neznámý provider default.
- `build_inbox_planner_fn` používá explicitní allowlist `antigravity`, `claude`,
  `codex`. Hermes v něm není a nesmí se do něj dostat změnou obecného
  provider-order. Inbox plánování tak nikdy nepoužije Hermes.
- Hermes mimo Inbox zůstává vždy Nous-only: `provider=nous` a
  `model=upstage/solar-pro4:free`. Každý free provider musí fail-closed odmítnout
  placený model nebo tichý placený fallback; platí to i pro Inbox planning.

Historický maintenance re-ranking změnil priority i po Inbox intake a
způsobil kolizi `P5.06 — Bazar ... požadavek 1` s opravnými/PM kartami. Stav
byl jednorázově ručně opraven přímo v živém Trellu: opravné a PM karty mají
jedinečné `P5.01` až `P5.05`; Bazar požadavky 1 až 17 mají jedinečné `P2.01`
až `P2.17`, tedy požadavek 5 je `P2.05`. Po tomto repair zásahu už PM
priority nepřiděluje ani nemění; pouze respektuje hodnotu získanou při intake.

---

## 8. Mapování: rozhodování PM/PO o provideru a modelu LLM podle typu úkolu (oprava PM [Inbox 6a96e12f])

Historická mapa níže zachycuje předchozí návrh. Aktuální kontrakt je v části
8.6: PM vybírá providera a typ úkolu, konkrétní model volí provider, Inbox
intake Hermes nikdy nepoužije a free provider nesmí tiše přejít na placený
model.

### 8.1 Tři typy úkolu, které PM providerovi/LLM zadává

| Typ úkolu | Kdy nastává | Funkce, která provider/model vybírá |
|---|---|---|
| **Inbox planning** (čtení a klasifikace nové Inbox položky) | Read-only, karta ještě není `Připraveno` | `orchestrator_runner.build_inbox_planner_fn` (vlastní hardcoded allowlist, viz 8.2) |
| **Implementace** (`Pracuje se`) | Karta má neprovedené `implementation` DoD položky | `scheduler.pick_next_project` + `orchestrator_runner.build_run_fn` |
| **Audit** (`Testování`) | Karta čeká na accepted/rejected verdikt | `scheduler.pick_next_audit_project` + `orchestrator_runner.build_audit_run_fn` |

### 8.2 Výběr providera podle typu úkolu

- **Inbox planning** (`ai_project_manager/orchestrator_runner.py:215-227`): `build_inbox_planner_fn`
  definuje vlastní samostatný allowlist `allowed = ("antigravity", "claude", "codex")` přímo v
  `orchestrator_runner.py:227` a vybírá první provider z tohoto pevného pořadí, který je
  `provider_registry.is_available(...)`. Hermes je z plánování natvrdo vyloučen tím, že v tomto
  tuple vůbec není, a Gemini je z PM úplně stažen (viz 7.5), takže také chybí.
  Pozn. (zjištění, ne oprava — mimo rozsah této karty): `ai_project_manager/inbox.py:36-44` definuje
  paralelní funkci `inbox_planner_providers` a konstantu `INBOX_PLANNING_FORBIDDEN_PROVIDERS =
  {"hermes", "gemini"}`, které vyjadřují stejnou politiku (žádný Hermes/Gemini v Inbox planningu),
  ale produkční dispatch `build_inbox_planner_fn` je nevolá a nijak na ně neodkazuje — v
  `orchestrator_runner.py` není žádný `from .inbox import`/`import inbox`. Oba symboly z `inbox.py`
  jsou tedy vůči skutečné rozhodovací cestě pro výběr providera mrtvý kód; jediní volající jsou
  `tests/test_inbox.py:9,29`. Efektivní chování (vyloučení Hermes a Gemini) je dnes shodné, protože
  je duplikované přímo v `orchestrator_runner.py:227`, ale je to nezávislá, nikoli sdílená
  implementace.
- **Implementace** (`scheduler.py:98-158`, funkce `pick_next_project`): pro danou kartu se vezme
  `providers_for_project.get(project.name)` (`AI_PM_PROVIDERS_FOR_PROJECT`), jinak výchozí
  `AI_PM_PROVIDERS` pořadí; vybere se první provider v tomto pořadí, který je momentálně
  `is_available` (stav `AVAILABLE`, ne `LIMITED`/`ERROR`).
- **Audit** (`scheduler.py:176-219`, funkce `pick_next_audit_project`): stejné pořadí
  `providers_for_project`/`AI_PM_PROVIDERS` jako u implementace, ale navíc filtruje providery,
  kteří jsou `provider_registry.is_capability_limited(name, audit_capability_key(project))` —
  perzistentní, per-projekt-a-scope značka (`providers.py:223-235`, `mark_capability_limited`),
  odlišná od běžného `LIMITED`/`ERROR` kvótového stavu. Toto je jediné místo, kde volba
  **providera** skutečně závisí na typu úkolu (implementace vs. audit): audit dispatch má
  dodatečný filtr, který implementační dispatch nemá — provider zůstává použitelný pro
  implementaci jiných karet i poté, co byl kvůli konkrétnímu auditnímu scope kategoricky
  odmítnut (např. bezpečnostní hranice Hermes guardu, viz runtime contract).
- **`AI_PM_PROVIDERS=auto`** (výchozí hodnota): PM sám žádného konkrétního providera nepin­uje;
  `--agent auto` deleguje skutečnou volbu na vlastní failover řetězec ai-orchestrátoru
  (`orchestrator_runner.py:104-107`, komentář: „AO's canonical default order remains hermes,
  gemini, antigravity, claude-code, codex for other users" — PM sám Gemini z tohoto řetězce
  odstranil, viz 7.5).
- **Explicitní `--provider-order`** (`use_provider_failover=True`, `_tick_provider_order`,
  `orchestrator_runner.py:110-124`): když PM pošle konkrétní vybraný provider, přiloží i celé
  pořadí pro same-tick failover uvnitř ai-orchestrátoru — vybraný provider první, pak zbytek
  pevného `PM_FAILOVER_PROVIDER_ORDER = (hermes, antigravity, claude-code, codex)`; u audit
  dispatche se z tohoto pořadí navíc odstraní providery capability-limited pro dané auditní
  scope (parametr `project` je předán jen v audit větvi, `orchestrator_runner.py:1198`).

### 8.3 Historický stav výběru modelu LLM (již nepoužívaný)

- `AI_PM_PROVIDER_MODELS` je volitelná JSON mapa provider → seřazený neprázdný seznam modelů.
  Při startu CLI (`cli.py:129-132`) se pro **každý** nakonfigurovaný provider zavolá
  `provider_registry.configure_models(name, config.provider_models.get(name, []))` — i pro
  providery bez záznamu v `AI_PM_PROVIDER_MODELS` (dostanou prázdný seznam). První položka
  seznamu se stává `selected_model` (`providers.py:97-109`); prázdný seznam znamená
  `selected_model = None`.
- Implementační dispatch (`build_run_fn`, `orchestrator_runner.py:899-902`) a auditní dispatch
  (`build_audit_run_fn`, `orchestrator_runner.py:1160-1163`) používají **doslova stejný výraz**:
  `None if provider.casefold() == "hermes" else provider_registry.selected_model(provider)`.
  Do žádné z obou funkcí nevstupuje fáze/typ úkolu jako parametr ovlivňující volbu modelu —
  jediné zohlednění typu úkolu v `--model` je nepřímé, přes to, jaký `provider` byl už vybrán
  podle 8.2. Model vybraný pro implementaci a model vybraný pro audit téže karty je tedy vždy
  identický, pokud se mezitím nezměnil `AI_PM_PROVIDER_MODELS`/stav providera.
  Pozn.: `build_inbox_planner_fn` (typ úkolu „Inbox planning") používá tentýž
  `provider_registry.selected_model(provider)` bez Hermes výjimky (Hermes je z plánování už
  vyloučen na úrovni výběru providera, viz 8.2), takže žádná zvláštní logika navíc.
- **Providery bez možnosti volby modelu:**
  - **Hermes** — jediný provider, kde je nepředání `--model` vynucené v kódu bez ohledu na
    obsah `AI_PM_PROVIDER_MODELS` nebo typ úkolu (`orchestrator_runner.py:895-902`,
    `runner.py:100-104,138-139`). I kdyby operátor omylem nastavil
    `AI_PM_PROVIDER_MODELS["hermes"]`, PM ho nikdy nepoužije. Důvodová zpráva zapisovaná do
    Trella/Slacku je vždy „model je pevně daný Hermes Nous-only kontraktem" — Hermes tedy má
    jeden pevný model (Nous-only kontrakt) a PM u něj o modelu vůbec nerozhoduje.
  - **Kterýkoli jiný nakonfigurovaný provider bez záznamu v `AI_PM_PROVIDER_MODELS`** —
    `selected_model` zůstává `None`, `--model` se vůbec nepřidá do argv a použije se výchozí
    model daného CLI/agenta. PM tento stav hlásí explicitně jako „provider nemá
    nakonfigurovaný model; použit bude jeho výchozí model" (`runner.py:111`) / „provider
    použil svůj výchozí model" (`runner.py:151`) — nikdy neodhaduje jméno modelu z názvu
    providera.
  - Pokud ai-orchestrator ve výsledku vrátí `active_model`/`model`/`usage.total.model`, tento
    potvrzený běhový model nahradí nakonfigurovanou preferenci jen v reportovacím textu
    (`runner.py:115-152`) — na už odeslaný dispatch to zpětně nepůsobí.

### 8.4 Zjištěná mezera (zaznamenáno mapováním, uzavřeno v 8.5)

Volba konkrétního modelu dříve nezohledňovala typ úkolu, fázi workflow ani požadovanou kvalitu —
běžná implementace i audit stejné karty vždy dostaly stejný, staticky nakonfigurovaný první
model daného providera (viz 8.3, popis stavu v době mapování). Odlišení „úsporný model pro
rutinní implementaci, kvalitnější model pro audit/složitou opravu" tehdy nebylo v kódu
implementováno. Toto přesně odpovídalo už dříve evidovanému bodu **7.4, položka 2** („Výběr
konkrétního modelu v rámci providera"). Následující iterace téže karty [Inbox 6a96e12f] mezeru
zavírá — viz **8.5**.

### 8.5 Historický návrh výběru modelu podle typu úkolu (nahrazeno)

- `ProviderRegistry.model_for_task(name, task_type)` (`providers.py`) doplňuje `selected_model`
  o parametr typu úkolu (`TASK_INBOX_PLANNING`, `TASK_IMPLEMENTATION`, `TASK_AUDIT`). Provider
  s 0 nebo 1 nakonfigurovaným modelem se chová naprosto stejně jako dřív pro každý typ úkolu —
  diferenciace nastává jen tehdy, když `AI_PM_PROVIDER_MODELS` skutečně obsahuje víc než jeden
  model pro daného providera, takže mimo tento opt-in případ se chování nemění.
- Konvence seřazeného seznamu: index 0 = nejúspornější/výchozí model (Inbox planning i rutinní
  implementace), poslední index = nejkvalitnější nakonfigurovaný model (audit — nezávislý
  auditní gate, kde záleží víc na správnosti než na propustnosti).
- `build_inbox_planner_fn` a `build_run_fn` (implementace) volají
  `provider_registry.model_for_task(provider, TASK_INBOX_PLANNING/TASK_IMPLEMENTATION)`, tedy
  vždy `models[0]` — beze změny oproti předchozímu `selected_model`. `build_audit_run_fn` volá
  `model_for_task(provider, TASK_AUDIT)`, tedy `models[-1]` — u providera s víc než jedním
  nakonfigurovaným modelem tak audit dostane jiný (kvalitnější) model než implementace téže
  karty.
- `runner.py` (`_provider_selection_reason`/`_actual_provider_selection_reason`, volané z
  `run_once` a `run_once_audit`) reportuje do Trella/Slacku stejný model, jaký skutečně použije
  `orchestrator_runner.py` pro daný typ úkolu, a u auditu s víc než jedním modelem odlišuje důvod
  textem „model … je pro audit nejkvalitnější nakonfigurovaný model providera" místo
  „model … je pro implementaci první preferovaný model providera". Důvod výběru provideru
  zůstává v téže Slack zprávě zachován jako samostatná část.
- Hermes (`PROVIDERS_WITHOUT_MODEL_SELECTION`) zůstává beze změny — `model_for_task` se pro něj
  vůbec nevolá (`supports_model_selection` guard na všech dispatch místech), model je nadále
  pevně daný Nous-only kontraktem bez ohledu na typ úkolu.

Historický maintenance re-ranking změnil priority i po Inbox intake a
způsobil kolizi `P5.06 — Bazar ... požadavek 1` s opravnými/PM kartami. Stav
byl jednorázově ručně opraven přímo v živém Trellu: opravné a PM karty mají
jedinečné `P5.01` až `P5.05`; Bazar požadavky 1 až 17 mají jedinečné `P2.01`
až `P2.17`, tedy požadavek 5 je `P2.05`. Po tomto repair zásahu už PM
priority nepřiděluje ani nemění; pouze respektuje hodnotu získanou při intake.

---

## 9. Analýza routingu: Inbox planning → PM → ai-orchestrator → provider/model
(Inbox požadavek „AI Project Manager / ai-orchestrator — analýza routingu",
2026-09-05; navazuje na mapování v sekci 8, doplňuje ho o aktuální řádkové
odkazy a o místa, která sekce 8 dosud nepojmenovala. Toto je čistě analytická/
dokumentační karta — žádné chování mimo tento zápis nebylo měněno.)

### 9.1 Celá cesta v pořadí volání

1. **Inbox planning** (`orchestrator_runner.build_inbox_planner_fn`,
   `orchestrator_runner.py:363-`) — čte novou Inbox kartu, vybírá providera
   z pevného `INBOX_PLANNER_PROVIDERS = ("antigravity", "claude", "codex")`
   (`orchestrator_runner.py:318`) a zapisuje `selection = {"provider":...,
   "model": _inbox_model_hint(...), ...}` (`orchestrator_runner.py:413-421`).
2. **PM/scheduler** (`scheduler.pick_next_project` /
   `pick_next_audit_project`) vybírá jen **providera** (první dostupný v
   `AI_PM_PROVIDERS`/`AI_PM_PROVIDERS_FOR_PROJECT`) — nikdy model.
3. **`ProjectRecord`** (`models.py:140-203`) nese `provider: Optional[str]`
   jako jediné trvalé, Trello-perzistované pole vztahující se k volbě LLM.
   **Žádné pole `model` v `ProjectRecord` neexistuje** — `to_dict`/`from_dict`
   (`models.py:262-301`) ho tedy ani nemůže serializovat.
4. **Card Contract** (`card_contract.py:203-213`, `KNOWN_FIELDS`) přesně
   odpovídá bodu 3: obsahuje `"provider"`, ale žádné `"model"`. Trello (jediný
   zdroj pravdy dle runtime kontraktu) tedy **nikdy** neperzistuje deklarovaný
   model — jen providera.
5. **Dispatch** (`orchestrator_runner.build_run_fn`/`build_audit_run_fn`,
   `orchestrator_runner.py:1137-`/`1392-`) sestaví `full_command` s `--agent
   {agent_name}` (`orchestrator_runner.py:1239-1254`, `1463-`), ale **bez
   `--model`** — v obou funkcích existuje pouze mrtvá lokální proměnná
   `selected_model = None` (`orchestrator_runner.py:1193`, `1437`), která se
   nikam dál nepoužije (grep na `selected_model` v tomto souboru vrací
   výhradně tyto dva přiřazovací řádky). Ai-orchestrator tedy dostává jen
   providera/agenta a text úkolu; konkrétní model si volí sám.
6. **Návrat z ai-orchestrátoru**: pokud outbox obsahuje `active_model`/
   `model`/`usage.total.model`, PM ho pouze **zapíše do reportovacích
   textů** (Trello `next_step`/Slack), nikdy zpětně neovlivní už odeslaný
   dispatch (`runner.py:103-152`, `slack_notify.py:124-231`).

### 9.2 Přesná místa, kde se deklarovaná volba providera/modelu ztrácí nebo
nahrazuje hodnotou „provider default" / „model nezjištěn"

| # | Místo (soubor:řádek) | Co se děje |
|---|---|---|
| 1 | `orchestrator_runner.py:321-332` (`_inbox_model_hint`) | Před voláním Inbox planneru se sestaví jen **operátorský hint** — `f"provider default (návrh katalogu: {configured}; ...)"` nebo `"provider default (provider rozhodne...)"`. `configured` pochází z `provider_registry.model_for_task(...)`, ale tato hodnota se nikam dál nepředává jako skutečný `--model`; je to čistě informativní text. |
| 2 | `orchestrator_runner.py:1193` a `:1437` | `selected_model = None` je nastaveno v `run_fn`/`audit_run_fn`, ale nikdy dál použito — je to mrtvý zbytek z dřívějšího návrhu (sekce 8.3-8.5), kdy se model ještě vybíral přes `model_for_task`. Jediné aktivní chování dnes je, že se `--model` do `full_command` vůbec nepřidává. |
| 3 | `providers.py:145-168` (`model_for_task`) | Vlastní docstring přiznává: „Production dispatch does not call this helper; it is retained for diagnostics...". Jediný produkční volající je `_inbox_model_hint` (bod 1) — pro hint text, ne pro argv. |
| 4 | `runner.py:103-105` (`_display_model`) | `return model or "provider default (nezjištěn)"` — kdykoli ai-orchestrátor ve výsledku nevrátí potvrzený `active_model`/`model`, human-facing text v Trellu/Slacku dostane doslova řetězec „provider default (nezjištěn)". |
| 5 | `slack_notify.py:190-193` (`provider_route_detail`) | `model_by_provider.get(name, 'nezjištěn')` — pro každého providera v `provider_sequence`, pro kterého žádná `usage.events` položka ani `active_model` neobsahuje jeho model, se ve „model path" zobrazí doslova `nezjištěn`. |
| 6 | `daemon.py:178-193` (`_run_recovery_pass`, návrat po vypršení `retry_after`) | Při obnově karty z čekání na providera se `project.extra_data["provider_selection"]` explicitně přepíše na `selected_model=None, model=None, actual_provider=None, actual_model=None, source="provider_default"` — jakákoli dříve zaznamenaná model/„actual" hodnota se tímto krokem zahodí, protože nový běh z checkpointu je nový výběr, ne pokračování stejného potvrzeného modelu. |
| 7 | `card_contract.py:203-213` (`KNOWN_FIELDS`) + `models.py:140-203` (`ProjectRecord`) | Structural: **Trello karta jako jediný zdroj pravdy nemá pole pro model vůbec** — jen `provider`. I kdyby nějaká vrstva model chvilkově znala (`extra_data["provider_selection"]["model"]`), přežije to jen v neverzovaném `extra_data`, ne jako kanonické pole s vlastní validací/migrací jako `provider`. |
| 8 | `ai_project_manager/inbox.py:34-45` (`INBOX_PLANNING_FORBIDDEN_PROVIDERS`, `inbox_planner_providers`) | Nepoužívaný duplikát politiky „žádný Hermes/Gemini v Inbox planningu" — `orchestrator_runner.py` tuto funkci nikdy nevolá (žádný `from .inbox import` v `orchestrator_runner.py`). Efektivní chování je dnes shodné s `INBOX_PLANNER_PROVIDERS` (bod 1 sekce 9.1), ale je to nezávislá kopie, ne sdílený zdroj — riziko budoucího rozjetí, ne ztráta dnes. Již zaznamenáno v sekci 8.2; potvrzeno stále platným v této iteraci. |

### 9.3 Shrnutí: je to bug, nebo záměr?

Žádné z míst v 9.2 není tichá regrese — kód na každém z nich má komentář nebo
docstring vysvětlující záměr „PM nepředává `--model`, provider si vybere sám"
(viz sekce 8.6, aktuální kontrakt). „provider default (nezjištěn)" a
„nezjištěn" jsou tedy **záměrově** čitelné zástupné texty pro operátora, ne
ztracená data — reálná hodnota modelu nikdy neexistovala k okamžiku dispatch,
protože PM ji cíleně nezjišťuje předem. Jediná položka, která přesahuje čistý
záměr, je bod 2 (mrtvé `selected_model = None` proměnné) a bod 8 (nepoužívaný
duplicitní modul `inbox.py`) — obě jsou neškodný mrtvý kód, ne funkční chyba,
a jejich úklid je mimo rozsah této analytické karty (viz bod 10 v sekci 7.4).

### 9.4 Co zůstává mimo rozsah této karty

Tato karta je čistě analytická — žádný ze zdrojových souborů uvedených výše
nebyl touto iterací upraven. Případný úklid mrtvého kódu (`selected_model`
proměnné, duplicitní `inbox.py` funkce) nebo rozšíření Card Contractu o
kanonické pole pro potvrzený model patří do samostatné, výslovně schválené
karty — ne do této, jejímž jediným DoD je doložit, kde se volba ztrácí.
