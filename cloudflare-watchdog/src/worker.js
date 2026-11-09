/**
 * Sardegna MeteoLive — sorveglianza esterna indipendente.
 * Non effettua richieste dirette alle API delle reti meteorologiche.
 */
const REPOSITORY = 'matteotidili/sardegna-meteolive-data';
const API_BASE = 'https://api.github.com/repos/' + REPOSITORY;
const STALE_CHECK_MINUTES = 18;
const RECENT_RUN_MINUTES = 12;

export const NETWORKS = [
  {name:'MeteoHub DPCN + MeteoNetwork MNW',file:'data/stations.json',companionFile:'data/meteonetwork.json',workflow:'update-meteohub.yml'},
  {name:'Aeronautica Militare',file:'data/am.json',workflow:'update-am.yml'},
  {name:'Ecowitt Villaspeciosa',file:'data/ecowitt.json',workflow:'update-ecowitt.yml',stationObservations:true},
];

export function minutesOld(timestamp, nowMs) {
  const parsed = Date.parse(timestamp);
  if (!Number.isFinite(parsed)) return Infinity;
  return Math.max(0, (nowMs - parsed) / 60000);
}

async function github(env, path, fetchImpl, init = {}) {
  const response = await fetchImpl(API_BASE + path, {
    ...init,
    headers: {
      'Accept':'application/vnd.github+json',
      'Authorization':'Bearer ' + env.GITHUB_ACTIONS_TOKEN,
      'User-Agent':'sardegna-meteolive-external-watchdog',
      'X-GitHub-Api-Version':'2022-11-28',
      ...(init.body ? {'Content-Type':'application/json'} : {}),
    },
  });
  if (!response.ok) throw new Error('GitHub API ' + path.split('?')[0] + ': HTTP ' + response.status);
  if (response.status === 204) return null;
  return response.json();
}

function decodeGithubContent(data) {
  if (typeof data?.content !== 'string' || data.encoding !== 'base64') {
    throw new Error('GitHub contents: base64 payload missing');
  }
  const binary = atob(data.content.replace(/\s/g, ''));
  const bytes = Uint8Array.from(binary, c => c.charCodeAt(0));
  return JSON.parse(new TextDecoder().decode(bytes));
}

export function latestObservation(data, stationObservations) {
  if (!stationObservations) return data.generated_at;
  const readings = (data.stations || [])
    .map(station => Date.parse(station?.last))
    .filter(Number.isFinite);
  return readings.length ? new Date(Math.max(...readings)).toISOString() : null;
}

export async function inspectNetwork(network, env, nowMs, fetchImpl = fetch) {
  const readFile = async path => {
    const encoded = path.split('/').map(encodeURIComponent).join('/');
    return decodeGithubContent(await github(env, '/contents/' + encoded + '?ref=main', fetchImpl));
  };
  // DPCN e MNW sono ora scaricate insieme: sorvegliamo entrambi i file
  // ma avviamo UN SOLO workflow MeteoHub e non duplichiamo le richieste API.
  const [data, companion] = await Promise.all([
    readFile(network.file),
    network.companionFile ? readFile(network.companionFile) : Promise.resolve(null),
  ]);
  // Ecowitt: generated_at indica l'acquisizione, stations[].last la misura.
  const primaryCheckAge = minutesOld(data.checked_at || data.generated_at, nowMs);
  const primaryObservationAge = minutesOld(
    latestObservation(data, network.stationObservations), nowMs
  );
  const companionCheckAge = companion
    ? minutesOld(companion.checked_at || companion.generated_at, nowMs) : null;
  const companionObservationAge = companion
    ? minutesOld(companion.generated_at, nowMs) : null;
  const checkedAgeMinutes = companion ? Math.max(primaryCheckAge, companionCheckAge) : primaryCheckAge;
  const observationAgeMinutes = companion ? Math.max(primaryObservationAge, companionObservationAge) : primaryObservationAge;
  const result = {
    network: network.name,
    checkedAgeMinutes: Math.round(checkedAgeMinutes),
    observationAgeMinutes: Math.round(observationAgeMinutes),
    ...(companion ? {
      dpcnCheckedAgeMinutes: Math.round(primaryCheckAge),
      mnwCheckedAgeMinutes: Math.round(companionCheckAge),
      mnwObservationAgeMinutes: Math.round(companionObservationAge),
    } : {}),
    outcome: 'fresh',
  };

  // Soglie personalizzate: MNW interroga MeteoHub ogni 30 min, non ogni 18.
  const staleMinutes = network.staleMinutes ?? STALE_CHECK_MINUTES;
  const recentRunMinutes = network.recentRunMinutes ?? RECENT_RUN_MINUTES;
  // Acquisizione recente + osservazione vecchia: ritardo della fonte, non del cron.
  if (checkedAgeMinutes < staleMinutes) {
    result.outcome = observationAgeMinutes > 30 ? 'source_delayed' : 'fresh';
    return result;
  }

  // Nessun avvio duplicato mentre un workflow recente è già in coda.
  const runs = await github(env, '/actions/workflows/' + network.workflow + '/runs?per_page=10', fetchImpl);
  const hasRecentRun = (runs.workflow_runs || []).some(run =>
    minutesOld(run.created_at, nowMs) < recentRunMinutes
  );
  if (hasRecentRun) {
    result.outcome = 'recent_run_pending';
    return result;
  }

  await github(env, '/actions/workflows/' + network.workflow + '/dispatches',
    fetchImpl, {method:'POST',body:JSON.stringify({ref:'main'})});
  result.outcome = 'recovery_dispatched';
  return result;
}

export async function runChecks(env, nowMs = Date.now(), fetchImpl = fetch) {
  if (!env?.GITHUB_ACTIONS_TOKEN) throw new Error('Missing GITHUB_ACTIONS_TOKEN secret');
  const settled = await Promise.allSettled(
    NETWORKS.map(n => inspectNetwork(n, env, nowMs, fetchImpl))
  );
  const results = settled.map((r,i) => r.status === 'fulfilled'
    ? r.value
    : {network:NETWORKS[i].name,outcome:'error',message:String(r.reason)});
  for (const result of results) console.log(JSON.stringify(result));
  if (settled.some(r => r.status === 'rejected')) {
    throw new Error('At least one network could not be inspected; check Worker logs');
  }
  return results;
}

export default {
  async scheduled(controller, env, ctx) {
    ctx.waitUntil(runChecks(env));
  },
};
