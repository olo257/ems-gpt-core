# EMS-GPT Core — historia zmian

Ten plik rejestruje wydania. Nie jest specyfikacją; obowiązujące reguły są w `DOCS.md` i `PLANNER_CONTRACT.md`.

## 0.38.9 — ochrona SOC po sprzedaży i kontrolowany most do BUY

- Sprzedaż z baterii zachowuje bazowy SOC aktywnego programu TOU oraz energię
  potrzebną odbiorom do chwili wejścia kolejnego, niższego programu. Planner
  nie może już sprzedać dokładnie do 40% i wymusić później importu dla domu.
- Programy 5 i 6 mogą zostać tymczasowo obniżone wyłącznie wtedy, gdy ten sam
  opublikowany plan zawiera rzeczywisty zakup do baterii w ciągu maksymalnie
  180 minut oraz bezpieczne zamknięcie doby co najmniej na `soc_required`.
- Executor prowadzi próg programu 5/6 za ilościową trajektorią SOC, wyłącza
  ładowanie programu podczas mostu i przywraca bazowe wartości po utracie
  warunków, zmianie planu lub zakończeniu zakupu.

## 0.38.8 — bezpieczna migracja relacji `slot_id`

- Zastąpiono pełne, restartowe `UPDATE JOIN` migracją wykonywaną partiami dobowymi.
- Przed backfillem tworzone są indeksy dla `slot_id/slot_start` oraz kalendarza lokalnych slotów.
- Po każdej partii wykonywany jest commit; błąd powoduje rollback bieżącej partii, a kolejny start bezpiecznie wznawia migrację.
- Po pełnym zakończeniu zapisywany jest trwały marker `slot_id_backfill_batched_0_38_8`; kolejne restarty nie powtarzają aktualizacji historycznej.
- Połączenie używane przez migrację ma lokalnie wydłużony timeout 120 s, bez zmiany zwykłych połączeń runtime.
- Dodano test regresyjny symulujący 730 dni historii i 5 110 krótkich partii.

## 0.38.7

- Naprawiono ostatni etap ochrony `BATTERY_IMPORT`: `planned_buy_kwh` pobrane z
  MariaDB jest teraz konwertowane jako skalar SQL, a nie jako obiekt stanu Home
  Assistant. Poprawny zakup nie jest już odrzucany z
  `IMPORT_PLAN_OR_SOC_UNAVAILABLE` mimo obecnej energii planu, targetu i SOC.

## 0.38.6

- Executor wiąże ochronę `BATTERY_IMPORT` z dokładnym `plan_run_id` zapisanym
  w wersji komendy. Nie może już odrzucić poprawnego zakupu jako
  `IMPORT_PLAN_OR_SOC_UNAVAILABLE` wskutek odczytu innego rekordu tego samego
  slotu z `planned_buy_kwh=NULL`.
- Obserwacja `PV_CWU` korzysta ze stanu rzeczywiście sterowanej grzałki
  `light.sm_pro_3248d_1_grzalka_cwu`. Autonomiczne grzanie CWU przez pompę
  ciepła nie jest już klasyfikowane jako ręczne uruchomienie `PV_CWU`.
- Obserwacja `BATTERY_EXPORT` obejmuje wyłącznie rozładowanie przypisane do
  rzeczywistego eksportu sieciowego. Autokonsumpcja bateria→dom nie jest już
  klasyfikowana jako ręczna sprzedaż.

## 0.38.5

- Naprawiono klasyfikację wykonania `BATTERY_IMPORT`: ładowanie baterii z PV
  nie jest już oznaczane jako ręczne uruchomienie importu. Obserwowana energia
  procesu obejmuje wyłącznie część dodatniego importu sieciowego pozostałą po
  pokryciu rzeczywistego deficytu odbiorów i jest ograniczona zmierzonym
  ładowaniem baterii.

## 0.38.4

- Konfiguracja aplikacji zawiera teraz dziesięć jawnych odniesień do skryptów
  `ON/OFF` executora dla `BATTERY_IMPORT`, `BATTERY_EXPORT`, `PV_CWU`, `PV_EV`
  oraz `HP_HEAT_DHW`. Pola mają domyślne wartości odpowiadające istniejącym
  encjom `script.ems_gpt_core_*` i można je edytować w panelu konfiguracji.
- Executor korzysta z jawnych pól jako źródła prawdy. Dotychczasowy
  `connector_service_map_json` pozostaje wyłącznie fallbackiem migracyjnym.

## 0.38.3

- Jeżeli sprzedaż z baterii koliduje z historycznym celem SOC końca doby,
  planer najpierw przelicza cały horyzont bez `SELL_BAT`. Bateria nadal może
  zasilać odbiory domu; blokowany jest wyłącznie celowy eksport jej energii.
- Replan po ostatnim wykonalnym oknie uzupełnienia nie odrzuca już całego
  planu, gdy historyczny cel końca bieżącej doby stał się fizycznie
  nieosiągalny. Cel pozostaje publikowany, a planer zapisuje jawny shortfall i
  wybiera najlepszą wykonalną trajektorię.
- Zwolnienie niewykonalnej granicy dotyczy wyłącznie konkretnej doby; cele
  kolejnych dób pozostają twardymi ograniczeniami.

## 0.38.2

- Historyczna prognoza końcowego SOC 7/14/28 jest twardą minimalną granicą
  zamknięcia każdej doby w horyzoncie, a nie wyłącznie ostatniej doby planu.
  Przy obecnej średniej około 40% planer nie może publikować końca dnia około 20%.
- BUY pozostaje zakupem energii do baterii: nie może powstać wyłącznie dla
  zużycia domu, a energia ładowania musi być co najmniej równa dobrowolnej
  części zasilania odbiorów z sieci. Poza BUY pozostaje tylko techniczny ślad
  kwantyzacji albo import nieunikniony z powodu rezerwy lub limitu mocy.

## 0.38.1

- Naprawiono interpretację bazodanowego zera dla `heat_pump_window`: PPD nie
  może już zamienić wartości `0`, `"0"` ani `b'\\x00'` na rekomendację `ON`.
- Executor niezależnie sprawdza opublikowane okno planera przed utworzeniem i
  przed wysłaniem komendy `HP_HEAT_DHW=ON`; niespójność działa fail-closed.
- Energia planowana procesu HP jest prezentowana jako zero, gdy opublikowane
  okno HP jest wyłączone; historyczna energia CWU nie udaje już ogrzewania.
- Usunięto uproszczone, zdublowane walidacje importu domu z końca planera i z
  PPD. O fizycznie nieuniknionym imporcie decyduje wyłącznie optymalizator,
  który uwzględnia limit 5 kW, dostępną energię, rezerwę i sprawność baterii.
  Import wynikający z przekroczenia mocy rozładowania nie odrzuca już planu po RCE.

## 0.38.0

- Rozdzielono odpowiedzialność planera, PPD i executora. Planer pozostaje
  jedynym właścicielem ilościowych przebiegów `BATTERY_IMPORT`,
  `BATTERY_EXPORT` i `HP_HEAT_DHW`, ponieważ procesy te wpływają na bilans,
  trajektorię SOC i target.
- PPD nie przelicza już ekonomiki importu ani eksportu baterii i nie nadpisuje
  polityk sieciowych planera. Publikuje zamrożone rekomendacje do executora.
- PPD odrzuca publikację, jeżeli `BUY_ALLOWED` nie zgadza się z ilościowym
  `planned_buy_kwh` albo `SELL_BAT` z `planned_sell_kwh`; zerowy plan zakupu nie
  może zostać ukryty pod aktywną decyzją automatyczną.
- Widok Procesy rozdziela rekomendację planera, stan planowany, tryb
  `AUTO/FORCE_ON/FORCE_OFF`, stan efektywny oraz planowaną energię. Wiersz
  `BATTERY_IMPORT` z zerową energią jest prezentowany jako plan `OFF`, a ręczna
  blokada HP nie jest już mylona z rekomendacją planera `ON`.
- Planer nie odczytuje już `ems_gpt_core_process_decisions` ani override'ów.
  Minione sloty HP rozlicza według własnego wcześniej opublikowanego planu;
  również brak telemetrii nie usuwa zaplanowanego slotu z ciągłości cyklu.
  Różnice ręczne i rzeczywiste pozostają odchyleniem wykonania.
- `AUTO`, `FORCE_ON` i `FORCE_OFF` pozostają wyłącznie w warstwie wykonawczej.
  Nie zmieniają wstecz planu ani targetu.
- `PV_CWU` i `PV_EV` pozostają niezależną alokacją PPD po zamrożeniu targetu.
- Kwalifikacja temperaturowa `HP_HEAT_DHW` korzysta z rzeczywistych zapisów
  `sensor.klimat_w_ogrodzie_temperature`, a nie z prognozy `weather.dom`.
  Wymagane są co najmniej 3 zapisy z okna 00:00–06:00, dzięki czemu częściowa
  awaria HA nie blokuje planu, ale zbyt mała próbka nadal działa fail-closed.
- Średnie końcowego SOC 7/14/28 dni są liczone z trzech niezależnych okien
  kompletnych dób bez awarii, bez zależności od 7-dniowego zakresu odbudowy
  tabel agregacyjnych.
- Dodano regresje kontraktu planer→PPD oraz niezależności planera od PPD.

## 0.37.8

- Aktywne `FORCE_OFF` procesu `HP_HEAT_DHW` blokuje teraz również automatyczne okna planera, a nie tylko wykonanie komendy.
- Przycisk `Wyłącz` wymusza `heat_pump_window=0` i usuwa obciążenie HP z planu; dopiero `Auto` przywraca kwalifikację temperaturową.
- Usunięto rozbieżność, w której wykonanie miało `OFF`, lecz tabela Procesy nadal publikowała `HP_HEAT_DHW=ON / TAK`.

## 0.37.7

- Naprawiono klasyfikację wykonania `HP_HEAT_DHW`: zwykła energia przygotowania CWU nie jest już doliczana do energii ogrzewania domu.
- Tryb `DHW only` nie może już zostać zapisany jako zewnętrzne/ręczne `HP_HEAT_DHW=ON` i przeniesiony do kolejnych przebiegów planera.
- Stan procesu ogrzewania wynika wyłącznie z energii obiegu CO; energia CWU pozostaje raportowana oddzielnie.

## 0.37.6

- Publikacja bieżących cen jest ponawiana co minutę aż do potwierdzenia obu helperów; pojedynczy błąd HA przy otwarciu slotu nie pozostawia już ceny nieaktualnej przez cały slot.
- Po `input_number.set_value` Core odczytuje helper i sprawdza zapisaną wartość. Diagnostyka pokazuje oczekiwaną i rzeczywistą wartość oraz zakres `min`/`max` helpera.
- Wynik ostatniej próby publikacji jest dostępny w stanie aplikacji jako `current_prices`.

## 0.37.5

- Planer nie dziedziczy już `heat_pump_window=1` z poprzedniego planu; każdy przebieg rozpoczyna ocenę HP od `NIE`.
- Próg HP ponownie pochodzi wyłącznie z ustawienia panelu `night_heating_threshold_c`.
- Bieżąca cena zakupu jest zapisywana wyłącznie do istniejącej encji `input_number.ems_gpt_cena_zakupu_biezaca`.
- Usunięto równoległą publikację cen do `sensor.gpt_ems_*`; Core aktualizuje tylko dwa istniejące helpery i nie tworzy duplikatów encji.

## 0.37.4

- Próg uruchomienia `HP_HEAT_DHW` jest pobierany z helpera `input_number.temperatura_nocna_pompy_ciepla`; brak lub niedostępność helpera blokuje automatyczne ogrzewanie.
- Bieżące ceny są publikowane bezpośrednio do kanonicznych sensorów `sensor.gpt_ems_cena_zakupu` i `sensor.gpt_ems_cena_sprzedazy`. Zapis do dotychczasowych helperów pozostaje zgodnościowy i jego błąd nie blokuje sensorów.

## 0.37.3

- Naprawiono wykonawcę `HP_HEAT_DHW`: automatyczny stan `OFF` z opublikowanego planu nie jest już pomijany, gdy sprężarka nadal pracuje. Tylko jawny `FORCE_ON` może utrzymać ogrzewanie poza zaplanowanym oknem.
- Usunięto błędne przejmowanie trwającego cyklu EMS jako sterowania zewnętrznego po 30 minutach pracy.

## 0.37.2

- Przywrócono twarde dzienne okno automatycznego `HP_HEAT_DHW`.
- W dni robocze i weekend ogrzewanie może rozpocząć się najwcześniej o 07:00,
  bezpośrednio po zakończeniu porannego okna `SELL`.
- Okno kończy się najpóźniej o 19:00 albo wcześniej, przed początkiem
  wieczornego `SELL`; żaden slot `SELL` nie może uruchomić ogrzewania.
- Okna `BUY` nie wpływają na początek ani koniec okna pompy ciepła.
- Energia `HP_HEAT` jest dodawana do bilansu i targetu BUY wyłącznie po
  spełnieniu skonfigurowanego progu nocnej temperatury. Kwalifikacja wymaga
  kompletnych 24 próbek prognozy 00:00–06:00; brak danych, niepełna prognoza
  oraz temperatura równa lub wyższa od progu dają `HP_HEAT=0`.
- Optymalizator otrzymuje wyłącznie sloty należące do okna. Ręczne `Włącz`
  pozostaje nadrzędnym poleceniem operatora.

## 0.37.1

- Naprawiono `No feasible SOC state at horizon slot` po wdrożeniu 0.37.0.
- Kontrakt `SOC required` korzysta teraz z faktycznych przepływów baterii,
  więc nie zalicza nieuniknionego poboru sieciowego jako energii, którą
  bateria musi posiadać przed najbliższym oknem uzupełnienia.
- Wymagany SOC jest ograniczony do fizycznie osiągalnej trajektorii bez
  dobrowolnej sprzedaży; przyszłe okno BUY/PV nie tworzy wymagania wcześniej,
  niż energia może zostać dostarczona.
- Dodano regresję startu na rezerwie oraz zachowano test 48 h / 192 slotów.

## 0.37.0

- Przebudowano logikę SOC na cztery niezależne kontrakty: rezerwę fizyczną,
  ciągły `SOC required`, cel ładowania BUY i floor sprzedaży.
- `SOC required` jest liczony wstecz z dokładnie przydzielonych kWh PV/BUY;
  samo okno uzupełnienia nie resetuje już wymagania do 15%.
- Usunięto oscylacyjną pętlę targetu. Plan powstaje deterministycznie w dwóch
  przebiegach: ekonomicznym i kontraktowym.
- Minimalny wymagany SOC jest egzekwowany w każdym slocie, a cel ładowania
  wyłącznie kończy import do baterii w BUY.
- PPD pozostaje tylko czytelnikiem planu i odrzuca ukryty import na zużycie,
  zamiast oznaczać go jako `NEUTRAL`.
- Dodano kolumny `soc_reserve_pct`, `soc_required_pct`,
  `soc_charge_target_pct` i `soc_sale_floor_pct`; stare pola pozostają aliasami
  zgodności podczas migracji.

## 0.36.15

- Naprawiono regresję 0.36.14, w której obsługa cyklu ścieżek BUY wracała
  do planu bez kontraktu targetu i mogła obniżyć SOC przez około 22% do
  technicznej rezerwy 15%.
- Przy oscylacji zachowywany jest ostatni wykonalny plan policzony z pełnym
  mostem energetycznym oraz jego terminami osiągnięcia targetu.
- Plan bazowy bez targetów nie jest już publikowany jako rozwiązanie awaryjne.

## 0.36.14

- Naprawiono błąd `SOC_TARGET_PATH_OSCILLATION`, w którym dwa wykonalne
  zestawy slotów BUY naprzemiennie zmieniały się po przeliczeniu targetu.
- Przy wykryciu cyklu planer używa stabilnego, pełnohoryzontowego wyniku
  ekonomicznego, który już spełnia bilans fizyczny i terminalny SOC.
- Targety wykonawcze dla BUY i SELL są wtedy wiązane z zaakceptowanym SOC
  końcowym tych przepływów; oscylacja i wybrana ścieżka są zapisywane w
  diagnostyce zamiast przerywać każdy replan.

## 0.36.13

- Naprawiono krytyczny błąd, w którym niewykonalny wariant `PV_FIRST`
  przerywał cały replan komunikatem `No feasible SOC state`.
- `PV_FIRST` jest wariantem alternatywnym: gdy nie da się go wykonać przy
  aktualnym SOC i ograniczeniach mocy, planer odrzuca wyłącznie ten wariant,
  publikuje wcześniej zweryfikowany plan `STANDARD` i zapisuje powód
  odrzucenia w diagnostyce.

## 0.36.12

- Dodano wariant planu `PV_FIRST`. Jeżeli drogi wcześniejszy zakup zajmuje
  pojemność, a pobliska nadwyżka PV byłaby sprzedawana taniej, target końca
  okna BUY jest obniżany wyłącznie o energię możliwą do bezpiecznego
  przesunięcia na PV bez zejścia poniżej rezerwy przed jego nadejściem.
- Planer porównuje pełny wynik PLN wariantu standardowego i `PV_FIRST` przy
  identycznym SOC początkowym i terminalnym, po czym publikuje wariant
  ekonomicznie lepszy. Wyniki i wybór zapisuje diagnostyka przebiegu.
- `Daily` ponownie zapisuje rzeczywiste godziny początku i końca produkcji PV,
  również podczas odbudowy historycznej.
- Widok `Daily` pokazuje liczbę wszystkich dób, dób dopuszczonych do uczenia
  i rekordów otwartych lub odrzuconych.
- Dla każdej doby zapisano średnie rzeczywistego SOC końcowego z 7, 14 i 28
  dni, liczebności prób oraz ważoną prognozę terminalnego SOC.

## 0.36.11

- Wydzielono PPD jako osobny przebieg uruchamiany dopiero po atomowej
  publikacji planu. PPD otrzymuje własny `ppd_run_id` i jawny status w tabeli
  przebiegów modułów.
- Planer nie zapisuje już decyzji wykonawczych. PPD czyta wskazany
  `plan_run_id`, nie może zmieniać SOC ani targetu i zapisuje wyłącznie
  polityki, rekomendacje, elastyczny podział nadwyżki PV oraz decyzje procesów.
- Executor wersjonuje polecenia parą `plan_run_id` + `ppd_run_id`, dlatego nie
  wykona decyzji pochodzącej z innego przebiegu PPD.
- Bliskie okno PV może teraz usunąć wcześniej wybrany poranny BUY. Iteracje
  targetu zastępują zbiór zakupów aktualnym wynikiem zamiast kumulować go;
  oscylacja BUY–PV kończy przebieg bez publikacji.

## 0.36.10

- Zamknięto lukę, w której minimalny krok ładowania `+0,25% SOC` pozwalał
  zasilać znacznie większe bieżące zużycie domu z sieci w oknie BUY.
- W każdym celowym slocie BUY energia kierowana do baterii musi być co
  najmniej równa dobrowolnemu importowi na zużycie. Import technicznie
  nieunikniony po osiągnięciu minimalnego SOC pozostaje dozwolony.
- Panel automatycznie włącza kolumny `SOC początek` i `SOC koniec` w zapisanych
  wcześniej konfiguracjach widoków Godzinowe i Dobowe, zachowując pozostałe
  preferencje kolumn użytkownika.

## 0.36.9

- Usunięto krytyczny mechanizm `SOC_HOLD_FOR_FUTURE_SALE`, który mógł
  zasilać bieżące odbiorniki z sieci i sztucznie utrzymywać stały SOC baterii
  w oczekiwaniu na późniejszą sprzedaż.
- Usunięto rekomendację „Ochrona SOC przed sprzedażą”. Poza celowym
  ładowaniem w oknie BUY import odbiorników jest dopuszczalny wyłącznie jako
  fizycznie nieunikniony przepływ po osiągnięciu technicznego minimum SOC.
- Autokonsumpcja z baterii nie jest blokowana przez przyszłe okno sprzedaży;
  każdy slot nadal musi spełnić pełny bilans energii.

## 0.36.8

- Agregacje godzinowe i dzienne zapisują rzeczywisty `SOC początek` oraz
  `SOC koniec` wyłącznie z zamkniętych slotów wykonania.
- `SOC początek` okresu jest równy ostatniemu rzeczywistemu `SOC koniec`
  poprzedniego okresu, dzięki czemu zmiana stanu baterii jest ciągła między
  kolejnymi godzinami i dobami.
- Odbudowa agregacji po restarcie stosuje tę samą zasadę; wartości planowane
  ani przyszłe sloty nie są używane do domykania rzeczywistego bilansu.
- Panel godzinowy i dzienny pokazuje obie granice SOC.
- Rzeczywisty SOC zamknięcia kompletnych dób wyznacza ważoną prognozę SOC
  na koniec horyzontu: domyślnie 50% dla średniej 7-dniowej oraz po 25% dla
  średnich 14- i 28-dniowej. Wagi są konfigurowalne i muszą sumować się do
  100%.
- Historyczna prognoza jest wyłącznie terminalnym warunkiem końca doby.
  Target każdego slotu 15-minutowego nadal powstaje wstecz z jego bilansu
  energii, prognozowanego zużycia, PV, HP/CWU, sprawności i okien zakupu.

## 0.36.7

- Panel Planera pokazuje prognozę całego poboru HP oraz osobno historyczny
  pobór CWU, które od wersji 0.36.6 są używane do wyliczania `soc_target`.
- Analityka raportuje plan–wykonanie ładowania i rozładowania baterii jako
  WAPE, błąd aktywnych slotów, F1 zdarzeń oraz bias energii.
- Metryki baterii pozostają wyłącznie diagnostyczne (`DIAGNOSTIC_READ_ONLY`)
  i nie wprowadzają automatycznych korekt do planera ani PPD.
- Agregacja analityczna ponownie filtruje każdy historyczny tryb HP, dzięki
  czemu stare próbki jałowego kanału CO sprzed 0.36.4 nie są już sumowane w
  bieżącym 30-dniowym raporcie.

## 0.36.6

- Prognoza obciążenia pompy ciepła używana przez optymalizator SOC jest teraz
  zapisywana również w planie slotowym. Osobna kolumna CWU pokazuje historyczny
  pobór przypisany do cyklu, w tym powtarzalne grzanie około 06:00.
- Plan, diagnostyka i wyliczenie targetu korzystają z tego samego obrazu
  przyszłego obciążenia HP; wartości nie pozostają już wyłącznie wewnątrz
  przebiegu optymalizatora.

## 0.36.5

- Decyzja sprzedaży baterii ma końcową kontrolę ekonomiczną typu fail-closed:
  cena sprzedaży musi pokrywać najtańszy późniejszy odkup z uwzględnieniem
  sprawności ładowania i rozładowania, degradacji oraz minimalnej marży.
- Diagnostyka decyzji `BATTERY_EXPORT` zapisuje cenę odkupu, wymagany próg
  sprzedaży, oczekiwaną marżę i wynik kontroli ekonomicznej.
- Historia targetu pozostaje wyłącznie diagnostyczna, ale odciążenie PV jest
  teraz rozpoznawane na podstawie rzeczywistej nadwyżki PV ponad nieuniknione
  zużycie przez dwa kolejne sloty. Sama produkcja PV mniejsza od obciążenia nie
  skraca już sztucznie horyzontu i nie zaniża targetu wymaganego z perspektywy
  wykonania.

## 0.36.4

- Odfiltrowano pobór jałowy nieaktywnego kanału pompy ciepła, który był
  błędnie raportowany jako energia CO mimo zatrzymanego kompresora i zerowej
  produkcji ciepła.
- Energia pobrana, wytworzona i COP pozostają liczone niezależnie dla CO, CWU
  i chłodzenia; żaden wspólny COP nie służy do przypisywania trybu pracy.
- Dodano niezależne, przełączalne korekty: stała korekta bezpieczeństwa ±10%
  jest domyślnie wyłączona, a korekta historyczna PV jest stosowana osobno do
  PV1 i PV2 po osiągnięciu wymaganej jakości danych.
- Otwarte prognozy zużycia są ponownie wyliczane z aktualnego profilu
  historycznego zamiast zachowywać zawyżoną wartość zapisaną wcześniej.
- Planer uczy się osobnego profilu poboru HP w trybie CWU dla dni roboczych
  i weekendów. Powtarzalny poranny cykl około 06:00 jest uwzględniany w
  zapotrzebowaniu i `soc_target` jeszcze przed jego uruchomieniem.

## 0.36.3

- Naprawiono regresję kroczącego replanu, w której nieosiągalny w ostatnim
  slocie termin `soc_target` usuwał wszystkie stany DP i przełączał planer
  oraz PPD w tryb ograniczony.
- Termin targetu jest teraz ograniczany do najwyższego SOC fizycznie
  osiągalnego z aktualnego zbioru stanów, dostępnej mocy, PV i uprawnienia
  BUY. Oryginalny target nadal pozostaje sufitem ładowania z sieci.
- Publikowany `soc_target` odzwierciedla wartość rzeczywiście wykonalną;
  niezasilony hard target poza terminem nadal kończy się błędem zamkniętym.

## 0.36.0

- Wydzielono `PV_CWU` i `PV_EV` z `planner_service.py` do osobnego
  `ppd_service.py`, wykonywanego dopiero po zamrożeniu pełnej trajektorii
  `soc_target`. Odbiorniki elastyczne nie zwiększają i nie zmieniają targetu.
- Każdy proces otrzymuje jedno ciągłe dzienne okno PPD od pierwszego do
  ostatniego wykonalnego punktu. Słabszy slot PV wewnątrz okna nie rozcina
  zgody; rzeczywiste włączanie i wyłączanie pozostaje po stronie automatyzacji.
- Cykliczny replan używa aktualnego SOC, więc nieplanowany wzrost SOC z PV
  może przesunąć początek okna wcześniej.
- Sprzedaż baterii oraz ekonomiczna blokada są twardymi granicami okna.
- `PV_EV` wymaga osiągnięcia `soc_target`, bez wcześniejszego dodatkowego
  warunku `soc_target + 20 pp`. CWU zachowuje pierwszeństwo w ilościowym
  przydziale prognozowanej nadwyżki.
- Dodano testy ciągłości, izolacji targetu, korekty po zmianie SOC, granicy
  sprzedaży baterii i rozdzielenia dni.

## 0.35.8

- `soc_target` ogranicza wyłącznie ładowanie baterii z sieci. Nadwyżka PV może
  ładować baterię dalej, aż do fizycznego maksimum SOC, zanim zostanie
  przeznaczona do odbiorników elastycznych albo eksportu.
- Panel tłumaczy stany techniczne: `RUNNING` jako `URUCHOMIONY`, `STARTING`
  jako `STARTUJE`, `LIVE`/produkcję jako `URUCHOMIONE`, a `OFF` i `DISABLED`
  jako `WYŁĄCZONY`.
- Kolory stanów: start — żółty, uruchomienie/produkcja — zielony, `OFF` —
  czerwony, `DISABLED` — niebieski.

## 0.35.7

- Dodano obserwacyjny moduł historycznej oceny `soc_target`. Moduł rekonstruuje
  wymagany target z rzeczywistego zużycia pomniejszonego wyłącznie o EV do pierwszego
  potwierdzonego odciążenia PV albo następnego okna BUY; nie uczy się z dawnych
  targetów jako prawdy i nie zapisuje niczego do planera ani wykonawcy.
- Zużycie ogrzewania i DHW pompy ciepła pozostaje częścią wymaganego targetu;
  produkcyjnego `actual_dhw_kwh` nie uznano błędnie za elastyczną grzałkę.
- Próbki z luką slotów, awarią, niedostateczną telemetrią, brakującą energią lub
  niezamkniętym horyzontem są jawnie odrzucane. Brak danych kolejnej doby nie
  jest zastępowany sztucznym zerem ani targetem 15%.
- Dodano metryki niedoszacowania P80/P90, limitowaną sugestię korekty oraz
  osobną kontrolę slotu 18:30. Sugestia pojawia się dopiero po minimalnej
  liczbie poprawnych próbek i pozostaje `SHADOW_READ_ONLY`.
- Dodano tabelę i widok `Historia targetu` oraz parametry zakresu analizy,
  minimalnej liczby próbek, limitu korekty i progu odciążenia PV.
- Wykonawca pozostaje domyślnie wyłączony.

## 0.35.6

- Rozszerzono ciągłość targetu z pojedynczego punktu PV na całe ciągłe okno
  produkcji. Największy wymagany target okna obowiązuje jako sufit ładowania od
  pierwszego prognozowanego PV, z podziałem przy faktycznie wybranym BUY.
- Naprawiono produkcyjny przypadek 07:00–09:00, w którym SOC pozostawał na
  17,75%, a poranna nadwyżka PV była eksportowana do 11:45.
- Wykonawca pozostaje domyślnie wyłączony.

## 0.35.5

- Target końcowego slotu uzupełnienia PV jest propagowany wstecz na całe
  podejście do tego okna. Wcześniejsza poranna nadwyżka ładuje baterię do
  targetu zamiast być sprzedawana przy niskim SOC.
- Techniczne minimum SOC pozostaje rezerwą awaryjną, a nie celem operacyjnym.
- Dodano regresję dla przejścia nocnego minimum do wieloslotowego okna PV.
- Wykonawca pozostaje domyślnie wyłączony.

## 0.35.4

- Naprawiono odczyt bilansu aktywnego slotu po wdrożeniu 0.35.3. Import domu
  jest wyliczany z istniejących przepływów PV, baterii i obciążenia zamiast z
  nieistniejącej kolumny `grid_load_kwh`.
- Prognoza zużycia jest uzupełniana dla całego otwartego horyzontu profilem
  slotu, a przy zbyt krótkiej historii konserwatywną średnią z ostatnich trzech
  dób. Planer blokuje publikację, jeśli mimo tego pozostanie `NULL`.
- Pusty slot bez przepływów otrzymuje rekomendację `Neutralny`; etykieta
  `Zasilanie z sieci` wymaga rzeczywistego, nieuniknionego importu domu.
- Produkcyjny replan 0.35.3 potwierdził zbieżność: `ACCEPTED`, 124 sloty.
- Wykonawca pozostaje domyślnie wyłączony.

## 0.35.3

- Usunięto oscylację ścieżki targetu, gdy PV przed wybranym BUY całkowicie
  zastępuje zakup. Okno uzupełnienia pozostaje granicą targetu niezależnie od
  końcowej liczby kupionych kWh.
- Bilans aktywnego slotu używa jednej strony pomiarowej: uwzględnia sprawność
  ładowania i rozładowania, import domu oraz tylko PV należące do bilansu
  podstawowego. Elastyczna nadwyżka PV pozostaje poza bilansem targetu.
- Błąd replanu jest zatrzaskiwany jako `DEGRADED` do czasu kolejnego
  poprawnego planu i nie jest nadpisywany przez cykl statusowy.
- Wykonawca pozostaje domyślnie wyłączony.

## 0.35.2

- Naprawiono zamykanie wstecznego kontraktu `soc_target`: faktycznie wybrany
  BUY odcina wcześniejsze sloty od zapotrzebowania po tym oknie, a wystarczające
  konserwatywne PV odcina most dopiero po pełnym pokryciu zobowiązania.
- Granica BUY zachowuje przed oknem tę część energii, której nie można fizycznie
  uzupełnić w wybranych slotach przy limicie 5 kW i sprawności ładowania.
- Częściowe PV nadal pomniejsza wymagany target, ale nie usuwa niedoboru.
- Kafelki modułów mają stałą wysokość i są tworzone tylko raz. Odświeżanie
  zmienia wyłącznie ich tekst i klasy stanu, bez przebudowy całej siatki.
- Kafelek RCE przeniesiono na ostatnią pozycję siatki modułów.
- Kafelek aktywnego slotu pokazuje czas bez sufiksu strefy oraz obie strony
  planowanego bilansu energii i ich odchylenie.
- Wykonawca pozostaje domyślnie wyłączony.

## 0.35.1

- Rozdzielono technicznie nieunikniony import domu przy minimalnym SOC od
  decyzji `BUY`, która nadal służy wyłącznie ładowaniu baterii. Planer nie
  kończy już błędem `No feasible SOC state at horizon slot 0`, gdy PV i energia
  ponad rezerwę nie wystarczają do pokrycia pierwszego slotu.
- Panel pokazuje dla każdego modułu stan, aktualną/ostatnią czynność i czas jej
  odświeżenia.
- Dodano przycisk **Przelicz plan**, korzystający z tej samej serializowanej,
  atomowej ścieżki publikacji co replan automatyczny. Panel pokazuje trwanie,
  sukces albo dokładny błąd przebiegu.
- Stały kontrakt importu wymuszonego, BUY i ręcznego replanu zapisano w
  `PLANNER_CONTRACT.md` i `DOCS.md`.

## 0.35.0

- Rozdzielono kompletność importu RCE od wyniku prognoz i planera. Pełny zestaw
  `96/96` pozostaje zatwierdzonym importem również wtedy, gdy późniejszy przebieg
  planera kończy się błędem.
- Błąd przebiegu zależnego ma osobny status `planner=ERROR` i zdarzenie
  `rce_dependent_cycle_failed`; nie uruchamia ponownego importu ani nie zastępuje
  kompletnych cen częściowym zestawem.
- Ręczne odświeżenie RCE zwraca wynik kompletności cen niezależnie od błędu
  późniejszego planowania.
- Plan po RCE i późniejsze replany korzystają z tej samej serializowanej ścieżki
  planowania oraz atomowej publikacji.
- Zastąpiono godzinny replan przebiegiem dla każdego slotu 15-minutowego. Start
  następuje po ustabilizowaniu wejść; nieudany przebieg może zostać ponowiony w
  tym samym bezpiecznym oknie bez usuwania ostatniego zaakceptowanego planu.
- Replan jest blokowany bez zatwierdzonego RCE oraz podczas odświeżania prognoz
  po północy. Błąd replanu degraduje moduł planera, ale nie oznacza awarii bazy.
- Dodano kanoniczny `PLANNER_CONTRACT.md`: trwałe definicje bilansu,
  `soc_target`, `soc_floor`, priorytetu PV, BUY/SELL, etapów i walidacji.

## 0.34.5

- `soc_floor` is now derived per accepted battery-sale slot from its planned
  ending SOC; it is no longer copied from the active Deye TOU program.
- Outside deliberate battery sale, `soc_floor` falls back to the technical
  reserve and does not constrain battery discharge for native consumption.
- `soc_target` remains the independent energy commitment and sale safeguard.

## 0.34.4

- `soc_floor` pozostaje wyłącznie sprzętową podłogą celowej sprzedaży baterii;
  nie jest podnoszony do `soc_target` i nie ogranicza autokonsumpcji.
- Energia dostępna dla `SELL_BAT` jest liczona wyłącznie ponad efektywnym
  progiem `max(soc_floor, soc_target)`. Gdy bieżący SOC jest niższy od targetu,
  planowana sprzedaż baterii wynosi zero również w ekonomicznym oknie SELL.
- Publikacja planu jest blokowana przez `SALE_TARGET_VIOLATION`, jeżeli celowa
  sprzedaż kończy slot poniżej targetu energetycznego.
- Przy każdym otwarciu slotu Core publikuje do Home Assistant obie ceny z tego
  samego rekordu `ems_gpt_slots`. Sensory `sensor.gpt_ems_cena_zakupu` oraz
  `sensor.gpt_ems_cena_sprzedazy` nie zależą już od starego modelu RCE.
- Wykonawca pozostaje domyślnie wyłączony.

## 0.34.3

- Cała nadwyżka PV poniżej `soc_target` jest przypisywana do baterii, także
  gdy jest mniejsza niż krok prognozy SOC 0,25%. Kwantyzacja SOC nie może już
  tworzyć fałszywego eksportu ani rekomendacji `Sprzedaż PV`.

## 0.34.2

- Wybrane okno BUY wyznacza termin osiągnięcia `soc_target`, ale nie obniża
  sufitu ładowania we wcześniejszych slotach PV.
- Przy zachowaniu bilansu PV jest kierowane do baterii przed sprzedażą, jeżeli
  zastępuje późniejszy, droższy zakup sieciowy. Dodano regresję dla sekwencji
  `PV surplus → BUY`.

## 0.34.1

- Planer publikuje wynik wieloprzebiegowy wyłącznie po zbieżności wybranych
  slotów BUY z kontraktem `soc_target`; brak zbieżności kończy przebieg
  bezpiecznym odrzuceniem.
- Ponowne liczenie bez osieroconej ochrony SOC nie może po cichu zmienić ścieżki
  zakupu. Taka zmiana również odrzuca plan zamiast publikować nieaktualny target.

## 0.34.0

- Okno `BUY` jest wyłącznie zezwoleniem. Target nie jest już zerowany na każdym
  kolejnym oknie, lecz na faktycznie wybranym ekonomicznie uzupełnieniu.
- Dodano wieloprzebiegowe planowanie: pełnohoryzontowy dispatch, wsteczny
  kontrakt SOC z wybranej ścieżki oraz ponowną optymalizację do zbieżności.
- Wcześniejszy tani BUY może zasilić baterię ponad późniejszym drogim oknem,
  z zachowaniem pojemności, sprawności, limitu 5 kW i końcowego SOC.
- `soc_target` pozostaje sufitem ładowania, a w slocie sprzedaży jest co
  najmniej równy podłodze sprzedaży `soc_floor`.
- Zablokowano niejawne zasilanie domu z sieci przy technicznym minimum SOC;
  wyjątek pozostaje wyłącznie dla potwierdzonej ekonomicznej ochrony SOC przed
  późniejszą sprzedażą.
- Dodano regresje dla taniego wieczornego BUY, drogiego porannego BUY oraz
  częściowej odbudowy przez PV. Wykonawca pozostaje domyślnie wyłączony.

## 0.33.6

- Zarchiwizowano historyczne flagi slotów w `ems_gpt_slot_legacy_flags_0335`, a następnie usunięto 16 nieużywanych kolumn dublujących polityki i ilościowe przepływy.
- Migracja jest idempotentna, oznaczona w `ems_gpt_core_migrations` i nie usuwa żadnego rekordu slotu ani wartości bez wcześniejszej kopii.
- Bez zmian w algorytmie SOC i wykonawcy; produkcja pozostaje wyłączona podczas porządkowania.

## 0.33.5

- Wprowadzono pojedyncze kanoniczne pole `market_window` o wartościach `BUY`, `SELL` albo `NEUTRAL`; wykres RCE i walidacja planu korzystają wyłącznie z niego.
- Planer przestał zapisywać i publikować dublujące flagi polityk oraz przepływów. Decyzje zachowują enumy, a źródłem prawdy dla energii są pola `planned_*_kwh`.
- Panel Planer nie pokazuje dublujących flag. Historyczne kolumny pozostają czasowo w tabeli jako nieużywana warstwa zgodności przed fizyczną migracją.
- Walidacja odrzuca niepoprawne okno rynku, ujemne przepływy oraz jednoczesne ładowanie i rozładowanie baterii. Wykonawca pozostaje domyślnie wyłączony.

## 0.33.4

- Rozszerzono bezpieczny, tylko-do-odczytu audyt `ems_gpt_slots` o osobne wyniki dla całej historii, wszystkich niezamkniętych slotów oraz niezamkniętych slotów bieżącego kontraktu `CORE_0_33_0`.
- Log startowy zawiera teraz pełny słownik 128 kolumn wraz z typem, pozycją, kluczem, wartością domyślną i liczbą wypełnionych rekordów oraz grupy potencjalnie dublujących się pól.
- Nie wykonuje migracji ani zapisu do tabeli slotów; dane audytu są podstawą kolejnego, jawnego etapu porządkowania schematu.

## 0.33.3

- Audyt kolumn `ems_gpt_slots` uruchamia się jednorazowo po starcie i zapisuje
  podsumowanie do logu dodatku, dzięki czemu wynik jest dostępny przez
  konektor Home Assistant bez tworzenia bocznego dostępu SQL.
- Audyt pozostaje wyłącznie odczytowy; schemat, dane, planer i wykonawca nie są
  modyfikowane.
- Ujednolicono także wewnętrzny numer aplikacji z metadanymi dodatku; 0.33.2
  raportowała w endpointach wcześniejszy numer wykonawczy mimo poprawnie
  zainstalowanego pakietu.

## 0.33.2

- Dodano wyłącznie odczytowy audyt kolumn kanonicznej tabeli
  `ems_gpt_slots` pod endpointem `/api/slot-column-audit`.
- Audyt zwraca pełny kontrakt `information_schema`, liczbę wypełnionych
  rekordów, grupy pól potencjalnie dublujących się oraz liczniki rozbieżności
  enumów, flag PPD i ilościowych przepływów PV.
- Ta wersja jest obowiązkowym etapem przed migracją kompaktującą: nie usuwa,
  nie przemianowuje i nie nadpisuje żadnej kolumny ani danych historycznych.
- Algorytm planera oraz wykonawca pozostają bez zmian; wykonawca nadal jest
  domyślnie wyłączony.

## 0.33.1

- Ograniczono diagnostykę `ppd_reason` do trwałego kontraktu kolumny, aby
  rozszerzone dane audytowe 0.33 nie blokowały atomowej publikacji planu.

## 0.33.0

- Planer wykonuje jawne przebiegi po wspólnej tabeli slotów 15-minutowych:
  `WINDOWS`, `LOAD`, `PV`, `SLOT_BALANCE`, `TARGET_COMMITMENT`, `DISPATCH`,
  `FLEX_SURPLUS`, `VALIDATE`. Każdy przebieg odpowiada wyłącznie za własne pola.
- `soc_target` powstaje w przebiegu wstecznym z prognozowanego deficytu zużycia,
  sprawności i zarezerwowanej przyszłej nadwyżki PV. Jest twardym warunkiem
  wykonalności oraz sufitem ładowania; posiada termin, źródło i ilość
  zarezerwowanego PV.
- `soc_floor` pozostaje wyłącznie podłogą celowej sprzedaży baterii. Zwykłe
  zużycie może zejść poniżej floor, ale nie może naruszyć przyszłego kontraktu
  targetu ani technicznego minimum SOC.
- Okno BUY jest zezwoleniem na ładowanie baterii, a nie samodzielnym zakupem
  dla odbiorników. Kolejne dostępne okno BUY jest granicą bilansu i zmienną
  pełnohoryzontowego wyboru ekonomicznego.
- Podstawowy bilans baterii i domu nie zawiera sprzedaży nadwyżki PV. Nadwyżka
  jest rozdzielana osobnym przebiegiem pomiędzy ekonomiczną sprzedaż, CWU, EV i
  redukcję; curtailment pozostaje ostatnią możliwością.
- Przekroczenie 120 sekund albo niespójność dowolnego slotu przerywa transakcję;
  częściowy plan nigdy nie zastępuje ostatniego zaakceptowanego planu.
- Panel Planer pokazuje wszystkie niezamknięte sloty. Wykres RCE obejmuje tylko
  48 godzin, oznacza bieżący slot i nie zawiera już widoku 365 dni.
- Wykonawca pozostaje domyślnie i trwale wyłączony po instalacji aktualizacji.

## 0.32.12

- Każda wyraźna dolina BUY jest poszerzana do minimalnej liczby slotów
  wynikającej z pojemności, mocy, sprawności, SOC minimalnego i bazowego SOC TOU.
- Końcowe okno BUY nie może już być cenowo poprawne, lecz fizycznie za krótkie
  do osiągnięcia wymaganego terminalnego SOC.

## 0.32.11

- Częściowe okno PV ładuje baterię w stronę targetu, lecz nie musi osiągnąć
  pełnego targetu, jeżeli prognozowana nadwyżka jest fizycznie za mała.
- Twarde osiągnięcie targetu obowiązuje na końcu okna BUY; niewykorzystany
  niedobór po PV przechodzi do następnego wykonalnego PV/BUY.

## 0.32.10

- Okna BUY wymagają wyraźnego minimum w czterogodzinnym otoczeniu; drobne
  lokalne wahania nie mogą już rozszerzyć BUY na prawie cały horyzont.
- Flagi BUY i SELL są wzajemnie wykluczające.
- Restart respektuje zapisane executor_enabled=false i nie przełącza
  samoczynnie wykonawcy z OFF na LIVE tylko dlatego, że mapowania istnieją.

## 0.32.9

- Target obliczony z energii jest zaokrąglany w górę do wykonawczego kroku
  SOC 0,25% przed optymalizacją, walidacją i publikacją.
- Usunięto fałszywe odrzucenie planu, gdy dyskretny SOC końcowy był nieznacznie
  wyższy od niezaokrąglonego targetu.

## 0.32.8

- Ostatni target dostępnego horyzontu uwzględnia wymagany terminalny SOC,
  gdy nie istnieje już następne okno PV/BUY.
- Usunięto niewykonalność planu 0.32.7, w której sufit ostatniego BUY wynosił
  15%, a warunek końcowy wymagał wyższego SOC programu TOU.

## 0.32.7

- `sale_window` jest twardą zgodą na sprzedaż z baterii; poza oknem SELL
  bateria może zasilać dom, lecz nie może eksportować energii.
- `soc_floor` ogranicza wyłącznie sprzedaż z baterii i nie blokuje zwykłej
  autokonsumpcji aż do technicznego minimum SOC.
- `soc_target` jest niezależny od floor, obejmuje zapotrzebowanie tylko do
  następnego okna PV/BUY i stanowi twardy sufit ładowania.
- PV ładuje baterię do targetu przed eksportem nadwyżki.
- Okna BUY/SELL są ponownie wyznaczane dla całego otwartego horyzontu;
  opadające ramię ceny nie jest już błędnie oznaczane jako BUY.
- Publikacja sprawdza zgodę SELL, floor sprzedaży, sufit/osiągnięcie targetu
  oraz fizyczne domknięcie bilansu każdego slotu.

## 0.32.6

- Niezrealizowany `soc_target` jest egzekwowany dopiero w najbliższym
  wykonalnym slocie PV/BUY; poza oknem uzupełnienia nie blokuje zasilania domu.
- Usunięto błąd `No feasible SOC state at horizon slot 0` przy późnym replanie.

## 0.32.5

- Naprawiono normalizację flagi `buy_window` zwracanej przez MariaDB jako
  `TINYINT`/`Decimal`: wartość `0` bezwarunkowo blokuje ładowanie sieciowe.
- Wszystkie bieżące i historyczne flagi okien są na granicy bazy zamieniane na
  jawne `True/False`; wartości inne niż `0/1/true/false` zatrzymują publikację.
- Dodano test regresyjny potwierdzający zakup wyłącznie dla wartości `True`.

## 0.32.4

- `soc_target` jest teraz twardym ograniczeniem optymalizatora, a nie opisem przepływu wyliczanym po fakcie.
- Target powstaje ponad granicą sprzedaży aktywnego programu i obejmuje zapotrzebowanie do kolejnego wykonalnego PV/BUY.
- PV odbudowuje baterię do targetu przed dopuszczeniem eksportu; wcześniejsze tańsze BUY zabezpiecza poranny deficyt.
- Publikacja planu jest blokowana, gdy wynikowy SOC znajduje się poniżej targetu.

## 0.32.3

- Naprawiono wykonawcze odczytanie `soc_floor_pct` z MariaDB. Wartość SQL jest
  teraz parsowana jako skalar, a nie jak obiekt stanu Home Assistant; poprawny
  plan sprzedaży nie jest już odrzucany jako `PLAN_FLOOR_UNAVAILABLE`.
- Dodano test regresyjny dla wartości liczbowej i tekstowej zwracanej przez
  sterownik MariaDB.

## 0.32.2

- Planer używa żywych czasów programów TOU Deye, ale ich ograniczenia SOC zawsze
  bierze z konfigurowalnego baseline. Tymczasowy `soc_target` lub `soc_floor`
  ustawiony przez wykonawcę nie może już przesunąć sprzedaży do późniejszego,
  tańszego slotu po zmianie programu.
- `BATTERY_IMPORT` przed włączeniem Grid sprawdza rzeczywisty SOC, wynikowy target
  oraz zaplanowaną energię zakupu. Jeżeli target jest już osiągnięty albo przepływ
  nie przekracza progu planu, wykonawca pozostawia import wyłączony, ustawia aktywny
  program na `Charging=Disabled` i bezpiecznie przywraca bazowy SOC.
- Dodano testy regresji izolacji baseline planera oraz brakującego baseline.
- Rozdzielono kontrakty SOC: `soc_floor` ogranicza wyłącznie celową sprzedaż,
  natomiast `soc_target` jest liczonym wstecz zapotrzebowaniem po slocie,
  koniecznym do wykonania przyszłego zużycia i zaakceptowanych przepływów do
  następnego uzupełnienia. Target nie jest kopiowany z floor ani z bieżącego SOC.
- Sprzedaż nie tworzy automatycznego obowiązku odkupienia całej sprzedanej energii.
  Planer bilansuje most energetyczny do następnego realnego uzupełnienia: najpierw
  prognozowane PV, a BUY do baterii pokrywa wyłącznie pozostały niedobór.
- Zwykłe zasilanie odbiorników z sieci nie jest decyzją zakupową EMS. Jest
  dopuszczalne ponad techniczną resztę kwantyzacji wyłącznie jako ekonomicznie
  uzasadniona ochrona SOC przed późniejszą sprzedażą i jest tak jawnie opisane.
- Plan sprzedaży nie może zostać opublikowany, jeżeli końcowy SOC slotu narusza
  `soc_floor`; przypadek taki kończy przebieg błędem `SALE_FLOOR_VIOLATION`.

## 0.32.1

- Wykonawca przed włączeniem `BATTERY_IMPORT` zapisuje pierwotny SOC aktywnego
  programu Deye i ustawia jego SOC zgodnie z wynikowym `soc_target` slotu.
- Po zakończeniu importu, osiągnięciu planowanego SOC albo nieudanym uruchomieniu
  skryptu wykonawca przywraca dokładnie zapisaną wartość programu.
- Migawka przywracania jest trwała i nie jest nadpisywana w kolejnych slotach,
  dzięki czemu zachowuje poprawną wartość także po restarcie dodatku.
- Przywracanie korzysta z konfigurowalnych wartości bazowych programów Deye
  (`20/20/40/40/40/30`), więc ręczne 100% nie stanie się nowym baseline.
- Przed `BATTERY_EXPORT` aktywny program otrzymuje wynikowy `soc_floor`, aby
  wyższy bazowy SOC programu nie zatrzymał sprzedaży przed limitem planera;
  guard `SOC po` nadal kończy eksport ilościowo i przywraca baseline.
- Bazowy SOC jest przywracany dopiero po potwierdzeniu jednocześnie wyłączonego
  ładowania sieciowego i trybu eksportu, aby nie uruchomić nieplanowanego zakupu
  ani nie zatrzymać drugiego aktywnego kierunku przepływu.
- Zakończenie sprzedaży ma wymuszoną kolejność: `Zero Export To Load`, aktywny
  program `Charging=Disabled`, przywrócenie bazowego SOC. Planowany zakup wykonuje
  kolejność odwrotną: `Charging=Grid`, `soc_target`, włączenie importu.
- Zakończenie zakupu ma niezależną kolejność bezpieczeństwa: wyłączenie
  `Battery Grid Charging`, ustawienie aktywnego programu na `Charging=Disabled`,
  a dopiero potem przywrócenie jego bazowego SOC.
- Ustawienie targetu działa fail-closed: import nie zostanie uruchomiony, jeżeli
  aktywny program lub jego encja SOC są niedostępne.

## 0.32.0

- Planer optymalizuje cały dostępny ciągły horyzont RCE z krokiem SOC 0,25%.
- Kolejne przebiegi obejmują okna, zużycie, PV, ekonomikę, uzupełnienie energii,
  wynikowe SOC, walidację i PPD.
- Tani zakup zabezpiecza przyszłe zużycie przy malejącej produkcji PV, odległą
  sprzedaż albo wymagany SOC końca horyzontu; obsługuje też odkup po sprzedaży.
- `soc_floor` i `soc_target` są wynikami zaakceptowanych przepływów, a PPD
  powstaje dopiero po zakończeniu planowania.
- Dodano testy odległego zakupu, sprzedaży i zużycia bez sprzedaży.

## 0.31.3

- `soc_floor` ogranicza wyłącznie celową sprzedaż energii z baterii; zwykłe
  zużycie domu może korzystać z baterii aż do technicznego `battery_min_soc_pct`.
- `soc_target` jest wyliczany z bilansu prognozowanego zużycia domu, pompy
  ciepła, sprawności baterii, nadwyżek PV i skończonej mocy kolejnych slotów
  zakupu.
- Usunięto archiwalne wymuszenie `evening_soc_target_pct=60%` o 19:45 oraz
  sztuczny końcowy target zależny od `historical_soc_drop_p80_pct` i
  `terminal_soc_value_weight`.
- Bufor niepewności pozostaje proporcjonalny do energii wymaganej na odcinku,
  zamiast dodawać stałą liczbę punktów SOC.
- Dodano regresje dla autokonsumpcji poniżej floor, blokady sprzedaży, bilansu
  do kolejnego zakupu, wpływu PV oraz braku zależności od godziny zegarowej.
- Bez zapisów do programów SOC Deye 1–6.

## 0.31.2

- Usunięto sztywne poranne i wieczorne sesje kupna/sprzedaży. Okna wynikają z
  cen, PPD oraz ograniczeń SOC, a nie z godziny zegarowej.
- Po wykonanej sprzedaży planer śledzi energię wymagającą odtworzenia i korzysta
  z kolejnych opłacalnych slotów zakupu aż do pokrycia deficytu, utraty
  rentowności albo osiągnięcia limitu pojemności.
- Ocena cyklu uwzględnia sprawność ładowania i rozładowania, koszt degradacji,
  minimalną marżę, `soc_floor`, `soc_target`, aktywny próg TOU i limit 5 kW.
- Wykresy RCE mają trwały poziomy pasek przewijania oraz tooltip punktu z datą
  i czasem, ceną sprzedaży i ceną zakupu w PLN/kWh z trzema miejscami po przecinku.
- Wykonawca kontroluje SOC co minutę i kończy binarny import lub eksport po
  osiągnięciu ilościowego `SOC po` zaplanowanego dla bieżącego slotu.
- Bez zapisów do programów SOC Deye 1–6.

## 0.31.1

- Dodano natywny wykres cen RCE bez zewnętrznych bibliotek.
- Widok 48-godzinny pokazuje sloty 15-minutowe, cenę sprzedaży, cenę zakupu
  z marżą oraz tła okien zakupu i sprzedaży.
- Widok 365-dniowy agreguje historię do średnich dziennych, aby nie obciążać
  panelu dziesiątkami tysięcy punktów.
- Endpoint `/api/rce-chart` jest wyłącznie odczytowy i zachowuje `no-store`.
- Bez zmian w planerze, PPD, wykonawcy LIVE i programach SOC Deye 1–6.

## 0.31.0

- Dodano osobny kontrakt gotowości `/ready`, który kontroluje świeżość telemetrii HA.
- Scheduler nie maskuje już braku telemetrii statusem `RUNNING` i heartbeat procesu.
- Po braku wejścia HA wykonawca nie wystawia ani nie wysyła nowych poleceń.
- Status API publikuje wiek ostatniej poprawnej próbki oraz liczbę kolejnych niepowodzeń.
- Dodano konfigurowalne progi `telemetry_degraded_seconds` i `telemetry_stale_seconds`.
- Dodano test regresyjny incydentu 2026-09-13 23:45–06:21.
- Wszystkie odpowiedzi API i odczyty panelu używają `no-store`; Diagnostyka pokazuje
  najnowsze raporty oraz jawny czas ostatniego odświeżenia.
- Przełączanie zakładek jest blokowane na czas aktywnego odczytu.
- Czas w panelu ma format `HH:MM:SS`, a data z czasem `YYYY-MM-DD HH:MM:SS`
  w strefie `Europe/Warsaw`, bez migracji ani zmiany semantyki pól w MariaDB.
- Widok Sugestie / TODO został skrócony do opisu i statusu; pełna rekomendacja,
  metadane przebiegu, notatka operatora oraz decyzje są dostępne w popupie.
- Recovery nadal domyka wszystkie zakończone sloty idempotentnie; brak materiału
  źródłowego pozostaje jawnym `MISSING_OUTAGE`, bez syntetycznych pomiarów.
- Bez zmian w planerze, PPD i semantyce wykonawcy LIVE. Observer pozostaje
  `SHADOW_READ_ONLY`, a `COOL_DHW` pozostaje nieaktywną zapowiedzią na lato.

## 0.30.1

- Ujednolicono dobowe początki i końce pracy CO, CWU i COOL do typu `TIME`
  (`HH:MM:SS`), zgodnego z istniejącym schematem produkcyjnym.
- Usunięto błąd startu 0.30.0 `Data too long for column
  'dhw_production_start_time'`; materializacja pozostaje idempotentna.

## 0.30.0

- Rozszerzono wykonanie, agregację godzinową, dobową i analitykę pompy ciepła
  na trzy niezależne tryby: CO, CWU i chłodzenie.
- Dla każdego trybu zapisywane są energia pobrana, energia wytworzona i COP;
  osobno utrzymywane są także sumy całej pompy oraz liczba slotów pracy.
- Dobowe wykonanie zawiera początki i końce produkcji CO, CWU i chłodzenia.
- Zarejestrowano sześć istniejących liczników energii HP z Home Assistant jako
  kontrakt uzgodnienia i późniejszego importu historii, w tym pracy letniej.
- Dodano `COOL_DHW` do panelu zarządzania jako nieaktywny proces planowany na
  lato. Nie wykonuje decyzji ani poleceń i nie wpływa na tryb LIVE.

## 0.29.1

- Przywrócono idempotentną aktualizację materializacji godzinowej i dobowej po
  każdym przejściu do nowego slotu 15-minutowego.
- Zakończone godziny są automatycznie domykane, a zaległości po przerwie
  uzupełniane bez wymyślania danych pomiarowych.
- Aktywna tabela panelu odświeża się automatycznie co 30 sekund, z pominięciem
  edytowanej konfiguracji.
- Dodano test kontraktowy chroniący połączenie harmonogramu z materializacją.

## 0.28.0

- Zakończono refaktoryzację monolitu: schemat i migracje wydzielono do
  `schema_service.py`, a planer, PPD i optymalizator HP do `planner_service.py`.
- `app.py` pozostaje warstwą kompozycji usług; algorytmy, SQL i kolejność
  publikacji planu nie zostały zmienione.
- Usunięto z panelu pole czasu ręcznego `HP_HEAT_DHW`. Ręczne
  `Włącz / Blokuj / Auto` pozostaje, a `FORCE_ON` pobiera czas z
  `hp_min_cycle_hours` w centralnej konfiguracji.
- Dodano bezsekretny plan dwóch kopii `ems_gpt`: lokalnej i na OMV,
  z retencją, sumami SHA-256, testem odtworzenia i procedurą wdrożenia.
- Bez zmian w wykonawcy LIVE, logice PPD, Observerze `SHADOW_READ_ONLY`,
  recovery, strefie `Europe/Warsaw` i ochronie programów SOC Deye 1–6.

## 0.27.5

- Usunięto tymczasowy endpoint i cały kod operatorski archiwizacji po poprawnym zakończeniu operacji 45/45.
- W bazie pozostały wyłącznie zweryfikowane kopie `archive_20260913__*` oraz 23 aktywne tabele produkcyjne.
- Bez zmian w planerze, PPD, wykonawcy, recovery, Observerze i programach SOC Deye 1–6.

## 0.27.4

- Dodano jednorazową, jawnie potwierdzaną archiwizację 45 zatwierdzonych tabel historycznych.
- Każda tabela jest kopiowana pod prefiks `archive_20260913__` wraz ze strukturą i indeksami, liczba rekordów jest porównywana, a oryginał usuwany dopiero po zgodności.
- Operacja jest wznawialna, ograniczona stałą listą i wymaga identyfikatora zweryfikowanego backupu HA.
- Bez zmian w planerze, PPD, wykonawcy, recovery, Observerze i programach SOC Deye 1–6.

## 0.27.3

- Rozszerzono audyt o lekki, wyłącznie odczytowy katalog wszystkich tabel schematu `ems_gpt`: rozmiar, estymowana liczba rekordów i zależności SQL.
- Katalog nie skanuje zawartości tabel produkcyjnych; dokładne liczenie i daty pozostają ograniczone do wymaganego audytu tabel `v3`.
- Dodano endpoint `GET /api/database-catalog` i bezsekretowe wpisy `database_catalog_*` w logu dodatku.
- Bez zmian w planerze, PPD, wykonawcy, recovery, Observerze i programach SOC Deye 1–6.

## 0.27.2

- Dodano wyłącznie odczytowy audyt obiektów MariaDB zawierających `v3` w nazwie: dokładna liczba rekordów, rozmiar, możliwy ostatni zapis oraz zależności z widoków, triggerów, procedur, zdarzeń i kluczy obcych.
- Audyt jest wykonywany raz podczas startu i zapisuje bezsekretowy manifest w logu dodatku, dzięki czemu może zostać odebrany przez konektor EMS-HASS-MCP.
- Dodano endpoint `GET /api/database-audit`; nie wykonuje on operacji DDL ani DML i nie udostępnia konfiguracji połączenia.
- Bez zmian w planerze, PPD, wykonawcy, recovery, Observerze i programach SOC Deye 1–6.

## 0.27.1

- Po restarcie przed 14:00 stan RCE odtwarza również poprawny znacznik `NEXT` zapisany poprzedniego dnia dla bieżącej doby.
- Usunięto błędne `NOT_RUN` przy kompletnych cenach; logika cen, planera, PPD i wykonawcy pozostaje bez zmian.

## 0.27.0

- Skomasowano kolejny etap analityki w jednym wydaniu: PV WAPE jest liczone wyłącznie dla aktywnych slotów produkcji, z jawną liczbą slotów PV.
- Dla sporadycznych przepływów importu i eksportu dodano MAE aktywnych slotów oraz F1 wykrycia zdarzenia; historyczne WAPE pozostaje dla ciągłości porównań.
- Dodano ocenę wiarygodności metryk narastającą do pełnego siedmiodniowego okna Core.
- Analityka wylicza ograniczone rekomendowane mnożniki korekty PV1, PV2 i zużycia; wartości są obserwacyjne i nie zmieniają planu automatycznie.
- Diagnostyka kontroluje świeżość i jakość analityki oraz zgodność najnowszego przebiegu Observera z analizą źródłową.
- Panel Analityka pokazuje nowe metryki, a Observer otrzymuje je w trwałym, wyłącznie odczytowym wejściu.
- Brak zmian w PPD, planerze, wykonawcy i programach SOC Deye 1–6; Observer pozostaje `SHADOW_READ_ONLY`.

## 0.26.21

- Oddzielono bazowe zużycie domu od odbiorników planowanych osobno: EV oraz pompy ciepła/CWU.
- Profile uczenia, Load WAPE, Load bias i błąd slotu korzystają teraz z obciążenia bazowego; całkowite wykonanie i rozliczenia energii pozostają bez zmian.
- Dodano jawny znacznik metodologii `HOUSEHOLD_EXCLUDING_EV_AND_HEAT_PUMP` do szczegółów przebiegu analityki.

## 0.26.20
- Usunięto wyścig aktualizacji z watchdogiem: serwer HTTP startuje przed inicjalizacją i odtwarzaniem bazy.
- Dodano lekki endpoint liveness `/live`, używany wyłącznie przez Supervisor do kontroli procesu.
- Endpoint `/health` nadal sprawdza pełną gotowość: bazę, stan silnika i świeżość heartbeat.

## 0.26.19
- Ujednolicono zakres WAPE, bias, SOC MAE i odchylenia finansowego z oknem jakości EMS-GPT Core.
- Obserwator nie miesza już bieżących wyników Core ze starszymi planami i wykonaniami V3.
- Dane historyczne nadal służą do budowy profili zużycia i PV; liczba slotów metryk jest zapisywana w szczegółach przebiegu.

## 0.26.18
- Doprecyzowano okno jakości analityki do slotów opublikowanych i wykonanych przez telemetrię EMS-GPT Core.
- Jawne sloty `MISSING_OUTAGE` pozostają w oknie jakości, więc przerwy i awarie nadal obniżają wynik.
- Wykluczono starsze opublikowane rekordy V3 bez wykonania Core, które w 0.26.17 nadal zaniżały ocenę.

## 0.26.17
- Naprawiono zaniżoną ocenę jakości analityki: mianownik obejmuje teraz sloty z planem opublikowanym przez EMS-GPT Core.
- Historyczne sloty importowane bez planu Core nadal uczestniczą w dostępnych metrykach, ale nie są błędnie traktowane jako braki Core.
- Brak danych w opublikowanym slocie nadal obniża wynik; liczebność okna jakości jest zapisywana w szczegółach przebiegu.

## 0.26.16
- Wydzielono wartości domyślne i ładowanie konfiguracji do modułu `config_service.py`.
- Zachowano kolejność nadpisywania: wartości domyślne → opcje dodatku → ustawienia runtime.
- Dodano testy wartości domyślnych, priorytetu ustawień runtime i izolacji konfiguracji.

## 0.26.15
- Wydzielono obliczanie czasu lokalnego i początku slotu z `app.py` do modułu `time_service.py`.
- Zachowano dotychczasową konwersję strefy czasowej i zaokrąglanie do konfigurowalnej długości slotu.
- Dodano testy granic slotów, konwersji UTC do Europe/Warsaw i walidacji długości slotu.

## 0.26.14

- Spolszczono techniczne statusy w panelu: `CONNECTED` jest prezentowane jako `POŁĄCZONY`, a `LIVE` jako `PRODUKCJA`.
- Zmieniono etykietę i potwierdzenie przycisku wykonawcy na tryb `PRODUKCJA`.
- Wartości kontraktu API pozostają bez zmian dla zgodności wykonawcy i diagnostyki.

## 0.26.13

- Wydzielono bootstrap starszych tabel i odtwarzanie przerwanych przebiegów do `recovery_service.py`.
- Zachowano idempotentne oznaczanie starych przebiegów jako `ABORTED_RECOVERED` oraz audyt diagnostyczny.
- Migracja ze źródłowej bazy pozostaje domyślnie wyłączona i korzysta z walidowanych nazw SQL.
- Bez zmian w planerze, PPD, wykonawcy, obserwatorze i programach SOC 1–6.

## 0.26.12

- Wydzielono połączenie z MariaDB i walidację identyfikatorów SQL do `database_service.py`.
- Zachowano transakcje commit/rollback, timeouty oraz ustawianie strefy czasowej sesji bazy.
- `app.py` korzysta z jednego współdzielonego adaptera bazy dla wszystkich modułów.
- Bez zmian w schemacie, planerze, PPD, wykonawcy, obserwatorze i programach SOC 1–6.

## 0.26.11

- Wydzielono współdzielony stan procesu i blokadę ciężkich zadań do `runtime_service.py`.
- Harmonogram, API i inicjalizacja nadal korzystają z jednego obiektu stanu i jednej blokady wykonania.
- Zachowano kontrakt odtwarzania, statusy modułów i ostrzeganie o oczekiwaniu na blokadę.
- Bez zmian w planerze, PPD, wykonawcy, analityce, obserwatorze i programach SOC 1–6.

## 0.26.10

- Wydzielono komunikację z Home Assistantem do `ha_gateway_service.py`.
- Odczyt stanów, wywołania usług i konwersja wartości zachowują dotychczasowe timeouty oraz obsługę błędów.
- Odczyt sześciu programów Deye TOU pozostaje wyłącznie do odczytu; programy SOC 1–6 nie są modyfikowane.
- Bez zmian w planerze, PPD, wykonawcy, analityce i obserwatorze.

## 0.26.9

- Wydzielono kanoniczny kalendarz slotów i backfill relacji do `slot_calendar_service.py`.
- Zachowano obsługę dni DST z 92, 96 albo 100 rzeczywistymi slotami oraz jednoznaczne `slot_id` w UTC.
- Dodano testy długości doby przy zmianie czasu w Europie/Warszawie.
- Bez zmian w telemetrii, planerze, PPD, wykonawcy i programach SOC 1–6.

## 0.26.8

- Wydzielono zamykanie slotów, szczegóły wykonania, agregaty i odbudowę po restarcie do `materialization_service.py`.
- Zachowano kolejność: telemetria → zamknięcie slotu → szczegóły wykonania → agregaty godzinowe/dobowe.
- Usługa otrzymuje jawne adaptery bazy, zegara, konfiguracji i audytu.
- Bez zmian w planerze, PPD, wykonawcy i programach SOC 1–6.

## 0.26.7

- Wydzielono równoległy odczyt encji Home Assistant i zapis próbek do `telemetry_service.py`.
- Zachowano mapowanie encji, normalizację W/kW/MW, znak mocy baterii i relację do kanonicznego slotu DST.
- `app.py` przekazuje telemetrii jawne adaptery źródeł, zegara i bazy.
- Bez zmian w zamykaniu slotów, planerze, PPD, wykonawcy i programach SOC 1–6.

## 0.26.6

- Dodano kafelek RCE w panelu ze statusem, dniem docelowym, liczbą slotów i wynikiem planera.
- Odtworzenie dziennego wyniku RCE po restarcie jest teraz widoczne także w logu dodatku.
- Bez zmian w przebiegu RCE, cenach, planerze, PPD, wykonawcy i programach SOC 1–6.

## 0.26.5

- Dodano jawny stan ostatniego przebiegu RCE do `/api/status`: wynik, dzień docelowy, liczba slotów i status planera.
- Po restarcie stan RCE jest odtwarzany z dziennego zdarzenia sukcesu, bez ponawiania już zakończonego importu.
- Log dodatku zapisuje rozpoczęcie, zakończenie albo błąd automatycznego importu RCE.
- Niepełna doba pozostaje `PARTIAL` i nie uruchamia publikacji planu; wyjątek ustawia stan RCE na `ERROR`.
- Bez zmian w cenach, oknach, planerze, PPD, wykonawcy i programach SOC 1–6.

## 0.26.4

- Wydzielono pobieranie prognoz PV, prognozy pogody i cen RCE do `ingestion_service.py`.
- Usługa danych otrzymuje jawne adaptery bazy, zegara, kalendarza DST, Home Assistant i audytu.
- Zachowano dotychczasowe źródła Open-Meteo i PSE oraz identyczne reguły wyznaczania okien zakupu i sprzedaży.
- Bez zmian w planerze, PPD, wykonawcy i programach SOC 1–6. Observer pozostaje `SHADOW_READ_ONLY`.

## 0.26.3

- Wydzielono ustawienia operatora, override'y procesów i cykl życia komend do `executor_service.py`.
- `app.py` przekazuje wykonawcy jawne adaptery stanu, bazy, zegara, Home Assistant i audytu.
- Zachowano dotychczasowy allowlist skryptów, potwierdzenie aktywacji, TTL komend i blokadę eksportu poniżej aktywnego floor TOU.
- Bez zmian w harmonogramie, planerze, PPD i programach SOC 1–6. Observer pozostaje `SHADOW_READ_ONLY`.

## 0.26.2

- Wydzielono serwer HTTP i komplet endpointów Ingress do `api_service.py`.
- `app.py` buduje handler z jawnych adapterów, pozostając koordynatorem uruchomienia.
- Zachowano identyczne ścieżki GET/POST, limity odpowiedzi, kontrolę zdrowia i nagłówek użytkownika Ingress.
- Panel nadal jest dostarczany z osobnego `webui.html`.
- Bez zmian w harmonogramie, planerze, PPD, wykonawcy i programach SOC 1–6.

## 0.26.1

- Wydzielono obliczenia analityczne do `analytics_service.py`; kontrakt metryk i zapisy SQL pozostały bez zmian.
- Wydzielono minutowy harmonogram do `scheduler_service.py` z jawną listą adapterów operacji.
- `app.py` koordynuje uruchomienie usług i zachowuje dotychczasowe nazwy funkcji używane przez API.
- Zachowano częstotliwości: telemetria co minutę, replan w minutach 07/22/37/52, analityka raz na godzinę oraz diagnostyka cztery razy na dobę.
- Bez zmian w PPD, programach SOC 1–6, oknach procesów i trybie wykonawcy.

## 0.26.0

- Rozpoczęto modularizację rdzenia: panel WWW przeniesiono do osobnego zasobu, a Observer, diagnostykę i cykl życia TODO do niezależnych usług.
- `app.py` pozostaje koordynatorem zgodności dla harmonogramu i API; publiczne endpointy oraz wywołania nie zmieniły nazw.
- Usługi otrzymują jawne adaptery bazy, zegara i audytu, co ogranicza sprzężenie z serwerem HTTP i ułatwia osobne testowanie.
- Obraz dodatku kopiuje wszystkie moduły Pythona i plik panelu.
- Bez zmian w PPD, planerze, programach SOC 1–6 i wykonawcy. Observer nadal działa wyłącznie jako `SHADOW_READ_ONLY`.

## 0.25.20

- Wszystkie komendy `READY_FOR_CONNECTOR`, `DISPATCHED` i `ACCEPTED` po przekroczeniu TTL są atomowo zamykane jako `EXPIRED`; diagnostyka nie raportuje już historycznych komend jako aktywnych.
- Sugestie Observera mają trwały cykl życia. Pierwsze dwa kolejne dni mają status `WATCHING`, a dopiero trzeci kolejny dzień tego samego problemu podnosi wpis do `SUGGESTED`.
- Znikające obserwacje są archiwizowane, a po północy archiwizowane są również rozpatrzone i nieaktualne wpisy.
- Nieaktualne alarmy diagnostyczne są automatycznie oznaczane jako `RESOLVED` po pierwszym raporcie, w którym problem już nie występuje.
- Dodano audytowalne decyzje operatora `ACCEPTED`, `REJECTED` i `RESOLVED` przez endpoint `/api/todo/review`.
- Observer pozostaje `SHADOW_READ_ONLY`; nie zapisuje planu, PPD, komend ani usług Home Assistant. Programy SOC 1–6 pozostają chronione.

## 0.25.19

- Ograniczono pełną odbudowę 7 dni z wykonywania co minutę do startu aplikacji i jednego przebiegu dziennie około 01:00.
- Zserializowano ciężkie zadania bazy danych uruchamiane przez silnik i ręczne endpointy.
- Naprawiono harmonogram analityki, który wcześniej sprawdzał minutę początku slotu i nie mógł trafić w minutę 8.
- Watchdog HTTP uwzględnia teraz wiek heartbeat i uznaje silnik za niesprawny po 180 sekundach bez aktualizacji.
- Bez zmian w PPD, wykonawcy i programach SOC 1–6.

## 0.25.17

- Połączono plan okna HP z bilansem energii i ścieżką SOC baterii.
- Energia HP wpływa na floor, target, rozładowanie oraz plan taniego doładowania.
- Dodano test regresyjny zapobiegający ponownemu rozdzieleniu planu HP od SOC.

## 0.25.16

- Naprawiono pobieranie godzinowej prognozy z Home Assistant: `weather.get_forecasts` jest wywoływane z wymaganym `return_response`.
- Parametr odpowiedzi jest używany wyłącznie dla usług, które zwracają dane; wywołania skryptów wykonawcy pozostają bez zmian.
- Wykonawca pozostaje domyślnie wyłączony.

# EMS-GPT Core — changelog

## 0.25.8 — poprawny kafelek stanu aplikacji

- usunięto informację o pompie cyrkulacyjnej z górnego kafelka `Stan`;
- kafelek `Stan aplikacji` pokazuje status EMS-GPT Core, wersję i czas ostatniego heartbeat;
- odczyt stanu pompy pozostaje dostępny przez API procesu, bez eksponowania go w kafelku aplikacji.
- przy każdym uruchomieniu aplikacja przechodzi w tryb produkcyjny `LIVE`, jeżeli kompletny bezpieczny zestaw skryptów wykonawczych jest dostępny;
- operator nadal może wyłączyć wykonawcę (`OFF`) i ponownie włączyć go (`LIVE`) z kafelka;
- brak pełnego mapowania skryptów powoduje bezpieczny start `OFF` zamiast częściowego sterowania.

## 0.25.7

- Planer i wykonawca respektują sprzętowy próg SOC aktywnego programu TOU Deye.
- Niewykonalna sprzedaż baterii jest blokowana z jawną diagnostyką bez zapisu programów SOC 1–6.

## 0.25.6 — uproszczenie panelu cyrkulacji

- usunięto z karty `MANUAL_CIRCULATION` dodatkową kontrolkę stanu pompy;
- rzeczywisty stan pompy przeniesiono do górnego kafelka `Stan`;
- kafelek pokazuje `DZIAŁA`, `WYŁĄCZONA` albo `BRAK DANYCH` i odświeża się co 5 sekund;
- pozostawiono przyciski procesu `Włącz / Blokuj / Auto`;
- encja `switch.sm_lite_1616r_2_pompa_cyrkulacyjna` oraz odczyt API pozostają dostępne poza kartą.

## 0.25.5 — stan cyrkulacji i poprawny status wykonawcy

- karta `MANUAL_CIRCULATION` pokazuje rzeczywisty stan przekaźnika pompy;
- wskaźnik rozróżnia `DZIAŁA`, `WYŁĄCZONA` oraz `BRAK DANYCH`;
- stan encji `switch.sm_lite_1616r_2_pompa_cyrkulacyjna` jest odświeżany co 5 sekund;
- aktywny i potwierdzony wykonawca pokazuje teraz `LIVE`, zamiast mylącego `CONNECTOR_REQUIRED`.
- naprawiono adapter usług HA: `script.turn_on` nie żąda już nieobsługiwanych danych zwrotnych, które powodowały `HTTP 400`;
- wyłączony wykonawca pokazuje teraz jednoznacznie `OFF`.

## 0.25.4 — porządek kart operatora

- usunięto kartę `HP_DHW` z panelu Procesy;
- proces `HP_DHW`, jego API, historia i skrypty „Wymuś ciepłą wodę” pozostają dostępne w backendzie;
- karta `HP_HEAT_DHW` nadal obsługuje automatyczne przełączenie `Heat+DHW` i powrót do `DHW only`.
- karta „Wykonawca” po kliknięciu udostępnia szybkie `Włącz LIVE` i `Wyłącz`;
- aktywacja LIVE wymaga potwierdzenia, kompletnego mapowania skryptów i jest zapisywana trwale bez restartu.

## 0.25.3 — właściwa polityka pompy ciepła

- Stan bazowy pompy pozostaje `DHW only`.
- `HP_HEAT_DHW` przełącza na `Heat+DHW` tylko w skonfigurowanym oknie HP, gdy minimalna prognozowana temperatura nocna 00:00–06:00 jest niższa od parametru `Nocny próg ogrzewania [°C]`.
- Brak prognozy temperatury działa fail-closed i blokuje automatyczne `Heat+DHW`.
- Po zakończeniu okna decyzja `OFF` przywraca `DHW only`.
- `HP_DHW ON/OFF` zachowuje osobną obsługę `Wymuś ciepłą wodę` i jest procesem wyłącznie na żądanie operatora.
- Executor pozostaje domyślnie w `DRY_RUN`; programy SOC 1–6 nie są modyfikowane.

## 0.25.2 — siódmy proces i komplet wykonawczy

- Dodano proces `HP_HEAT_DHW` jako niezależny od `HP_DHW`, z trwałymi decyzjami, override `AUTO/FORCE_ON/FORCE_OFF`, przebiegiem i kartą w panelu.
- `HP_HEAT_DHW` pozostaje domyślnie `ON_DEMAND`; automatyczna polityka godzinowa wymaga osobnego odbioru i nie jest aktywowana w tej wersji.
- Przygotowano mapowanie do osobnych, idempotentnych skryptów CORE `ON` i `OFF`; executor po aktualizacji nadal pozostaje w `DRY_RUN`.
- Naprawiono serializację dat w zdarzeniach override (`datetime is not JSON serializable`).
- Zachowano dynamiczne pobieranie marży zakupu z konfiguracji; `0,59 PLN/kWh` jest wyłącznie wartością domyślną.

## 0.25.1 — konfiguracja stałych operacyjnych

- Usunięto ostatnie wykonawcze zależności telemetrii od V1/V2/V3: RCE jest odczytywane z kanonicznego rekordu `ems_gpt_slots`, a moc baterii bezpośrednio z falownika Deye.
- Zachowano migrację starej opcji `legacy_helpers`; jest interpretowana jako `discharge_positive` i nie powoduje odczytu usuniętych helperów.
- Recovery SLOT/HOUR/DAILY obejmuje domyślnie 7 dni (konfigurowalne w panelu w zakresie 1–31 dni), a kolejka zamykania obsługuje do 2688 zaległych slotów.
- Brak bezpośredniego sensora baterii działa fail-closed: nie są tworzone zastępcze wartości ładowania ani rozładowania.

- Potwierdzono, że marża zakupu jest pobierana dynamicznie z ustawienia panelu; `0,59 PLN/kWh` pozostaje wyłącznie bezpieczną wartością domyślną dla nowej instalacji.
- Do panelu przeniesiono tolerancję okna zakupu, progi przepływów planowanych i technicznych oraz limity SOC floor/target.
- Konfigurowalne są okna sprzedaży oraz okna pracy HP dla dni roboczych i weekendów.
- Do panelu przeniesiono progi kompletności telemetrii, kwalifikacji do uczenia oraz progi ostrzeżeń AI Observera.
- Ujednolicono ocenę jakości SLOT, HOUR i backfillu tak, aby korzystała z jednego progu konfiguracyjnego.
- Pakiet przygotowany offline; bez publikacji, stagingu i restartu dodatku przed odbiorem RCE.

## 0.25.0 — migracja analiz, AI Observer i pełny horyzont RCE

- Rozszerzono analitykę V3 o bias PV/zużycia/importu/eksportu, błąd SOC oraz odchylenie wyniku PLN.
- Dodano AI Observer w trybie `SHADOW_READ_ONLY`: zapis wejścia, wyniku, autooceny i sugestii/TODO bez prawa zmiany planu, PPD lub urządzeń.
- Panel otrzymał osobny widok AI Observer oraz dodatkowe kolumny analityczne.
- Planer przetwarza wszystkie ciągłe przyszłe sloty z dostępnym RCE zamiast stałego limitu 96.
- Diagnostyka ocenia pełny dostępny horyzont RCE, z uwzględnieniem dób DST 92/96/100.
- Executor pozostaje wyłączony; brak zapisów do programów SOC 1–6.

## 0.24.3 — zgodność metadanych wersji

- Agregaty HOUR zapisują `source_version` wyliczany z bieżącej wersji aplikacji zamiast historycznej stałej `CORE_0_22_1`.
- Ujednolicono numer wersji runtime i manifestu lokalnego dodatku.
- Executor pozostaje wyłączony; brak zapisów do programów SOC 1–6.

## 0.24.2 — allocator PV w HOUR i DAILY

- Dodano osobne sumy PV→BAT, PV→CWU, PV→EV, PV→sieć i ograniczenia PV w HOUR.
- Dodano dobowe sumy PV→BAT, PV→CWU, PV→EV i ograniczenia PV w DAILY.
- Odbudowa po awarii wylicza pola ze SLOT bez imputacji wartości actual.
- Executor pozostaje wyłączony; brak zapisów do programów SOC 1–6.

## 0.24.1 — domknięcie audytu migracji

- Nowe rekordy wykonania procesów otrzymują kanoniczny `slot_id` już przy zapisie; nie wymagają restartowego backfillu.
- Przepływ baterii poniżej 0,050 kWh/slot jest traktowany jako techniczny i nie uruchamia obserwacji procesu importu/eksportu.
- Diagnostyka przed publikacją cen następnego dnia używa realnie dostępnego horyzontu do końca doby zamiast fałszywego wymagania 96 cen.
- Otwarte TODO diagnostyczne są deduplikowane per dzień, moduł i tytuł.
- Status AI Observer jest jawny: `DISABLED` albo `NOT_IMPLEMENTED`; sam kontrakt tabeli nie jest raportowany jako działający moduł.
- Executor pozostaje wyłączony; nie zmieniono programów SOC 1–6.

## 0.24.0

- Rozdzielono `SOC przed`, `SOC po`, `SOC floor` i `SOC target`; target nie jest już wymuszany do wartości floor.
- Końcowy przebieg SOC jest liczony sekwencyjnie, z ciągłością między sąsiednimi slotami.
- Dodano ilościowy allocator PV→BAT→CWU→EV→sieć/ograniczenie.
- Rozszerzono relacje tabel zależnych o kanoniczny `slot_id` i bezpieczny backfill danych historycznych.
- Naprawiono formatter panelu usuwający literę T z nazw BATTERY, NEUTRAL i podobnych wartości.

## 0.23.0

- Dodano kanoniczny kalendarz slotów oparty o UTC z `slot_id`, offsetem, foldem i lokalnym indeksem doby.
- Doby Europe/Warsaw mają automatycznie 92, 96 albo 100 slotów podczas zmian DST.
- RCE zachowuje ceny ujemne, oczekuje liczby slotów właściwej dla doby i jest ponawiane od 14:00 co 10 minut.
- Usunięto pełne uruchomienie planera o północy; nowy plan powstaje po kompletnym imporcie następnej doby RCE.
- Naprawiono kwalifikację procesu BATTERY_IMPORT dla polityki `BUY_ALLOWED`.
- Migracja jest addytywna: dotychczasowy `slot_start` pozostaje kluczem zgodności do osobnego odbioru przełączenia historycznych relacji SQL.

## 0.22.1 — recovery SLOT/HOUR/DAILY po awarii

- Zaległe sloty są przetwarzane chronologicznie w zakresie do 384 rekordów na cykl.
- Slot bez telemetrii po zakończeniu otrzymuje terminalny stan `MISSING_OUTAGE`; wartości actual pozostają `NULL`.
- Slot odtworzony z zachowanych próbek otrzymuje jawny stan `RECOVERED` i ocenę pokrycia.
- HOUR jest przebudowywany dla bieżącej i poprzedniej doby oraz zapisuje liczniki expected/terminal/recovered/missing, pokrycie, stan zamknięcia i bramę uczenia.
- DAILY jest przebudowywany dla obu dotkniętych dób, obsługuje 92/96/100 slotów DST, a `closed_at` jest ustawiane tylko po terminalnym zamknięciu zakończonej doby.
- Niepełne i brakujące sloty nie zasilają profili uczenia.
- Panel HOUR i DAILY pokazuje kompletność, jakość, odzyskanie, braki oraz kwalifikację do uczenia.
- Executor pozostaje wyłączony, a recovery nie steruje urządzeniami.

## 0.22.0 — kompletna, jawna macierz PPD

- Dodano trwałe, osobne flagi PPD dla sieci: `BUY_ALLOWED`, `NO_BUY` i `NEUTRAL`.
- Dodano rozdzielone decyzje sprzedaży baterii i PV: `SELL_BAT/NO_SELL_BAT` oraz `SELL_PV/NO_SELL_PV`.
- Dodano trzy wzajemnie wykluczające się stany pompy ciepła: `PREFERRED`, `NEUTRAL` i `AVOID`.
- Walidacja planu odrzuca publikację, jeśli którakolwiek grupa PPD nie jest kompletna lub jednoznaczna.
- Macierz PPD jest zapisywana zarówno w stagingu planera, jak i w opublikowanych slotach.
- Executor pozostaje wyłączony; migracja PPD nie uruchamia sterowania urządzeniami.

## 0.21.2

- Dodano tabelę „Przebiegi” z planem, stanem obserwowanym, energią i źródłem sterowania.
- Rozbieżność plan–obserwacja jest jawnie klasyfikowana; aktywność bez komendy CORE otrzymuje oznaczenie „ZEWNĘTRZNE / RĘCZNE”.
- Bieżący rekord dobowy bez zakończonej kontroli jakości jest pokazywany jako OPEN.
- Kolumny nowej tabeli można konfigurować w ostatniej karcie „Konfiguracja”.

## 0.21.1 — konfigurowalne i rozszerzone tabele

- Rozszerzono Planer o PV1/PV2, plan pompy ciepła, okna BUY/SELL, przepływy baterii, pełne decyzje PPD i przyczynę decyzji.
- Rozszerzono tabelę godzinową o SOC, liczbę slotów i czas aktualizacji.
- Rozszerzono tabelę dobową o baterię, eksport PV, pompę ciepła, CWU/CO, czasy produkcji oraz jakość i kompletność danych.
- Dodano edytor widoczności kolumn w karcie Konfiguracja, osobny dla każdego widoku tabeli.
- Wybór kolumn jest przechowywany lokalnie w przeglądarce i można go przywrócić do wartości domyślnych.
- Brak opcjonalnej encji bezpośredniej mocy baterii nie przerywa cyklu telemetrii po restarcie HA.

## 0.21.0 — pakiet offline do walidacji etapowej

- Dodano bezpieczny wybór źródła mocy baterii: dotychczasowe helpery pozostają domyślne, a `sensor.inverter_battery_power` można włączyć dopiero po potwierdzeniu znaku.
- PPD zachowuje złożoność O(n), uwzględnia konfigurowalną wartość końcowego SOC, wagę niepewności oraz bazowy cel około 60% o 20:00.
- Dodano trwałe ręczne override `AUTO/FORCE_ON/FORCE_OFF` dla sześciu procesów, z czasem ważności, przyczyną i historią.
- Dodano kontrakt komend z TTL, idempotencją intencji, wersją planu, źródłem decyzji i śladem bezpieczeństwa.
- Dodano potwierdzenia konektora `ACCEPTED/EXECUTED/REJECTED/FAILED`; wygasła komenda nie może zostać przyjęta ani wykonana.
- Executor pozostaje domyślnie wyłączony. Tryb techniczny jest domyślnie `DRY_RUN` i nie wywołuje usług Home Assistant.
- Zamknięcie slotu zapisuje plan, efektywny stan, obserwację i energię procesu; brak poboru EV jest oznaczany jako `IDLE_OR_DISCONNECTED`, a nie automatycznie jako błąd.
- Diagnostyka kontroluje wygasłe aktywne komendy, wielokrotne override oraz bramę executora.
- Alerty diagnostyczne tworzą deduplikowane po raporcie wpisy w trwałej tabeli TODO.
- Rozbudowano API o `overrides`, `commands`, `process-execution`, `todo`, zmianę override, staging komend i potwierdzenie konektora.
- Widok Procesy otrzymał responsywne sterowanie operatora dla sześciu procesów; ręczna cyrkulacja domyślnie trwa 45 minut.
- Dodano dokument etapowego odbioru. Pakiet nie jest przeznaczony do aktywacji executora przed ukończeniem bramek.

## 0.20.0

- Wykonanie zapisuje SOC początku, minimum, końca i zmianę SOC w slocie.
- Rzeczywista temperatura pochodzi z `sensor.klimat_w_ogrodzie_temperature`.
- Moce ogrzewania, CWU i chłodzenia HeishaMon są całkowane do energii kWh na slot.
- Zapisywane są energie pobrane/użytkowe oraz COP osobno dla trybów i łącznie.
- Dodano tryb HP, licznik uruchomień i godziny pracy sprężarki.
- Tabela Wykonanie pokazuje nowe pola SOC, temperatury i efektywności pompy.

## 0.19.0

- Telemetria odczytuje niezależne moce PV1 i PV2 bezpośrednio z falownika.
- Wykonanie zapisuje rzeczywistą energię PV1/PV2 w każdym slocie.
- Dodano temperatury zasilania i powrotu HP, delta T, częstotliwość i prąd sprężarki oraz przepływ.
- Slot zapisuje jednoznaczny status pracy pompy na podstawie częstotliwości sprężarki.
- Tabela Wykonanie pokazuje nowe pola PV i pompy; pola logiczne zachowują format `TAK/NIE`.
- Analityka liczy osobne WAPE dla PV1, PV2 i produkcji łącznej.

## 0.18.0

- Kompletny import 96 cen RCE automatycznie uruchamia jeden spójny cykl zależny.
- Po RCE odświeżane są prognozy PV i pogody, uzupełniane profile zużycia i publikowany Planer/PPD.
- Cykl RCE działa również podczas blokady zwykłych przeliczeń w godzinie 14:00.
- Ręczne odświeżenie RCE używa identycznego przebiegu i zwraca wynik publikacji planu.
- Niepełny import nie publikuje planu i jawnie zwraca `WAITING_FOR_96_RCE_ROWS`.

## 0.17.1

- Odtwarzanie po restarcie uzupełnia brakujące szczegóły Wykonania z telemetrii ostatnich 24 godzin.
- Migracja jest idempotentna i nie nadpisuje istniejących rekordów szczegółowych.
- Historyczne próbki CWU zapisane w kW są normalizowane podczas odtworzenia.

## 0.17.0

- Wykonanie zapisuje osobny, trwały rekord szczegółów każdego slotu.
- Dodano rzeczywistą energię EV i CWU, całkowity eksport sieciowy, liczbę próbek i procent pokrycia slotu.
- Eksport nie jest arbitralnie dzielony na PV/baterię; do czasu danych o trybie falownika ma status `UNRESOLVED`.
- Diagnostyka kontroluje średnie pokrycie telemetrii i liczbę slotów z pokryciem poniżej 80%.
- Tabela Wykonanie pokazuje nowe przepływy oraz jakość danych.
- Czujniki mocy są normalizowane do watów; obsługiwane są źródła raportujące W, kW i MW.
- Wszystkie widoki tabel mają jawny poziomy pasek przewijania.
- Binarne pola PPD są prezentowane jako `TAK` i `NIE`, bez zmiany zwykłych wartości liczbowych 0/1.
- Nagłówek tabeli i pierwsza kolumna `Slot` pozostają zablokowane podczas przewijania.
- Druga kolumna Planera i Wykonania pokazuje trwałą rekomendację planu.

## 0.16.0

- Konfigurację przeniesiono do ostatniej zakładki głównego panelu tabel.
- Parametry są grupowane w sekcje: Ceny i ekonomia, Bateria oraz Prognozy i procesy.
- Układ zakładki jest przygotowany do rozbudowy o kolejne grupy ustawień.

## 0.15.1

- Parametry operacyjne PPD usunięto z konfiguracji Supervisor po potwierdzeniu działania karty.
- Konfiguracja dodatku zawiera wyłącznie ustawienia techniczne; wartości PPD pozostają w trwałym pliku runtime.

## 0.15.0

- Panel zawiera kartę Parametry PPD z walidowanymi polami i przyciskiem Zastosuj.
- Parametry operacyjne są zapisywane trwale w `/data/runtime-settings.json`.
- Zmiany obowiązują od następnego przeliczenia bez restartu aplikacji.
- Każda aktualizacja parametrów jest zapisywana w dzienniku zdarzeń MariaDB.

## 0.14.1

- Po imporcie nowej doby RCE aplikacja natychmiast uzupełnia prognozę zużycia z profili.
- Endpoint RCE przyjmuje parametr `day=YYYY-MM-DD`, co umożliwia bezpieczne odświeżenie otwartych slotów bieżącej doby po zmianie marży.

## 0.14.0

- Harmonogram używa rzeczywistej minuty lokalnej zamiast minuty zaokrąglonego slotu.
- Naprawiono automatyczne uruchomienia planera, analityki i diagnostyki.
- Import następnej doby RCE jest ponawiany co 5 minut od 14:00 do 16:59 aż do uzyskania 96 rekordów.
- Po kompletnym imporcie RCE aplikacja odświeża Open‑Meteo przed uruchomieniem PPD.

## 0.13.0

- Pojemność, minimalny SOC, moc baterii, minimalną marżę i P80 przeniesiono do konfiguracji aplikacji.
- Progi PV→CWU i PV→EV są konfigurowalne; wartości startowe to 2,0 kW i 1,5 kW.
- CWU otrzymuje nadwyżkę po osiągnięciu dynamicznego celu SOC, bez wcześniejszego sztucznego wymogu 90%.
- EV zachowuje priorytet po CWU i rezerwę 20 punktów procentowych ponad cel SOC.

## 0.12.1

- Do konfiguracji dodano sprawność ładowania i rozładowania baterii.
- Do konfiguracji dodano koszt degradacji magazynu energii w PLN/kWh.
- PPD oraz okna ekonomiczne RCE nie zależą już od helperów V3 dla tych parametrów.

## 0.12.0

- Marża zakupu została przeniesiona do edytowalnej konfiguracji aplikacji.
- Domyślna wartość `purchase_margin_pln_kwh` wynosi 0,59 PLN/kWh.
- Import RCE nie zależy już od zerowego helpera pozostałego po V3.

## 0.11.1

- Źródło PV1/PV2 zmieniono na dedykowane encje Open‑Meteo.
- Prognoza temperatury, zachmurzenia i opadów pochodzi z godzinowej prognozy `weather.dom` (Open‑Meteo).
- Usunięto zależność prognoz aplikacji od Forecast.Solar.

## 0.11.0

- PPD zapisuje trwałą macierz sześciu procesów wraz z decyzją, przyczyną i ważnością.
- Dodano procesy BATTERY_IMPORT, BATTERY_EXPORT, PV_CWU, PV_EV, MANUAL_CIRCULATION i HP_DHW.
- Sprzedaż baterii jest wyznaczana niezależnie od starego planu V3 oraz blokowana poniżej podłogi SOC.
- Panel otrzymał widok Procesy; wszystkie decyzje pozostają niewykonywalne bez przyszłego konektora.

## 0.10.0

- Aplikacja tworzy sezonowe profile rozkładu produkcji PV z rzeczywistych wykonań.
- Dzienne prognozy Forecast.Solar PV1/PV2 są rozkładane na kwadranse z zachowaniem dokładnej sumy energii.
- Prognozy PV nie zależą już od ciągłego zapisu wykonywanego przez produkcyjny V3.

## 0.9.0

- Analityka tworzy 672 profile zużycia: dzień tygodnia × godzina × kwadrans.
- Profile przechowują średnią, średnią obciętą, P80, liczbę próbek i WAPE.
- Prognoza brakującego zużycia korzysta z profilu rzeczywistych wykonań po minimum trzech próbkach.

## 0.8.0

- Wykonanie: bezpośrednie próbkowanie dokładnej mocy ładowania i rozładowania baterii.
- Wykonanie: trwała energia ładowania/rozładowania w zamykanym slocie.
- Telemetria: dodano moc EV i CWU do danych źródłowych przyszłej analityki procesów.
- Odczyty Home Assistant są wykonywane równolegle, aby skrócić cykl próbkowania.

## 0.7.1

- Idempotentne odzyskiwanie przerwanych przebiegów planera i analityki po restarcie aplikacji.

## 0.7.0

- PPD: pełny, ciągły horyzont 96 slotów i wyłącznie ceny `PSE_API`.
- PPD: liniowy przebieg wsteczny dla przyszłych cen i ryzyka pogodowego.
- PPD: koszt odtworzenia energii uwzględnia sprawność, degradację i minimalną marżę.
- Analityka: trwałe przebiegi, jakość slotów i WAPE dla PV, zużycia, importu i eksportu.
- Diagnostyka: raporty świeżości telemetrii, kompletności planu, cen, wykonania i zablokowanych przebiegów.
- Panel: nowe widoki Analityka i Diagnostyka.

## 0.6.0 — 2026-09-09

- wdrożono aplikację Supervisor bez oznaczenia V3/V4;
- dodano własną ikonę i panel Ingress;
- utworzono odseparowaną bazę MariaDB `ems_gpt`;
- zmigrowano 50 tabel `ems_gpt_*` 1:1;
- odcięto konto aplikacji od bazy rekordera po migracji;
- przeniesiono zegar slotów, telemetrię i wznowienie po restarcie;
- przeniesiono etapowy planer oraz atomową publikację;
- przeniesiono dynamiczne SOC floor/target i polityki PPD;
- dodano bezpośredni import RCE PSE z dynamiczną marżą;
- dodano zamykanie wykonania, agregację godzinową i otwartą dobę;
- dodano profilowanie brakującego zużycia;
- wykonawca urządzeń pozostaje wyłączony do czasu wdrożenia konektora.

## Testy odbiorowe

- kompilacja Python: OK;
- instalacja i healthcheck: OK;
- MariaDB i odczyt HA: OK;
- migracja 50 tabel: OK;
- plan manualny: ACCEPTED, 54 otwarte sloty opublikowane;
- zapis wykonania: OK, slot zamknięty z 17 próbek;
- agregacja godzinowa: OK;
- agregacja dobowa: OK;
- restart wyłącznie aplikacji: OK, plan i baza zachowane;
- brak poleceń do urządzeń: potwierdzony.
