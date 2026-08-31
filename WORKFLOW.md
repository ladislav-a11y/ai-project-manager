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

1. `Testování` — nejdříve se zpracuje karta, která čeká na nezávislý audit.
2. `Čeká na AI` — karta čeká pouze na obnovení provideru nebo na lidské
   rozhodnutí; provider-limit se po termínu vrací do původní fáze.
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
musí být pro dirty checkout dokončena před `Hotovo`: test, explicitní rozsah
commitu, čistý pracovní strom a záloha/ověřený remote HEAD. U schválené
`post_done_finalization` karty se tento krok nesmí vydávat za hotový předem a
musí se provést bezprostředně jako navazující finalizace. Pokud finalizátor
nebo jeho allowlist není k dispozici, běžnou kartu audit nepřijme; PM nesmí
použít globální `git add -A` jako náhradní řešení. Čistý checkout bez změn
nevyžaduje prázdný commit.

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

### Inbox intake

`INBOX / Nápady` je hlavní boardový vstup PM; osobní Inbox se nikdy nemění.
Intake musí před implementačním dispatch oddělit přípravu od práce: každá
nová karta se nejprve zařadí do `Připraveno`, případně rozdělí na samostatné
úkoly, dostane prioritu `P5` až `P0` a každý připravený název ji musí viditelně
obsahovat jako `P<n> — název`. Až další tick smí takovou kartu přesunout do
`Pracuje se`. Při přípravě se používá pouze uživatelský text a název Inbox
karty; strojový blok `PM-DATA` se nesmí považovat za nové zadání. Chybějící,
nejasná nebo víceznačná projektová identita je fail-closed a karta zůstává v
Inboxu s konkrétním požadavkem na člověka.

Přednost mají opravy PM/orchestrátoru a potvrzené live regrese; priorita je
součástí názvu i Card Contractu. Intake musí být idempotentní podle neměnného
ID zdrojové karty a nesmí vytvořit duplicitní pracovní kartu.

Stav `ERROR` je čekací stav, který nejdříve projde recovery passem. Známá
providerová/protokolová chyba se smí automaticky vrátit do `Připraveno` se
zachovaným checkpointem; neznámá nebo opakovaná chyba vyžaduje člověka.
`ERROR` nesmí trvale zablokovat ostatní karty ani vyvolat nekonečné retry.

## Důkaz dokončení

Každý přechod musí být zpětně čitelný z Trella: viditelné DoD, poslední výstup,
důvod čekání nebo odmítnutí, checkpoint a auditní evidence. Lokální soubory,
historické logy ani tvrzení agenta samy o sobě nejsou důkazem dokončení.

## Hermes kvalifikační pravidla

Hermes je pouze experimentální provider a smí být spuštěn výhradně přes Nous
free LLM: `provider=nous` a model s explicitní příponou `:free` (aktuálně
`upstage/solar-pro4:free`). Jakýkoli jiný provider nebo model je porušení
contractu, nikoli fallback.

Před každým Hermes během musí orchestrator nastavit a zalogovat absolutní
`TERMINAL_CWD` pro izolovaný scratch/worktree. Samotné `--in`, pracovní
adresář procesu ani textová odpověď Hermese nejsou důkazem provedené práce.
Handoff je úspěšný teprve po ověření postcondition orchestrátorem: exit code,
provider/model z usage, povolený rozsah změn a skutečný filesystem/Git diff.
Pokud Hermes pouze popisuje postup, vrátí „success“ bez postcondition nebo
selže na pracovním adresáři, výsledek je `rejected`/`blocked` a nesmí se
započítat do DoD.
