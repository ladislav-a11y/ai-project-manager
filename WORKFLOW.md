# Řízený workflow AI Project Manageru

Trello je jediný zdroj pravdy. Stav, pořadí, DoD, checkpoint, čekání na
provider i auditní výsledek se vždy načítají z Trella a po změně se do něj
bezprostředně zapisují.

Každý zápis popisu karty musí respektovat bezpečný limit 14 000 znaků. PM smí
zkrátit pouze diagnostickou nebo viditelnou historii s explicitní značkou;
PM-DATA, checkpoint a DoD se nikdy nesmí slepě oříznout. Pokud se úplný
kontrakt nevejde ani po zkrácení historie, zápis i lifecycle přechod se
odmítnou fail-closed a do Trella se nesmí poslat poškozený popis.

## Pevné pořadí

Každý tick načte a podle nastavení provede Inbox intake; intake je samostatná
fáze a nesmí obejít čekání, audit ani aktivní práci. Pořadí dispatch části je:

1. `Čeká na AI` — nejdříve se zkontrolují provider-limitní čekání. Po termínu
   nebo při dostupném failoveru se karta okamžitě vrátí do své návratové fáze a
   obnovená implementace dostane první příležitost k práci z checkpointu.
2. `Testování` — dokud existuje karta čekající na audit, zpracuje se audit-only
   přes ai-orchestrator; audit je vždy nezávislý a používá AI. Při potřebě
   dopracování se karta vrací do `Pracuje se` s konkrétním feedbackem.
3. `Pracuje se` — agent provádí implementaci a postupně plní DoD.
4. `Připraveno` — nový úkol smí být vybrán až po vyřešení předchozího řetězce.
5. `Hotovo` — pouze po úspěšných testech, přijetí nezávislým auditem a úplném
   viditelném DoD. U výslovně schválené karty s již dokončeným live ověřením
   může být controllerová finalizace (commit, záloha a remote) vedena jako
   navazující krok po `Hotovo`; tato výjimka musí být zapsaná v PM-DATA jako
   `completion_policy.mode=post_done_finalization` a nikdy nemění auditní
   autoritu ai-orchestratoru.

## Povinné přechody

- `Připraveno → Pracuje se`: právě jeden aktivní implementační úkol.
- Nový úkol z `Připraveno` nezačne, dokud je v `Pracuje se`, `Čeká na AI` nebo
  `Testování` jiný úkol; čekající karta se nejprve obnoví a auditní fronta se
  zpracuje před novou implementací.
- `Pracuje se → Testování`: všechna implementační DoD jsou ověřena; tento
  přechod se zapíše ještě před spuštěním testů a auditu.
- `Pracuje se → Čeká na AI`: provider-limit; checkpoint a návratová fáze se
  zachovají.
- `Testování → Hotovo`: pouze explicitní přijatý verdikt ai-orchestrátoru.
- `Testování → Pracuje se` nebo `Připraveno`: pouze explicitní odmítnutí auditu
  s konkrétní zpětnou vazbou.
- `Testování → Čeká na AI`: pouze provider-limit; návratová fáze zůstává
  `Testování`.

Terminální finalizace je vlastněna controllerem, nikdy agentem. Standardně
musí být pro dirty checkout dokončena před `Hotovo`: test, rozsah commitu,
čistý pracovní strom a záloha/ověřený remote HEAD. Rozsah commitovaných cest
se odvozuje ze skutečného `git status` daného projektu, pokud pro něj není
nakonfigurován explicitní kurátorovaný seznam (`AI_ORCHESTRATOR_FINALIZE_PATHS`)
- takový seznam zůstává jen přísnější volitelnou výjimkou pro projekt, který
smí měnit vlastní řídicí kód (např. AI Project Manager při self-update); pro
běžný cílový projekt (např. Station Agent) žádný ruční seznam nevyžaduje.
V obou případech se nikdy nepoužije globální `git add -A` - cesty se vždy
stagují jednotlivě a explicitně vyjmenované. Push na vzdálený remote naopak
zůstává vždy vázaný na explicitní `AI_ORCHESTRATOR_ALLOWED_PUSH_REMOTES`
záznam pro daný projekt - bez něj finalizace zůstane commitnutá jen lokálně,
`pushed=true` se nesplní a karta se vrátí do `Pracuje se` s konkrétním
důvodem, dokud povolení nepřidá člověk. U schválené `post_done_finalization`
karty se tento krok nesmí vydávat za hotový předem a musí se provést
bezprostředně jako navazující finalizace. Čistý checkout bez změn nevyžaduje
prázdný commit.

Implementační agent nikdy sám neuzavírá kartu do `Hotovo`. Neúplné nebo
neověřené DoD se nesmí označit jako hotové. Karta vrácená z auditu se nesmí
automaticky znovu vydávat za dokončenou jen proto, že její staré DoD zůstalo
zaškrtnuté.

Položky DoD mají ve strukturovaných datech fázi `implementation` (výchozí,
zpětně kompatibilní) nebo `audit`. Implementační handoff obsahuje výhradně
první skupinu. Auditní položky se poprvé předají až audit-only handoffu v
následujícím PM ticku; jejich stav proto nikdy nemůže vyvolat další
implementační běh ani spotřebu tokenů implementačního agenta.

## Tick a priorita

Jeden tick zpracuje nejvýše jednu kartu. Auditní a čekací fáze mají přednost
před výběrem nového úkolu. U úkolů ve stejné fázi rozhoduje priorita `P5` až
`P0`, přičemž opravná karta vrácená z auditu má přednost před běžnou kartou.
Scheduler musí být idempotentní a při absenci bezpečně zpracovatelné práce
nesmí volat AI.

Dočasné testovací artefakty mají samostatný fail-closed lifecycle. Pytest
vytváří unikátní `.pytest-basetemp-*` pro konkrétní session a po jejím
dokončení smaže pouze tento vlastní adresář. Trvalý scheduler smí mezi tick-y,
tedy až po návratu implementačního nebo auditního subprocessu, odstranit jen
expirované přímé `.pytest-basetemp-*` potomky explicitního
`AI_PM_ARTIFACT_CLEANUP_ROOT`. Symlinky, soubory, čerstvé adresáře, jiné názvy,
zdroje, runtime data a pracovní změny nejsou kandidáty; bez explicitního
kořene je autonomní cleanup vypnutý.

### Inbox intake

`INBOX / Nápady` je hlavní boardový vstup PM; osobní Inbox se nikdy nemění.
Intake musí před implementačním dispatch oddělit přípravu od práce: každá
nová karta se nejprve zařadí do `Připraveno`, případně rozdělí na samostatné
úkoly, dostane prioritu `P5` až `P0` a každý název workflow karty mimo Inbox ji
musí viditelně obsahovat jako `P<n> — název`. Až další tick smí takovou kartu přesunout do
`Pracuje se`. Inbox intake přijme nejvýše jeden zdrojový projekt za tick;
vybere nejvyšší připravenou prioritu a při shodě použije stabilní ID karty.
Při přípravě se používá pouze uživatelský text a název Inbox
karty; strojový blok `PM-DATA` se nesmí považovat za nové zadání. Známý
projekt se nejdříve určí z explicitního mapování/štítku, případně z jediné
projektové identity v titulku; titulek má přednost před pouhou zmínkou jiného
projektu v popisu (např. původcem chyby). Jediná identita pouze v popisu je
přípustná jen při jednoznačné shodě. Chybějící, nejasná nebo víceznačná
identita existujícího projektu je fail-closed a karta zůstává v Inboxu s
konkrétním požadavkem na člověka. U skutečně nového nápadu
bez neprioritního štítku PM vytvoří stabilní izolovanou identitu ve tvaru
`<název> [Inbox <zdrojové ID>]`, založí její adresář pod explicitně
konfigurovaným `AI_PM_PROJECTS_ROOT` a připraví kartu stejně jako ostatní
úkoly. Tím se nový nápad nesmí sloučit s podobným existujícím projektem;
neznámý nebo konfliktní štítek se naopak nikdy nepřepisuje odhadem.

Při rozdělení se zdrojová Inbox karta smí dočasně ponechat pouze do okamžiku,
než jsou bezpečně zapsány všechny podúkoly. Po dokončení splitu se zdroj stane
prvním kanonickým podúkolem a musí opustit Inbox; nesmí být současně veden jako
samostatná pracovní karta ani jako duplicitní receipt. Při retry se identita
smí obnovit jen z jednoznačně shodných již zapsaných podúkolů podle zdrojového
ID, nikdy z podobnosti názvů.

Všechny podúkoly vzniklé z jedné Inbox karty tvoří jeden nedělitelný Inbox
batch. PM u každého podúkolu zachová `source_card_id`, `subtask_index`,
`subtask_count`, `execution_order` a `depends_on_subtask_indices`. V každém
workflow seznamu musí být celý batch fyzicky souvislý; řazení podle priority
nesmí proložit kartu jiného Inbox projektu. Karta navíc viditelně uvádí číslo
podúkolu a jeho přímé návaznosti. Planner nesmí do jednoho vstupu smíchat dvě
projektové identity; při nejasnosti musí intake skončit v Inboxu s požadavkem
na lidské upřesnění.

Inbox planner je samostatná AI-planning fáze a volí pouze z povolených
dostupných providerů (Hermes je z PM úplně vyřazen, viz sekci „Hermes -
vyřazen z produkčního PM routingu" níže, ne jen z této fáze). PM planneru ani
implementaci/auditu nepředává `--model`: konkrétní model volí provider podle
typu úkolu a skutečně použitý model se bere až z AO outboxu. Žádný free
provider nesmí při nedostupnosti svého povoleného free modelu tiše zvolit
placený LLM.
Před každým použitím providera PM oznámí jeho výběr, důvod, typ úkolu a modelový
plán; po dokončení uloží skutečný model potvrzený providerem. Intake navíc
zapisuje do `PM-DATA` `intake_provider_reason`, `intake_model_reason` a
`intake_selection_reason`, aby byl výběr dohledatelný na každém podúkolu i ve
Slacku. Pokud je provider omezený, Slack i stavová zpráva uvádí absolutní
`retry_at` a odpočet `retry za`; PM jej do té doby znovu nevolá.
Globální stav `LIMITED` nebo `ERROR` s `retry_after` je závazný pro všechny
workflow fáze: PM takového providera nepředá ani do dalšího AO failover řetězce
až do termínu revalidace. Do té doby se provider pouze lokálně přeskočí;
po termínu proběhne právě jedna dostupnostní revalidace.

### Bezpečná změna runtime

Pokud běží PM tick, persistentní PM nebo jeho ai-orchestrator child proces,
nesmí se současně opravovat kód, workflow pravidla ani runtime konfigurace.
Nejprve se běh bezpečně ukončí a ověří se, že PM/AO již neběží; teprve potom
je dovolena oprava. Po změně se PM spouští pouze řízeným `--once` tickem.
AO musí v outboxu vracet `provider_statuses` pro všechny providery v daném
failover pořadí. Každý `LIMITED` záznam nese absolutní UTC `retry_at`; PM
zapíše všechny tyto termíny do persistentního stavu a do PM-DATA/Trella
readbacku. `retry_after_seconds` je pouze zpětně kompatibilní fallback,
nikdy se nesmí použít tak, že by se ostatní limity ztratily.

Opravy, potvrzené chyby, regrese a rework mají vždy závaznou nejvyšší prioritu
(`P5`); ani explicitní nižší štítek ze zdrojového Inboxu je nesmí snížit.
Přednost mají opravy PM/orchestrátoru (`P5`), bezpečnostní, produkční nebo
blokující dopady (`P5`), potvrzené live regrese (`P4`), běžné opravné požadavky
(`P4`), realizovatelné změny funkcionality (`P3`) a teprve potom běžná,
budoucí nebo rešeršní práce (`P2` až `P0`). Výsledná priorita se posuzuje pro
každý rozdělený podúkol zvlášť podle jeho obsahu; zděděné `P0` nesmí všechny
podúkoly sloučit do stejné priority. V jedné dávce se priorita nesmí
opakovat: při kolizi PM použije desetinné podpriority (`P2.01`, `P2.02`),
které zůstávají pod další celočíselnou úrovní; vždy platí, že vyšší číslo má
vyšší prioritu. Pokud by ani tato rozšířená škála nestačila, PM ji rozšíří
stejným monotónním pravidlem, nikdy však nesníží význam vyšší celočíselné
úrovně. Toto pravidlo platí při Inbox intake do `Připraveno`. Priorita je
součástí názvu i Card Contractu. Po zařazení je priorita neměnná: PM ji nesmí
re-rankingem přidělit znovu ani odvozovat z aktuálního listu, fáze, providera
nebo textu karty. Intake musí být
idempotentní podle neměnného ID zdrojové karty
a nesmí vytvořit duplicitní pracovní kartu.

AI planner současně určuje posloupnost podúkolů pomocí zero-based
`depends_on` indexů. Každý index musí odkazovat na jiný podúkol stejné Inbox
dávky; cyklus, chybějící index nebo duplicitní priorita znamená fail-closed a
zdroj zůstane v Inboxu. Scheduler smí mezi dependency-ready podúkoly použít
prioritu jako pořadí, ale nikdy nesmí spustit podúkol před dokončením jeho
závislostí. Toto pořadí a návaznosti se ukládají do Card Contractu, aby
pozdější tick nepracoval proti zdrojové posloupnosti.

Při načtení starší připravené karty PM nejdříve provede bezpečnou in-memory
migraci známého PM-DATA: opraví historický auditní text na explicitní
nezávislý audit ai-orchestratoru, ale prioritu ani její pořadí už nemění.
Prioritní štítek a titul jsou po intake autoritativní stav Trella. Neznámé,
konfliktní nebo poškozené hodnoty se neopravují odhadem a
zůstávají fail-closed. Ruční konektorový limit 2048 znaků není důvod ke
zkracování kontraktu; PM používá interní synchronizaci s bounded PM-DATA.

Starší připravené Inbox karty, které mají obecný bod „Ověřit relevantní
chování v živém prostředí...“ ve fázi `implementation`, se při této migraci
automaticky přeřadí do fáze `audit`. Zůstávají ve `Připraveno` a zachovají si
svůj konkrétní pracovní rozsah; není nutné je vracet do Inboxu ani znovu
rozdělovat. Nově připravená karta má mít právě jeden implementační výsledek,
zatímco testování, live evidence a verdikt patří do auditní fáze.

Stav `ERROR` je čekací stav, který nejdříve projde recovery passem. Známá
providerová/protokolová chyba se smí automaticky vrátit do `Připraveno` se
zachovaným checkpointem; neznámá nebo opakovaná chyba vyžaduje člověka.
`ERROR` nesmí trvale zablokovat ostatní karty ani vyvolat nekonečné retry.

## Důkaz dokončení

Každý přechod musí být zpětně čitelný z Trella: viditelné DoD, poslední výstup,
důvod čekání nebo odmítnutí, checkpoint a auditní evidence. Lokální soubory,
historické logy ani tvrzení agenta samy o sobě nejsou důkazem dokončení.
Výsledky kvalifikačních smoke testů, live testů a volby modelu uložené na
ověřených kartách v `Hotovo` jsou závazným vývojovým podkladem pro navazující
implementaci a diagnostiku; nesmějí však zpětně měnit terminální stav ani
nahradit aktuální auditní důkaz.

## Hermes — vyřazen z produkčního PM routingu

Hermes byl po ověření v produkčním provozu vyřazen z PM provider poolu
(`AI_PM_PROVIDERS` v `scripts/run-ai-project-manager.ps1`,
`PM_FAILOVER_PROVIDER_ORDER` v `orchestrator_runner.py`, `AUTO_PROVIDER_ORDER`
v `scheduler.py`), protože se ukázalo, že jej PM nemůže spolehlivě použít jako
produkčního providera - stejně jako byl dříve vyřazen Gemini (viz sekce
„Ověřená blokace Gemini" výše). PM proto Hermese už nikdy nevybírá ani
neregistruje, v žádné fázi (Inbox planning, implementace, audit) a ani přes
explicitní `-ProviderOverride`. Historická kvalifikační pravidla specifická
pro Hermes (Nous-only kontrakt `upstage/solar-pro4:free`, `--safe-mode`,
`HERMES_MAX_ITERATIONS=20`, tvrdý 180s timeout, izolovaný `TERMINAL_CWD`)
zůstávají popsaná v `ai-orchestrator/AGENTS.md` (pravidlo 11e) pro případ, že
by ai-orchestrator použil Hermese mimo PM; samostatný PoC vedle PM/
orchestrátoru (`ai-orchestrator/poc/hermes_agent`) tímto vyřazením není
dotčen.

Inbox planning/intake je samostatná AI fáze před worker dispatch. Produkční PM
musí lidský vstup nejprve předat prvnímu dostupnému provideru z pořadí
`antigravity → claude → codex` (viz `INBOX_PLANNER_PROVIDERS`) a pravidlo je
vynucené i samostatným read-only `plan-inbox` handoffem přes ai-orchestrator.
AI planner musí vrátit validní atomické úkoly s různými prioritami, jinak
zdroj zůstane v Inboxu fail-closed. PM uloží skutečný intake provider a model
do Card Contractu a oznámí je ve Slacku. Deterministické heuristiky jsou pouze
testovací/fallback knihovna, nikoli produkční vlastník intake rozhodnutí.
Handoff je úspěšný teprve po ověření postcondition orchestrátorem: exit code,
provider/model z usage, povolený rozsah změn a skutečný filesystem/Git diff.
Přesun karty jiným providerem provider z pořadí neodstraňuje: při dalším ticku
se smí znovu účastnit po úspěšné dostupnostní revalidaci, ale PM nesmí jeho
stav `ERROR` nebo `LIMITED` slepě přepsat na `AVAILABLE`. V Card Contractu a
Slacku se rozlišuje `selected_provider` (provider vybraný PM) od
`actual_provider` a `actual_model` potvrzených outboxem; při interním failoveru
se musí zobrazit celá `provider_sequence`.
Pokud provider pouze popisuje postup, vrátí „success" bez postcondition nebo
selže na pracovním adresáři, výsledek je `rejected`/`blocked` a nesmí se
započítat do DoD. Staré diagnostické hlášení o chybějícím `provider`/`model`
v usage po pádu před dokončením turnu je protokolová chyba k automatickému
recovery a nesmí samo o sobě kartu převést do trvalého `human_required`.
