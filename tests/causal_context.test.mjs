import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync,writeFileSync,appendFileSync,rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { ReferenceCursor, CausalHistory } from '../polycast/causal_context.mjs';
const point=(timestamp,price=String(60000n*10n**18n))=>({schema_version:1,timestamp_ms:timestamp,received_at_ms:timestamp+10,
  full_accuracy_value:price,feed_protocol:'rtds',price_encoding:'e18',price_source:'chainlink',symbol:'btcusd',quote_currency:'USD',window_seconds:60});

test('reference cursor preserves partial lines and distinguishes exact sub-float conflicts',()=>{
  const directory=mkdtempSync(join(tmpdir(),'polycast-reference-')),path=join(directory,'relay.jsonl'),start=Date.now()-120000;
  try{
    const latest=JSON.stringify(point(Date.now()-1000)),cut=Math.floor(latest.length/2);
    writeFileSync(path,JSON.stringify(point(start))+'\n'+latest.slice(0,cut));
    const cursor=new ReferenceCursor(path),rows=[];const append=row=>{rows.push(row);return rows.length;};
    cursor.poll(append,Date.now());assert.equal(rows.length,1);
    appendFileSync(path,latest.slice(cut)+'\n');cursor.poll(append,Date.now());assert.equal(rows.length,2);
    assert.equal(cursor.context(start,Date.now(),Date.now(),process.hrtime.bigint()).status,'proxy_available');
    appendFileSync(path,JSON.stringify(point(start,String(60000n*10n**18n+1n)))+'\n');cursor.poll(append,Date.now());
    assert.equal(cursor.context(start,Date.now(),Date.now(),process.hrtime.bigint()).status,'proxy_unavailable','exact conflict must not disappear through Number rounding');
  }finally{rmSync(directory,{recursive:true});}
});

test('unverified clock does not advance a cursor; truncation clears cached reference versions',()=>{
  const directory=mkdtempSync(join(tmpdir(),'polycast-reference-reset-')),path=join(directory,'relay.jsonl'),start=Date.now()-120000;
  try{
    writeFileSync(path,JSON.stringify(point(start))+'\n'+JSON.stringify(point(Date.now()-1000))+'\n');
    const cursor=new ReferenceCursor(path),rows=[];const append=row=>{rows.push(row);return rows.length;};
    cursor.poll(append,null);assert.equal(cursor.position,null);assert.equal(rows.length,0);
    cursor.poll(append,Date.now());assert.equal(cursor.context(start,Date.now(),Date.now(),process.hrtime.bigint()).status,'proxy_available');
    writeFileSync(path,JSON.stringify(point(Date.now()-1000))+'\n');cursor.poll(append,Date.now());
    assert.equal(cursor.context(start,Date.now(),Date.now(),process.hrtime.bigint()).status,'proxy_unavailable');
    assert.equal(rows.filter(row=>row.kind==='reference_read_gap').length,1);
  }finally{rmSync(directory,{recursive:true});}
});

test('history preserves invalid observations, bounds storage and clears on a clock reset',()=>{
  const history=new CausalHistory();let value;
  for(let i=0;i<200;i++){
    value=history.update({received_at_ms:i*1000,as_of_utc_ms:i*1000,monotonic_ns:String(i*1000000000),
      clock_reference:null,market:{market_id:'1'},coinbase:{book:null,connection_id:'cb'},polymarket:{up:null,connection_id:'pm'}},i+1);
  }
  assert.ok(value.coinbase.length<=66);assert.equal(value.coinbase.at(-1).valid,false);
  assert.ok(value.polymarket_up.length<=16);history.clear();assert.equal(history.coinbase.length,0);
});
