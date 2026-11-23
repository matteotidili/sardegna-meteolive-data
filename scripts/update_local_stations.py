"""Collect public local-station readings, retaining the provider's observation time."""
import json
import math
import re
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import threading
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
# Panoramicams pages expose an Ecowitt public JSON feed. Multiple camera pages
# reuse each feed; keep one map station per distinct sensor rather than cloning
# the same observation at every camera position. Coordinates are the reference
# camera locations for the associated area and are therefore indicative.
PANORAMICAMS_REQUESTS = threading.BoundedSemaphore(3)

PANORAMICAMS_STATIONS = [
    dict(station_id='panoramicams-sinis', feed='GW1100A-WIFI8F00', area='sinis', name='San Giovanni di Sinis – Panoramicams', lat=39.885152, lon=8.430593, source_url='https://panoramicams.com/san-giovanni-di-sinis/'),
    dict(station_id='panoramicams-porto-pollo', feed='GW1100A-WIFI86B9', area='portopollo', name='Porto Pollo – Panoramicams', lat=41.193837, lon=9.316426, source_url='https://panoramicams.com/porto-pollo-sottovento/'),
    dict(station_id='panoramicams-costa-rei', feed='GW1100A-WIFI855D', area='costarei', name='Costa Rei – Panoramicams', lat=39.265682, lon=9.580918, source_url='https://panoramicams.com/piscina-rei-capo-ferrato/'),
    dict(station_id='panoramicams-sa-barra', feed='GW1100A-WIFI935C', area='sabarra', name='Sant’Antioco – Sa Barra', lat=39.090983, lon=8.440464, source_url='https://panoramicams.com/sa-barra-sant-antioco/'),
    dict(station_id='panoramicams-san-teodoro', feed='GW1100A-WIFI4CD4', area='santeodoro', name='San Teodoro – Panoramicams', lat=40.784039, lon=9.668824, source_url='https://panoramicams.com/la-cinta-san-teodoro/'),
    dict(station_id='panoramicams-la-caletta', feed='GW1100A-WIFI818B', area='lacaletta', name='La Caletta – Panoramicams', lat=40.602164, lon=9.748702, source_url='https://panoramicams.com/la-caletta-siniscola/'),
    dict(station_id='panoramicams-punta-saline', feed='GW1100A-WIFI3299', area='puntasaline', name='Punta Saline – Olbia', lat=40.908131, lon=9.571293, source_url='https://panoramicams.com/punta-saline-olbia/'),
    dict(station_id='panoramicams-capoterra', feed='GW1100A-WIFI3322', area='capoterra', name='Capoterra – La Maddalena', lat=39.146931, lon=9.014834, source_url='https://panoramicams.com/spiaggia-la-maddalena-capoterra/'),
    dict(station_id='panoramicams-olbia', feed='GW1100A-WIFI4E1E', area='olbia', name='Olbia – Panoramicams', lat=40.917368, lon=9.552461, source_url='https://panoramicams.com/golfo-di-olbia/'),
    dict(station_id='panoramicams-alghero', feed='GW1100A-WIFI2FF4', area='alghero', name='Alghero – Mugoni', lat=40.616597, lon=8.204152, source_url='https://panoramicams.com/mugoni-alghero/'),
    dict(station_id='panoramicams-poetto', feed='GW1100A-WIFIB452', area='poetto', name='Cagliari – Poetto', lat=39.201229, lon=9.159600, source_url='https://panoramicams.com/poetto-cagliari/'),
    dict(station_id='panoramicams-stintino', feed='GW1100A-WIFIFECC', area='stintino', name='Stintino – Le Saline', lat=40.903741, lon=8.237674, source_url='https://panoramicams.com/le-saline-stintino/'),
    dict(station_id='panoramicams-la-conia', feed='GW1100A-WIFI0928', area='laconia', name='Cannigione – La Conia', lat=41.124833, lon=9.437239, source_url='https://panoramicams.com/la-conia-golfo-di-arzachena/'),
]
QUARTUMETEO_URL = 'https://quartuweather.altervista.org/'
DESULO_URL = 'https://www.bakumeteo.it/desulo/wltab6b.php'
TONARA_URL = 'https://www.tonarameteo.it/'
LACONI_URL = 'https://www.laconimeteo.it/wxindex.php'
SERRAMANNA_URL = 'https://www.serramannameteo.it/index.htm'
MEANA_URL = 'https://www.meanasardometeo.it/'
NOLZA_URL = 'https://www.oroseimeteo.it/stazioni/nolza/wltab6b.php'
SANTELIAS_URL = 'https://stazioni.oroseimeteo.it/Santelias/wltab6buvrad.php'
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
        'wind': {'km/h': 1, 'km/hr': 1, 'mph': 1.609344, 'm/s': 3.6, 'knots': 1.852, 'knot': 1.852, 'kt': 1.852, 'kts': 1.852, 'kn': 1.852},
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


def parse_desulo(page, station=None):
    parser = PageText()
    parser.feed(page)
    text = ' '.join(' '.join(parser.parts).split())
    stamp = re.search(r'Dati aggiornati il (\d{2}/\d{2}/\d{2}) alle ore (\d{1,2})[.:](\d{2})(?::(\d{2}))?', text)
    if not stamp:
        raise ValueError('Desulo observation time unavailable')
    observed = datetime.strptime(f'{stamp[1]} {stamp[2]}:{stamp[3]}:{stamp[4] or "00"}', '%d/%m/%y %H:%M:%S').replace(tzinfo=ZoneInfo('Europe/Rome'))
    def number(label, unit):
        found = re.search(label + r'\s*([+-]?\d[\d.,]*)\s*' + unit, text, re.I)
        return number_value(found[1]) if found else None
    temp = number('Temperatura:', '°C')
    if temp is None or not -60 <= temp <= 65:
        raise ValueError('Desulo outdoor temperature unavailable')
    extremes = re.search(r'Estremi Oggi:\s*([+-]?\d+(?:[.,]\d+)?)\s*°C\s*\(\s*\d{1,2}[.:]\d{2}\s*\)\s*([+-]?\d+(?:[.,]\d+)?)\s*°C', text)
    wind = re.search(r'Velocit[aà] attuale:\s*[+-]?\d+(?:[.,]\d+)?\s*Km/h\s*(?:da\s+)?([NSEWO]{1,3})\b', text, re.I)
    compass = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']
    direction = wind[1].upper().replace('O', 'W') if wind else None
    # Coordinates/elevation: Sardegna Clima station Desulo (NU), id 64.
    row = dict(
        station_id='desulo-bakumeteo', name='Desulo',
        lat=40.0157, lon=9.2311, elev=945,
        source_url=DESULO_URL, last=observed.astimezone(timezone.utc).isoformat(),
        observation_type='Sardegna Clima · Desulo 945 m',
        temp=temp, tmin=number_value(extremes[2]) if extremes else None,
        tmax=number_value(extremes[1]) if extremes else None,
        rh=number('Umidit[aà]:', '%'), pressure_hpa=number('Pressione:', 'hPa'),
        wind_kmh=number('Velocit[aà] attuale:', 'Km/h'),
        wind_dir=compass.index(direction) * 22.5 if direction in compass else None,
        gust_kmh=number('Raffica giornaliera:', 'Km/h'),
        rain_today=number('Pioggia giorno:', 'mm'),
        rain_rate=number('Intensit[aà]:', 'mm/h'), rain_48h=None,
    )

    if station:
        row.update(station)
    return row


def fetch_desulo():
    for attempt in range(2):
        try:
            with urllib.request.urlopen(DESULO_URL, timeout=40) as response:
                raw = response.read()
            break
        except urllib.error.URLError:
            if attempt:
                raise
    try:
        page = raw.decode('utf-8')
    except UnicodeDecodeError:
        page = raw.decode('latin-1')
    return parse_desulo(page)


def public_json(url):
    with urllib.request.urlopen(url, timeout=40) as response:
        return json.load(response)


def public_html(url):
    with urllib.request.urlopen(url, timeout=40) as response:
        raw = response.read()
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        return raw.decode('latin-1')


def compass_degrees(text):
    labels = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']
    text = str(text or '').strip().upper().replace('O', 'W')
    return labels.index(text) * 22.5 if text in labels else None


def parse_tonara(p):
    observed = datetime.fromisoformat(p['updatedAt'].replace('Z', '+00:00'))
    if observed.tzinfo is None:
        raise ValueError('Tonara observation timezone unavailable')
    temp = number_value(p.get('tempC'))
    if temp is None or not -60 <= temp <= 65:
        raise ValueError('Tonara outdoor temperature unavailable')
    def n(k): return number_value(p.get(k))
    # Operator's station sheet: 40°01'28 N, 09°10'19 E, 910m.
    # The homepage's JSON-LD geo describes a different town and is not used.
    return dict(station_id='tonarameteo', name='Tonara – TonaraMeteo',
        lat=40.0244444444, lon=9.1719444444, elev=910,
        last=observed.astimezone(timezone.utc).isoformat(), source_url=TONARA_URL,
        observation_type='TonaraMeteo · osservazioni pubbliche del gestore',
        temp=temp, tmin=n('tempMinToday'), tmax=n('tempMaxToday'), rh=n('humidity'),
        pressure_hpa=n('pressureHpa'), wind_kmh=n('windKmh'), wind_dir=n('windDirDeg'),
        gust_kmh=n('gustKmh'), rain_today=n('rainToday'), rain_rate=n('rainRateMmH'), rain_48h=None)


def fetch_tonara():
    return parse_tonara(public_json(TONARA_URL + 'api/weather.php'))


def parse_laconi(page):
    parser = PageText(); parser.feed(page)
    text = ' '.join(' '.join(parser.parts).split())
    stamp = re.search(r'Dati ricevuti\s*:\s*@?\s*(\d{2}/\d{2}/\d{4})\s*(\d{1,2}:\d{2})', text)
    if not stamp:
        raise ValueError('Laconi observation timestamp unavailable')
    observed = datetime.strptime(f'{stamp[1]} {stamp[2]}', '%d/%m/%Y %H:%M').replace(tzinfo=ZoneInfo('Europe/Rome'))
    def n(pattern):
        match = re.search(pattern, text, re.I)
        return number_value(match[1]) if match else None
    temp = n(r'Temperatura:\s*([+-]?[\d.,]+)\s*°C')
    if temp is None or not -60 <= temp <= 65:
        raise ValueError('Laconi outdoor temperature unavailable')
    wind = re.search(r'Vento:\s*([NSEWO]{1,3})\s*([\d.,]+)\s*km/h', text)
    extreme = re.search(r'Oggi Ieri Max:\s*([+-]?[\d.,]+)\s*°C.*?Min:\s*([+-]?[\d.,]+)\s*°C', text)
    # Coordinates/elevation from the operator's wxabout.php station sheet.
    return dict(station_id='laconimeteo', name='Laconi – LaconiMeteo',
        lat=39.8530555556, lon=9.0486111111, elev=512,
        source_url=LACONI_URL, last=observed.astimezone(timezone.utc).isoformat(),
        observation_type='LaconiMeteo · Davis Vantage Pro2 · dati del gestore',
        temp=temp, tmin=number_value(extreme[2]) if extreme else None,
        tmax=number_value(extreme[1]) if extreme else None,
        rh=n(r'Umidità:\s*([\d.,]+)\s*%'), pressure_hpa=n(r'Barometro:\s*([\d.,]+)\s*hPa'),
        wind_kmh=number_value(wind[2]) if wind else None, wind_dir=compass_degrees(wind[1]) if wind else None,
        gust_kmh=n(r'Oggi:\s*([\d.,]+)\s*km/h'), rain_today=n(r'Pioggia odierna:\s*([\d.,]+)\s*mm'),
        rain_rate=n(r'Accumulo \(/hr\):\s*([\d.,]+)\s*mm'), rain_48h=None)


def fetch_laconi():
    return parse_laconi(public_html(LACONI_URL))


def dms_coordinate(text):
    nums = re.findall(r'\d+(?:\.\d+)?', str(text))
    if len(nums) != 3:
        raise ValueError('Station coordinate unavailable')
    d, m, sec = map(float, nums)
    if m >= 60 or sec >= 60:
        raise ValueError('Invalid station coordinate')
    return (d + m/60 + sec/3600) * (-1 if re.search(r'[SW]', str(text)) else 1)


def parse_serramanna(p):
    if 'Serramanna' not in str(p.get('location')):
        raise ValueError('Serramanna station identity changed')
    observed = datetime.strptime(p['update'], '%d/%m/%Y %H:%M:%S').replace(tzinfo=ZoneInfo('Europe/Rome'))
    def field(key, kind, unit):
        return metric_value(dict(convertedValue=p.get(key), unitLabel=p.get(unit)), kind)
    temp = field('temp', 'temperature', 'tempunit')
    if temp is None or not -60 <= temp <= 65:
        raise ValueError('Serramanna outdoor temperature unavailable')
    daily = str(p.get('rollovertime')).lower() == 'midnight'
    elevation = re.fullmatch(r'([\d.,]+)\s*m', str(p.get('altitude')))
    return dict(station_id='serramannameteo', name='Serramanna – Campu Sa Lua',
        lat=dms_coordinate(p['latitude']), lon=dms_coordinate(p['longitude']),
        elev=number_value(elevation[1]) if elevation else None,
        source_url=SERRAMANNA_URL, last=observed.astimezone(timezone.utc).isoformat(),
        observation_type='SerramannaMeteo · ' + str(p.get('stationtype') or 'Cumulus MX'),
        temp=temp, tmin=field('tempTL','temperature','tempunit') if daily else None,
        tmax=field('tempTH','temperature','tempunit') if daily else None,
        rh=number_value(p.get('hum')), pressure_hpa=field('press','pressure','pressunit'),
        wind_kmh=field('wspeed','wind','windunit'), wind_dir=number_value(p.get('avgbearing')),
        gust_kmh=field('wgustTM','wind','windunit') if daily else None,
        rain_today=field('rmidnight','rain','rainunit'), rain_rate=metric_value(dict(convertedValue=p.get('rrate'),unitLabel=str(p.get('rainunit'))+'/h'),'rain_rate'),
        rain_48h=None)


def fetch_serramanna():
    return parse_serramanna(public_json('https://www.serramannameteo.it/websitedata.json'))


def parse_meana(p):
    if str(p.get('station',{}).get('city','')).casefold() != 'meanasardo':
        raise ValueError('Meana Sardo station identity changed')
    observed = weatherlink_time(p.get('time',{}).get('unixtime'))
    units = p['station'].get('units','').split(',')
    if len(units) < 4:
        raise ValueError('Meana Sardo units unavailable')
    def field(group, key, kind, unit):
        return metric_value(dict(convertedValue=p.get(group,{}).get(key),unitLabel=unit),kind)
    temp = field('temperature','current','temperature',units[0])
    if temp is None or not -60 <= temp <= 65:
        raise ValueError('Meana Sardo outdoor temperature unavailable')
    wind_unit = 'km/h' if units[2] == 'kmh' else units[2]
    rain_rate = field('rain','currrate','rain_rate',units[3]+'/h')
    # The operator's own renderer reports no rain when current rain is zero,
    # otherwise leaves intensity unavailable if the rate field is missing.
    if rain_rate is None and number_value(p.get('rain',{}).get('current')) == 0:
        rain_rate = 0
    return dict(station_id='meanasardometeo-peldu-murone',name='Meana Sardo – Peldu Murone',
        lat=39.95,lon=9.0666666667,elev=588,location_precision='arcminute',
        source_url=MEANA_URL,last=observed.isoformat(),
        observation_type='MeanaSardoMeteo · Davis Vantage Vue · coordinate arrotondate del gestore',
        temp=temp,tmin=field('temperature','min','temperature',units[0]),tmax=field('temperature','max','temperature',units[0]),
        rh=number_value(p.get('humidity',{}).get('current')),
        pressure_hpa=field('barometer','current','pressure',units[1]),
        wind_kmh=field('wind','current','wind',wind_unit),wind_dir=compass_degrees(p.get('wind',{}).get('currdir')),
        gust_kmh=field('wind','todaygust','wind',wind_unit),rain_today=field('rain','today','rain',units[3]),
        rain_rate=rain_rate,rain_48h=None)


def fetch_meana():
    return parse_meana(public_json('https://www.meanasardometeo.it/anemos/dbmeteo-proxy.asp'))


def fetch_nolza():
    row = parse_desulo(public_html(NOLZA_URL),dict(station_id='meanasardometeo-nolza',name='Meana Sardo – Nolza',
        lat=39.916944,lon=9.074722,elev=720,source_url=NOLZA_URL,observation_type='MeanaSardoMeteo / OroseiMeteo · Nolza'))
    try:
        wind = public_json('https://www.oroseimeteo.it/stazioni/nolza/vento.php')
        if not wind.get('error'):
            row['wind_kmh'] = number_value(wind.get('wind_speed'))
            row['wind_dir'] = compass_degrees(wind.get('wind_direction'))
    except (OSError, ValueError, TypeError):
        pass
    return row


def fetch_santelias():
    return parse_desulo(public_html(SANTELIAS_URL),dict(station_id='meanasardometeo-santelias',name='Meana Sardo – Sant’Elias',
        lat=39.930275,lon=9.121454,elev=1080,source_url=SANTELIAS_URL,observation_type='MeanaSardoMeteo / OroseiMeteo · Sant’Elias'))


def parse_meteoman(raw):
    values = raw.strip().split('|')
    if len(values) < 33:
        raise ValueError('Meteoman public data incomplete')
    observed = datetime.strptime(values[0]+' '+values[1], '%d/%m/%y %H:%M').replace(tzinfo=ZoneInfo('Europe/Rome'))
    def metric(i, kind, unit):
        return metric_value(dict(convertedValue=values[i],unitLabel=values[unit]),kind)
    temp = metric(2,'temperature',14)
    if temp is None or not -60 <= temp <= 65:
        raise ValueError('Meteoman outdoor temperature unavailable')
    # Coordinates are rounded to minutes in the operator's station sheet.
    return dict(station_id='villacidro-meteoman', name='Villacidro',
        lat=39.5,lon=8.7,elev=265,location_precision='arcminute',
        source_url='https://meteoman.altervista.org/wxindex_new.php',last=observed.astimezone(timezone.utc).isoformat(),
        observation_type='Meteoman · Davis Vantage Vue · coordinate arrotondate del gestore',
        temp=temp,tmin=metric(28,'temperature',14),tmax=metric(26,'temperature',14),
        rh=number_value(values[3]),pressure_hpa=metric(10,'pressure',15),
        wind_kmh=metric(5,'wind',13),wind_dir=number_value(values[7]),gust_kmh=metric(32,'wind',13),
        rain_today=metric(9,'rain',16),rain_rate=metric_value(dict(convertedValue=values[8],unitLabel=values[16]+'/h'),'rain_rate'),rain_48h=None)


def fetch_meteoman():
    return parse_meteoman(public_html('https://meteoman.altervista.org/WLrealtime.txt'))


def parse_decimomannu(page):
    # This provider publishes a three-decimal map location but no reliable elevation.
    return parse_desulo(page,dict(station_id='decimomannu-meteoproject',name='Decimomannu – Osservatorio comunale',
        lat=39.312,lon=8.961,elev=None,location_precision='0.001 degree',
        source_url='https://stazioni.meteoproject.it/dati/decimomannu/tabella-vuota.php',
        observation_type='MeteoProject · osservatorio comunale di Decimomannu'))


def fetch_decimomannu():
    return parse_decimomannu(public_html('https://stazioni.meteoproject.it/dati/decimomannu/tabella-vuota.php'))


def parse_vedetta_station(page, station_id, name, lat, lon, source_url, observation_type):
    parser = PageText(); parser.feed(page)
    text = ' '.join(' '.join(parser.parts).split())
    stamp = re.search(r'(\d{2}:\d{2}:\d{2})\s+(\d{2}/\d{2}/\d{4})',text)
    if not stamp: raise ValueError(f'{name} observation time unavailable')
    observed = datetime.strptime(stamp[2]+' '+stamp[1],'%d/%m/%Y %H:%M:%S').replace(tzinfo=ZoneInfo('Europe/Rome'))
    def n(label,unit):
        m=re.search(label+r'\s*([+-]?[\d.,]+)\s*'+unit,text,re.I)
        return number_value(m[1]) if m else None
    def wind(label):
        value=n(label,'kts')
        return round(value*1.852,3) if value is not None else None
    temp=n('Temperatura:','°C')
    speed=wind('Vento Attuale:')
    if temp is None and speed is None: raise ValueError(f'{name} observations unavailable')
    direction=re.search(r'Direzione:\s*([NSEWO]{1,3})\b',text,re.I)
    return dict(station_id=station_id,name=name,
        lat=lat,lon=lon,elev=None,
        source_url=source_url,
        last=observed.astimezone(timezone.utc).isoformat(),observation_type=observation_type,
        temp=temp,tmin=None,tmax=None,rh=n('Umidità:','%'),pressure_hpa=n('Pressione:','hpa'),
        wind_kmh=speed,wind_dir=compass_degrees(direction[1]) if direction else None,
        gust_kmh=wind('Raffica MAX OGGI:'),rain_today=None,rain_rate=None,rain_48h=None)


def parse_panoramicams(payload, station):
    records = payload if isinstance(payload, list) else [payload]
    valid = []
    for record in records:
        if not isinstance(record, dict) or not record.get('dateutc'):
            continue
        try:
            observed = datetime.strptime(record['dateutc'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        valid.append((observed, record))
    if not valid:
        raise ValueError('Panoramicams Ecowitt observation unavailable')
    observed, record = max(valid, key=lambda item: item[0])

    temp_c = number_value(record.get('tempc'))
    temp_f = number_value(record.get('tempf'))
    temp_from_f = (temp_f - 32) * 5 / 9 if temp_f is not None and -80 <= temp_f <= 140 else None
    # Some feeds return a mismatched Celsius field while Fahrenheit remains valid.
    if temp_c == -17.78 and temp_f is None:
        temp = None  # Ecowitt missing-temperature sentinel; tempinf is indoor.
    elif temp_c is None or not -60 <= temp_c <= 65 or (temp_from_f is not None and abs(temp_c - temp_from_f) > 2):
        temp = temp_from_f
    else:
        temp = temp_c
    if temp is not None and not -60 <= temp <= 65:
        temp = None

    def bounded(key, low, high):
        value = number_value(record.get(key))
        return value if value is not None and low <= value <= high else None

    observed_local = observed.astimezone(ZoneInfo('Europe/Rome'))
    today_local = datetime.now(timezone.utc).astimezone(ZoneInfo('Europe/Rome')).date()
    daily_valid = observed_local.date() == today_local
    pressure_in = bounded('baromrelin', 20, 35)
    wind = bounded('windspeedkmh', 0, 250)
    if wind is None:
        mph = bounded('windspeedmph', 0, 160)
        wind = round(mph * 1.609344, 3) if mph is not None else None
    wind_direction = bounded('winddir_avg10m', 0, 360)
    if wind_direction is None:
        wind_direction = bounded('winddir', 0, 360)
    gust = bounded('maxdailygust', 0, 300) if daily_valid else None
    if gust is None and daily_valid:
        gust = bounded('windgustkmh', 0, 300)
    rain_day_in = bounded('dailyrainin', 0, 20) if daily_valid else None
    rain_rate_in = bounded('rainratein', 0, 10)
    rh = bounded('humidity', 0, 100)
    if all(value is None for value in (temp, wind, rh, rain_rate_in)):
        raise ValueError('Panoramicams Ecowitt has no usable current observations')

    return dict(
        station_id=station['station_id'], name=station['name'],
        lat=station['lat'], lon=station['lon'], elev=None,
        source_url=station['source_url'], last=observed.isoformat(),
        observation_type='Panoramicams · Ecowitt · coordinate indicativa dell’area',
        temp=round(temp, 2) if temp is not None else None, tmin=None, tmax=None,
        rh=rh, pressure_hpa=round(pressure_in * 33.8638866667, 1) if pressure_in is not None else None,
        wind_kmh=wind, wind_dir=wind_direction, gust_kmh=gust,
        rain_today=round(rain_day_in * 25.4, 2) if rain_day_in is not None else None,
        rain_rate=round(rain_rate_in * 25.4, 2) if rain_rate_in is not None else None,
        rain_48h=None,
    )


def fetch_panoramicams(station):
    url = 'https://panoramicams.com/ecowitt/get_json.php?' + urllib.parse.urlencode({
        'station': 'panoramicamsweather_' + station['feed'], 'location': station['area']})
    # The public feed rejects bare urllib requests; send the same page context
    # and JSON Accept header used by the Panoramicams embedded widget.
    request = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (compatible; SardegnaMeteoLive/1.0)',
        'Referer': 'https://panoramicams.com/',
        'Accept': 'application/json',
    })
    # Keep the provider's endpoint from receiving a burst of many simultaneous
    # requests when all webcam stations are refreshed in the same run.
    with PANORAMICAMS_REQUESTS:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
    return parse_panoramicams(payload, station)


def parse_caletta(page):
    return parse_vedetta_station(page,'siniscola-la-caletta','Siniscola – La Caletta',40.602227,9.750968,
        'https://vedetta.org/webcam/italia/sardegna/nuoro/siniscola-la-caletta/',
        'Vedetta · El Barrio del Mar · dati numerici pubblici')


def fetch_caletta():
    return parse_caletta(public_html('https://vedetta.org/image-client/siniscola-la-caletta/meteo/dati_meteo.html'))


def fetch_punta_negra():
    return parse_vedetta_station(public_html('https://vedetta.org/image-client/alghero-punta-negra/meteo/dati_meteo.html'),
        'alghero-punta-negra','Alghero – Punta Negra',40.593717,8.272801,
        'https://vedetta.org/webcam/italia/sardegna/sassari/alghero-punta-negra/',
        'Vedetta · Punta Negra · dati numerici pubblici')


def fetch_sant_antioco():
    return parse_vedetta_station(public_html('https://vedetta.org/image-client/sant-antioco/meteo/dati_meteo.html'),
        'sant-antioco','Sant’Antioco',39.098063,8.420910,
        'https://vedetta.org/webcam/italia/sardegna/carbonia-iglesias/sant-antioco/',
        'Vedetta · Isola di Sant’Antioco · dati numerici pubblici')


def fetch_isola_rossa():
    return parse_vedetta_station(public_html('https://vedetta.org/vedetta/isola-rossa/index.php'),
        'isola-rossa','Isola Rossa – La Marinedda',41.014469,8.886764,
        'https://vedetta.org/webcam/italia/sardegna/sassari/isola-rossa/',
        'Vedetta · La Marinedda · dati numerici pubblici')


def add_sampled_temperature_extremes(rows, retained):
    """Fill missing daily extremes from unique 10-minute network observations."""
    for row in rows:
        temp = number_value(row.get('temp'))
        observed = parse_datetime(row.get('last'))
        if observed is None:
            continue
        local_day = observed.astimezone(ZoneInfo('Europe/Rome')).date().isoformat()
        old = retained.get(row.get('station_id'), {})
        old_stamp = parse_datetime(old.get('last'))
        old_day = old.get('extrema_day') or (
            old_stamp.astimezone(ZoneInfo('Europe/Rome')).date().isoformat() if old_stamp else None)
        same_day = old_day == local_day
        old_temp = number_value(old.get('temp')) if same_day else None
        old_min = number_value(old.get('tmin')) if same_day else None
        old_max = number_value(old.get('tmax')) if same_day else None
        direct_min = number_value(row.get('tmin'))
        direct_max = number_value(row.get('tmax'))
        if temp is None and direct_min is None and direct_max is None:
            continue
        is_new_sample = temp is not None and (old_stamp is None or observed > old_stamp)
        samples = (int(old.get('extrema_samples') or (1 if old_temp is not None else 0)) if same_day else 0)
        if is_new_sample:
            samples += 1

        sampled_min = min([v for v in (old_min, temp) if v is not None], default=None)
        sampled_max = max([v for v in (old_max, temp) if v is not None], default=None)
        row['tmin'] = direct_min if direct_min is not None else sampled_min
        row['tmax'] = direct_max if direct_max is not None else sampled_max
        sampled_fields = []
        if direct_min is None and sampled_min is not None:
            sampled_fields.append('min')
        if direct_max is None and sampled_max is not None:
            sampled_fields.append('max')

        if sampled_fields:
            reported_fields = []
            if direct_min is not None: reported_fields.append('min')
            if direct_max is not None: reported_fields.append('max')
            row['extrema_source'] = 'provider_reported+network_sampled_10min' if reported_fields else 'network_sampled_10min'
            row['extrema_samples'] = samples
            row['extrema_day'] = local_day
            if 'min' in sampled_fields:
                row['tmin_provisional'] = True
            if 'max' in sampled_fields:
                row['tmax_provisional'] = True


def parse_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError):
        return None


def collect(sources, previous):
    """Refresh providers concurrently, retry errors, and isolate outages."""
    retained = {row['station_id']: row for row in previous.get('stations', [])}
    results = {}
    failures = {}

    def fetch_with_retry(fetcher):
        last_error = None
        for attempt in range(3):
            try:
                return fetcher(), None
            except Exception as error:
                last_error = error
                if attempt < 2:
                    time.sleep((2, 5)[attempt])
        return None, last_error

    with ThreadPoolExecutor(max_workers=10, thread_name_prefix='station') as pool:
        jobs = {pool.submit(fetch_with_retry, fetcher): station_id
                for station_id, fetcher in sources}
        for future in as_completed(jobs):
            station_id = jobs[future]
            try:
                row, error = future.result()
            except Exception as unexpected:
                row, error = None, unexpected
            if row is not None:
                results[station_id] = row
                continue
            failures[station_id] = type(error).__name__ if error else 'UnexpectedError'
            if station_id.startswith('panoramicams-') and error is not None:
                print(f'{station_id} source error: {error!r}')
            if station_id == 'desulo-bakumeteo' and isinstance(error, urllib.error.URLError):
                print('Desulo public source connection:', str(error.reason)[:180])
            if station_id in retained:
                row = dict(retained[station_id])
                if station_id == 'desulo-bakumeteo':
                    row.update(name='Desulo',observation_type='Sardegna Clima · Desulo 945 m')
                results[station_id] = row

    fresh = sum(1 for station_id, _ in sources if station_id in results and station_id not in failures)
    if not fresh:
        raise RuntimeError('No local station could be refreshed; previous dataset preserved')
    rows = [results[station_id] for station_id, _ in sources if station_id in results]
    add_sampled_temperature_extremes(rows, retained)
    # Track the highest gust exposed by each station every collector cycle.
    # The timestamp is when the web app observed the value, not necessarily
    # the exact physical instant of the peak.
    today = datetime.now(ZoneInfo('Europe/Rome')).date().isoformat()
    for row in rows:
        observed = parse_datetime(row.get('last'))
        gust = number_value(row.get('gust_kmh'))
        old = retained.get(row.get('station_id'), {})
        old_day = old.get('gust_day') or (
            parse_datetime(old.get('last')).astimezone(ZoneInfo('Europe/Rome')).date().isoformat()
            if parse_datetime(old.get('last')) else None)
        if observed is None or observed.astimezone(ZoneInfo('Europe/Rome')).date().isoformat() != today:
            continue
        peak = number_value(old.get('gust_day_max_kmh')) if old_day == today else None
        peak_at = old.get('gust_day_max_at') if old_day == today else None
        if gust is not None and (peak is None or gust > peak):
            peak, peak_at = gust, observed.isoformat()
        row['gust_day_max_kmh'] = peak
        row['gust_day_max_at'] = peak_at
        row['gust_day'] = today
        row['gust_day_source'] = 'network_sampled_10min'
    print(f'Refreshed {fresh}/{len(sources)} stations; {len(failures)} provider(s) failed after retries')
    return dict(source='Stazioni amatoriali · WeatherLink / Ecowitt / Panoramicams / Domusnovas Meteo / OroseiMeteo / QuartuMeteo / TonaraMeteo / LaconiMeteo / SerramannaMeteo / MeanaSardoMeteo',
                generated_at=datetime.now(timezone.utc).isoformat(), stations=rows, errors=failures)


def main():
    try:
        previous = json.loads(OUTPUT.read_text())
    except (OSError, ValueError):
        previous = {}
    payload = collect([('domusnovas-meteo', fetch_domusnovas), ('liceo-alberti-cagliari', fetch_alberti), ('geremeas-weatherlink', fetch_geremeas), ('olbia-weatherlink', fetch_olbia), ('porto-rafael-weatherlink', fetch_porto_rafael), ('monte-lusei-weatherlink', fetch_lusei), ('marina-olbia-weatherlink', fetch_marina_olbia), ('collinas-weatherlink', fetch_collinas), ('costa-corallina-weatherlink', fetch_corallina), ('iglesias-marco-polo-ecowitt', fetch_iglesias), ('quartu-quartumeteo', fetch_quartumeteo), ('desulo-bakumeteo', fetch_desulo), ('villacidro-meteoman', fetch_meteoman), ('decimomannu-meteoproject', fetch_decimomannu), ('siniscola-la-caletta', fetch_caletta), ('alghero-punta-negra', fetch_punta_negra), ('sant-antioco', fetch_sant_antioco), ('isola-rossa', fetch_isola_rossa)] + [(station['station_id'], lambda station=station: fetch_panoramicams(station)) for station in PANORAMICAMS_STATIONS] + [('tonarameteo', fetch_tonara), ('laconimeteo', fetch_laconi), ('serramannameteo', fetch_serramanna), ('meanasardometeo-peldu-murone', fetch_meana), ('meanasardometeo-nolza', fetch_nolza), ('meanasardometeo-santelias', fetch_santelias)] + [('oroseimeteo-' + key, lambda key=key: fetch_oroseimeteo(key)) for key in OROSEIMETEO_STATIONS], previous)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
    for row in payload['stations']:
        print(row['station_id'], row['last'])
    if payload['errors']:
        print('Sources temporarily unavailable:', payload['errors'])


if __name__ == '__main__':
    main()
