"""Collect public local-station readings, retaining the provider's observation time."""
import json
import math
import re
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

DOMUSNOVAS_URL = 'http://domusnovasmeteo.altervista.org/Domusmeteo_attuale.htm'
ALBERTI_URL = 'https://www.weatherlink.com/embeddablePage/show/005f46805f72422cb4fb809dfdda94b9/summary'
GEREMEAS_URL = 'https://www.weatherlink.com/embeddablePage/show/b705697e439146cf9ce467059fbd41d9/summary'
OLBIA_URL = 'https://www.weatherlink.com/embeddablePage/show/7c12f8f5ec584ea193e52eea4a55c0c3/summary'
PORTO_RAFAEL_URL = 'https://www.weatherlink.com/embeddablePage/show/70ab2a521ef347dea1c43d082d591bf8/wide'
LUSEI_URL = 'https://www.weatherlink.com/embeddablePage/show/a4c8319b1ee6449dabdbd71c408a078c/wide'
MARINA_OLBIA_URL = 'https://www.weatherlink.com/embeddablePage/show/a6eb95fef32b4318af568792c6a48ee2/fullscreen'
COLLINAS_URL = 'https://www.weatherlink.com/embeddablePage/show/865c69d0529a4b2d907ab00a67d2935d/signature'
CORALLINA_URL = 'https://www.weatherlink.com/embeddablePage/show/e0be4d8b618a43808d5677a51aa291ea/fullscreen'
IGLESIAS_SHARE_URL = 'https://www.ecowitt.net/home/share?authorize=59C96F&device_id=bEFwaHNOMEd0TGRob0pZRStYTldyZz09'
OROSEIMETEO_URL = 'https://oroseimeteo.com/api/stations.php'
# Coordinates/elevations published by OroseiMeteo /api/rete/stazioni.php.
OROSEIMETEO_STATIONS = {
    'orosei': ('Orosei Centro', 40.378, 9.694, 19),
    'aratha': ('Agro di Aratha', 40.357, 9.692, 9),
    'calaginepro': ('Calaginepro', 40.448149, 9.785657, 8),
    'funtanas': ('Orosei Funtanas Cortesas', 40.412422, 9.753746, 15),
}
QUARTUMETEO_URL = 'https://quartuweather.altervista.org/'
DESULO_URL = 'https://www.bakumeteo.it/desulo/wltab6b.php'
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
        recorded_time = number_value(daily.get((name + ' Time').casefold(), {}).get('value'))
        # Legacy WeatherLink stations report daily extreme times as HHMM (1344 = 13:44),
        # whereas newer consoles use Unix timestamps. Only timestamps carry a date.
        is_clock = recorded_time is not None and recorded_time == int(recorded_time) and 0 <= recorded_time <= 2359 and recorded_time % 100 < 60
        if recorded_time is not None and recorded_time > 0 and not is_clock and weatherlink_time(recorded_time).astimezone(ZoneInfo('Europe/Rome')).date() != observed.astimezone(ZoneInfo('Europe/Rome')).date():
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

    # Reject gross sensor/reset artefacts (e.g. Marina di Olbia: -28.5°C
    # daily minimum alongside 20.5°C current temperature), keeping other measures.
    for key in ('tmin', 'tmax'):
        value = row[key]
        if value is not None and (abs(value - temperature) > 40 or (key == 'tmin' and value > temperature + 0.2) or (key == 'tmax' and value < temperature - 0.2)):
            row[key] = None
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


def fetch_olbia():
    # Position supplied by the user's WeatherLink shared map for Olbia.
    return fetch_weatherlink(OLBIA_URL, ('olbia', 'meteo'), dict(
        station_id='olbia-weatherlink', name='Olbia Meteo',
        lat=40.92269, lon=9.52219, elev=None, source_url=OLBIA_URL,
        observation_type='Olbia Meteo · WeatherLink pubblico',
    ))


def fetch_porto_rafael():
    # Position confirmed by the user's WeatherLink shared map.
    return fetch_weatherlink(PORTO_RAFAEL_URL, ('porto', 'rafael'), dict(
        station_id='porto-rafael-weatherlink', name='Porto Rafael',
        lat=41.181923, lon=9.380664, elev=None, source_url=PORTO_RAFAEL_URL,
        observation_type='Porto Rafael · WeatherLink pubblico',
    ))


def fetch_lusei():
    # Position confirmed by the shared map; elevation from the station operator.
    return fetch_weatherlink(LUSEI_URL, ('monte', 'lusei'), dict(
        station_id='monte-lusei-weatherlink', name='Monte Lusei',
        lat=39.807163, lon=9.326586, elev=1116, source_url=LUSEI_URL,
        observation_type='Monte Lusei · WeatherLink pubblico',
    ))


def fetch_marina_olbia():
    # Position confirmed by the user's WeatherLink shared map.
    return fetch_weatherlink(MARINA_OLBIA_URL, ('marina', 'olbia'), dict(
        station_id='marina-olbia-weatherlink', name='Marina di Olbia',
        lat=40.916139, lon=9.523805, elev=None, source_url=MARINA_OLBIA_URL,
        observation_type='Marina di Olbia · WeatherLink pubblico',
    ))


def fetch_collinas():
    # Position confirmed by the user's WeatherLink shared map.
    return fetch_weatherlink(COLLINAS_URL, ('collinas',), dict(
        station_id='collinas-weatherlink', name='Collinas',
        lat=39.63833, lon=8.84326, elev=None, source_url=COLLINAS_URL,
        observation_type='Collinas · WeatherLink pubblico',
    ))


def fetch_corallina():
    # Position confirmed by the user's WeatherLink shared map.
    return fetch_weatherlink(CORALLINA_URL, ('marina', 'costa', 'corallina'), dict(
        station_id='costa-corallina-weatherlink', name='Marina di Costa Corallina',
        lat=40.882806, lon=9.626597, elev=None, source_url=CORALLINA_URL,
        observation_type='Marina di Costa Corallina · WeatherLink pubblico',
    ))


def parse_iglesias_share(payload, received):
    if not isinstance(payload, dict) or str(payload.get('errcode')) != '0':
        raise ValueError('Ecowitt shared observations unavailable')
    groups = payload.get('data') or {}
    def record(group, field):
        return (groups.get(group) or {}).get('data', {}).get(field, {})
    def value(group, field, kind):
        entry = record(group, field)
        unit = str(entry.get('unit') or '').replace('℃', '°C').replace('℉', '°F')
        return metric_value(dict(convertedValue=entry.get('value'), unitLabel=unit), kind)
    # Use the outdoor sensor's reported local minute, never acquisition time or
    # the dashboard-wide timespan (another sensor can update independently).
    stamp = str(record('temp', 'tempf').get('time') or '')
    match = re.fullmatch(r'(Today|Yesterday) (\d{1,2}):(\d{2})', stamp)
    if not match:
        raise ValueError('Ecowitt outdoor observation timestamp unavailable')
    day = received.astimezone(ZoneInfo('Europe/Rome')).date()
    if match[1] == 'Yesterday':
        day -= timedelta(days=1)
    observed = datetime(day.year, day.month, day.day, int(match[2]), int(match[3]), tzinfo=ZoneInfo('Europe/Rome')).astimezone(timezone.utc)
    if observed > received + timedelta(minutes=5):
        raise ValueError('Ecowitt outdoor observation timestamp in future')
    temp = value('temp', 'tempf', 'temperature')
    if temp is None or not -60 <= temp <= 65:
        raise ValueError('Ecowitt outdoor temperature unavailable')
    same_day = observed.astimezone(ZoneInfo('Europe/Rome')).date() == received.astimezone(ZoneInfo('Europe/Rome')).date()
    direction = number_value(record('wind', 'winddir').get('value'))
    return dict(
        station_id='iglesias-marco-polo-ecowitt',
        name='Iglesias – Via Marco Polo (pos. indicativa)',
        lat=39.3008623, lon=8.5586961, elev=None,
        location_precision='street', location_source='OpenStreetMap: Via Marco Polo; civico 12 non verificato',
        source_url=IGLESIAS_SHARE_URL, last=observed.isoformat(),
        observation_type='Ecowitt pubblico · sensori esterni · posizione indicativa lungo la via',
        temp=temp,
        tmin=value('temp', 'min_daily_tempf', 'temperature') if same_day else None,
        tmax=value('temp', 'max_daily_tempf', 'temperature') if same_day else None,
        rh=value('temp', 'humidity', 'humidity'),
        wind_kmh=value('wind', 'windspeedmph', 'wind'),
        wind_dir=direction if direction is not None and 0 <= direction <= 360 else None,
        gust_kmh=value('wind', 'max_daily_windgustmph', 'wind') if same_day else None,
        pressure_hpa=value('pressure', 'baromrelin', 'pressure'),
        rain_today=value('rain', 'dailyrainin', 'rain') if same_day else None,
        rain_rate=value('rain', 'rainratein', 'rain_rate'), rain_48h=None,
    )


def fetch_iglesias():
    params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(IGLESIAS_SHARE_URL).query))
    # Read-only request used by the public share page, not the account API.
    request = urllib.request.Request('https://www.ecowitt.net/index/home',
        data=urllib.parse.urlencode(params).encode(),
        headers={'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'})
    with urllib.request.urlopen(request, timeout=40) as response:
        received = parsedate_to_datetime(response.headers['Date']) if response.headers.get('Date') else datetime.now(timezone.utc)
        return parse_iglesias_share(json.load(response), received)


def parse_oroseimeteo(payload, key):
    if not isinstance(payload, dict) or payload.get('ok') is not True:
        raise ValueError('OroseiMeteo public observations unavailable')
    raw = payload.get('stations', {}).get(key)
    if not isinstance(raw, dict):
        raise ValueError('OroseiMeteo station unavailable')
    name, lat, lon, elev = OROSEIMETEO_STATIONS[key]
    if raw.get('name') != name:
        raise ValueError('OroseiMeteo station identity changed')
    observed = datetime.strptime(raw['updated_at'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=ZoneInfo('Europe/Rome'))
    generated = datetime.fromisoformat(payload['generated_at'].replace('Z', '+00:00'))
    if generated.tzinfo is None:
        raise ValueError('OroseiMeteo dataset timezone unavailable')
    current_day = observed.date() == generated.astimezone(ZoneInfo('Europe/Rome')).date()
    temp = number_value(raw.get('temperature'))
    if temp is None or not -60 <= temp <= 65:
        raise ValueError('OroseiMeteo outdoor temperature unavailable')
    def number(field):
        return number_value(raw.get(field))
    direction = number('wind_dir_deg')
    return dict(
        station_id='oroseimeteo-' + key, name=name,
        lat=lat, lon=lon, elev=elev,
        last=observed.astimezone(timezone.utc).isoformat(),
        source_url='https://oroseimeteo.com/stazioni.php',
        observation_type='OroseiMeteo · ' + str(raw.get('detail') or 'rete locale'),
        temp=temp, tmin=number('temp_min') if current_day else None,
        tmax=number('temp_max') if current_day else None,
        rh=number('humidity'), pressure_hpa=number('pressure'),
        wind_kmh=number('wind_speed'),
        wind_dir=direction if direction is not None and 0 <= direction <= 360 else None,
        gust_kmh=number('gust_day') if current_day else None,
        rain_today=number('rain_day') if current_day else None,
        rain_rate=number('rain_rate'), rain_48h=None,
    )


@lru_cache(maxsize=1)
def oroseimeteo_payload():
    # One public request for all four stations per collector invocation.
    with urllib.request.urlopen(OROSEIMETEO_URL, timeout=40) as response:
        return json.load(response)


def fetch_oroseimeteo(key):
    return parse_oroseimeteo(oroseimeteo_payload(), key)


def parse_quartumeteo(current, extremes):
    if not isinstance(current, dict):
        raise ValueError('QuartuMeteo observations unavailable')
    observed = datetime.fromisoformat(str(current.get('timestamp') or '').replace('Z', '+00:00'))
    if observed.tzinfo is None:
        raise ValueError('QuartuMeteo observation timezone unavailable')
    def number(payload, key):
        value = number_value(payload.get(key))
        return round(value, 1) if value is not None else None
    temp = number(current, 'outsideTempC')
    if temp is None or not -60 <= temp <= 65:
        raise ValueError('QuartuMeteo outdoor temperature unavailable')
    try:
        extreme_time = datetime.fromisoformat(str(extremes.get('timestamp') or '').replace('Z', '+00:00'))
        daily_valid = extreme_time.tzinfo is not None and extreme_time.astimezone(ZoneInfo('Europe/Rome')).date() == observed.astimezone(ZoneInfo('Europe/Rome')).date()
    except (AttributeError, TypeError, ValueError):
        daily_valid = False
    direction = number(current, 'windDirectionDeg')
    # Coordinates and 15m elevation from the operator-linked AWEKAS station 10101.
    return dict(
        station_id='quartu-quartumeteo', name='Quartu Sant’Elena – QuartuMeteo',
        lat=39.2333333333, lon=9.1833333333, elev=15,
        source_url=QUARTUMETEO_URL, last=observed.astimezone(timezone.utc).isoformat(),
        observation_type='QuartuMeteo.it · Davis Vantage Pro2 Wireless · dati pubblici del gestore',
        temp=temp,
        tmin=number(extremes, 'dayLowOutsideTempC') if daily_valid else None,
        tmax=number(extremes, 'dayHighOutsideTempC') if daily_valid else None,
        rh=number(current, 'outsideHumidityPct'),
        pressure_hpa=number(current, 'barometerHpa'),
        wind_kmh=number(current, 'windSpeedKmh'),
        wind_dir=direction if direction is not None and 0 <= direction <= 360 else None,
        gust_kmh=number(extremes, 'dayHighWindGustKmh') if daily_valid else None,
        rain_today=number(current, 'dayRainMm'), rain_rate=number(current, 'rainRateMmPerHour'),
        rain_48h=None,
    )


def fetch_quartumeteo():
    with urllib.request.urlopen(QUARTUMETEO_URL + 'current.json', timeout=40) as response:
        current = json.load(response)
    try:
        with urllib.request.urlopen(QUARTUMETEO_URL + 'day-extremes.json', timeout=40) as response:
            extremes = json.load(response)
    except (OSError, ValueError):
        extremes = {}
    return parse_quartumeteo(current, extremes)


def parse_desulo(page):
    parser = PageText()
    parser.feed(page)
    text = ' '.join(' '.join(parser.parts).split())
    stamp = re.search(r'Dati aggiornati il (\d{2}/\d{2}/\d{2}) alle ore (\d{1,2})[.:](\d{2})', text)
    if not stamp:
        raise ValueError('Desulo observation time unavailable')
    observed = datetime.strptime(f'{stamp[1]} {stamp[2]}:{stamp[3]}', '%d/%m/%y %H:%M').replace(tzinfo=ZoneInfo('Europe/Rome'))
    def number(label, unit):
        found = re.search(label + r'\s*([+-]?\d+(?:[.,]\d+)?)\s*' + unit, text, re.I)
        return number_value(found[1]) if found else None
    temp = number('Temperatura:', '°C')
    if temp is None or not -60 <= temp <= 65:
        raise ValueError('Desulo outdoor temperature unavailable')
    extremes = re.search(r'Estremi Oggi:\s*([+-]?\d+(?:[.,]\d+)?)\s*°C\s*\(\s*\d{1,2}[.:]\d{2}\s*\)\s*([+-]?\d+(?:[.,]\d+)?)\s*°C', text)
    wind = re.search(r'Velocit[aà] attuale:\s*[+-]?\d+(?:[.,]\d+)?\s*Km/h\s*([NSEWO]{1,3})\b', text, re.I)
    compass = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']
    direction = wind[1].upper().replace('O', 'W') if wind else None
    # Coordinates/elevation: Sardegna Clima station Desulo (NU), id 64.
    return dict(
        station_id='desulo-bakumeteo', name='Desulo – BakuMeteo',
        lat=40.0157, lon=9.2311, elev=945,
        source_url=DESULO_URL, last=observed.astimezone(timezone.utc).isoformat(),
        observation_type='BakuMeteo / Sardegna Clima · Desulo 945 m',
        temp=temp, tmin=number_value(extremes[2]) if extremes else None,
        tmax=number_value(extremes[1]) if extremes else None,
        rh=number('Umidit[aà]:', '%'), pressure_hpa=number('Pressione:', 'hPa'),
        wind_kmh=number('Velocit[aà] attuale:', 'Km/h'),
        wind_dir=compass.index(direction) * 22.5 if direction in compass else None,
        gust_kmh=number('Raffica giornaliera:', 'Km/h'),
        rain_today=number('Pioggia giorno:', 'mm'),
        rain_rate=number('Intensit[aà]:', 'mm/h'), rain_48h=None,
    )


def fetch_desulo():
    with urllib.request.urlopen(DESULO_URL, timeout=40) as response:
        raw = response.read()
    try:
        page = raw.decode('utf-8')
    except UnicodeDecodeError:
        page = raw.decode('latin-1')
    return parse_desulo(page)


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
    return dict(source='Stazioni amatoriali · WeatherLink / Ecowitt / Domusnovas Meteo / OroseiMeteo / QuartuMeteo / BakuMeteo',
                generated_at=datetime.now(timezone.utc).isoformat(), stations=rows, errors=failures)


def main():
    try:
        previous = json.loads(OUTPUT.read_text())
    except (OSError, ValueError):
        previous = {}
    payload = collect([('domusnovas-meteo', fetch_domusnovas), ('liceo-alberti-cagliari', fetch_alberti), ('geremeas-weatherlink', fetch_geremeas), ('olbia-weatherlink', fetch_olbia), ('porto-rafael-weatherlink', fetch_porto_rafael), ('monte-lusei-weatherlink', fetch_lusei), ('marina-olbia-weatherlink', fetch_marina_olbia), ('collinas-weatherlink', fetch_collinas), ('costa-corallina-weatherlink', fetch_corallina), ('iglesias-marco-polo-ecowitt', fetch_iglesias), ('quartu-quartumeteo', fetch_quartumeteo), ('desulo-bakumeteo', fetch_desulo)] + [('oroseimeteo-' + key, lambda key=key: fetch_oroseimeteo(key)) for key in OROSEIMETEO_STATIONS], previous)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
    for row in payload['stations']:
        print(row['station_id'], row['last'])
    if payload['errors']:
        print('Sources temporarily unavailable:', payload['errors'])


if __name__ == '__main__':
    main()
