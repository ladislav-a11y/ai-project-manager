# AI Project Manager

Autonomní řídicí vrstva nad Trello boardem a `ai-orchestrator`. Trello je
zdroj pravdy pro projekty i jediný ruční vstup (`Inbox`). Jeden scheduler tick
načte Inbox a projekty, vybere nejvýše prioritní neblokovanou práci, předá ji
orchestrátoru a výsledek zapíše zpět do Trella.

Pokud není práce nebo je provider dočasně omezený, tick nevolá AI. Stav
providerů, `retry_after` a checkpointy se ukládají atomicky a po restartu se
znovu načtou.

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
| `TRELLO_INBOX_LIST` | `Inbox` | Název jediného ručního vstupu |
| `AI_ORCHESTRATOR_CMD` | `ai-orchestrator` | Příkaz orchestrátoru; předávají se další argumenty |
| `AI_PM_PROVIDERS` | `auto` | Čárkou oddělené providery |
| `AI_PM_POLL_INTERVAL_SECONDS` | `300` | Maximální prodleva mezi polling tick-y |
| `AI_ORCHESTRATOR_TIMEOUT_SECONDS` | bez limitu | Timeout jednoho běhu orchestrátoru |
| `AI_PM_PROJECT_PATHS` | `{}` | JSON mapa stabilního project key/názvu na checkout |
| `AI_PM_PROJECTS_ROOT` | prázdné | Společný adresář checkoutů jako fallback; při startu se ukotví na absolutní cestu |
| `AI_PM_CARD_PROJECT_KEYS` | `{}` | JSON mapa Trello card ID -> stabilní project identita (jednorázová migrace starých karet, viz níže) |
| `AI_PM_PROVIDERS_FOR_PROJECT` | `{}` | JSON mapa projektu na seřazený seznam providerů |
| `AI_ORCHESTRATOR_SPEC_DIR` | `specs` | Adresář generovaných specifikací |
| `AI_ORCHESTRATOR_OUTBOX_DIR` | `outbox` | Outbox výsledků orchestrátoru |
| `AI_PM_PROVIDER_STATE_PATH` | `provider_state.json` | Perzistentní stav providerů a checkpointů |
| `AI_PM_HOLDER` | `project-manager` | Identita držitele projektového zámku |
| `SLACK_WEBHOOK_URL` | prázdné | Slack incoming-webhook URL; samo o sobě notifikace nezapne |
| `AI_PM_SLACK_ENABLED` | vypnuto | Explicitní opt-in (`1`, `true`, `yes`, `on`) pro Slack notifikace |

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
nikde nezmiňuje název projektu). Pro takové karty automatická migrace podle
obsahu (fráze v názvu/popisu) záměrně nic neuhodne - nikdy nenamapuje kartu
na špatné repo jen proto, že se v textu objevila nejednoznačná shoda.

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
(bez `-Once`), tedy na stejné místo, které načítá produkční Trello/Slack
konfiguraci z `.secrets/scheduler.clixml` a teprve pak spouští tento watchdog
wrapper - `.bat` sám žádné tajemství nezná ani neduplikuje. Spouští se přes
`start` ve vlastní, oddělené konzoli, aby zavření konzole, ze které byl
`.bat` vyvolán (naplánovaná úloha, zástupce ve Startup složce, interaktivní
shell), neposlalo CTRL_CLOSE/CTRL_LOGOFF signál sdílenou konzolí až do celého
běžícího stromu watchdog+PM a neukončilo ho s `STATUS_CONTROL_C_EXIT`
(`0xC000013A`), i když se samotným PM nic špatného neděje.

Na Windows lze použít připravené skripty v `scripts/`. Runner očekává DPAPI
credential soubor `.secrets/scheduler.clixml`; jeho hodnoty musí odpovídat
polím `TrelloKey`, `TrelloToken`, `TrelloBoardId` a `SlackWebhookUrl`. Po jeho
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

Pro bezpečné zastavení použijte z kořene projektu:

```powershell
.\stop_ai_project_manager.bat
```

Stop nejdříve zakáže a zastaví úlohu `AI Project Manager Scheduler`, aby ji
repetition trigger znovu nespustil, a potom ukončí pouze dohledaný PM/watchdog
podstrom podle cesty tohoto checkoutu. Úloha zůstane registrovaná pro pozdější
opětovné zapnutí; jiné procesy `python.exe` se necílí.

Live stav včetně doručení Slacku (HTTP 200 zaznamenané bez webhooku v logu)
a následného automatického ticku ověří:

```powershell
powershell -NoProfile -File scripts/verify-scheduler.ps1 -WaitForNextTick
```

Když scheduler musí zůstat na HOLD, lze stejnou trvalou DPAPI konfiguraci
ověřit izolovaným doručovacím testem z nového procesu PowerShellu. Příkaz
nespouští tick, nečte ani nemění Trello a skončí nenulově, pokud Slack nevrátí
HTTP 200:

```powershell
powershell -NoProfile -File scripts/run-ai-project-manager.ps1 -SlackProbe
```

## Stavový model a obnova

Přesná pravidla pořadí, povolených přechodů a důkazů dokončení jsou v
[`WORKFLOW.md`](WORKFLOW.md). Implementace je musí dodržovat i v režimu
jednorázového `--once` ticku; trvalý scheduler pouze opakuje stejný tick.

Priorita projektu je `P0` až `P5` (vyšší číslo má přednost). Blokované nebo
zamčené projekty se nepouštějí. Session/quota limit přepne provider na
`LIMITED`, uloží checkpoint a `retry_after`; do deadline probíhá jen lokální
polling. Po deadline se provider bez zvláštního AI požadavku znovu povolí a
práce pokračuje z checkpointu. Opakované identické chyby zastavuje guard, aby
daemon nevytvářel nekonečnou smyčku permission/test pokusů.

## Ověření

```powershell
python -m pytest -q
```

Testy jsou plně lokální; produkční Trello ani orchestrátor nevolají.

# Provider model selection

`AI_PM_PROVIDER_MODELS` is an optional JSON mapping from every configured
provider to an ordered, non-empty model list. The first entry is the selected
model and is passed to ai-orchestrator as `--model` for both implementation and
audit dispatches. The PM persists that selection with the Trello run evidence and includes
the provider and model in Slack start/result messages. If ai-orchestrator
returns `active_model`, `model`, or `usage.total.model`, the confirmed runtime
model replaces the configured selection in result messages. Missing model
information is reported explicitly as an unknown provider default; it is never
guessed from the provider name.

Example: `{"claude":["claude-opus-4-1","claude-sonnet-4"],"codex":["gpt-5.6"]}`.
