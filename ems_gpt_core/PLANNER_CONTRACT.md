# EMS-GPT Core — kanoniczny kontrakt RCE, planera i SOC

Status: obowiązujący. Ten dokument jest źródłem prawdy dla implementacji,
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
- Każdy przebieg pracuje na jednej migawce danych wejściowych i publikuje cały
  zaakceptowany plan atomowo. Plan częściowy nie może zastąpić ostatniego
  poprawnego planu.
- Aktualny, rozpoczęty slot nie jest ponownie planowany. Pierwszym zmienianym
  rekordem jest następny nieotwarty slot.

## 2. Import RCE

Import i planowanie są dwoma niezależnymi etapami.

1. Pobrać ceny dla wskazanego dnia.
2. Zweryfikować oczekiwaną liczbę ciągłych slotów i komplet obu cen.
3. Dopiero kompletny zestaw zatwierdzić trwałym zdarzeniem RCE.
4. Po zatwierdzeniu uruchomić pakiet planowania na pełnym ciągłym horyzoncie.

Standardowy dzień ma 96 slotów. Dzień zmiany czasu ma liczbę wynikającą z osi
czasu. `rows == expected` jest warunkiem zatwierdzenia; stała liczba 96 nie
może być użyta jako uniwersalny warunek.

Błąd prognozy lub planera po zatwierdzeniu cen:

- nie zmienia importu RCE na `ERROR`;
- nie powoduje ponownego pobierania ani częściowego nadpisania cen;
- ma własny status i zdarzenie diagnostyczne;
- pozostawia ostatni poprawny plan, a jeśli jest on nieaktualny — blokuje nowe
  komendy wykonawcze.

## 3. Kolejność przebiegów planera

Każdy pakiet wykonuje etapy w tej kolejności:

1. `RCE_RAW` — skopiowanie zatwierdzonych cen do tabeli roboczej.
2. `WINDOWS` — oznaczenie każdego slotu dokładnie jednym stanem `BUY`, `SELL`
   albo `NEUTRAL`; `BUY` i `SELL` nie mogą się nakładać.
3. `FORECAST_LOAD` — prognoza zużycia bazowego oraz sterowalnych odbiorników.
4. `FORECAST_PV` — prognoza PV1, PV2 i sumy, z oddzielnymi korektami.
5. `ENERGY_BALANCE` — bilans fizyczny każdego slotu.
6. `BATTERY` — możliwe ładowanie i rozładowanie z ograniczeniami mocy,
   pojemności, sprawności i rezerwy technicznej.
7. `DEFICIT` — niedobór energii do następnego wykonalnego uzupełnienia PV lub
   BUY oraz wybór ekonomicznych slotów zakupu.
8. `SOC` — wynikowe `soc_target`, `soc_floor`, SOC przed i po slocie.
9. `SURPLUS` — nadwyżka PV w kolejności: autokonsumpcja, ładowanie baterii do
   targetu, sprzedaż PV, a ograniczenie produkcji na końcu. Surowa elastyczna
   nadwyżka pozostaje dostępna dla PPD.
10. `PLAN DECISIONS` — planer zamraża rekomendowane przebiegi importu baterii,
    eksportu baterii i HP razem z ilościami użytymi w bilansie oraz target.
11. `PPD` — osobny `ppd_service.py` publikuje te trzy rekomendacje bez ich
    ponownego liczenia oraz tworzy ciągłe okna `PV_CWU` i `PV_EV` z zamrożonej
    nadwyżki; nie zwraca żadnego wejścia do targetu.
12. `VALIDATE` — kontrola całego horyzontu; dopiero potem atomowa publikacja.

Planer może wykonywać wiele przebiegów po tej samej tabeli roboczej, ale każdy
etap modyfikuje wyłącznie pola należące do niego. Jeżeli walidacja jednego slotu
nie przejdzie, kolejny przebieg może zmienić wybór przyszłego BUY/SELL lub
alokację energii. Nie wolno naprawiać błędu przez kopiowanie `soc_floor` do
`soc_target`, zakup poza BUY ani sprzedaż nadwyżki potrzebnej baterii.

## 4. Bilans energii slotu

### Import technicznie nieunikniony a decyzja BUY

Jeżeli po wykorzystaniu PV i całej energii baterii dostępnej ponad techniczną
rezerwę nadal pozostaje zapotrzebowanie domu, sieć fizycznie pokrywa ten
niedobór. Taki `grid_load_kwh` jest przepływem resztowym bilansu, a nie decyzją
ekonomiczną `BUY` i nie tworzy okna zakupu.

- `BUY` oznacza wyłącznie zaplanowane ładowanie baterii (`grid_charge_kwh > 0`);
- planer nie może dobrowolnie zasilać domu z sieci, gdy bateria może pokryć
  zużycie, z wyjątkiem jawnie ekonomicznej ochrony energii przed późniejszą
  sprzedażą;
- osiągnięcie technicznego minimum SOC nie może uczynić horyzontu
  niewykonalnym — niepokryty przez PV i baterię dom przechodzi na sieć;
- przepływ wymuszony nie podnosi `soc_target` i nie jest oznaczany jako zakup
  do baterii.

Dla każdego slotu musi zachodzić, z jedną konwencją punktu pomiarowego:

`PV + rozładowanie baterii + import = zużycie + ładowanie baterii + eksport`

Eksport należy rozdzielić na:

- celową sprzedaż energii z baterii;
- sprzedaż nadwyżki PV.

Sprzedaż nadwyżki PV jest pozabilansowa dla wyznaczania przyszłego deficytu
baterii: nie tworzy długu energetycznego i nie zwiększa zakupu. Nadal pozostaje
częścią fizycznego bilansu slotu i wyniku finansowego.

W jednym slocie bateria nie może być jednocześnie ładowana i rozładowywana.
Każdy przepływ musi spełniać limit mocy 5 kW, czas 0,25 h, dostępną pojemność,
SOC, sprawność i progi techniczne.

## 5. `soc_target`

`soc_target` jest zapotrzebowaniem energetycznym do najbliższego realnego,
wykonalnego źródła uzupełnienia: prognozowanego PV albo wybranego okna BUY.
Jednocześnie jest sufitem ładowania z sieci w oknie BUY.

Jeżeli ekonomiczny przebieg wybrał przyszłe okno BUY, jego wynikowy target jest
również sufitem ładowania z PV w poprzedzającym odcinku prowadzącym do tego
okna. Nie jest tam jeszcze obowiązkowym minimum SOC. Dzięki temu PV ma zawsze
pierwszeństwo i może zmniejszyć albo całkowicie wyeliminować późniejszy zakup;
obowiązek osiągnięcia targetu powstaje dopiero na końcu wybranego okna BUY.
Wyzerowanie końcowego zakupu przez wcześniejsze PV nie usuwa samej granicy
uzupełnienia w następnym przebiegu; okno i kupiona energia są odrębnymi polami
kontraktu. Zapobiega to oscylacji ścieżki `BUY → PV → brak BUY → BUY`.

Ta sama zasada dotyczy całego ciągłego okna produkcji PV: największy wymagany
target tego okna jest jego sufitem ładowania od pierwszego slotu z prognozowaną
produkcją. Pierwsza dostępna nadwyżka PV ładuje baterię do tego targetu.
Wybrany BUY rozdziela dwa mosty energetyczne. Planer nie może pozostawić
lokalnego targetu na technicznym minimum i eksportować wcześniejszego PV w
oczekiwaniu na późniejszy slot tego samego okna.

Techniczne minimum SOC jest granicą awaryjną, nie celem operacyjnym. Planowana
ścieżka nie może celowo sprowadzać baterii do tej wartości ani uzależniać
wykonalności od idealnego rozpoczęcia prognozowanego PV. Target zawiera zapas
wynikający z konserwatywnej korekty zużycia i PV.

Na końcu każdej lokalnej doby obowiązuje dodatkowa minimalna granica: ważona
prognoza rzeczywistego SOC zamknięcia z niezależnych okien 7/14/28 dni.
Granica dotyczy każdej doby w horyzoncie, jest zaokrąglana w górę do kroku SOC
i nie może zostać wyzerowana przez BUY lub PV następnego dnia.

Target obejmuje:

- prognozowane zużycie do granicy uzupełnienia;
- zaplanowane sterowalne odbiorniki;
- straty ładowania i rozładowania;
- rezerwę techniczną i niepewność prognozy;
- przyszłe PV, które faktycznie może trafić do baterii;
- energię potrzebną do wykonania zaakceptowanej przyszłej sprzedaży baterii.

Target nie jest progiem sprzedaży i nie może być kopiowany z floor. Powinien
być zwykle wyższy od floor. Jeżeli wymaganie przekracza pojemność, target wynosi
100%, a wcześniejsza sprzedaż lub obciążenie sterowalne musi zostać ograniczone
do wykonalnego poziomu.

Zakup energii służy ładowaniu baterii. Import pokrywający samodzielnie dom nie
jest decyzją ekonomiczną planera. Wyjątek stanowi jawnie wykazana ochrona energii
baterii przed późniejszą, bardziej opłacalną sprzedażą; musi ona przejść ocenę
pełnego cyklu wraz ze sprawnościami, degradacją i możliwością odtworzenia SOC.

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
2. ładowanie baterii do `soc_target`;
3. CWU;
4. EV;
5. sprzedaż pozostałej nadwyżki PV;
6. ograniczenie produkcji jako ostatnia możliwość.

CWU/EV lub sprzedaż PV nie mogą wystąpić, gdy ta sama energia jest potrzebna do
osiągnięcia targetu. Dopuszczenie CWU/EV wymaga nadwyżki po target oraz spełnienia
ich własnych progów i ekonomiki.

## 8. Ekonomiczne BUY i SELL

- Okna są wynikiem cen całego dostępnego horyzontu, sprawności, kosztu degradacji
  i ograniczeń fizycznych; nie są stałymi godzinami.
- BUY jest wybierany spośród najtańszych wykonalnych slotów przed terminem
  targetu. Jeśli jeden slot nie wystarcza, zakup rozszerza się na kolejne
  najtańsze sloty.
- Nie wolno kupować w SELL ani kupować tylko dlatego, że bieżący SOC jest niski,
  jeżeli PV przed terminem bezpiecznie pokryje deficyt.
- SELL_BAT jest dopuszczalny tylko dla energii ponad wszystkie przyszłe
  zobowiązania i tylko gdy pełny cykl ma dodatni wynik netto.
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
- Energia HP jest dodawana do bilansu i targetu wyłącznie dla zakwalifikowanego
  profilu. HP może zwiększyć potrzebne ładowanie baterii, ale samo nie tworzy
  okna BUY.

## 12. Kontrakt granicy PPD

- `BATTERY_IMPORT`, `BATTERY_EXPORT` i `HP_HEAT_DHW` są planowane ilościowo
  wyłącznie przez planer. PPD nie posiada drugiej implementacji ekonomiki,
  okien ani targetu dla tych procesów.
- PPD kopiuje ich zamrożone decyzje do wersjonowanej macierzy procesów.
- `PV_CWU` i `PV_EV` są jedynymi procesami, których okna PPD może wyliczyć po
  publikacji planu; nie zmieniają one targetu.
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
- ładowanie PV do targetu przed CWU, EV i sprzedażą nadwyżki;
- sprzedaż baterii bez naruszenia targetu i floor;
- fizyczny bilans każdego slotu;
- odrzucenie całego planu przy pojedynczym niewykonalnym slocie;
- zachowanie ostatniego poprawnego planu po błędzie replanu;
- zakończenie obliczeń przed granicą kolejnego slotu;
- wyzerowanie odziedziczonego `heat_pump_window` na początku przebiegu;
- HP wyłączone przy niepełnej prognozie i temperaturze równej progowi;
- brak HP przed 07:00, po 19:00 i w każdym slocie `SELL`;
- publikację cen jednego slotu wyłącznie do dwóch kanonicznych helperów.
