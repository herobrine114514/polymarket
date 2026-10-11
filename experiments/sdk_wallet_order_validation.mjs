/** Native SDK -> owner identity -> fixed FOK economics, synthetic accounts only. */
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync,writeFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {validateWalletIdentity} from './sdk_wallet_identity_guard.mjs';
import {validateOfflineOrder} from './sdk_order_guard.mjs';

const root=new URL('../',import.meta.url),sdk=new URL('.polycast/oss-polymarket-sdk/node_modules/@polymarket/client/',root);
assert.equal(JSON.parse(readFileSync(new URL('package.json',sdk))).version,'0.12.0');
const requireCrypto=createRequire(new URL('.polycast/oss-chainlink-sdk/package.json',root));
const {Wallet,TypedDataEncoder,verifyTypedData}=requireCrypto('ethers');
const owner=Wallet.createRandom(),other=Wallet.createRandom(); // Unfunded, in-memory; never printed/saved.
const wallet='0x'+'55'.repeat(20),alternate='0x'+'66'.repeat(20);
const condition='0x'+'11'.repeat(32),token=BigInt('0x'+'ab'.repeat(32)).toString();
const sha=bytes=>createHash('sha256').update(bytes).digest('hex');
const json=value=>JSON.stringify(value,(_,v)=>typeof v==='bigint'?v.toString():v);
let negRisk=false,requests=0;
const checks=[],observations=[];
function check(name,fn){fn();checks.push({name,passed:true});}
globalThis.fetch=async(input,init)=>{
 const req=new Request(input,init),url=new URL(req.url);requests++;
 assert.ok(requests<=50);assert.equal(req.method,'GET');assert.equal(url.origin,'https://clob.polymarket.com');
 assert.ok(![...req.headers.keys()].some(k=>k.startsWith('poly_')||k==='authorization'));
 let raw;
 if(url.pathname==='/book')raw={market:condition,asset_id:token,timestamp:String(Date.now()),
  bids:[{price:'0.50',size:'100'}],asks:[{price:'0.52',size:'100'}],min_order_size:'5',
  tick_size:'0.01',neg_risk:negRisk,last_trade_price:'0.51',hash:'11'.repeat(20)};
 else if(url.pathname===`/clob-markets/${condition}`)raw={c:condition,ao:true,v:'v1',mos:5,mts:.01,
  itode:true,nr:negRisk,fd:{r:.07,e:1,to:true},t:[{t:token,o:'Yes'},{t:(BigInt(token)+1n).toString(),o:'No'}]};
 else if(url.pathname===`/markets-by-token/${token}`)raw={condition_id:condition};
 else throw Error('Unexpected synthetic SDK route');
 return new Response(json(raw),{status:200,headers:{'content-type':'application/json'}});
};
const {createPublicClient,SignerType,WalletType,OrderType}=await import(new URL('dist/index.js',sdk));
const {prepareLimitOrder,postOrder}=await import(new URL('dist/actions/index.js',sdk));
for(const risk of [false,true]){
 negRisk=risk;
 for(const type of [WalletType.EOA,WalletType.POLY_PROXY,WalletType.GNOSIS_SAFE,WalletType.DEPOSIT_WALLET]){
  const client=createPublicClient(),funder=type===0?owner.address:wallet;
  Object.defineProperty(client,'account',{value:{signer:owner.address,wallet:funder,signerType:SignerType.OWNER,walletType:type}});
  const flow=await prepareLimitOrder(client,{assetId:token,side:'BUY',price:'0.52',size:'5'});
  const first=await flow.next();assert.equal(first.value.kind,'signOrder');
  const typed=first.value.payload,{EIP712Domain,...types}=typed.types;
  const signature=await owner.signTypedData(typed.domain,types,typed.message);
  assert.equal(verifyTypedData(typed.domain,types,typed.message,signature),owner.address);
  const finish=await flow.next(signature);assert.equal(finish.done,true);
  const prepared={order:{...finish.value,orderType:OrderType.FOK},typed};
  const nowMs=Number(prepared.order.timestamp)+100;
  const context={nowMs,intent:{protocol:'ctf-v1',side:'BUY',orderType:'FOK',funder,signer:owner.address,
   signerRole:'owner',signatureType:type,conditionId:condition,tokenId:token,sharesMicro:'5000000',
   priceMicro:'520000',maxCostMicro:'2687360'},market:{protocol:'ctf-v1',conditionId:condition,
   acceptingOrders:true,tokens:[token],negRisk:risk,observedAtMs:nowMs,deadlineMs:nowMs+60000,
   executionDelayMs:250,minimumSharesMicro:'5000000',tickMicro:'10000',feeRateMicro:'70000',
   feeExponent:1,feeTakerOnly:true},prepared};
  check(`type_${type}_risk_${risk}_native_signature_and_fixed_fok_economics`,()=>{
   const result=validateOfflineOrder(context);
   assert.equal(result.can_submit,false);assert.equal(result.maximum_cost_micro,'2687360');
   assert.equal(result.cryptographic_signature_verified,true);assert.equal(result.onchain_eip1271_verified,false);
  });
  // Actual SDK serializer, stopped in memory before auth/network transmission.
  let wire;const stopped=new Error('SYNTHETIC_CAPTURE_STOP');
  const captureClient={credentials:{key:'SYNTHETIC-ONLY'},secureClob:{post(path,options){
   assert.equal(path,'/order');wire=json(options.json);throw stopped;}}};
  await assert.rejects(postOrder(captureClient)(prepared.order),error=>error===stopped);
  assert.ok(wire,'Actual serializer did not reach the in-memory capture');
  const decoded=JSON.parse(wire);
  check(`type_${type}_risk_${risk}_wire_retains_signature_type_fok_and_reserve`,()=>{
   assert.equal(decoded.orderType,'FOK');assert.equal(decoded.order.signatureType,type);
   assert.equal(decoded.order.signature,prepared.order.signature);assert.equal(decoded.order.takerAmount,'5000000');
   assert.equal(decoded.order.makerAmount,'2600000');assert.equal(decoded.deferExec,false);
  });
  const cases={
   wrong_owner:x=>{x.intent.signer=other.address;},
   wrong_funder:x=>{x.intent.funder=alternate;},
   changed_signature_type:x=>{x.intent.signatureType=(type+1)%4;},
   modified_signed_amount:x=>{const m=type===3?x.prepared.typed.message.contents:x.prepared.typed.message;
    x.prepared.order.makerAmount='2600001';m.makerAmount=2600001n;},
   signature_bytes_changed:x=>{const s=x.prepared.order.signature;
    const changed=(parseInt(s.slice(2,4),16)^1).toString(16).padStart(2,'0');
    x.prepared.order.signature='0x'+changed+s.slice(4);},
   unexpected_domain_field:x=>{x.prepared.typed.domain.salt='0x'+'00'.repeat(32);},
   delegated_signer_without_authorization:x=>{x.intent.signerRole='session';},
  };
  for(const [name,mutate] of Object.entries(cases))check(`type_${type}_risk_${risk}_rejects_${name}`,()=>{
   const changed=structuredClone(context);mutate(changed);
   assert.throws(()=>validateWalletIdentity(changed));
  });
  if(type===3){
   const extra={
    wrapper_wallet_replay:x=>{x.prepared.typed.message.verifyingContract=alternate;},
    wrapper_chain_replay:x=>{x.prepared.typed.message.chainId=1n;},
    wrapper_domain_separator:x=>{const s=x.prepared.order.signature;
     x.prepared.order.signature=s.slice(0,132)+'00'.repeat(32)+s.slice(196);},
    wrapper_order_hash:x=>{const s=x.prepared.order.signature;
     x.prepared.order.signature=s.slice(0,196)+'00'.repeat(32)+s.slice(260);},
    wrapper_description_length:x=>{const s=x.prepared.order.signature;x.prepared.order.signature=s.slice(0,-4)+'0001';},
    wrapper_trailing_bytes:x=>{x.prepared.order.signature+='00';},
   };
   for(const [name,mutate] of Object.entries(extra))check(`deposit_risk_${risk}_rejects_${name}`,()=>{
    const changed=structuredClone(context);mutate(changed);assert.throws(()=>validateWalletIdentity(changed));
   });
  }
  observations.push({signature_type:type,negative_risk:risk,wrapped_signature_bytes:(prepared.order.signature.length-2)/2,
   wire_sha256:sha(wire),typed_data_digest:TypedDataEncoder.hash(typed.domain,types,typed.message),
   maximum_cost_micro:'2687360',onchain_validated:false});
 }
}
const frozenURL=new URL('.polycast/sdk-order-offline-20261002/frozen-order.json',root);
let frozenBytes;
try{frozenBytes=readFileSync(frozenURL);}catch(error){if(error.code!=='ENOENT')throw error;}
if(frozenBytes){const frozen=JSON.parse(frozenBytes);
check('legacy_frozen_eoa_signature_still_valid_without_rewriting_artifact',()=>{
 const identity=validateWalletIdentity({intent:{funder:frozen.signed_order.maker,signer:frozen.signed_order.signer},
  market:{negRisk:false},prepared:{order:frozen.signed_order,typed:frozen.typed}});
 assert.equal(identity.can_submit,false);assert.equal(identity.recovered_signer_matches_intent,true);
 assert.equal(sha(readFileSync(frozenURL)),sha(frozenBytes));
});
}
const sources={};for(const path of ['experiments/sdk_wallet_identity_guard.mjs','experiments/sdk_order_guard.mjs',
 'experiments/sdk_wallet_order_validation.mjs','.polycast/oss-polymarket-sdk/package-lock.json'])
 sources[path]=sha(readFileSync(new URL(path,root)));
const report={checked_at:new Date().toISOString(),scope:'offline_synthetic_owner_wallet_compatibility',sdk_version:'0.12.0',
 ethers_version:'6.17.0',checks,observations,sources,passed:checks.length,synthetic_wallet_cases:8,
 synthetic_sdk_reads:requests,native_network_calls:0,user_wallet_used:false,real_credentials_used:false,
 production_database_written:false,real_orders_submitted:0,live_ready:false,onchain_eip1271_verified:false,
 session_signer_authorization_supported:false};
if(process.argv.includes('--report'))writeFileSync(new URL('.polycast/sdk-wallet-identity-validation-20261003.json',root),json(report)+'\n');
console.log(json({passed:report.passed,synthetic_wallet_cases:8,synthetic_sdk_reads:requests,
 native_network_calls:0,real_orders_submitted:0,session_signer_authorization_supported:false,live_ready:false}));
