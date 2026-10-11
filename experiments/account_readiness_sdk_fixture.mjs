/** Cross-check pinned SDK GET authentication with a synthetic EOA and no network. */
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createHmac} from 'node:crypto';

const root=new URL('../',import.meta.url);
const sdk=new URL('.polycast/oss-polymarket-sdk/node_modules/@polymarket/client/',root);
assert.equal(JSON.parse(readFileSync(new URL('package.json',sdk))).version,'0.12.0');
const signer='0x'+'11'.repeat(20),wallet=signer;
const credentials={key:'11111111-1111-4111-8111-111111111111',
  secret:Buffer.alloc(32,42).toString('base64'),passphrase:'SYNTHETIC-LOCAL-FIXTURE'};
let walletOperations=0;
const noWallet=()=>{walletOperations++;throw new Error('Wallet operations forbidden');};
const requests=[];
globalThis.fetch=async(input,init)=>{
 const req=new Request(input,init),url=new URL(req.url);
 assert.equal(url.origin,'https://clob.polymarket.com');assert.equal(req.method,'GET');
 assert.equal(await req.text(),'');assert.ok(requests.length<4);
 assert.ok(['/auth/api-keys','/balance-allowance','/auth/ban-status/closed-only'].includes(url.pathname));
 const timestamp=req.headers.get('POLY_TIMESTAMP');
 assert.equal(req.headers.get('POLY_ADDRESS').toLowerCase(),signer);
 assert.equal(req.headers.get('POLY_API_KEY'),credentials.key);
 assert.equal(req.headers.get('POLY_PASSPHRASE'),credentials.passphrase);
 const expected=createHmac('sha256',Buffer.from(credentials.secret,'base64'))
  .update(timestamp+'GET'+url.pathname).digest('base64url')+'=';
 // 32-byte HMAC produces one padding character.
 assert.equal(req.headers.get('POLY_SIGNATURE'),expected);
 requests.push({path:url.pathname,timestamp,signature:expected,query:Object.fromEntries(url.searchParams)});
 if(url.pathname==='/balance-allowance'){
  assert.equal(url.searchParams.get('signature_type'),'0');
  assert.equal(url.searchParams.get('asset_type'),'COLLATERAL');
 }
 const raw=url.pathname==='/auth/api-keys'?{apiKeys:[credentials.key]}:
  url.pathname==='/balance-allowance'?{balance:'1234567',allowances:{[wallet]:'999999'}}:{closed_only:false};
 return new Response(JSON.stringify(raw),{status:200,headers:{'content-type':'application/json'}});
};
const {createSecureClient}=await import(new URL('dist/index.js',sdk));
const {fetchBalanceAllowance}=await import(new URL('dist/actions/index.js',sdk));
const client=await createSecureClient({wallet,credentials,
 signer:{getAddress:async()=>signer,signTypedData:noWallet,signMessage:noWallet,sendTransaction:noWallet}});
assert.equal(requests.length,1,'Synthetic SDK initialization only validates the supplied key');
const balance=await fetchBalanceAllowance(client,{assetType:'COLLATERAL'});
assert.equal(String(balance.balance),'1234567');
assert.equal(await client.fetchClosedOnlyMode(),false);
assert.equal(walletOperations,0);
console.log(JSON.stringify({scope:'synthetic-sdk-read-only-protocol',sdk_version:'0.12.0',
 native_network_calls:0,wallet_operations:0,initialization_reads:1,
 requests:requests.filter(r=>r.path!=='/auth/api-keys')}));
