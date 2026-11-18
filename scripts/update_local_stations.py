"""Collect public local-station readings, retaining the provider's observation time."""
import json
import math
import re
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

DOMUSNOVAS_URL = 'http://domusnovasmeteo.altervista.org/Domusmeteo_attuale.htm'
ALBERTI_URL = 'https://www.weatherlink.com/embeddablePage/show/005f46805f72422cb4fb809dfdda94b9/summary'
GEREMEAS_URL = 'https://www.weatherlink.com/embeddablePage/show/b705697e439146cf9ce467059fbd41d9/summary'
OUTPUT = Path('data/local_stations.json')


class PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, text):
        if not self.hidden and text.strip():
            self.parts.append(text.strip())


def parse_domusnovas(page):
    parser = PageText()
    parser.feed(page)
    text = ' '.join(' '.join(parser.parts).split())
    stamp = re.search(r'Dati aggiornati alle ore (\d{1,2}:\d{2}) del (\d{2}/\d{2}/\d{2})\b', text)
    if not stamp:
        raise ValueError('Domusnovas: missing observation time')
    observed = datetime.strptime(f'{stamp[2]} {stamp[1]}', '%d/%m/%y %H:%M').replace(tzinfo=ZoneInfo('Europe/Rome'))

    def number(label, unit=None):
        pattern = label + r'\s*([+-]?\d+(?:[.,]\d+)?)' + (r'\s*' + unit if unit else '')
        match = re.search(pattern, text, re.I)
        return float(match[1].replace(',', '.')) if match else None

    direction = re.search(r'Direzione del vento\s*([NSEWO]{1,3})\b', text, re.I)
    compass = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']
    direction = direction[1].upper().replace('O', 'W') if direction else None
    temperature = number(r'Temperatura', '°C')
    if temperature is None or not -60 <= temperature <= 65:
        raise ValueError('Domusnovas: missing or invalid temperature')
    return dict(
        station_id='domusnovas-meteo', name='Domusnovas Meteo',
        lat=39.3167, lon=8.65, elev=138,
        last=observed.astimezone(timezone.utc).isoformat(),
        source_url=DOMUSNOVAS_URL,
        observation_type='Domusnovas Meteo · Davis Vantage Pro2 Wireless',
        temp=temperature, tmin=number('MIN', '°C'), tmax=number('MAX', '°C'),
        rh=number(r"Umidit[aà]['’]?", '%'),
        pressure_hpa=number('Pressione', 'hPa'),
        wind_kmh=number(r"Velocit[aà]['’]? del vento", 'km/hr'),
        wind_dir=compass.index(direction) * 22.5 if direction in compass else None,
        gust_kmh=number(r'Max Vento\s*(?:[NSEWO]+\s*)?a', 'km/hr'),
        rain_today=number('Precipitazione Oggi', 'mm'),
        rain_rate=number(r"Intensit[aà]['’]? pioggia", 'mm/hr'),
        rain_48h=None,
    )


def number_value(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, str):
            value = value.strip().replace('\u00a0', '').replace(' ', '')
            if ',' in value and '.' in value:
                if value.rfind(',') > value.rfind('.'):
                    value = value.replace('.', '').replace(',', '.')
                else:
                    value = value.replace(',', '')
            else:
                value = value.replace(',', '.')
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def metric_value(record, kind):
    value = number_value(record.get('convertedValue'))
    unit = str(record.get('unitLabel') or '').replace(' ', '').replace('°', '').lower()
    if value is None:
        return None
    if kind == 'rain_rate' and record.get('sensorDataTypeId') == 615 and unit in ('mm', 'in', 'inch'):
        unit += '/h'
    if kind == 'temperature':
        return value if unit == 'c' else round((value - 32) * 5 / 9, 1) if unit == 'f' else None
    factors = {
        'humidity': {'%': 1},
        'wind': {'km/h': 1, 'km/hr': 1, 'mph': 1.609344, 'm/s': 3.6, 'knots': 1.852, 'kt': 1.852},
        'pressure': {'hpa': 1, 'mb': 1, 'mbar': 1, 'inhg': 33.8638867, 'mmhg': 1.3332239},
        'rain': {'mm': 1, 'in': 25.4, 'inch': 25.4, '"': 25.4},
        'rain_rate': {'mm/h': 1, 'mm/hr': 1, 'in/h': 25.4, 'in/hr': 25.4, '"/h': 25.4, '"/hr': 25.4},
    }
    factor = factors.get(kind, {}).get(unit)
    return round(value * factor, 1) if factor is not None else None


def weatherlink_time(value):
    value = number_value(value)
    if value is None or value <= 0:
        raise ValueError('WeatherLink: missing observation timestamp')
    return datetime.fromtimestamp(value / 1000 if value > 100_000_000_000 else value, timezone.utc)


def parse_alberti(payload, station=None):
    if not isinstance(payload, dict) or payload.get('noAccess'):
        raise ValueError('Alberti: public observations unavailable')
    observed = weatherlink_time(payload.get('lastReceived'))
    current = {r.get('sensorDataName', '').casefold(): r for r in payload.get('currConditionValues', [])}
    daily = {r.get('sensorDataName', '').casefold(): r for r in payload.get('highLowValues', [])}
    def field(name, kind):
        return metric_value(current.get(name.casefold(), {}), kind)
    def extreme(name, kind):
        recorded_time = daily.get((name + ' Time').casefold(), {}).get('value')
        if recorded_time is not None and weatherlink_time(recorded_time).astimezone(ZoneInfo('Europe/Rome')).date() != observed.astimezone(ZoneInfo('Europe/Rome')).date():
            return None
        return metric_value(daily.get(name.casefold(), {}), kind)
    temperature = field('Temp', 'temperature')
    if temperature is None or not -60 <= temperature <= 65:
        raise ValueError('Alberti: missing or invalid outdoor temperature')
    rain = next((r for r in payload.get('aggregatedValues', []) if r.get('sensorDataName') == 'Rain'), {})
    rain_today = metric_value({'convertedValue': rain.get('convertedValues', {}).get('DAY'), 'unitLabel': rain.get('unitLabel')}, 'rain')
    # The embed itself uses raw degrees here; convertedValue can contain a sector-conversion artefact.
    direction = number_value(current.get('wind direction', {}).get('value'))
    row = dict(
        station_id='liceo-alberti-cagliari', name='Cagliari – Liceo Alberti',
        lat=39.208, lon=9.12, elev=5,
        last=observed.isoformat(), source_url=ALBERTI_URL,
        observation_type='Liceo Alberti Cagliari · Davis Vantage Pro2 Wireless · WeatherLink pubblico',
        temp=temperature, tmin=extreme('Low Temp', 'temperature'), tmax=extreme('High Temp', 'temperature'),
        rh=field('Hum', 'humidity'), pressure_hpa=field('Barometer', 'pressure'),
        wind_kmh=field('Wind Speed', 'wind'), wind_dir=direction if direction is not None and 0 <= direction <= 360 else None,
        gust_kmh=extreme('High Wind Speed', 'wind'),
        rain_today=rain_today, rain_rate=field('Rain Rate', 'rain_rate'), rain_48h=None,
    )

    if station:
        row.update(station)
    return row


def fetch_domusnovas():
    with urllib.request.urlopen(DOMUSNOVAS_URL, timeout=40) as response:
        # The legacy Davis page uses Latin-1 (degree signs and Italian accents).
        page = response.read().decode(response.headers.get_content_charset() or 'latin-1')
    return parse_domusnovas(page)


def fetch_weatherlink(url, identity, station=None):
    with urllib.request.urlopen(url, timeout=40) as response:
        page = response.read().decode('utf-8')
    name = re.search(r'wl\._systemName\s*=\s*"([^"]+)"', page)
    token = re.search(r'wl\._deviceUrlToken\s*=\s*"([a-f0-9]{32})"', page)
    if not name or not all(part in name[1].casefold() for part in identity) or not token:
        raise ValueError('Alberti: public embed identity changed or unavailable')
    # Follow only the public summary request advertised by this embeddable page.
    # No WeatherLink account, private API or credentials are used.
    with urllib.request.urlopen('https://www.weatherlink.com/embeddablePage/summaryData/' + token[1], timeout=40) as response:
        return parse_alberti(json.load(response), station)


def fetch_alberti():
    return fetch_weatherlink(ALBERTI_URL, ('alberti', 'cagliari'))


def fetch_geremeas():
    # Public station location: MeteoNetwork srd040 Geremeas Country Club.
    return fetch_weatherlink(GEREMEAS_URL, ('geremeas', 'sardegna'), dict(
        station_id='geremeas-weatherlink', name='Geremeas',
        lat=39.164, lon=9.386, elev=10, source_url=GEREMEAS_URL,
        observation_type='Geremeas · Davis Vantage Pro2 Wireless · WeatherLink pubblico',
    ))


def collect(sources, previous):
    retained = {row['station_id']: row for row in previous.get('stations', [])}
    rows, failures, fresh = [], {}, 0
    for station_id, fetcher in sources:
        try:
            rows.append(fetcher())
            fresh += 1
        except (OSError, ValueError, KeyError, TypeError, OverflowError) as error:
            failures[station_id] = type(error).__name__
            if station_id in retained:
                rows.append(retained[station_id])
    if not fresh:
        raise RuntimeError('No local station could be refreshed; previous dataset preserved')
    return dict(source='Stazioni locali · Domusnovas Meteo / Liceo Alberti Cagliari / Geremeas',
                generated_at=datetime.now(timezone.utc).isoformat(), stations=rows, errors=failures)


def main():
    try:
        previous = json.loads(OUTPUT.read_text())
    except (OSError, ValueError):
        previous = {}
    payload = collect([('domusnovas-meteo', fetch_domusnovas), ('liceo-alberti-cagliari', fetch_alberti), ('geremeas-weatherlink', fetch_geremeas)], previous)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
    for row in payload['stations']:
        print(row['station_id'], row['last'])
    if payload['errors']:
        print('Sources temporarily unavailable:', payload['errors'])


if __name__ == '__main__':
    main()
