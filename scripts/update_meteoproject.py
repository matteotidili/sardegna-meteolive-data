"""Collect the public Cagliari Liceo Euclide observations, preserving source time."""
import html
import json
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SOURCE = 'https://www.meteoproject.it/ftp/stazioni/cagliari/index-vuoto.php'


def parse_page(page):
    text = html.unescape(re.sub(r'<[^>]+>', ' ', re.sub(r'<script\b[^>]*>.*?</script>', '', page, flags=re.S)))
    text = ' '.join(text.split())
    match = re.search(r'Dati aggiornati il (\d{2}/\d{2}/\d{2}) alle ore (\d{2}\.\d{2})', text)
    if not match:
        raise ValueError('Missing observation time')
    stamp = datetime.strptime(' '.join(match.groups()), '%d/%m/%y %H.%M').replace(tzinfo=ZoneInfo('Europe/Rome'))
    def number(label):
        found = re.search(label + r'\s*([+-]?\d+(?:[.,]\d+)?)', text)
        return float(found[1].replace(',', '.')) if found else None
    extremes = re.search(r'Estremi Oggi:\s*([\d.,]+)\s*°C\s*\([^)]*\)\s*([\d.,]+)\s*°C', text)
    direction = re.search(r'Velocità attuale:\s*[\d.,]+\s*Km/h\s*([NSEW]+)', text)
    compass = ['N','NNE','NE','ENE','E','ESE','SE','SSE','S','SSW','SW','WSW','W','WNW','NW','NNW']
    row = dict(station_id='cagliari-euclide', name='Cagliari – Liceo Euclide', lat=39.252, lon=9.111, elev=20,
               last=stamp.astimezone(timezone.utc).isoformat(), source_url=SOURCE,
               temp=number('Temperatura:'), rh=number('Umidità:'), wind_kmh=number('Velocità attuale:'),
               wind_dir=compass.index(direction[1])*22.5 if direction and direction[1] in compass else None,
               rain_today=number('Pioggia giorno:'), rain_rate=number('Intensità:'), pressure_hpa=number('Pressione:'),
               tmax=float(extremes[1].replace(',', '.')) if extremes else None,
               tmin=float(extremes[2].replace(',', '.')) if extremes else None,
               gust_kmh=number('Raffica giornaliera:'), observation_type='stazione automatica Davis Vantage Pro 2')
    if row['temp'] is None:
        raise ValueError('Missing temperature')
    return row


def fetch_latest():
    last_error = None
    for attempt in range(3):
        try:
            request = urllib.request.Request(SOURCE, headers={
                'User-Agent': 'Mozilla/5.0 (compatible; SardegnaMeteoLive/1.0)',
                'Cache-Control': 'no-cache',
            })
            with urllib.request.urlopen(request, timeout=25) as response:
                row = parse_page(response.read().decode('utf-8'))
            return row
        except Exception as error:
            last_error = error
            if attempt < 2:
                time.sleep((2, 5)[attempt])
    raise RuntimeError(f'MeteoProject source unavailable after 3 attempts: {type(last_error).__name__}') from last_error


if __name__ == '__main__':
    row = fetch_latest()
    payload = dict(source='MeteoProject · Liceo Euclide', generated_at=datetime.now(timezone.utc).isoformat(), stations=[row])
    Path('data/meteoproject.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2)+'\\n')
    print('Published observation:', row['last'])
