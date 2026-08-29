# Záloha vlákna – 2026-08-28

## Dohodnutá pravidla

- Trello je jediný zdroj pravdy.
- Workflow se zpracovává odzadu: `Testování` → `Čeká na AI` → `Pracuje se` → `Připraveno`.
- V jednom ticku se zpracovává pouze jeden projekt; scheduler běží autonomně, ale nesmí spouštět další projekt před dokončením předchozího.
- Do `Hotovo` smí karta pouze po plné sadě testů a nezávislém auditu ai-orchestrátoru.
- Při nefunkčním PM/orchestrátoru se opravuje kód přímo; neprovádí se zbytečné duplicitní úkoly ani commity bez výslovného požadavku.
- Priorita se vyhodnocuje v kontextu workflow; auditní/opravené úlohy mají přednost před běžnou novou prací.

## Ověřený stav před autonomním během

- P4 `Oprava precedence workflow stavu a stale blocked_by`: v Trellu `Hotovo`, všech 5 DoD splněno.
- Oprava P4: fyzický Trello seznam zůstává autoritativní; staré `lifecycle_status=done` už nemůže kartu z `Připraveno` přesunout do `Hotovo`.
- PM testy po opravě: `542 passed`.
- P4 audit ai-orchestrátoru: `ACCEPTED`.
- Slack notifikace při P4: `HTTP 200`.

## Aktuální běh

- Další vybraná karta: `P2 — Trvalá produkční Slack observabilita PM`.
- Scheduler/PM byl uživatelem potvrzen jako běžící autonomně; běh se nemá ručně restartovat.
- První implementační iterace P2 dokončila změny a testy prošly `544`; body živého testu a auditu zůstaly správně nedoložené.
- Zjištěná chyba: ai-orchestrátor opakoval implementační iterace pouze kvůli bodu `plná testovací sada + accepted/rejected verdikt`, který patří kontroleru/auditu, nikoli implementačnímu agentovi.
- Dozor zachytil spotřebu `896 777` hlášených tokenů po 7 iteracích bez auditu; tento běh se nemá považovat za dokončený.

## Požadovaná oprava

Auditní/integration DoD bod vlastněný ai-orchestrátorem nesmí blokovat implementační smyčku. Po dokončení ostatních implementačních bodů a úspěšných testech má proběhnout právě jeden nezávislý audit; při přijetí se karta zapíše jako `Hotovo`, při odmítnutí se vrátí pouze konkrétní zamítnuté body.

## Další ověření

1. Ověřit, že běžící scheduler dokončí nebo bezpečně ukončí aktuální run bez falešného `Hotovo`.
2. Ověřit novou logiku cíleným testem a plnou sadou.
3. Ověřit Trello list, DoD, `stop_reason`, `blocked_by`, Slack a tokeny.
