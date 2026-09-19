# Rešerše: GNU Radio přijímač DMR / D-STAR / C4FM (YSF) na Raspberry Pi 5 s RTL-SDR v3

Stav: rešerše a návrh, bez implementace kódu. Účel dokumentu: shrnout doporučenou
technickou cestu a připravit konkrétní, atomické navazující zadání pro AI Project
Manager.

## 1. Zadání a rámec

Cíl: navrhnout řešení, které na Raspberry Pi 5 s připojeným RTL-SDR v3 dekóduje
čtyři digitální hlasové módy používané v radioamatérském provozu: **DMR**,
**D-STAR**, **YSF (System Fusion)** a jeho modulaci **C4FM**. Řešení má být
založené na GNU Radio jako SDR front-endu.

Zamýšlená cílová architektura (bráno jako kontext pro doporučení, není součástí
tohoto rozsahu ani této rešerše samotné):

- Raspberry Pi 5 běží headless jako vzdálený remote přijímač u antény.
- Správa, ovládání a případné poslechové/monitorovací rozhraní je v aplikaci na
  Windows PC, která se k Pi připojuje po síti.

Z toho plyne, že Pi musí posílat výstup (dekódované audio + metadata) po síti a
přijímat řídicí příkazy, ale to je předmětem samostatné navazující implementace,
ne tohoto dokumentu.

## 2. Shrnutí doporučení (TL;DR)

1. **GNU Radio pouze jako SDR front-end** (příjem, ladění, kanalizace, FM
   diskriminátor), ne jako místo, kde se dekóduje samotný digitální hlasový
   protokol. Nativní, udržované GNU Radio bloky pro DMR/D-STAR/YSF v roce 2026
   neexistují (staré projekty `gr-dmr`, `gr-dstar` jsou mrtvé, nekompatibilní s
   moderním GNU Radio 3.10/3.11 API).
2. **Dekódování přes externí multi-mode dekodér DSD-FME** (fork DSD/DSDcc,
   aktivně udržovaný, `lwvmobile/dsd-fme` na GitHubu), který z diskriminátorového
   audia (FM demod výstup) umí rozpoznat a dekódovat DMR, D-STAR i YSF/C4FM v
   jednom procesu, včetně softwarové náhrady AMBE/AMBE+2 vokodéru.
3. GNU Radio flowgraph (RTL-SDR zdroj → kanalizace/decimace → FM demodulace →
   resampling na 48 kHz) posílá surové PCM audio do DSD-FME přes named pipe nebo
   UDP. Toto je hlavní integrační bod mezi GNU Radio a dekodérem.
4. RTL-SDR v3 dekóduje **jeden kanál najednou** (konvenční, ne trunkovaný
   provoz). Pro paralelní příjem více frekvencí/módů současně je potřeba víc
   dongle + víc instancí flowgraphu/dekodéru.
5. Kvalita hlasu ze softwarového AMBE je znatelně horší než z hardwarového
   AMBE vokodéru (USB dongle typu ThumbDV/DVMEGA). Pro čisté monitorování
   (odposlech provozu, ne HiFi audio) je softwarová varianta dostačující a je
   doporučeným výchozím bodem; HW dongle je volitelné rozšíření.

## 3. Hardwarové komponenty

| Komponenta | Doporučení | Poznámka |
| --- | --- | --- |
| SBC | Raspberry Pi 5 (4 GB+ RAM), aktivní chlazení (oficiální Active Cooler) | GNU Radio flowgraph + DSD-FME běží nepřetržitě; bez chlazení hrozí thermal throttling při dlouhodobém běhu |
| Napájení | Oficiální 27W USB-C PD adaptér | Podpěťové napájení je nejčastější příčina nestability USB SDR periferií na Pi |
| SDR | RTL-SDR Blog v3 (R820T2 tuner, TCXO ±1 ppm) | Uživatelem již vlastněný HW; TCXO je důležitý pro stabilní ladění úzkopásmových digitálních módů |
| Anténa | Vertikál/anténa laděná na pásmo cílových repeatrů (typicky 2 m / 70 cm pro DMR/D-STAR/YSF repeatery) | Volba podle konkrétních frekvencí, mimo rozsah tohoto dokumentu |
| USB propojení | Kvalitní stíněný USB kabel/prodlužka + feritová tlumivka mezi Pi a dongle | Raspberry Pi 5 je známý vyšším RFI šumem z USB3/PCIe než Pi 4; přiblížení SDR k desce zhoršuje SNR |
| Volitelně: HW AMBE vokodér | DVSI ThumbDV nebo DVMEGA (USB) | Zlepší kvalitu dekódovaného hlasu oproti SW AMBE náhradě; extra USB port a náklad |
| Volitelně: filtr/preselektor | Pásmový filtr/LNA před dongle | Řeší přetížení/intermodulaci u silných blízkých vysílačů, řešit až podle reálného spektra na lokalitě |

## 4. Softwarový stack

- **OS**: Raspberry Pi OS 64-bit (Bookworm), headless.
- **RTL-SDR driver**: `librtlsdr` + zablokování konfliktního DVB jádrového
  modulu (`blacklist dvb_usb_rtl28xxu`).
- **GNU Radio**: 3.10/3.11 z distribučních balíčků nebo `radioconda`/PyBOMBS,
  plus **gr-osmosdr** jako zdrojový blok pro RTL-SDR.
- **GRC flowgraph** (návrh, ne implementace): RTL-SDR Source → Frequency
  Xlating FIR Filter (výběr kanálu + decimace) → Quadrature (FM) Demod →
  Rational Resampler na 48 kHz → File/UDP/Pipe Sink jako výstup diskriminátoru.
- **Dekodér**: **DSD-FME** (aktivní fork DSDcc/DSD+ konceptu), zkompilovaný pro
  ARM64 na Pi 5, čte 48 kHz/16-bit PCM ze stdin/pipe/UDP a dekóduje DMR,
  D-STAR i YSF/C4FM (a navíc NXDN, dPMR, P25 fáze 1, které nejsou cílem, ale
  jsou "zadarmo" ve stejném nástroji).
- **mbelib/AMBE náhrada**: součást DSD-FME řetězce pro softwarové obnovení
  hlasu z AMBE/AMBE+2 rámců.
- **Provozní vrstva**: `systemd` služby pro GNU Radio flowgraph i DSD-FME
  proces (autostart, restart při pádu), logrotate pro dekódované logy.
- **Vzdálený přístup** (kontext k cílové architektuře, ne implementace nyní):
  SSH + VPN (WireGuard nebo Tailscale) mezi Pi a Windows PC, protože Pi je
  "vzdálený remote přijímač".

### Alternativa zvážená a zamítnutá jako primární cesta

**SDRangel** (samostatná C++ SDR aplikace, ne GNU Radio) má vestavěné
demodulátory/dekodéry pro DMR, D-STAR i YSF a běží na ARM64 Linuxu. Je rychlejší
k rozjetí (jeden proces, žádné lepení GNU Radio + externí dekodér), ale
zadání explicitně žádá řešení na bázi **GNU Radio**, a SDRangel jím není –
uvádím ho zde jen jako referenční alternativu, kdyby se v budoucnu ukázalo, že
hybridní GNU Radio + DSD-FME řetězec nestačí kvalitou nebo údržbou.

## 5. Přenos dat/audia (kontext, ne implementace)

Vzhledem k cílové architektuře (Pi = remote přijímač, ovládání a provoz z
Windows aplikace) bude navazující implementace muset řešit tři samostatné
roviny, doporučeně oddělené:

1. **Řídicí rovina**: Windows aplikace ⇄ Pi přes lehké API (HTTP/WebSocket)
   pro výběr frekvence/módu, start/stop, čtení stavu. Nepřenášet syrové IQ přes
   síť (datový tok v řádu MB/s je zbytečný, když se dekóduje přímo na Pi).
2. **Audio rovina**: dekódované PCM audio z Pi na Windows přes nízkolatenční
   síťový audio přenos (např. RTP/UDP stream, Icecast/Ogg, nebo Mumble klient
   na Pi + desktop klient na Windows).
3. **Metadata rovina**: strukturované události z dekodéru (talkgroup/ID,
   volací značka, čas, kvalita signálu) posílané jako JSON přes
   WebSocket/MQTT/REST pro zobrazení a logování ve Windows aplikaci.

Toto rozdělení je doporučení pro budoucí návrh, nikoliv součást tohoto
rešeršního výstupu.

## 6. Omezení a rizika

- **Jeden kanál na jeden dongle**: RTL-SDR v3 realisticky dekóduje jeden
  konvenční kanál najednou. Simultánní příjem více frekvencí/módů vyžaduje
  více dongle a odpovídající počet paralelních GNU Radio + DSD-FME instancí;
  Pi 5 (4× Cortex-A76 @ 2.4 GHz) by měl zvládnout řádově 2–4 paralelní řetězce,
  ale to je potřeba změřit, ne jen předpokládat.
- **Trunking není v rozsahu**: pokud by cílová DMR síť byla trunkovaná
  (Tier III / Capacity+/Cap Max s řídicím kanálem), DSD-FME sledování trunku
  neřeší plnohodnotně. Pro běžný amatérský provoz (jednotlivé repeatery,
  konvenční kanály) toto omezení typicky nevadí, ale je potřeba to potvrdit
  podle konkrétní cílové sítě.
- **Kvalita a zralost podpory jednotlivých módů v DSD-FME není stejná**: DMR
  podpora je nejvyzrálejší a nejrozšířenější; D-STAR a YSF/C4FM podpora je ve
  forku novější a je nutné ji ověřit na konkrétní verzi před závazným
  plánováním rozsahu prací.
- **Softwarový AMBE je právně a kvalitativně kompromis**: jde o
  community-vyvinutou náhradu patentovaného vokodéru, běžně používanou v
  hobby SDR komunitě (DSD+, DSDcc, OP25), ale hlasová kvalita je znatelně nižší
  než u originálního HW AMBE čipu. Pro monitorování provozu je to přijatelné,
  pro "hezký poslech" ne.
- **RFI z Raspberry Pi 5**: Pi 5 je vůči Pi 4 popisován jako zdroj vyššího
  vysokofrekvenčního rušení z USB3/PCIe sběrnice, což může zhoršit citlivost
  RTL-SDR, pokud je dongle fyzicky blízko desky nebo bez stínění.
- **Žádné nativní, udržované GNU Radio OOT moduly** pro tyto čtyři módy –
  proto je architektura nutně hybridní (GNU Radio front-end + externí
  dekodér), ne čistě "GNU Radio blocks only".
- **Legální/etický kontext**: monitorování amatérského digitálního hlasového
  provozu je v ČR/EU pro tyto radioamatérské módy standardně legální, ale
  pokud by se cíl v budoucnu rozšířil na komerční/neamatérské DMR sítě, je
  potřeba to posoudit zvlášť (mimo rozsah tohoto dokumentu).

## 7. Doporučená cesta (kroky ověření)

1. PoC příjem: GNU Radio + gr-osmosdr + librtlsdr na Pi 5, ověřit stabilní
   příjem z RTL-SDR v3 (např. jednoduchý FM demod flowgraph nebo `gqrx`).
2. Postavit flowgraph pro kanalizaci a FM demodulaci s výstupem
   diskriminátorového audia (48 kHz PCM) do named pipe/UDP.
3. Zkompilovat DSD-FME pro ARM64, napojit na výstup flowgraphu, ověřit
   dekódování v pořadí DMR → D-STAR → YSF/C4FM (od nejzralejší podpory k
   nejméně zralé).
4. Vyhodnotit kvalitu SW AMBE audia a rozhodnout, zda je HW AMBE dongle
   potřeba.
5. Teprve poté navrhnout a implementovat síťovou/servisní vrstvu pro
   vzdálenou architekturu (řídicí API, audio streaming, metadata) – samostatný
   navazující rozsah.

## 8. Konkrétní návrh navazujících zadání do AI Project Manageru

Podle `inbox_planning_recipe` (dependency-ordered atomické karty, každá pro
jeden cílový projekt/repozitář a s jedním nezávisle finalizovatelným
výsledkem) doporučuji rozdělit implementaci na samostatný nový projekt/repozitář
(např. `sdr-remote-receiver`), oddělený od AI Project Manageru i Station
Agenta, protože jde o samostatný HW/SW celek s vlastním testováním na reálném
Pi 5 hardwaru. Navrhované pořadí karet:

1. **Bootstrap projektu a PoC příjmu**: založit repozitář, GNU Radio +
   gr-osmosdr + RTL-SDR driver na Pi 5, minimální flowgraph ověřující příjem
   a FM demodulaci na konkrétní testovací frekvenci. Závislosti: žádné.
2. **Integrace DSD-FME a dekódování DMR**: napojit výstup flowgraphu na
   DSD-FME, ověřit reálné dekódování DMR provozu na známém repeateru.
   Závislost: karta 1.
3. **Rozšíření o D-STAR a YSF/C4FM dekódování**: rozšířit ověření na zbylé
   dva módy, zdokumentovat rozdíly v kvalitě/nastavení. Závislost: karta 2.
4. **Řídicí a stavové API na Pi** (frekvence/mód/start-stop/status) jako
   základ pro budoucí Windows klienta. Závislost: karta 2 (nemusí čekat na
   kartu 3).
5. **Přenos audia a metadat na Windows PC** (audio stream + metadata
   události). Závislost: karta 4.
6. **Zabezpečený vzdálený přístup a provozní odolnost** (VPN/SSH, systemd
   autostart/restart, monitoring teploty/throttlingu Pi 5 pod zátěží).
   Závislost: karta 4.

Windows aplikace samotná (UI pro ovládání a poslech) je až navazující, ještě
další rozsah po kartě 5, mimo tento dokument.

## 9. Zdroje myšlenkového postupu (orientační, ne závazné citace)

- GNU Radio + gr-osmosdr jako standardní SDR front-end pro RTL-SDR zařízení.
- DSD-FME (aktivní fork konceptu DSD/DSDcc) jako nejširší open-source
  multi-mode dekodér digitálního hlasu dostupný pro Linux/ARM.
- Známá omezení RTL-SDR v3 (jednokanálový příjem, 8bit ADC, ~3.2 MHz reálná
  šířka pásma) a Raspberry Pi 5 (RFI z USB3/PCIe, nutnost aktivního chlazení
  při trvalé zátěži).

Před zahájením implementace doporučuji u karty 1 ověřit aktuální stav a verzi
DSD-FME (D-STAR/YSF podpora se v projektu v čase mění) a případně rešerši
krátce zopakovat, pokud mezi tímto dokumentem a zahájením implementace uplyne
delší doba.
