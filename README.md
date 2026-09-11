# AI Project Manager v2

Autonomní řídicí vrstva nad Trello boardem a `ai-orchestrator`. Trello je
zdroj pravdy pro projekty i jediný ruční vstup (`Inbox`). Jeden scheduler tick
načte Inbox a projekty, připraví nové Inbox požadavky do `Připraveno` s prioritou
v názvu; při každém ticku nejdříve obnoví `Čeká na AI`, potom zpracuje auditní
`Testování` a teprve po vyprázdnění těchto fází vybere práci v `Pracuje se` nebo
`Připraveno`. Výsledek zapíše zpět do Trella.

V2 workflow má právě jeden implementační slot: karta ponechaná v `Pracuje se`
po providerovém běhu (například při čekání na controller finalizaci) blokuje
přijetí další karty z `Připraveno`. Další karta se zařadí až po vyřešení tohoto
stavu.

PM v2 nikdy nevolá broker ani žádného providera. Při práci pouze spustí
`ai-orchestrator`; hodnota `--agent provider-broker` v předaném příkazu je
instrukce pro AO, nikoli přímé volání z PM. Výběr, `lang`, limity, retry a
samotné volání provideru vlastní AI Orchestrator. Pokud je workflow prázdné a
v `INBOX / Nápady` čeká uživatelská karta, PM před Inbox plannerem jednou
spustí AO brokerový příkaz `refresh-provider-notes`; teprve po úspěšném
refreshi předá kartu do plánování. Při chybě refreshu karta zůstane v Inboxu.

## Požadavky a instalace

- Python 3.10+
- dostupný checkout a příkaz `ai-orchestrator`
- Trello API key, token a ID boardu

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e .
```

## Konfigurace

Povinné proměnné prostředí:

```powershell
$env:TRELLO_KEY = '<key>'
$env:TRELLO_TOKEN = '<token>'
$env:TRELLO_BOARD_ID = '<board-id>'
```

Nejdůležitější volitelné proměnné:

| Proměnná | Výchozí hodnota | Význam |
| --- | --- | --- |
| `TRELLO_INBOX_LIST` | `INBOX / Nápady` | Jediný povolený hlavní boardový ruční vstup |
| `AI_PM_ENABLE_INBOX` | `0` | Povolení řízeného intake hlavního boardového Inboxu |
| `AI_ORCHESTRATOR_CMD` | automaticky AO `.venv\Scripts\python.exe` + `orchestrator.py` | Volitelný explicitní příkaz orchestrátoru |
| `AI_ORCHESTRATOR_ROOT` | automaticky nalezený AO checkout | Volitelný kořen checkoutu ai-orchestratoru pro automatické sestavení příkazu |
| `AI_PM_PROVIDERS` | `groq,antigravity,claude-code,codex` | Metadata provider registry PM; konkrétního providera vybírá AO broker |
| `AI_PM_POLL_INTERVAL_SECONDS` | `300` | Maximální prodleva mezi polling tick-y |
| `AI_PM_ARTIFACT_CLEANUP_ROOT` | vypnuto | Explicitní kořen, v němž se mezi běhy mažou pouze expirované `.pytest-basetemp-*` adresáře |
| `AI_PM_ARTIFACT_RETENTION_HOURS` | `24` | Minimální stáří testovacího artefaktu před cleanupem; nezáporné číslo |
| `AI_ORCHESTRATOR_TIMEOUT_SECONDS` | bez limitu | Timeout jednoho běhu orchestrátoru |
| `AI_PM_PROJECT_PATHS` | `{}` | JSON mapa stabilního project key/názvu na checkout |
| `AI_PM_PROJECTS_ROOT` | prázdné | Schválený kořen pro izolované checkouty nových Inbox nápadů; není fallbackem pro nejasnou identitu existujícího projektu |
| `AI_PM_CARD_PROJECT_KEYS` | `{}` | JSON mapa Trello card ID -> stabilní project identita (jednorázová migrace starých karet, viz níže) |
| `AI_PM_PROVIDERS_FOR_PROJECT` | `{}` | JSON mapa projektu na seřazený seznam providerů |
| `AI_ORCHESTRATOR_SPEC_DIR` | `specs` | Adresář generovaných specifikací |
| `AI_ORCHESTRATOR_OUTBOX_DIR` | `outbox` | Outbox výsledků orchestrátoru |
| `AI_PM_PROVIDER_STATE_PATH` | `provider_state.json` | Perzistentní stav providerů a checkpointů |
| `AI_PM_HOLDER` | `project-manager` | Identita držitele projektového zámku |

JSON příklad mapování projektů:

```powershell
$env:AI_PM_PROJECT_PATHS = '{"AI Project Manager":"D:/orchestrator/ai-project-manager"}'
```

Tajné údaje neukládejte do repozitáře. Adresáře `.secrets/` a `runtime/` jsou
ignorované Gitem.

### Migrace starých karet na stabilní projektovou identitu

Karta, která existovala už před zavedením `project_key` labelu, nemá žádný
label ani text, ze kterého by šlo identitu bezpečně odvodit (typicky terse
provozní karta typu „P5 — Izolace testovacích Slack notifikací“, která
nikde nezmiňuje název projektu). Karta s jedinou známou projektovou identitou
v titulku se na tento checkout namapuje; titulek má přednost před pouhou
zmínkou jiného projektu v popisu, například původcem chyby. Jediná identita
jen v popisu je přípustná pouze při jednoznačné shodě. Nejednoznačná shoda se
fail-closed nikdy nepřesměruje na špatné repo.

Jednorázové, bezpečné řešení je `AI_PM_CARD_PROJECT_KEYS`: JSON mapa
neměnného Trello card ID na jednu z identit již nakonfigurovaných v
`AI_PM_PROJECT_PATHS`. Při dalším `--once` tiku scheduler kartě přiřadí a
na boardu perzistuje odpovídající label (a vytvoří ho, pokud na boardu ještě
neexistuje):

```powershell
$env:AI_PM_CARD_PROJECT_KEYS = '{"<trello-card-id>":"AI Project Manager"}'
```

Hodnota, která nejmenuje žádnou z existujících `AI_PM_PROJECT_PATHS` identit,
se tiše ignoruje (karta zůstane nezmigrovaná, nikdy nedostane vymyšlený
label). Proměnnou lze po úspěšné migraci z konfigurace zase odebrat - jakmile
je label jednou na kartě, přežívá každý další sync.

Pro ručně připravenou migraci lze místo interního ID použít i přesný aktuální
název karty. Tento fallback se aplikuje pouze tehdy, když má na načteném boardu
daný název právě jedna karta; při duplicitním názvu se bezpečně neprovede a je
nutné použít neměnné card ID.

## Spuštění

Nejprve proveďte jeden bezpečný integrační tick:

```powershell
python -m ai_project_manager --once --log-level INFO
```

Je-li workflow prázdné a v `INBOX / Nápady` čeká uživatelská karta, tento
jednorázový tick sám provede řízený refresh providerů a Inbox intake. Intake
smí vytvořit více malých podúkolů; jejich pracovní text je kompaktní a
provider-ready, aby se krátké úlohy mohly vejít do limitu Groq TPM. Závislosti
mezi podúkoly a zdrojová identita zůstávají zachované podle `WORKFLOW.md`.

Proces vrací kód `2` při chybné konfiguraci a v režimu `--once` kód `1` při
provozní chybě (například nedostupné Trello nebo selhání zápisu stavu). Bez
`--once` běží trvale a přechodné chyby po poll intervalu opakuje:

```powershell
python -m ai_project_manager --log-level INFO
```

### Self-update: bezpečný restart dlouho běžícího procesu

`--once` je bez rizika zastaralých importů - je to vždy nový proces, takže
načte aktuální kód. Dlouho běžící smyčka (bez `--once`) ale drží už jednou
naimportované moduly i poté, co `ai-orchestrator` přepíše soubory vlastního
balíčku na disku. Po každém ticku proto smyčka porovná otisk (SHA-256 obsahu
všech `*.py` souborů) aktuálního kódu s otiskem při startu procesu
(`self_update.py`). Při shodě nic nedělá; při změně nejdřív ověří, že je
bezpečné restart signalizovat - spustí regresní testy a ověří, že `git HEAD`
je rozřešitelný a strom nemá nevyřešené konflikty (`prepare_safe_restart`) -
teprve pak uloží perzistentní stav (provider/Trello checkpoint) a proces se
ukončí s vyhrazeným exit kódem `RESTART_REQUIRED_EXIT_CODE` (`75`). Pokud
testy/checkpoint neprojdou, restart se odloží a smyčka beze ztráty rozdělané
práce pokračuje na starém, ověřeně funkčním kódu.

Proces sám sebe nikdy nerestartuje - o skutečný restart (a případný rollback,
pokud nová verze neprojde live smoke testem) se stará samostatný nadřazený
watchdog proces:

```powershell
python -m ai_project_manager.watchdog --repo-root D:\orchestrator\ai-project-manager -- --log-level INFO
```

Watchdog po celou dobu drží OS zámek `runtime/watchdog.lock`. Druhý souběžný
start nad stejným checkoutem skončí s kódem `1`, takže dvě instance nikdy
nepollují stejný board ani nespustí tutéž práci paralelně. Soubor zámku může
na disku zůstat; rozhodující je zámek otevřeného handle, který operační systém
automaticky uvolní i při pádu procesu.

Watchdog spustí PM jako podproces; při běžném ukončení skončí i watchdog.
Při `RESTART_REQUIRED_EXIT_CODE` spustí nový podproces (nyní už s aktuálním
kódem) a ověří ho živým `--once` smoke testem. Pokud smoke test selže, vytvoří
z posledního known-good commitu oddělený `git worktree` v
`runtime/self_update_rollback_worktree` a znovu nastartuje starou verzi z něj.
Primární pracovní strom ani necommitnutou sebeaktualizaci nepřepíše, takže
vadná změna zůstane dostupná pro diagnostiku a opravu. Opakované restarty bez
úspěšného smoke testu jsou omezené (`--max-consecutive-restarts`, výchozích
5), aby vadná sebeaktualizace nikdy nezpůsobila nekonečnou crash smyčku.
Prodlevu před ověřením a restartem lze nastavit pomocí
`--restart-backoff-seconds` (výchozích 5 sekund); obě hodnoty musí být
nezáporné a prodleva navíc musí být konečné číslo (`NaN`/`Infinity` se
odmítnou při startu).
`start_ai_project_manager.bat` deleguje na `scripts/run-ai-project-manager.ps1`
(bez `-Once`), tedy na stejné místo, které načítá produkční Trello
konfiguraci z `.secrets/scheduler.clixml` a teprve pak spouští tento watchdog
wrapper - `.bat` sám žádné tajemství nezná ani neduplikuje. Spouští se přes
`start` ve vlastní, oddělené konzoli, aby zavření konzole, ze které byl
`.bat` vyvolán (naplánovaná úloha, zástupce ve Startup složce, interaktivní
shell), neposlalo CTRL_CLOSE/CTRL_LOGOFF signál sdílenou konzolí až do celého
běžícího stromu watchdog+PM a neukončilo ho s `STATUS_CONTROL_C_EXIT`
(`0xC000013A`), i když se samotným PM nic špatného neděje.

Na Windows lze použít připravené skripty v `scripts/`. Runner očekává DPAPI
credential soubor `.secrets/scheduler.clixml`; jeho hodnoty musí odpovídat
polím `TrelloKey`, `TrelloToken` a `TrelloBoardId`. Po jeho
přípravě zaregistrujte úlohu:

```powershell
powershell -NoProfile -File scripts/install-scheduler.ps1 -IntervalMinutes 5
```

Scheduler spouští persistentní watchdog; ten drží jedinou PM smyčku, odmítá
duplicitní instanci a ukládá transcript do `runtime/scheduler/`. Installer po
registraci ověří, že úloha zůstala povolená. Instalace úlohu úmyslně
nespouští a nikdy nevolá `Start-ScheduledTask`; persistentní watchdog se
spustí až triggerem po přihlášení nebo startu systému. Cesty k Pythonu a sousedním checkoutům lze
přepsat parametry runneru nebo `AI_PM_PYTHON_EXE`.

Pokud nezávislý audit vrátí konkrétní námitku, PM ji uloží jako feedback. U
rejection pouze auditních bodů automaticky přidá jeden nový neověřený
implementační DoD bod s námitkou jako zadáním a kartu vrátí do `Pracuje se`,
aby další provider skutečně provedl rework. Stejný audit se nesmí opakovat
bez změny; pouze explicitně označený nereworkový auditní gate může zůstat v
`Testování`.

Pro bezpečné zastavení použijte z kořene projektu:

```powershell
.\stop_ai_project_manager.bat
```

Stop nejdříve zakáže a zastaví úlohu `AI Project Manager Scheduler`, aby ji
repetition trigger znovu nespustil, a potom ukončí pouze dohledaný PM/watchdog
podstrom podle cesty tohoto checkoutu. Úloha zůstane registrovaná pro pozdější
opětovné zapnutí; jiné procesy `python.exe` se necílí.

Live stav watchdogu a následný automatický tick ověří:

```powershell
powershell -NoProfile -File scripts/verify-scheduler.ps1 -WaitForNextTick
```

## Stavový model a obnova

Přesná pravidla pořadí, povolených přechodů a důkazů dokončení jsou v
[`WORKFLOW.md`](WORKFLOW.md). Implementace je musí dodržovat i v režimu
jednorázového `--once` ticku; trvalý scheduler pouze opakuje stejný tick.

Priorita projektu je `P0` až `P5` (vyšší číslo má přednost); při rozdělení
jedné Inbox karty se kolize rozlišují desetinnými podúrovněmi, například
`P2.01` a `P2.02`. Každá workflow karta mimo Inbox má tuto aktuální prioritu
také přímo v názvu. Blokované nebo
zamčené projekty se nepouštějí. Session/quota limit přepne provider na
`LIMITED`, uloží checkpoint a `retry_after`; do deadline probíhá jen lokální
polling. Po deadline se provider bez zvláštního AI požadavku znovu povolí a
práce pokračuje z checkpointu. Opakované identické chyby zastavuje guard, aby
daemon nevytvářel nekonečnou smyčku permission/test pokusů.

AI Inbox intake ukládá u každého rozděleného podúkolu také `depends_on` a
prováděcí pořadí. Závislosti mají přednost před prioritou: vyšší priorita
řadí jen mezi podúkoly, jejichž předpoklady jsou v `Hotovo`; cyklický nebo
neúplný plán se do workflow nepřijme.
Všechny podúkoly jedné zdrojové Inbox karty mají společný `source_card_id`,
v Trellu zůstávají v jednom souvislém batchi a jejich karta viditelně ukazuje
pořadí i přímé závislosti. Planner nesmí spojit dvě projektové identity do
jednoho batchu. Pracovní text je omezen na `scope` 240, `task` 900 a
`next_step` 360 znaků; odůvodnění a ověřovací popis na 320 znaků a odkaz na
120 znaků. Celý Inbox popis, workflow a protokol se do providerového tasku
nepřenášejí.

## Ověření

```powershell
python -m pytest -q
```

Testy jsou plně lokální; produkční Trello ani orchestrátor nevolají.

# Provider model selection

PM only starts the task-family handoff. The v2 ai-orchestrator provider-broker
owns provider and model selection for Inbox planning, implementation, audit
and failover, using its own provider notes and configuration. PM does not
execute broker/provider code or pass a concrete provider/model choice on a
production handoff, so a PM catalog cannot silently override AO routing.
After the run, PM records only the actual `active_model`/`model`/`usage.total.model`
returned by ai-orchestrator, or explicitly records that the provider default
was not reported. It never guesses a model from the provider name.

`AI_PM_PROVIDER_MODELS` remains readable for backward-compatible provider-state
files and diagnostics, but it is not an operational model-routing input. A
deliberate direct ai-orchestrator caller may still use its explicit
`--provider-models` option; that is outside the PM production handoff.

Inbox planning follows the same model-ownership rule and is executed by AO's
Inbox Intake entry point. Hermes is retired from PM entirely (not just Inbox
planning, see `WORKFLOW.md`) after production use showed it could not be used
reliably as a PM provider; where ai-orchestrator still uses it outside PM
(e.g. its own `poc/hermes_agent`), it enforces the exact Nous-only free model
`upstage/solar-pro4:free`. A free-tier provider must remain free-only and fail
closed when no permitted free model is available; it must never silently fall
back to a paid LLM. These rules apply equally to Inbox intake, implementation
and independent audit.

Example: `{"claude":["claude-opus-4-1","claude-sonnet-4"],"codex":["gpt-5.6"]}`.

Groq free provider is intended primarily for small, atomic tasks.
