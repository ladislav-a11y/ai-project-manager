# P5 — Station Agent

## Goal

ÚČEL
Dokončit a otestovat Station Agent.

DNEŠNÍ PRÁCE - HOTOVO
[x] Ověřen živý běh AI Project Manager přes --once.
[x] Ověřeno správné vybrání projektu Station Agent.
[x] Ověřeno spuštění předání do orchestrátoru.
[x] Ověřeno zachycení chyby při nedostupnosti providera.
[x] Ověřena synchronizace stavu zpět do Trella.
[x] Přidány Slack notifikace pro výběr projektu, dispatch a chybu providera.

AKTUÁLNÍ STAV
Autonomní běh čeká na dostupnost AI providerů. Všechny tři dostupné AI služby mají nyní vyčerpané limity.

CO JEŠTĚ DOPLNIT
[ ] Po obnovení limitů provést skutečný autonomní běh s prací agenta.
[ ] Ověřit dokončení DoD Station Agent.
[ ] Doplnit testy pro pokračování po obnovení limitu.
[ ] Ověřit automatické obnovení práce bez ručního zásahu.

NOVÉ TESTY K PROVEDENÍ
[ ] E2E test: Trello -> Project Manager -> Orchestrator -> Provider -> Trello.
[ ] Test provider LIMIT -> retry_after -> automatický resume.
[ ] Test Slack notifikací při úspěchu i chybě.
[ ] Test restartu scheduleru a zachování checkpointu.

PŘIPOJENÉ NÁSTROJE
Codex a Antigravity připojeny pro další autonomní práci.

DALŠÍ KROK
Po obnovení limitů pokračovat automaticky.

## Definition of Done

- [ ] ÚČEL
Dokončit a otestovat Station Agent.

DNEŠNÍ PRÁCE - HOTOVO
[x] Ověřen živý běh AI Project Manager přes --once.
[x] Ověřeno správné vybrání projektu Station Agent.
[x] Ověřeno spuštění předání do orchestrátoru.
[x] Ověřeno zachycení chyby při nedostupnosti providera.
[x] Ověřena synchronizace stavu zpět do Trella.
[x] Přidány Slack notifikace pro výběr projektu, dispatch a chybu providera.

AKTUÁLNÍ STAV
Autonomní běh čeká na dostupnost AI providerů. Všechny tři dostupné AI služby mají nyní vyčerpané limity.

CO JEŠTĚ DOPLNIT
[ ] Po obnovení limitů provést skutečný autonomní běh s prací agenta.
[ ] Ověřit dokončení DoD Station Agent.
[ ] Doplnit testy pro pokračování po obnovení limitu.
[ ] Ověřit automatické obnovení práce bez ručního zásahu.

NOVÉ TESTY K PROVEDENÍ
[ ] E2E test: Trello -> Project Manager -> Orchestrator -> Provider -> Trello.
[ ] Test provider LIMIT -> retry_after -> automatický resume.
[ ] Test Slack notifikací při úspěchu i chybě.
[ ] Test restartu scheduleru a zachování checkpointu.

PŘIPOJENÉ NÁSTROJE
Codex a Antigravity připojeny pro další autonomní práci.

DALŠÍ KROK
Po obnovení limitů pokračovat automaticky.

## Constraints

- Do not run `git commit` (or `git commit --amend`) under any circumstances. Committing the result is the orchestrator's responsibility only, performed after it has verified your work.

<!-- PM-CHECKPOINT
{
  "run_id": "9a77147b8f3c4272a5b541e52caa2d48",
  "checkpoint": {},
  "provider": "claude-code",
  "project_name": "P5 — Station Agent"
}
-->
