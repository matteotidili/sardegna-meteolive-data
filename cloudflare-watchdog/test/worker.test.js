import test from 'node:test';
import assert from 'node:assert/strict';
import {inspectNetwork, minutesOld, latestObservation, NETWORKS} from '../src/worker.js';

const network = {name:'MeteoHub DPCN', file:'data/stations.json', workflow:'update-meteohub.yml'};
const now = Date.parse('2026-11-09T12:30:00Z');
const enc = data => Buffer.from(JSON.stringify(data)).toString('base64');
const env = {GITHUB_ACTIONS_TOKEN:'test-token-not-real'};

function fakeFetch(data, runs=[], dispatches=[]) {
  return async (url, init) => {
    if (url.includes('/contents/')) {
      return new Response(JSON.stringify({encoding:'base64',content:enc(data)}), {status:200});
    }
    if (url.includes('/runs?')) {
      return new Response(JSON.stringify({workflow_runs:runs}), {status:200});
    }
    if (url.includes('/dispatches') && init.method==='POST') {
      dispatches.push(JSON.parse(init.body));
      return new Response(null,{status:204});
    }
    throw new Error('Unexpected request: ' + url);
  };
}

test('normalizza età e data invalida', () => {
  assert.equal(minutesOld('2026-11-09T12:00:00Z', now), 30);
  assert.equal(minutesOld(undefined, now), Infinity);
});

test('acquisizione recente: non avvia workflow', async () => {
  const dispatches=[];
  const result=await inspectNetwork(network,env,now,fakeFetch({
    checked_at:'2026-11-09T12:20:00Z',generated_at:'2026-11-09T12:15:00Z'
  },[],dispatches));
  assert.equal(result.outcome,'fresh');
  assert.deepEqual(dispatches,[]);
});

test('fonte in ritardo: niente recupero se acquisizione recente', async () => {
  const dispatches=[];
  const result=await inspectNetwork(network,env,now,fakeFetch({
    checked_at:'2026-11-09T12:20:00Z',generated_at:'2026-11-09T10:20:00Z'
  },[],dispatches));
  assert.equal(result.outcome,'source_delayed');
  assert.deepEqual(dispatches,[]);
});

test('acquisizione vecchia: dispatch automatico', async () => {
  const dispatches=[];
  const result=await inspectNetwork(network,env,now,fakeFetch({
    checked_at:'2026-11-09T12:00:00Z',generated_at:'2026-11-09T12:00:00Z'
  },[],dispatches));
  assert.equal(result.outcome,'recovery_dispatched');
  assert.deepEqual(dispatches,[{ref:'main'}]);
});

test('esecuzione recente: evita duplicati', async () => {
  const dispatches=[];
  const result=await inspectNetwork(network,env,now,fakeFetch({
    checked_at:'2026-11-09T12:00:00Z',generated_at:'2026-11-09T12:00:00Z'
  },[{created_at:'2026-11-09T12:25:00Z'}],dispatches));
  assert.equal(result.outcome,'recent_run_pending');
  assert.deepEqual(dispatches,[]);
});

test('checked_at mancante: richiede recupero', async () => {
  const dispatches=[];
  const result=await inspectNetwork(network,env,now,fakeFetch({
    generated_at:'2026-11-09T12:00:00Z'
  },[],dispatches));
  assert.equal(result.outcome,'recovery_dispatched');
  assert.equal(dispatches.length,1);
});

const ecowitt = NETWORKS.find(n => n.file === 'data/ecowitt.json');

test('Ecowitt è inclusa tra le reti sorvegliate', () => {
  assert.ok(ecowitt);
  assert.equal(ecowitt.workflow, 'update-ecowitt.yml');
});

test('Ecowitt: la freschezza deriva da stations[].last, non dal generated_at', () => {
  assert.equal(latestObservation({
    generated_at:'2026-11-09T12:29:00Z',
    stations:[{last:'2026-11-09T10:10:00Z'}]
  }, true), '2026-11-09T10:10:00.000Z');
});

test('Ecowitt ferma da ore: watchdog richiede recupero', async () => {
  const dispatches=[];
  const result=await inspectNetwork(ecowitt,env,now,fakeFetch({
    generated_at:'2026-11-09T10:10:00Z',
    status:'ok',
    stations:[{name:'Villaspeciosa',last:'2026-11-09T10:09:00Z'}]
  },[],dispatches));
  assert.equal(result.outcome,'recovery_dispatched');
  assert.deepEqual(dispatches,[{ref:'main'}]);
});

test('Ecowitt con acquisizione recente ma dati sorgente vecchi: non duplica chiamate', async () => {
  const dispatches=[];
  const result=await inspectNetwork(ecowitt,env,now,fakeFetch({
    generated_at:'2026-11-09T12:27:00Z',
    status:'last_observation_only',
    stations:[{name:'Villaspeciosa',last:'2026-11-09T10:09:00Z'}]
  },[],dispatches));
  assert.equal(result.outcome,'source_delayed');
  assert.deepEqual(dispatches,[]);
});


const combined = NETWORKS.find(n => n.companionFile === 'data/meteonetwork.json');

function combinedFetch(dpcn, mnw, runs=[], dispatches=[]) {
  const fallback=fakeFetch(dpcn,runs,dispatches);
  return async (url,init) => {
    if(url.includes('/contents/') && url.includes('/meteonetwork.json')) {
      return new Response(JSON.stringify({encoding:'base64',content:enc(mnw)}),{status:200});
    }
    return fallback(url,init);
  };
}

test('DPCN e MNW hanno un solo workflow MeteoHub e due file monitorati', () => {
  assert.ok(combined);
  assert.equal(combined.workflow,'update-meteohub.yml');
  assert.equal(NETWORKS.filter(n=>n.workflow==='update-meteohub.yml').length,1);
});

test('MeteoHub unificata fresca: nessun recupero', async () => {
  const dispatches=[];
  const result=await inspectNetwork(combined,env,now,combinedFetch(
    {checked_at:'2026-11-09T12:21:00Z',generated_at:'2026-11-09T12:17:00Z'},
    {checked_at:'2026-11-09T12:21:00Z',generated_at:'2026-11-09T12:10:00Z'},
    [],dispatches
  ));
  assert.equal(result.outcome,'fresh');
  assert.deepEqual(dispatches,[]);
});

test('MNW ferma mentre DPCN si aggiorna: recupero UNICO da MeteoHub', async () => {
  const dispatches=[];
  const result=await inspectNetwork(combined,env,now,combinedFetch(
    {checked_at:'2026-11-09T12:22:00Z',generated_at:'2026-11-09T12:18:00Z'},
    {checked_at:'2026-11-09T11:40:00Z',generated_at:'2026-11-09T11:00:00Z'},
    [],dispatches
  ));
  assert.equal(result.outcome,'recovery_dispatched');
  assert.equal(result.mnwCheckedAgeMinutes,50);
  assert.deepEqual(dispatches,[{ref:'main'}]);
});

test('MNW fonte in ritardo, ma acquisizione comune recente: non duplicare MeteoHub', async () => {
  const dispatches=[];
  const result=await inspectNetwork(combined,env,now,combinedFetch(
    {checked_at:'2026-11-09T12:22:00Z',generated_at:'2026-11-09T12:18:00Z'},
    {checked_at:'2026-11-09T12:22:00Z',generated_at:'2026-11-09T10:00:00Z'},
    [],dispatches
  ));
  assert.equal(result.outcome,'source_delayed');
  assert.deepEqual(dispatches,[]);
});

test('MeteoHub unificata: esecuzione recente impedisce doppi avvii', async () => {
  const dispatches=[];
  const result=await inspectNetwork(combined,env,now,combinedFetch(
    {checked_at:'2026-11-09T11:40:00Z',generated_at:'2026-11-09T11:00:00Z'},
    {checked_at:'2026-11-09T11:40:00Z',generated_at:'2026-11-09T11:00:00Z'},
    [{created_at:'2026-11-09T12:25:00Z'}],dispatches
  ));
  assert.equal(result.outcome,'recent_run_pending');
  assert.deepEqual(dispatches,[]);
});
