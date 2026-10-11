import assert from 'node:assert/strict';
import test from 'node:test';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, writeFile, unlink, rmdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';

const collector = resolve('polycast/chainlink_twap60.mjs');
const fakeSocket = "globalThis.WebSocket = class { constructor(){this.readyState=0;process.stdout.write('ready\\n');} close(){this.readyState=3;} };";
function alive(pid) {
  try { process.kill(pid, 0); return true; }
  catch (error) { if (error.code === 'ESRCH') return false; throw error; }
}

test('collector exits when its owning parent is forcibly terminated', { timeout: 10000 }, async () => {
  const directory = await mkdtemp(join(tmpdir(), 'polycast-parent-test-'));
  const preload = join(directory, 'socket.mjs');
  await writeFile(preload, fakeSocket);
  let parent, childPid;
  try {
    const runner = `const {spawn}=require('node:child_process');
      const child=spawn(process.execPath,['--import',process.argv[1],process.argv[2],process.argv[3],'60'],
        {stdio:['pipe','pipe','ignore'],env:{...process.env,POLYCAST_PARENT_PIPE:'1'}});
      child.stdout.once('data',data=>process.stdout.write(JSON.stringify({pid:child.pid,ready:String(data).trim()})+'\\n'));
      setInterval(()=>{},1000);`;
    parent = spawn(process.execPath, ['-e', runner, pathToFileURL(preload).href, collector, join(directory, 'ticks.jsonl')],
      { stdio: ['ignore', 'pipe', 'pipe'] });
    const [data] = await once(parent.stdout, 'data');
    const started = JSON.parse(String(data).trim());
    assert.equal(started.ready, 'ready');
    childPid = started.pid;
    assert.ok(Number.isInteger(childPid) && childPid > 0);
    assert.equal(alive(childPid), true);
    const parentExit = once(parent, 'exit');
    parent.kill();
    await parentExit;
    const deadline = Date.now() + 5000;
    while (alive(childPid) && Date.now() < deadline) await delay(50);
    assert.equal(alive(childPid), false, 'a collector must not survive its dashboard');
  } finally {
    if (parent && parent.exitCode === null && parent.signalCode === null) parent.kill();
    if (childPid && alive(childPid)) process.kill(childPid);
    await unlink(preload);
    await rmdir(directory);
  }
});

test('closing the owner pipe ends a running collector without a websocket', { timeout: 10000 }, async () => {
  const directory = await mkdtemp(join(tmpdir(), 'polycast-pipe-test-'));
  const preload = join(directory, 'socket.mjs');
  await writeFile(preload, fakeSocket);
  let child;
  try {
    child = spawn(process.execPath, ['--import', pathToFileURL(preload).href, collector, join(directory, 'ticks.jsonl'), '60'],
      { stdio: ['pipe', 'pipe', 'pipe'], env: { ...process.env, POLYCAST_PARENT_PIPE: '1' } });
    const [ready] = await once(child.stdout, 'data');
    assert.equal(String(ready).trim(), 'ready');
    const exit = once(child, 'exit');
    child.stdin.end();
    const [code, signal] = await exit;
    assert.equal(code, 0);
    assert.equal(signal, null);
  } finally {
    if (child && child.exitCode === null && child.signalCode === null) child.kill();
    await unlink(preload);
    await rmdir(directory);
  }
});
