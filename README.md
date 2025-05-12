# Integracja Home Assistant z Pstryk API

[![Wersja](https://img.shields.io/badge/wersja-1.5.0-blue)](https://github.com/balgerion/ha_Pstryk/)

Integracja dla Home Assistant umożliwiająca śledzenie aktualnych cen energii elektrycznej oraz prognoz z platformy Pstryk.

## Funkcje  
- 🔌 Aktualna cena kupna i sprzedaży energii  
- 📅 Tabela 24h z prognozowanymi cenami  
- ⚙️ Konfigurowalna liczba "najlepszych godzin"  
- 🔻 Konfigurowalna liczba "najgorszych godzin"  
- 🕒 Cena w następnej godzinie  
- 🕰️ Automatyczna konwersja czasu UTC → lokalny  
- 🔄 Dane są aktualizowane minutę po pełnej godzinie  
- 🛡️ Debug i logowanie  
- 🧩 Konfiguracja z poziomu integracji  
- 🔑 Walidacja klucza API / Cache danych / Zabezpieczenie przed timeoutem API  
- 📡 Integracja wystawia po lokalnym MQTT dane dla EVCC  


## TODO
-
Taryfy dla Victron MQTT 
## Instalacja

### Metoda 1: Via HACS
1. W HACS przejdź do `Integracje`
2. Kliknij `Dodaj repozytorium`
3. Wpisz URL: `https://github.com/balgerion/ha_Pstryk`
4. Wybierz kategorię: `Integration`
5. Zainstaluj i zrestartuj Home Assistant

### Metoda 2: Ręczna instalacja
1. Utwórz folder `custom_components/pstryk` w katalogu konfiguracyjnym HA
2. Skopiuj pliki:
init.py
manifest.json
config_flow.py
const.py
sensor.py
logo.png (opcjonalnie)
3. Zrestartuj Home Assistant

## Konfiguracja
1. Przejdź do `Ustawienia` → `Urządzenia i usługi`
2. Kliknij `Dodaj integrację`
3. Wyszukaj "Psrryk Energy"
4. Wprowadź dane:
- **Klucz API**: Twój klucz z platformy Pstryk
- **Liczba najlepszych cen kupna**: (domyślnie 5)
- **Liczba najlepszych cen sprzedaży**: (domyślnie 5)

## Scrnshoty

![{5787179E-9AE8-415F-9D93-2884EF544768}](https://github.com/user-attachments/assets/3fdea007-8c43-40a0-a188-455dca9b805a)
![{34821286-F16C-496F-B87A-D2B534190B42}](https://github.com/user-attachments/assets/9fe65b19-dd5b-4d4f-bf0e-6980e41fb1f0)
![{CF93284B-2363-4494-A2CC-D10D1690B0D5}](https://github.com/user-attachments/assets/0665485a-3487-4b7f-9785-10846bbf3733)
![{4AB44C7A-202D-435D-A0CB-5753C591C087}](https://github.com/user-attachments/assets/e9a703d7-f7b5-4ea6-b497-b47be0226130)




## Użycie
### Dostępne encje
| Nazwa encji                          | Opis                          |
|--------------------------------------|-------------------------------|
| `sensor.pstryk_current_buy_price`    | Aktualna cena kupna + tabela           |
| `sensor.pstryk_current_sell_price`   | Aktualna cena sprzedaży   + tabela     |


Przykładowa Automatyzacja:

Włączanie bojlera
![IMG_4079](https://github.com/user-attachments/assets/ccdfd05c-3b38-4af5-a8db-36fe7fd645ee)

```yaml
alias: Optymalne grzanie wody
description: ""
triggers:
  - minutes: "1"
    trigger: time_pattern
    hours: /1
conditions:
  - condition: template
    value_template: >
      {% set current_hour = now().replace(minute=0, second=0,
      microsecond=0).isoformat(timespec='seconds').split('+')[0] %}

      {% set best_hours = state_attr('sensor.pstryk_current_buy_price',
      'best_prices') | map(attribute='start') | list %}

      {{ current_hour in best_hours }}
actions:
  - variables:
      current_hour: >-
        {{ now().replace(minute=0, second=0,
        microsecond=0).isoformat(timespec='seconds').split('+')[0] }}
  - choose:
      - conditions:
          - condition: state
            entity_id: light.shellypro3_34987a49142c_switch_2
            state: "off"
        sequence:
          - target:
              entity_id: switch.shellypro3_34987a49142c_switch_2
            action: switch.turn_on
            data: {}
          - data:
              message: |
                Grzanie włączone! Godzina: {{ current_hour }}, Cena: {{
                  state_attr('sensor.pstryk_current_buy_price', 'best_prices')
                  | selectattr('start', 'equalto', current_hour)
                  | map(attribute='price')
                  | first
                }} PLN
            action: notify.mobile_app_balg_iphone
      - conditions:
          - condition: state
            entity_id: light.shellypro3_34987a49142c_switch_2
            state: "on"
        sequence:
          - delay:
              hours: 1
              minutes: 5
          - target:
              entity_id: switch.shellypro3_34987a49142c_switch_2
            action: switch.turn_off
            data: {}


```

Rozładowanie magazynu energii - Sprzedaż po najlepszej cenie


##EVCC

###Scrnshoty
![{9EE9344A-DAA3-42C0-9084-D2F3B5AE1B08}](https://github.com/user-attachments/assets/1812343e-3fa7-4e44-9205-f8f0c524f771)
![{6B2DA5CA-5797-43FB-88DC-F908D9B72501}](https://github.com/user-attachments/assets/0a4a6e46-8b49-4a6b-8676-3e57bf272bf8)


###Konfiguracja
Tarrifs:
```yaml
currency: PLN
grid:
  type: custom
  forecast:
    source: mqtt
    topic: energy/forecast/buy

feedin:
  type: custom
  forecast:
    source: mqtt
    topic: energy/forecast/sell
```
