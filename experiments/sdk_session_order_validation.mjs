/** Pinned SDK session-format and permission faults; entirely synthetic.
 * No credentials, grants, real wallet, RPC or native network are available.
 */
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync,writeFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {validateWalletIdentity} from './sdk_wallet_identity_guard.mjs';
import {validateOfflineOrder} from './sdk_order_guard.mjs';
import {previewSessionAuthorization,SDK_SESSION_LIFETIME_SECONDS} from './sdk_session_permission_guard.mjs';

const root=new URL('../',import.meta.url),sdk=new URL('.polycast/oss-polymarket-sdk/node_modules/@polymarket/client/',root);
assert.equal(JSON.parse(readFileSync(new URL('package.json',sdk))).version,'0.12.0');
const requireCrypto=createRequire(new URL('.polycast/oss-chainlink-sdk/package.json',root));
const requireSdk=createRequire(new URL('package.json',sdk));
const {Wallet,AbiCoder}=requireCrypto('ethers'),{okAsync}=requireSdk('neverthrow');
const session=Wallet.createRandom(),owner=Wallet.createRandom(); // Unfunded and never persisted.
const wallet='0x'+'55'.repeat(20),otherWallet='0x'+'66'.repeat(20),condition='0x'+'11'.repeat(32);
const token=BigInt('0x'+'ab'.repeat(32)).toString(),zero='0x'+'00'.repeat(32);
const magic='6492649264926492649264926492649264926492649264926492649264926492';
const abi=['bytes32','bytes32','bytes'],coder=AbiCoder.defaultAbiCoder();
const sha=bytes=>createHash('sha256').update(bytes).digest('hex');
const json=value=>JSON.stringify(value,(_,v)=>typeof v==='bigint'?v.toString():v);
const checks=[],observations=[];let risk=false,reads=0,registryReads=0;
function check(name,fn){fn();checks.push({name,passed:true});}
globalThis.fetch=async(input,init)=>{
 const req=new Request(input,init),url=new URL(req.url);reads++;
 assert.ok(reads<=10);assert.equal(req.method,'GET');assert.equal(url.origin,'https://clob.polymarket.com');
 assert.ok(![...req.headers.keys()].some(k=>k.startsWith('poly_')||k==='authorization'));
 let raw;
 if(url.pathname==='/book')raw={market:condition,asset_id:token,timestamp:String(Date.now()),
  bids:[{price:'0.50',size:'100'}],asks:[{price:'0.52',size:'100'}],min_order_size:'5',
  tick_size:'0.01',neg_risk:risk,last_trade_price:'0.51',hash:'11'.repeat(20)};
 else if(url.pathname===`/clob-markets/${condition}`)raw={c:condition,ao:true,v:'v1',mos:5,mts:.01,
  itode:true,nr:risk,fd:{r:.07,e:1,to:true},t:[{t:token,o:'Yes'},{t:(BigInt(token)+1n).toString(),o:'No'}]};
 else if(url.pathname===`/markets-by-token/${token}`)raw={condition_id:condition};
 else throw Error('Unexpected synthetic public route');
 return new Response(json(raw),{status:200,headers:{'content-type':'application/json'}});
};
const {createPublicClient,SignerType,WalletType,OrderType}=await import(new URL('dist/index.js',sdk));
const {prepareLimitOrder,postOrder,fetchSessionKeys}=await import(new URL('dist/actions/index.js',sdk));

check('installed_sdk_lifetime_and_omitted_scope_defaults_match_reviewed_policy',()=>{
 const map=JSON.parse(readFileSync(new URL('dist/chunk-5JHFOBBZ.js.map',sdk)));
 const source=map.sourcesContent.find(s=>s?.includes('const SESSION_KEY_LIFETIME_SECONDS = 4_315 * 60 * 60;'));
 assert.ok(source,'Pinned SDK session source is missing');
 assert.ok(source.includes('SessionKeyKnownScope.ALL,'));
 assert.ok(source.includes('.default(DEFAULT_SESSION_KEY_SCOPES)'));
});
check('preview_requires_explicit_clob_only_and_reports_fixed_platform_duration',()=>{
 const preview=previewSessionAuthorization({address:session.address,scopes:['CLOB']});
 assert.equal(preview.platform_lifetime_seconds,15534000);assert.equal(SDK_SESSION_LIFETIME_SECONDS,15534000);
 assert.equal(preview.can_authorize,false);assert.equal(preview.custom_platform_expiry_supported,false);
});
for(const [name,request] of Object.entries({omitted_scope:{address:session.address},
 broad_default:{address:session.address,scopes:['ALL']},combos:{address:session.address,scopes:['CLOB','COMBOSRFQ']},
 duplicate:{address:session.address,scopes:['CLOB','CLOB']},empty:{address:session.address,scopes:[]},
 unsupported_custom_ttl:{address:session.address,scopes:['CLOB'],expiresIn:3600},
 pretending_batch_deadline_is_grant_expiry:{address:session.address,scopes:['CLOB'],deadline:3600},
 unknown_scope:{address:session.address,scopes:['FUTURE']}}))
 check(`preview_rejects_${name}`,()=>assert.throws(()=>previewSessionAuthorization(request)));

for(const negativeRisk of [false,true]){
 risk=negativeRisk;const client=createPublicClient();
 Object.defineProperty(client,'account',{value:{signer:session.address,wallet,
  signerType:SignerType.SESSION_KEY,walletType:WalletType.DEPOSIT_WALLET}});
 const flow=await prepareLimitOrder(client,{assetId:token,side:'BUY',price:'0.52',size:'5'});
 const first=await flow.next();assert.equal(first.value.kind,'signOrder');
 const typed=first.value.payload,{EIP712Domain,...types}=typed.types;
 const signed=await session.signTypedData(typed.domain,types,typed.message),finish=await flow.next(signed);
 assert.equal(finish.done,true);
 const prepared={typed,order:{...finish.value,orderType:OrderType.FOK}},nowMs=Number(prepared.order.timestamp)+100;
 const raw={wallet,signers:[{address:session.address,scopes:['CLOB'],valid_until:Math.floor(nowMs/1000)+15534000}]};
 const ownerClient={account:{signer:owner.address,wallet,signerType:SignerType.OWNER,
  walletType:WalletType.DEPOSIT_WALLET},secureClob:{get(path){
   registryReads++;assert.equal(path,'/v1/user/session-signers');
   return okAsync(new Response(json(raw),{headers:{'content-type':'application/json'}}));}}};
 const sdkRegistry=await fetchSessionKeys(ownerClient);
 check(`risk_${risk}_actual_sdk_owner_registry_normalizes_whole_unix_seconds`,()=>{
  assert.equal(sdkRegistry.length,1);assert.equal(sdkRegistry[0].address,session.address.toLowerCase());
  assert.equal(sdkRegistry[0].validUntil,raw.signers[0].valid_until);
  assert.equal('valid_until' in sdkRegistry[0],false);
 });
 const context={intent:{protocol:'ctf-v1',side:'BUY',orderType:'FOK',funder:wallet,signer:session.address,
  signerRole:'session',signatureType:3,conditionId:condition,tokenId:token,sharesMicro:'5000000',
  priceMicro:'520000',maxCostMicro:'2687360'},market:{protocol:'ctf-v1',conditionId:condition,acceptingOrders:true,
  tokens:[token],negRisk:risk,observedAtMs:nowMs,deadlineMs:nowMs+60000,executionDelayMs:250,
  minimumSharesMicro:'5000000',tickMicro:'10000',feeRateMicro:'70000',feeExponent:1,feeTakerOnly:true},nowMs,prepared,
  sessionRegistry:{scope:'unverified-offline-owner-registry-snapshot',path:'/v1/user/session-signers',
   account:{signer:owner.address,funder:wallet,wallet_type:3,credential_scope:'owner'},startedAtMs:nowMs-200,
   receivedAtMs:nowMs-100,response:raw},localLease:{signer:session.address,wallet,
   createdAtMs:nowMs-1000,expiresAtMs:nowMs+180000,paused:false}};
 check(`risk_${risk}_native_session_envelope_recovery_and_fok_economics`,()=>{
  const result=validateOfflineOrder(context);
  assert.equal(result.session_envelope_verified,true);assert.equal(result.session_permission_shape_verified,true);
  assert.equal(result.cryptographic_signature_verified,true);assert.equal(result.maximum_cost_micro,'2687360');
  assert.equal(result.authenticated_session_authorization_verified,false);
  assert.equal(result.release_existing_reservations,false);assert.equal(result.can_submit,false);
 });
 let wire;const stopped=new Error('SYNTHETIC_CAPTURE_STOP');
 await assert.rejects(postOrder({credentials:{key:'SYNTHETIC-ONLY'},secureClob:{post(path,options){
  assert.equal(path,'/order');wire=json(options.json);throw stopped;}}})(prepared.order),e=>e===stopped);
 check(`risk_${risk}_sdk_request_object_preserves_session_envelope`,()=>{
  const decoded=JSON.parse(wire);assert.equal(decoded.order.signature,prepared.order.signature);
  assert.equal(decoded.order.signatureType,3);assert.equal(decoded.orderType,'FOK');
  assert.equal(decoded.order.signer.toLowerCase(),wallet);assert.equal(decoded.order.maker.toLowerCase(),wallet);
 });
 const [signerId,reserved,inner]=coder.decode(abi,'0x'+prepared.order.signature.slice(2,-64));
 const withEnvelope=(x,values)=>{x.prepared.order.signature=coder.encode(abi,values)+magic;};
 const ownerSigned=await owner.signTypedData(typed.domain,types,typed.message);
 const mutations={
  missing_registry:x=>{delete x.sessionRegistry;},missing_local_lease:x=>{delete x.localLease;},
  revoked_or_absent:x=>{x.sessionRegistry.response.signers=[];},
  registry_wrong_wallet:x=>{x.sessionRegistry.response.wallet=otherWallet;},
  registry_not_owner:x=>{x.sessionRegistry.account.credential_scope='session';},
  registry_not_deposit:x=>{x.sessionRegistry.account.wallet_type=2;},
  registry_wrong_path:x=>{x.sessionRegistry.path='/auth/api-keys';},
  stale_registry:x=>{x.sessionRegistry.startedAtMs=nowMs-5001;},
  future_registry:x=>{x.sessionRegistry.receivedAtMs=nowMs+1;},
  registry_same_signer_as_owner:x=>{x.sessionRegistry.account.signer=session.address;},
  broader_all_scope:x=>{x.sessionRegistry.response.signers[0].scopes=['ALL'];},
  broader_combos_scope:x=>{x.sessionRegistry.response.signers[0].scopes=['CLOB','COMBOSRFQ'];},
  unknown_scope:x=>{x.sessionRegistry.response.signers[0].scopes=['FUTURE'];},
  duplicate_scope:x=>{x.sessionRegistry.response.signers[0].scopes=['CLOB','CLOB'];},
  expired_session:x=>{x.sessionRegistry.response.signers[0].valid_until=Math.floor(nowMs/1000);},
  session_near_expiry:x=>{x.sessionRegistry.response.signers[0].valid_until=Math.floor((nowMs+1250)/1000);},
  session_milliseconds_not_seconds:x=>{x.sessionRegistry.response.signers[0].valid_until=nowMs+15534000;},
  duplicate_signer:x=>{x.sessionRegistry.response.signers.push({...x.sessionRegistry.response.signers[0]});},
  camelcase_expiry_on_raw_response:x=>{const e=x.sessionRegistry.response.signers[0];e.validUntil=e.valid_until;delete e.valid_until;},
  local_paused:x=>{x.localLease.paused=true;},local_expired:x=>{x.localLease.expiresAtMs=nowMs;},
  local_future:x=>{x.localLease.createdAtMs=nowMs+1;},
  local_exceeds_30_minutes:x=>{x.localLease.expiresAtMs=x.localLease.createdAtMs+1800001;},
  local_wrong_signer:x=>{x.localLease.signer=owner.address;},local_wrong_wallet:x=>{x.localLease.wallet=otherWallet;},
  local_outlives_platform:x=>{x.sessionRegistry.response.signers[0].valid_until=Math.floor(nowMs/1000)+60;},
  owner_role_with_session_envelope:x=>{x.intent.signerRole='owner';},
  missing_session_marker:x=>{x.prepared.order.signature=x.prepared.order.signature.slice(0,-64);},
  wrong_signer_id:x=>withEnvelope(x,['0x'+owner.address.slice(2).padStart(64,'0'),reserved,inner]),
  unknown_envelope_extension:x=>withEnvelope(x,[signerId,'0x'+'01'.repeat(32),inner]),
  noncanonical_abi_trailing_bytes:x=>{const s=x.prepared.order.signature;x.prepared.order.signature=s.slice(0,-64)+'00'+magic;},
  noncanonical_abi_offset:x=>{const p=x.prepared.order.signature.slice(2,-64);
   x.prepared.order.signature='0x'+p.slice(0,128)+(128).toString(16).padStart(64,'0')+'00'.repeat(32)+p.slice(192)+magic;},
  nonzero_abi_padding:x=>{assert.notEqual(((inner.length-2)/2)%32,0);
   const s=x.prepared.order.signature;x.prepared.order.signature=s.slice(0,-66)+'01'+magic;},
  corrupted_inner_signature:x=>withEnvelope(x,[signerId,reserved,'0x00'+inner.slice(4)]),
  owner_signature_substituted_for_session:x=>withEnvelope(x,[signerId,reserved,ownerSigned+inner.slice(132)]),
  signature_type_changed:x=>{x.intent.signatureType=0;x.prepared.order.signatureType=0;},
 };
 for(const [name,mutate] of Object.entries(mutations))check(`risk_${risk}_rejects_${name}`,()=>{
  const changed=structuredClone(context);mutate(changed);assert.throws(()=>validateOfflineOrder(changed));
 });
 check(`risk_${risk}_session_recovery_alone_does_not_assert_live_authority`,()=>{
  const identity=validateWalletIdentity(context);assert.equal(identity.delegated_permission_verified,false);
  assert.equal(identity.can_submit,false);assert.equal(identity.onchain_eip1271_verified,false);
 });
 const wrongWalletClient={...ownerClient,secureClob:{get(){registryReads++;
  return okAsync(new Response(json({...raw,wallet:otherWallet}),{headers:{'content-type':'application/json'}}));}}};
 await assert.rejects(fetchSessionKeys(wrongWalletClient));
 checks.push({name:`risk_${risk}_native_sdk_rejects_registry_wallet_mismatch`,passed:true});
 for(const [name,account] of Object.entries({session_reader:{...ownerClient.account,signerType:SignerType.SESSION_KEY},
  safe_reader:{...ownerClient.account,walletType:WalletType.GNOSIS_SAFE}})){
  let attempted=false;
  await assert.rejects(fetchSessionKeys({...ownerClient,account,secureClob:{get(){attempted=true;throw Error('Forbidden read');}}}));
  assert.equal(attempted,false);checks.push({name:`risk_${risk}_native_sdk_denies_${name}_before_transport`,passed:true});
 }
 observations.push({negative_risk:risk,signature_type:3,wrapped_signature_bytes:(prepared.order.signature.length-2)/2,
  request_object_sha256:sha(wire),all_in_reserve_micro:'2687360',live_authority_verified:false});
}
const sources={};for(const p of ['experiments/sdk_wallet_identity_guard.mjs','experiments/sdk_order_guard.mjs',
 'experiments/sdk_session_permission_guard.mjs','experiments/sdk_session_order_validation.mjs',
 'experiments/account_readiness.py','.polycast/oss-polymarket-sdk/package-lock.json'])sources[p]=sha(readFileSync(new URL(p,root)));
const report={checked_at:new Date().toISOString(),scope:'offline_synthetic_session_compatibility',sdk_version:'0.12.0',
 checks,observations,sources,passed:checks.length,synthetic_wallet_cases:2,synthetic_public_reads:reads,
 synthetic_owner_registry_reads:registryReads,platform_lifetime_seconds:SDK_SESSION_LIFETIME_SECONDS,
 native_network_calls:0,real_credentials_used:false,user_wallet_used:false,wallet_grants_created:0,
 platform_keys_revoked:0,production_database_written:false,real_orders_submitted:0,
 authenticated_session_authorization_verified:false,live_ready:false};
if(process.argv.includes('--report'))writeFileSync(new URL('.polycast/sdk-session-validation-20261003.json',root),json(report)+'\n');
console.log(json({passed:report.passed,synthetic_wallet_cases:2,synthetic_public_reads:reads,
 synthetic_owner_registry_reads:registryReads,native_network_calls:0,wallet_grants_created:0,
 real_orders_submitted:0,authenticated_session_authorization_verified:false,live_ready:false}));
