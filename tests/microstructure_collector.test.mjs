import assert from 'node:assert/strict';
import test from 'node:test';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, writeFile, readFile, readdir, unlink, rmdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';

const collector = resolve('polycast/microstructure_collector.mjs');
const fake = "globalThis.WebSocket=class{constructor(){this.readyState=0;process.stdout.write('ready\\n');}close(){this.readyState=3;}};";
function alive(pid) { try { process.kill(pid, 0); return true; } catch (e) { if (e.code === 'ESRCH') return false; throw e; } }
async function remove(directory) { for (const name of await readdir(directory)) await unlink(join(directory, name)); await rmdir(directory); }

test('same-process public collector exits on owner pipe EOF and records the cause', { timeout: 10000 }, async () => {
  const directory = await mkdtemp(join(tmpdir(), 'polycast-micro-pipe-')); let child;
  const preload = join(directory, 'socket.mjs'); await writeFile(preload, fake);
  try {
    child = spawn(process.execPath, ['--import', pathToFileURL(preload).href, collector, directory,
      join(directory, 'missing-selection.json'), '60', String(1024 ** 2)],
      { stdio: ['pipe', 'pipe', 'pipe'], env: { ...process.env, POLYCAST_PARENT_PIPE: '1' } });
    await once(child.stdout, 'data'); const exited = once(child, 'exit'); child.stdin.end();
    const [code] = await exited; assert.equal(code, 0);
    assert.equal(JSON.parse(await readFile(join(directory, 'state.json'), 'utf8')).status, 'owner_closed');
  } finally { if (child && alive(child.pid)) child.kill(); await remove(directory); }
});

test('forcibly ending a dashboard cannot leave a public collector running', { timeout: 10000 }, async () => {
  const directory = await mkdtemp(join(tmpdir(), 'polycast-micro-parent-')); let parent, childPid;
  const preload = join(directory, 'socket.mjs'); await writeFile(preload, fake);
  try {
    const script = `const {spawn}=require('node:child_process');const c=spawn(process.execPath,
      ['--import',process.argv[1],process.argv[2],process.argv[3],process.argv[4],'60','1048576'],
      {stdio:['pipe','pipe','ignore'],env:{...process.env,POLYCAST_PARENT_PIPE:'1'}});
      c.stdout.once('data',()=>process.stdout.write(String(c.pid)));setInterval(()=>{},1000);`;
    parent = spawn(process.execPath, ['-e', script, pathToFileURL(preload).href, collector, directory,
      join(directory, 'missing-selection.json')], { stdio: ['ignore', 'pipe', 'pipe'] });
    const [output] = await once(parent.stdout, 'data'); childPid = Number(String(output)); assert.equal(alive(childPid), true);
    const exited = once(parent, 'exit'); parent.kill(); await exited;
    const deadline = Date.now() + 5000; while (alive(childPid) && Date.now() < deadline) await delay(50);
    assert.equal(alive(childPid), false);
  } finally {
    if (parent && alive(parent.pid)) parent.kill(); if (childPid && alive(childPid)) process.kill(childPid);
    await remove(directory);
  }
});

test('collector defers future-only recovery and requests a snapshot on activation', { timeout: 10000 }, async () => {
  const directory = await mkdtemp(join(tmpdir(), 'polycast-micro-scope-')); let child;
  const now = Date.now();
  const selection = { schema_version: 1, captured_at_ms: now, markets: [{ market_id: '1',
    start_at_ms: now + 2500, end_at_ms: now + 302500, up_asset: '3', down_asset: '4' }] };
  const preload = join(directory, 'socket.mjs');
  await writeFile(join(directory, 'selection.json'), JSON.stringify(selection));
  await writeFile(join(directory, 'clock.json'), JSON.stringify({ schema_version: 1, status: 'venues_agree',
    clock_id: 'test-only', offset_ms: 0, uncertainty_ms: 1, checked_at_local_ms: now }));
  await writeFile(preload, `globalThis.WebSocket=class {
    constructor(url){this.url=url;this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen?.();},10);}
    send(text){
      if(!this.url.includes('polymarket')||text==='PING'||this.sent)return;this.sent=true;
      const timestamp=String(Date.now());
      const books=['3','4'].map(asset_id=>({event_type:'book',asset_id,timestamp,
        bids:[{price:'.4',size:'2'}],asks:[{price:'.5',size:'3'}]}));
      this.onmessage?.({data:JSON.stringify(books)});
      this.onmessage?.({data:JSON.stringify({event_type:'price_change',timestamp,price_changes:[
        {asset_id:'3',side:'BUY',price:'.41',size:'2',best_bid:'.42',best_ask:'.5'}]})});
      process.stdout.write('quarantined\\n');
    }
    close(){this.readyState=3;setTimeout(()=>this.onclose?.(),0);}
  };`);
  try {
    child = spawn(process.execPath, ['--import', pathToFileURL(preload).href, collector, directory,
      join(directory, 'selection.json'), '60', '1048576'],
      { stdio: ['pipe', 'pipe', 'pipe'], env: { ...process.env, POLYCAST_PARENT_PIPE: '1' } });
    await once(child.stdout, 'data');
    await delay(1150);
    let state = JSON.parse(await readFile(join(directory, 'state.json'), 'utf8'));
    assert.equal(state.venues.polymarket.status, 'open', 'future-only failure must preserve the connection');
    assert.equal(state.quarantined_polymarket_books[0].asset_id, '3');
    const deadline = Date.now() + 4500;
    do { await delay(100); state = JSON.parse(await readFile(join(directory, 'state.json'), 'utf8')); }
    while (state.venues.polymarket.status !== 'closed' && Date.now() < deadline);
    assert.equal(state.venues.polymarket.close_reason, 'snapshot_required');
    const exited = once(child, 'exit'); child.stdin.end(); await exited;
    const names = (await readdir(directory)).filter(name => name.endsWith('.jsonl'));
    const rows = (await Promise.all(names.map(name=>readFile(join(directory,name),'utf8'))))
      .flatMap(text=>text.trim().split('\n').map(line=>JSON.parse(line)));
    const gaps = rows.filter(row=>row.kind==='capture_gap' && row.venue==='polymarket');
    assert.equal(gaps.length, 1); assert.deepEqual(gaps[0].active_assets, ['3','4']);
    assert.ok(gaps[0].received_at_ms >= selection.markets[0].start_at_ms + 1000);
    assert.ok(gaps[0].snapshot_wait_ms >= 1000, 'grace uses monotonic elapsed time');
    assert.equal(rows.filter(row=>row.kind==='book_quarantine').length, 1);
  } finally { if(child && alive(child.pid))child.kill(); await remove(directory); }
});

test('an active invalid book stays quarantined but a full snapshot within grace avoids reconnect', { timeout: 10000 }, async () => {
  const directory = await mkdtemp(join(tmpdir(), 'polycast-micro-grace-')); let child;
  const now = Date.now(), preload = join(directory, 'socket.mjs');
  await writeFile(join(directory, 'selection.json'), JSON.stringify({ schema_version: 1, captured_at_ms: now,
    markets: [{ market_id: '1', start_at_ms: now - 120000, end_at_ms: now + 180000, up_asset: '3', down_asset: '4' }] }));
  await writeFile(join(directory, 'clock.json'), JSON.stringify({ schema_version: 1, status: 'venues_agree',
    clock_id: 'test-only', offset_ms: 0, uncertainty_ms: 1, checked_at_local_ms: now }));
  await writeFile(preload, `globalThis.WebSocket=class {
    constructor(url){this.url=url;this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen?.();},10);}
    send(text){
      if(!this.url.includes('polymarket')||text==='PING'||this.sent)return;this.sent=true;
      const book=asset_id=>({event_type:'book',asset_id,timestamp:String(Date.now()),
        bids:[{price:'.4',size:'2'}],asks:[{price:'.5',size:'3'}]});
      this.onmessage?.({data:JSON.stringify([book('3'),book('4')])});
      this.onmessage?.({data:JSON.stringify({event_type:'price_change',timestamp:String(Date.now()),price_changes:[
        {asset_id:'3',side:'BUY',price:'.41',size:'2',best_bid:'.42',best_ask:'.5'}]})});
      process.stdout.write('quarantined\\n');
      setTimeout(()=>this.onmessage?.({data:JSON.stringify(book('3'))}),200);
    }
    close(){this.readyState=3;setTimeout(()=>this.onclose?.(),0);}
  };`);
  try {
    child = spawn(process.execPath, ['--import', pathToFileURL(preload).href, collector, directory,
      join(directory, 'selection.json'), '60', '1048576'],
      { stdio: ['pipe', 'pipe', 'pipe'], env: { ...process.env, POLYCAST_PARENT_PIPE: '1' } });
    await once(child.stdout, 'data'); await delay(1400);
    const state = JSON.parse(await readFile(join(directory, 'state.json'), 'utf8'));
    assert.equal(state.venues.polymarket.status, 'open');
    assert.deepEqual(state.quarantined_polymarket_books, []);
    assert.equal(state.snapshot_wait_started_at_ms, null);
    assert.equal(state.snapshot_grace_ms, 1000);
    const exited = once(child, 'exit'); child.stdin.end(); await exited;
    const names = (await readdir(directory)).filter(name=>name.endsWith('.jsonl'));
    const rows = (await Promise.all(names.map(name=>readFile(join(directory,name),'utf8'))))
      .flatMap(text=>text.trim().split('\n').map(line=>JSON.parse(line)));
    assert.equal(rows.filter(row=>row.kind==='capture_gap' && row.venue==='polymarket').length, 0);
    assert.equal(rows.filter(row=>row.kind==='book_quarantine').length, 1);
  } finally { if(child && alive(child.pid))child.kill(); await remove(directory); }
});

test('a wall clock step invalidates the old reference until a new clock id is observed', { timeout: 10000 },async()=>{
  const directory=await mkdtemp(join(tmpdir(),'polycast-clock-step-'));let child;
  const now=Date.now(),preload=join(directory,'socket.mjs');
  await writeFile(join(directory,'selection.json'),JSON.stringify({schema_version:1,captured_at_ms:now,markets:[{
    market_id:'1',start_at_ms:now-120000,end_at_ms:now+180000,up_asset:'3',down_asset:'4'}]}));
  await writeFile(join(directory,'clock.json'),JSON.stringify({schema_version:1,status:'venues_agree',clock_id:'before-step',
    offset_ms:0,uncertainty_ms:1,checked_at_local_ms:now}));
  await writeFile(preload,`const realNow=Date.now;let shift=0;Date.now=()=>realNow()+shift;
    setTimeout(()=>{shift=5000;},1200);
    globalThis.WebSocket=class{constructor(){this.readyState=0;process.stdout.write('ready\\n');}close(){this.readyState=3;setTimeout(()=>this.onclose?.(),0);}};`);
  try{
    child=spawn(process.execPath,['--import',pathToFileURL(preload).href,collector,directory,join(directory,'selection.json'),'60','1048576'],
      {stdio:['pipe','pipe','pipe'],env:{...process.env,POLYCAST_PARENT_PIPE:'1'}});
    await once(child.stdout,'data');await delay(3250);
    let state=JSON.parse(await readFile(join(directory,'state.json'),'utf8'));
    assert.equal(state.clock_steps,1);assert.equal(state.clock_verification,'unverified');
    await writeFile(join(directory,'clock.json'),JSON.stringify({schema_version:1,status:'venues_agree',clock_id:'after-step',
      offset_ms:-5000,uncertainty_ms:1,checked_at_local_ms:Date.now()+5000}));
    await delay(1150);state=JSON.parse(await readFile(join(directory,'state.json'),'utf8'));
    assert.equal(state.clock_verification,'public_venues_checked');
    const exited=once(child,'exit');child.stdin.end();await exited;
  }finally{if(child&&alive(child.pid))child.kill();await remove(directory);}
});
