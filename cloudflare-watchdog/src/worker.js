/**
 * Sardegna MeteoLive — sorveglianza esterna indipendente.
 * Non effettua richieste dirette alle API MeteoHub o Aeronautica.
 */
const REPOSITORY = 'matteotidili/sardegna-meteolive-data';
const API_BASE = 'https://api.github.com/repos/' + REPOSITORY;
const STALE_CHECK_MINUTES = 18;
const RECENT_RUN_MINUTES = 12;

export const NETWORKS = [
  {name:'MeteoHub DPCN',file:'data/stations.json',workflow:'update-meteohub.yml'},
  {name:'Aeronautica Militare',file:'data/am.json',workflow:'update-am.yml'},
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

export async function inspectNetwork(network, env, nowMs, fetchImpl = fetch) {
  const encodedPath = network.file.split('/').map(encodeURIComponent).join('/');
  const data = decodeGithubContent(await github(env, '/contents/' + encodedPath + '?ref=main', fetchImpl));
  const checkedAgeMinutes = minutesOld(data.checked_at, nowMs);
  const observationAgeMinutes = minutesOld(data.generated_at, nowMs);
  const result = {
    network: network.name,
    checkedAgeMinutes: Math.round(checkedAgeMinutes),
    observationAgeMinutes: Math.round(observationAgeMinutes),
    outcome: 'fresh',
  };

  // Acquisizione recente + osservazione vecchia: ritardo della fonte, non del cron.
  if (checkedAgeMinutes < STALE_CHECK_MINUTES) {
    result.outcome = observationAgeMinutes > 30 ? 'source_delayed' : 'fresh';
    return result;
  }

  // Nessun avvio duplicato mentre un workflow recente è già in coda.
  const runs = await github(env, '/actions/workflows/' + network.workflow + '/runs?per_page=10', fetchImpl);
  const hasRecentRun = (runs.workflow_runs || []).some(run =>
    minutesOld(run.created_at, nowMs) < RECENT_RUN_MINUTES
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
