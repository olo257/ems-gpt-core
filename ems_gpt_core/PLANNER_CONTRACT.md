# EMS-GPT Core — kanoniczny kontrakt RCE, planera i SOC

Status: obowiązujący; uaktualniono dla wersji 0.40.37. Ten dokument jest źródłem prawdy dla implementacji,
testów, diagnostyki i odbioru produkcyjnego. Zmiana sprzeczna z kontraktem nie
może zostać scalona bez jawnej aktualizacji dokumentu i testów regresyjnych.

## 1. Jednostka planowania

- Jednostką jest slot 15-minutowy w `ems_gpt_slots`.
- Plan obejmuje wszystkie ciągłe, niezamknięte sloty z dostępną ceną RCE, a nie
  arbitralną liczbę sąsiednich slotów.
- Granicą planu jest ostatni ciągły niezamknięty slot z zatwierdzoną ceną RCE.
  Jeżeli dostępna jest część bieżącej doby i cała następna doba, oba odcinki
  tworzą jeden plan. Replan nie może kończyć się na granicy dnia ani na 96
  rekordach.
- Dla każdego slotu tego horyzontu muszą istnieć prognozy zużycia i PV. Brak
  prognozy nie oznacza zera: materializacja uzupełnia zużycie profilem slotu,
  a przy krótkiej historii konserwatywną średnią z ostatnich trzech dób.
  Pozostały `NULL` odrzuca cały przebieg.
- Przejście miesiąca nie może wyzerować prognozy PV. Przy braku kompletnego
  profilu danego miesiąca używany jest najbliższy kompletny profil historyczny,
  z cyklicznym dystansem miesięcy (grudzień sąsiaduje ze styczniem).
- Chwilowy brak źródła PV zachowuje kompletne wartości SQL dla tych samych
  slotów. Braki można odtworzyć z ostatniej poprawnej migawki opublikowanego,
  zaakceptowanego planu; nie wolno kopiować prognoz innej doby ani wykonania.
  Wykorzystanie zapisu SQL i miesiąc wybranego profilu są jawne w audycie.
- Każdy przebieg pracuje na jednej migawce danych wejściowych i publikuje cały
  zaakceptowany plan atomowo. Plan częściowy nie może zastąpić ostatniego
  poprawnego planu.
- Aktualny, rozpoczęty slot nie jest ponownie planowany. Pierwszym zmienianym
  rekordem jest następny nieotwarty slot.
- Każdy replan odczytuje bieżący SOC ponownie z `sensor.inverter_battery` i
  używa go jako stanu początkowego pierwszego nieotwartego slotu. Nie wolno
  używać SOC z poprzedniego planu ani historycznego zamknięcia doby.

## 2. Import RCE

Import i planowanie są dwoma niezależnymi etapami.

1. Pobrać ceny dla wskazanego dnia.
2. Zweryfikować oczekiwaną liczbę ciągłych slotów i komplet obu cen.
3. Wyznaczyć okna BUY/SELL na pełnym, dostępnym horyzoncie cen i zatwierdzić
   je razem z cenami jako wejście RCE. `ingestion_service.py` jest jedynym
   właścicielem zapisów `buy_window`, `sale_window` i `market_window`.
4. Dopiero kompletny zestaw zatwierdzić trwałym zdarzeniem RCE.
5. Po zatwierdzeniu uruchomić pakiet planowania na pełnym ciągłym horyzoncie.

Standardowy dzień ma 96 slotów. Dzień zmiany czasu ma liczbę wynikającą z osi
czasu. `rows == expected` jest warunkiem zatwierdzenia; stała liczba 96 nie
może być użyta jako uniwersalny warunek.

Błąd prognozy lub planera po zatwierdzeniu cen:

- nie zmienia importu RCE na `ERROR`;
- nie powoduje ponownego pobierania ani częściowego nadpisania cen;
- ma własny status i zdarzenie diagnostyczne;
- pozostawia ostatni poprawny plan, a jeśli jest on nieaktualny — blokuje nowe
  komendy wykonawcze;
- odrzucony kandydat pozostaje zapisany przez 90 dni w oddzielnej historii
  prób planera i jest dostępny w API oraz panelu tylko do odczytu; nie może
  zastąpić zaakceptowanego planu ani zasilać PPD lub wykonawcy.

## 3. Kolejność przebiegów planera

Kontrakt wymaganej trajektorii SOC jest wyznaczany bez udziału wyniku
optymalizacji ekonomicznej: najpierw obciążenie bazowe i HP, prognoza PV,
sprawności, rezerwa oraz dopuszczone przez RCE okna BUY wyznaczają wymagane SOC
i terminy uzupełnienia. Dopiero potem optymalizator wybiera przepływy i
ekonomiczne wykorzystanie zakupów w granicach tego kontraktu. Przebieg
ekonomiczny nie może obniżać wymaganych rezerw ani celu zamknięcia doby.
Opcjonalny sufit ładowania jest oddzielony od obowiązkowego wymogu i terminu:
tańszy wcześniejszy BUY może zastąpić droższy późniejszy BUY, do fizycznej
pojemności baterii i limitów mocy. Ilość zakupu wybiera pełny bilans horyzontu;
podniesienie opcjonalnego sufitu nie wymusza zakupu do 100%.
Wykonawca otrzymuje najwyższy target rzeczywiście wybranego ciągu zakupów już
od pierwszego slotu tego ciągu. Wymagany SOC na końcu okna jest sprawdzany
osobno; opcjonalny szczyt wcześniejszego ładowania nie jest nowym wymogiem SOC.
Historyczny punkt odniesienia końca doby wynosi 40–45% SOC i chroni przed jego
zaniżaniem. Jeśli okno BUY kolejnej lokalnej doby przypada przed codziennym
startem CWU o 06:00, poprzedni cel dobowy jest wymagany po ostatnim ciągłym
oknie BUY przed 06:00, a nie przed północą. Wcześniejszy SOC nadal wynika z bilansu wstecznego
zużycia, PV, sprawności, rezerwy oraz fizycznej dostępności ładowania BUY, więc
plan utrzymuje energię do najbliższego PV/BUY i nie wymusza drogiego zakupu
przed tańszym oknem.
Każde przyszłe okno doby (w tym jutro po 14:00) jest uwzględniane wyłącznie,
gdy kompletne ceny RCE i prognozy znajdują się w ciągłym horyzoncie planu.
Niewykonalność celu jest raportowana jako niedobór, bez cichego obniżenia celu
do rezerwy.
Recovery osiągalności może obniżyć wyłącznie pośrednie ograniczenia trajektorii;
nie może zastąpić targetu zamknięcia doby ani sufitu BUY. Przy niewykonalnym
zamknięciu plan zachowuje oryginalny target w audycie, wyłącza sprzedaż baterii
i raportuje osiągalny SOC oraz różnicę do celu. Gdy nieosiągalny jest pośredni
most SOC, wymóg wykonawczy może spaść wyłącznie do wyliczonej osiągalności i
nie poniżej rezerwy technicznej; niedobór pozostaje widoczny w audycie.

W prognozie bilansu obciążenie `HP_DHW` jest już zawarte w zużyciu bazowym.
Dodatkowo uwzględniany jest zaplanowany cykl `HP_HEAT_DHW`; nie należy
doliczać ponownie profilu `HP_DHW`.

Każdy pakiet wykonuje etapy w tej kolejności:

1. `RCE_RAW` — skopiowanie zatwierdzonych cen do tabeli roboczej.
2. `WINDOWS` — walidacja okien BUY/SELL zapisanych przez import RCE.
   Planer nie wyznacza ich ponownie ani nie zapisuje do `ems_gpt_slots`;
   każdy slot musi mieć dokładnie jeden stan: `BUY`, `SELL` albo
   `NEUTRAL`, bez nakładania BUY i SELL.
3. `FORECAST_LOAD` — prognoza zużycia bazowego oraz sterowalnych odbiorników.
4. `FORECAST_PV` — prognoza PV1, PV2 i sumy, z oddzielnymi korektami.
5. `ENERGY_BALANCE` — bilans fizyczny każdego slotu.
6. `BATTERY` — możliwe ładowanie i rozładowanie z ograniczeniami mocy,
   pojemności, sprawności i rezerwy technicznej.
7. `DEFICIT` — niedobór energii do następnego wykonalnego uzupełnienia PV lub
   BUY oraz wybór ekonomicznych slotów zakupu.
8. `SOC` — wynikowe `soc_target`, `soc_floor`, SOC przed i po slocie.
9. `SURPLUS` — PV pokrywa autokonsumpcję, następnie ładuje baterię do
   fizycznego maksimum 100%, niezależnie od `soc_target`. Target ogranicza
   wyłącznie import z sieci. Po osiągnięciu 100% SOC PPD może dopuścić CWU,
   potem EV; pozostała nadwyżka jest sprzedawana tylko przy cenie > 0 PLN/kWh,
   a ograniczenie produkcji jest ostatnią możliwością.
10. `PLAN DECISIONS` — planer publikuje trajektorię SOC, fizyczne przepływy,
    ilości importu/eksportu baterii i polityki użyte w bilansie.
11. `PPD` — osobny `ppd_service.py` odczytuje aktualny plan, wylicza wolny PV
    z prognozy, obciążenia bazowego i HP oraz PV→baterii, zapisuje końcowy
    podział CWU/EV/eksport/ograniczenie i tworzy okna procesów. Nie zwraca
    żadnego wejścia do targetu.
12. `VALIDATE` — kontrola całego horyzontu; dopiero potem atomowa publikacja.

Planer może wykonywać wiele przebiegów po tej samej tabeli roboczej, ale każdy
etap modyfikuje wyłącznie pola należące do niego. Planer nie zapisuje końcowego
podziału PV ani rekomendacji — te pola należą wyłącznie do PPD. Okna cenowe pozostają
niezmiennym wejściem z importu RCE przez wszystkie przebiegi planera. Jeżeli walidacja jednego slotu
nie przejdzie, kolejny przebieg może zmienić wybór przyszłego BUY/SELL lub
alokację energii. Nie wolno naprawiać błędu przez kopiowanie `soc_floor` do
`soc_target`, zakup poza BUY ani sprzedaż nadwyżki potrzebnej baterii.

### 3.1. Walidacja fallbacku kontraktu SOC

Końcowy walidator używa wymagania SOC faktycznie egzekwowanego przez ostatni
zakończony przebieg optymalizatora. Gdy fallback usuwa niewykonalny, opcjonalny
kontrakt, walidator może zejść tylko do niezależnie obliczonego minimum
bezpieczeństwa i aktywnego celu dobowego. Rezerwa techniczna, zakaz zakupu poza
BUY, limity importu oraz zakaz sprzedaży poza SELL pozostają twarde.

## 4. Bilans energii slotu

### Zakup z sieci wyłącznie do ładowania baterii

- Planer może przypisać import z sieci wyłącznie do `grid_charge_kwh` w
  dozwolonym oknie BUY.
- `grid_load_kwh` nie może pokrywać prognozowanego zużycia domu ani w BUY, ani
  poza BUY. Dotyczy to również aktywnego mostu SOC i slotów z równoczesnym
  ładowaniem baterii.
- Jeżeli PV i bateria nie pokrywają zużycia bez importu na dom, kandydat planu
  jest niewykonalny; planer nie może ukrywać niedoboru pod przepływem sieciowym.
- Pozostaje wyłącznie techniczna resztka do 0,05 kWh, która wynika z kwantyzacji
  SOC. Nie może ona otworzyć okna BUY ani uzasadnić ładowania.
- Import baterii wraz z innymi przepływami nadal musi spełniać fizyczny limit
  sieci, moc baterii, pojemność, SOC i ograniczenia sprawności.

Dla każdego slotu musi zachodzić, z jedną konwencją punktu pomiarowego:

`PV + rozładowanie baterii + import = zużycie + ładowanie baterii + eksport`

Eksport należy rozdzielić na:

- celową sprzedaż energii z baterii;
- sprzedaż nadwyżki PV.

Sprzedaż nadwyżki PV jest pozabilansowa dla wyznaczania przyszłego deficytu
baterii: nie tworzy długu energetycznego i nie zwiększa zakupu. Nadal pozostaje
częścią fizycznego bilansu slotu i wyniku finansowego.

W jednym slocie bateria nie może być jednocześnie ładowana i rozładowywana.
Ładowanie baterii podlega limitowi `battery_max_power_kw` (domyślnie 5 kW, czyli
1,25 kWh na slot 15-minutowy). Łączny pobór domu i ładowania baterii podlega
`grid_import_limit_kw` (domyślnie 12 kW, czyli 3 kWh na slot). Każdy przepływ
musi też spełniać limit czasu 0,25 h, dostępną pojemność, SOC, sprawność i progi
techniczne.

Optymalizator używa dyskretnych stanów SOC. Zmiana energii wewnętrznej baterii
w każdym slocie musi być równa różnicy SOC przed i po slocie pomnożonej przez
pojemność. Energia PV mniejsza od kwantu stanu nie może być raportowana jako
ładowanie baterii bez odpowiadającej jej zmiany SOC; pozostaje jawnie
przypisana do eksportu albo ograniczenia produkcji. Walidator odrzuca plan,
jeśli ilości przepływów i przejście SOC nie są zgodne.

## 5. `soc_target`

`soc_required_pct` jest wymaganym zapotrzebowaniem energetycznym do realnego,
wykonalnego źródła uzupełnienia: prognozowanego PV albo dopuszczonego okna BUY.
`soc_target` / `soc_charge_target_pct` jest sufitem wykonawczego ładowania z sieci.
Wymagane SOC i termin końca okna są niezależne od opcjonalnego zwiększenia
zakupu w tańszym wcześniejszym BUY w celu zastąpienia droższego późniejszego BUY.

Opcjonalny sufit jest ograniczony bilansem przyszłego zapotrzebowania bez
zakładania przyszłych zakupów, z uwzględnieniem użytecznego PV, sprawności,
wymaganych rezerw i pojemności baterii. Sama tania lub ujemna cena nie tworzy
zapotrzebowania na dodatkowy zapas. Pełny bilans horyzontu wybiera rzeczywistą
ilość zakupu. Wykonawca otrzymuje wspólny najwyższy target wybranego ciągu
zakupów od pierwszego slotu; przerwa w zakupach rozpoczyna nowy ciąg.

`soc_target` ogranicza ładowanie z sieci. Nie ogranicza ładowania z PV, które
może uzupełniać baterię do fizycznego maksimum 100%. Dopiero po osiągnięciu
100% SOC PPD może dopuścić CWU/EV na podstawie prognozowanej nadwyżki, a
executor ponownie weryfikuje rzeczywisty SOC i moc PV. Jeżeli odbiornik działa,
jego pobór naturalnie zmniejsza eksport albo ograniczenie produkcji.

Techniczne minimum SOC jest granicą awaryjną, nie celem operacyjnym. Planowana
ścieżka nie może celowo sprowadzać baterii do tej wartości ani uzależniać
wykonalności od idealnego rozpoczęcia prognozowanego PV. Target zawiera zapas
wynikający z konserwatywnej korekty zużycia i PV.

Historyczny punkt odniesienia SOC na zamknięcie doby wynosi 40–45%.
Ważona średnia rzeczywistych zamknięć 7/14/28 dni jest ograniczana do tego
pasma, aby błędne historyczne zamknięcia nie zaniżały zapasu. Jeśli okno BUY
nowej lokalnej doby przypada przed codziennym startem CWU o 06:00, cel
poprzedniej doby jest wymagany po ostatnim ciągłym oknie BUY przed 06:00,
a nie przed północą. Do tego czasu niezależny bilans bezpieczeństwa utrzymuje
zapas na prognozowane zużycie, w tym CWU, do najbliższego PV/BUY; planer nie
wymusza wcześniejszego zakupu przed tańszym oknem.

Niezależny bilans bezpieczeństwa wymaga co najmniej 15% (lub wyższego minimum
konfiguracji) plus `soc_replenishment_buffer_pct` (domyślnie 2 p.p.) w każdym
slocie oraz energii na przyszły deficyt domu i HP. Uwzględnia sprawności i
`forecast_uncertainty_weight`. Wymaganie końca doby jest maksimum historycznego punktu odniesienia
i rezerwy wynikającej z bilansu nocnego w dostępnym horyzoncie.

BUY odciąża wcześniejszy bilans wyłącznie o fizycznie dostępną moc i czas
ładowania poza SELL. PV odciąża go tylko o nadwyżkę po odbiorach, ograniczoną
mocą ładowania. Początek okna bez wystarczającej energii nie zeruje deficytu.
Optymalizator wybiera ekonomicznie uzasadnione zakupy spełniające ten bilans;
końcowe kontrakty SOC odejmują wyłącznie faktycznie przydzielone zakupy.

Fallback najpierw usuwa SELL_BAT; może obniżyć niewykonalne wymaganie
historyczne końca doby, także na końcu pełnego horyzontu, ale nie może
obniżyć niezależnej rezerwy bezpieczeństwa. Jeżeli po obniżeniu celu
historycznego do wymagania bezpieczeństwa ten stan nadal jest nieosiągalny,
planner odrzuca przebieg i zachowuje ostatni poprawny plan.
Niewykonalny bilans bezpieczeństwa powoduje jawny błąd i odrzucenie wariantu.
Brak cen/prognoz poza horyzontem nie stanowi potwierdzenia bezpieczeństwa
kolejnej nocy. Po rozszerzeniu horyzontu bilans musi być przeliczony.

Target obejmuje:

- prognozowane zużycie do granicy uzupełnienia;
- zaplanowane sterowalne odbiorniki;
- straty ładowania i rozładowania;
- rezerwę techniczną i niepewność prognozy;
- przyszłe PV, które faktycznie może trafić do baterii;
- energię potrzebną do wykonania zaakceptowanej przyszłej sprzedaży baterii.

Target nie jest progiem sprzedaży i nie może być kopiowany z floor. Powinien
być zwykle wyższy od floor i nigdy nie przekracza 100%. Wymaganie mostu ponad
pojemność jest fizycznie niemożliwe i nie może zostać przekształcone w cel
zakupu do 100%. Planer zachowuje techniczną rezerwę, wyłącza sprzedaż baterii
i jawnie raportuje niewykonalność, gdy bilans zużycia wymagałby importu na dom.

Zakup energii służy wyłącznie ładowaniu baterii. Planer nie może kupować energii
na bieżące zużycie ani w celu zachowania baterii do późniejszej sprzedaży.

## 6. `soc_floor`

`soc_floor` jest wyłącznie dolną granicą celowej sprzedaży z baterii.

- Nie ogranicza naturalnego rozładowania baterii na zużycie domu; tu obowiązuje
  rezerwa techniczna BMS/konfiguracji.
- W slocie bez `SELL_BAT` ma wartość rezerwy technicznej i nie niesie targetu.
- W slocie `SELL_BAT` jest wynikiem zaakceptowanej ilości sprzedaży i końcowego
  SOC tego slotu.
- Celowa sprzedaż nie może zakończyć slotu poniżej `soc_floor` ani naruszyć
  energii zarezerwowanej przez `soc_target`.
- Dla aktywnego programu TOU floor sprzedaży obejmuje jego bazowy próg SOC oraz
  prognozowaną energię odbiorów do wejścia następnego, niższego programu. Chroni
  to przed importem domu bezpośrednio po zakończeniu sprzedaży.
- Tylko programy 5 i 6 mogą czasowo zejść poniżej bazowego progu: ten sam plan
  musi zawierać ilościowy BUY w skonfigurowanym krótkim horyzoncie i zachować
  wymagany SOC zamknięcia doby. Brak któregokolwiek warunku przywraca baseline.

## 7. Priorytet wykorzystania PV

Prognozowane i rzeczywiste PV jest alokowane w kolejności:

1. autokonsumpcja odbiorników;
2. ładowanie baterii PV do fizycznego maksimum 100%;
3. po osiągnięciu 100% SOC: CWU, następnie EV;
4. sprzedaż pozostałej nadwyżki PV wyłącznie przy cenie > 0 PLN/kWh;
5. ograniczenie produkcji jako ostatnia możliwość.

`soc_target` ogranicza wyłącznie energię ładowania z sieci i nie może
zablokować ładowania PV ponad target. CWU/EV wymagają zatwierdzenia PPD,
osiągniętego fizycznego SOC 100% i świeżej, rzeczywistej nadwyżki. Pozostała nadwyżka PV
jest sprzedawana tylko przy dodatniej cenie; cena nie zmienia kolejności
CWU → EV → sprzedaż, a przy cenie niedodatniej nadwyżkę należy ograniczyć.

Okno PPD jest wyłącznie pozwoleniem. W trybie AUTO wykonawca ponownie sprawdza
świeżą telemetrię. Poniżej 100% SOC blokuje odbiory elastyczne; po osiągnięciu
100% liczy nadwyżkę dostępną dla CWU/EV jako `PV - load`. Moc już pracujących CWU/EV jest dodawana
z powrotem wyłącznie na potrzeby histerezy. Odbiory te nie wracają do `load`,
`soc_target`, `soc_required` ani planowanej trajektorii SOC.

## 8. Ekonomiczne BUY i SELL

- Okna są wynikiem cen całego dostępnego horyzontu, sprawności, kosztu degradacji
  i ograniczeń fizycznych; nie są stałymi godzinami.
- BUY jest wybierany spośród najtańszych wykonalnych slotów przed terminem
  targetu. Jeśli jeden slot nie wystarcza, zakup rozszerza się na kolejne
  najtańsze sloty.
- Nie wolno kupować w SELL ani kupować tylko dlatego, że bieżący SOC jest niski,
  jeżeli PV przed terminem bezpiecznie pokryje deficyt.
- SELL_BAT jest dopuszczalny tylko przy cenie sprzedaży większej od zera,
  dla energii ponad wszystkie przyszłe zobowiązania i tylko gdy pełny cykl ma
  dodatni wynik netto. Warunek ceny jest sprawdzany podczas wyznaczania okna,
  optymalizacji oraz końcowej walidacji publikowanego przepływu.
- Przed publikacją SELL_BAT planer porównuje pełny plan z wariantem bez sprzedaży
  przy tej samej prognozie i wymaganym SOC. Różnica obejmuje faktyczne zakupy
  wybrane przez oba warianty, straty baterii, koszt energii pozostawionej na
  końcu horyzontu oraz import dla domu. Sprzedaż odpada, jeżeli obniża wynik
  netto lub zwiększa import na potrzeby domu ponad próg techniczny.
- Nie wolno doprowadzić sprzedażą do zakupu w droższym oknie, jeśli tańszy,
  wcześniejszy zakup lub rezygnacja z części sprzedaży daje lepszy wynik.
- Jeżeli wcześniejszy BUY zajmuje pojemność, a najbliższa nadwyżka PV byłaby
  eksportowana po cenie niższej od kosztu zakupu, planer musi porównać pełny
  wariant standardowy z wariantem PV-first. Przesunąć wolno wyłącznie energię
  ponad minimum potrzebne do bezpiecznego dotarcia do PV; publikowany jest
  wariant z lepszym wynikiem netto przy tym samym terminalnym SOC.

## 9. Replan

Replan używa ostatniego kompletnego, zatwierdzonego zestawu RCE i najświeższych
prognoz oraz SOC. Nie modyfikuje rozpoczętego ani zamkniętych slotów.

Replan jest wymagany:

- bezpośrednio po kompletnym imporcie RCE;
- po otwarciu każdego nowego slotu, jeżeli zmiana SOC/prognozy wpływa na
  wykonalność planu;
- po restarcie, gdy ostatni plan jest nieobecny, nieciągły albo starszy od
  zatwierdzonych wejść;
- po istotnej zmianie konfiguracji planera.

Przebiegi ciężkie są serializowane. Planowanie musi zakończyć się przed otwarciem
następnego slotu; przekroczenie budżetu odrzuca nowy plan. Kolejny replan może
rozpocząć się dopiero po zwolnieniu blokady. Porażka replanu nie usuwa ostatniego
poprawnego planu, ale plan nieaktualny względem aktywnego slotu lub wejść nie może
generować nowych komend.

## 10. Warunki publikacji i bezpieczeństwa

Publikacja jest dozwolona wyłącznie, gdy:

- horyzont jest ciągły, a ceny kompletne;
- liczba opublikowanych wierszy jest równa liczbie wszystkich ciągłych,
  niezamkniętych slotów wejściowych aż do końca dostępnego RCE;
- prognoza zużycia nie ma `NULL` w żadnym slocie horyzontu;
- każdy slot przechodzi bilans energii z tolerancją numeryczną;
- SOC i wszystkie przepływy są wykonalne;
- BUY/SELL/NEUTRAL oraz polityki PPD są jednoznaczne;
- `soc_target` i `soc_floor` zachowują swoje odrębne definicje;
- nie ma jednoczesnego ładowania/rozładowania ani BUY/SELL;
- nadwyżka PV jest liczona dopiero po potrzebach baterii;
- cały pakiet mieści się w budżecie czasu.

Wykonawca pozostaje domyślnie wyłączony. Publikacja planu nie jest zgodą na
sterowanie. Włączenie wykonawcy wymaga osobnej decyzji operatorskiej oraz świeżej
telemetrii, aktualnego zaakceptowanego planu i spełnionych bram bezpieczeństwa.

## 11. Kontrakt pompy ciepła

- Każdy przebieg inicjuje `heat_pump_window=0`; nie wolno kopiować wartości z
  poprzedniego opublikowanego planu.
- Próg pochodzi wyłącznie z konfiguracji panelu
  `night_heating_threshold_c`. Core nie oczekuje helpera HA z progiem.
- Kwalifikacja wymaga co najmniej 3 rzeczywistych zapisów temperatury ogrodu
  z okresu 00:00–06:00 i minimum ściśle niższego od progu. Mniejsza liczba
  zapisów oraz wartość równa progowi lub wyższa blokują ogrzewanie. Prognoza
  `weather.dom` nie jest źródłem tej decyzji.
- Okno zaczyna się najwcześniej o 07:00 po porannym `SELL`, a kończy
  najpóźniej o 19:00 lub przed wieczornym `SELL`. Reguła jest taka sama w dni
  robocze i weekend.
- `BUY` nie wyznacza granic okna HP. Żaden slot `SELL` nie może mieć
  automatycznego `HP_HEAT_DHW=ON`.
- Planer nie odczytuje PPD ani override'ów. `heat_pump_window` zawsze opisuje
  rekomendowany przebieg użyty w bilansie i target.
- `AUTO`, `FORCE_ON` i `FORCE_OFF` należą do warstwy PPD/executora. Zmieniają
  decyzję efektywną, ale nie przepisują rekomendacji planera.
- Dla minionych slotów dnia planer zakłada wykonanie własnego opublikowanego
  `heat_pump_window`; rzeczywiste odstępstwo pozostaje w analityce wykonania.
- Miniony slot bez opublikowanego planu lub z `NULL` w `heat_pump_window`
  nie daje zaliczenia ogrzewania. Planowanie trwa dalej, a brak jest zapisany
  w audycie `hp_elapsed_plan_history_missing`. Nie zmienia to historii w bazie
  ani ścisłej walidacji bieżących flag BUY/SELL/HP.
- Energia HP jest dodawana do bilansu i targetu wyłącznie dla zakwalifikowanego
  profilu. HP może zwiększyć potrzebne ładowanie baterii, ale samo nie tworzy
  okna BUY.

## 12. Kontrakt granicy PPD

- `BATTERY_IMPORT`, `SELL_BAT` i `HP_HEAT_DHW` są planowane ilościowo
  wyłącznie przez planer. PPD nie posiada drugiej implementacji ekonomiki,
  okien ani targetu dla tych procesów.
- PPD kopiuje ich zamrożone decyzje do wersjonowanej macierzy procesów.
- PPD publikuje `SELL_PV` jako osobny proces. Pole decyzji opisuje politykę
  `ALLOWED/BLOCKED`, a `eligible` opisuje planowane `ON/OFF`. Cena mniejsza
  lub równa zero daje `BLOCKED + OFF`; dodatnia cena bez nadwyżki daje
  `ALLOWED + OFF`. Proces nie uruchamia skryptu sprzedaży baterii.
- `PV_CWU` i `PV_EV` są jedynymi procesami, których okna PPD może wyliczyć po
  publikacji planu; nie zmieniają one targetu. Korzystają ze wspólnego okna
  dopuszczenia od istotnej dodatniej nadwyżki planu po osiągnięciu 100% SOC;
  osobne progi mocy są stosowane dopiero przez runtime guard.
- Ich wykonanie w `AUTO` wymaga świeżej telemetrii, SOC co najmniej 100%
  i nadwyżki `PV - load`.
  CWU ma pierwszeństwo przed EV, a brak danych lub utrata nadwyżki wymusza OFF.
- Executor nakłada override na decyzję planowaną i zapisuje osobno stan
  planowany, efektywny oraz obserwowany.
- Żadna tabela wynikowa PPD ani executora nie może być wejściem planera.

## 13. Publikacja cen do Home Assistant

Zakup i sprzedaż pochodzą zawsze z tego samego aktywnego slotu i są publikowane
wyłącznie do istniejących helperów:

- `input_number.ems_gpt_cena_zakupu_biezaca`;
- `input_number.ems_gpt_cena_sprzedazy_biezaca`.

Core nie tworzy helperów ani sensorów cenowych. Brak jednej ceny blokuje oba
zapisy. Błąd sekwencyjnego zapisu HA jest jawnie raportowany i ponawiany; nie
wolno podstawiać zera ani publikować wartości pochodzących z różnych slotów.

## 13. Minimalne testy regresyjne

Każda zmiana planera musi obejmować co najmniej:

- kompletny i częściowy import RCE, w tym dzień zmiany czasu;
- błąd planera po poprawnym imporcie bez utraty statusu RCE;
- restart po imporcie i odtworzenie planu;
- replan po zmianie SOC oraz prognoz PV/zużycia;
- brak zakupu w SELL i brak zwykłego zakupu dla domu;
- zakup rozłożony na wiele slotów przy limicie 5 kW;
- ładowanie PV ponad `soc_target` aż do fizycznego maksimum 100%, przy czym
  import z sieci kończy się na target; po nim obowiązuje kolejność CWU → EV →
  sprzedaż PV przy cenie dodatniej → ograniczenie produkcji;
- sprzedaż baterii bez naruszenia targetu i floor;
- fizyczny bilans każdego slotu;
- odrzucenie całego planu przy pojedynczym niewykonalnym slocie;
- zachowanie ostatniego poprawnego planu po błędzie replanu;
- zakończenie obliczeń przed granicą kolejnego slotu;
- wyzerowanie odziedziczonego `heat_pump_window` na początku przebiegu;
- HP wyłączone przy niepełnej prognozie i temperaturze równej progowi;
- brak HP przed 07:00, po 19:00 i w każdym slocie `SELL`;
- publikację cen jednego slotu wyłącznie do dwóch kanonicznych helperów.


## 10. Granica recovery i diagnostyki runtime

Jeżeli bieżący SOC jest już niższy od wymaganego mostu albo sam most
przekracza fizyczną pojemność, recovery może tymczasowo utrzymać wyłącznie
techniczną rezerwę do pierwszego przyszłego, dozwolonego BUY. Slot BUY/SELL
nigdy nie jest źródłem recovery. Sprzedaż baterii zostaje wyłączona, a
niewykonalny nadmiar jest raportowany; nie wolno zamieniać go w zakup do 100%.
Pomiar początkowy pozostaje niezmieniony, a każda osiągalna wymagana wartość
SOC jest sprawdzana na jednej wspólnej trajektorii.

Raport diagnostyczny musi uwzględniać bieżący stan modułów planera i PPD oraz
ich zatrzaśnięte błędy. Starszy opublikowany plan nie oznacza zdrowego systemu,
gdy planer jest zdegradowany lub błąd planera/PPD pozostaje zatrzaśnięty.

## 14. Doprecyzowanie SOC i fizycznego BUY — 12.10.2026 (0.40.16)

- Każdy replan zaczyna się od aktualnego, rzeczywiście zmierzonego SOC baterii; nie wolno podstawiać końcowego SOC poprzedniego planu jako stanu startowego.
- Cel SOC wynika z przyszłego bilansu obciążeń, prognoz PV i wymaganej rezerwy do kolejnej realnej możliwości uzupełnienia energii (PV albo BUY). Średnia historyczna 7/14/28 dni dotyczy celu końca doby, a nie stałego celu w każdym slocie.
- Najpierw wyznacza się wymaganą trajektorię SOC i deficyty energii, następnie fizycznie możliwe ilości ładowania w konkretnych slotach BUY. Samo oznaczenie okna BUY nie oznacza jeszcze zakupionej energii.
- Ładowanie baterii ma limit 5 kW, czyli maksymalnie 1,25 kWh na slot 15-minutowy, łącznie dla PV i sieci. Dopuszczalna moc importu z sieci 12 kW nie zwiększa limitu ładowania baterii.
- Zakup z sieci może ładować baterię tylko w BUY i nie ponad właściwy SOC target. Produkcja PV może ładować baterię ponad target, do 100% SOC.
- Przed publikacją walidacja sprawdza planowane kWh importu, sumę ładowania PV + sieć, ciągłość trajektorii SOC i osiągnięcie wymaganego celu przed jego terminem.
- Nie wolno po cichu obniżać wymaganego SOC ani usuwać terminów jego osiągnięcia tylko po to, aby zaakceptować niewykonalny plan. Niewykonalność musi być jawnie zgłoszona i nie może skutkować publikacją wadliwego kandydata.
- Warunek kompletności RCE dotyczy doby importowanej (zwykle 96 slotów), ale horyzont planera i jego widok obejmują wszystkie dostępne, ciągłe sloty bieżącej i następnej doby; limit 96 rekordów w UI nie jest kontraktem planowania.
- Zmiana konfiguracji planera, poza samym numerem wersji dodatku, wymaga osobnej wyraźnej zgody operatora. Zmiany wyłącznie prezentacyjne panelu nie mogą modyfikować obliczeń, wykonawcy ani PPD.
- Testy regresyjne kodu nie są równoznaczne z odbiorem produkcyjnym: po aktualizacji trzeba osobno potwierdzić wersję przebiegu planera w zapisanych slotach, okna BUY, planowane kWh zakupu i osiągalność SOC.


### Zakup tylko na ładowanie — 0.40.37

Przepływ `grid_charge_kwh` służy wyłącznie zwiększeniu SOC baterii w BUY.
Planowany `grid_load_kwh` powyżej progu technicznego jest zabroniony w każdym
slotcie, także gdy działa BUY lub wymagany jest most SOC. Jeżeli nie można
zbilansować obciążenia przez PV i rozładowanie baterii przy zachowaniu
ograniczeń SOC, planer zgłasza niewykonalność zamiast tworzyć plan z importem
dla domu.
